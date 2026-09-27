"""Phase 1 of the maker-entry experiment. Offline, read-only, places nothing.

THE QUESTION

The bot buys at market: taker, 0,20%, and it pays the half-spread. A post-only
(`MOC`) limit at the bid would pay maker 0,10% and capture the half-spread
instead of paying it. On this watchlist the half-spread runs from 0,002% (BTC)
to 0,625% (UNI), so the spread — not the fee — is most of the prize.

It is also most of the risk, and they are the same thing. To fill at the bid,
price must come DOWN to you. The entry signal is momentum breaking UP. So you
fill when the move fails and miss when it runs.

WHAT MAKES IT MEASURABLE

A market FALLBACK after T minutes. No trade is ever missed; the only exposure is
a worse fill on the ones that did not fill. That converts an unanswerable
question ("would we have missed the winners?") into an arithmetic one:

    benefit  = (ask - limit)/ask + fee saving,  on the fills
    cost     = (price at T - ask)/ask,          on the non-fills

reported in basis points of notional. Positive means the limit entry was worth
placing.

THE FILL MODEL IS DELIBERATELY PESSIMISTIC

A resting buy at price P is counted filled only if a 1m bar's LOW goes STRICTLY
below P. Price merely touching the bid does not clear a queue you joined at the
back of. The optimistic variant (low <= P) is reported alongside so the gap
between the two is visible rather than hidden in a choice of inequality.

TWO SAMPLES, AND THEY ANSWER DIFFERENT THINGS

  mechanics — every hour x coin in the archive. Large. Answers "how often does a
              bid fill, and what does waiting cost", independent of any signal.
  signal    — the bot's real entries. Small. The only sample that can show
              adverse selection, because only it knows which bars followed a
              decision to buy.

A good mechanics number with a bad signal number means the idea works except
when it matters, which is the failure mode worth catching.
"""
import sys
from datetime import datetime, timedelta, timezone

from . import bars1m, config, ledger, pairs, spreads

WINDOWS = (5, 10, 15, 30)

# THE GATE, registered 2026-09-27 BEFORE Phase 1 ran and failed on it. It lives
# in code so the re-test is graded by the same rule rather than by whoever reads
# the table next. Both conditions are required; one alone is not a result.
GATE_MECHANICS_BPS = 10.0      # best bucket must clear this, on the large sample
GATE_MIN_SIGNAL_MEDIAN_BPS = 0.0   # the MEDIAN trade must gain, not just the mean
PHASE1_RESULT = {"mechanics_best_bps": 5.4, "signal_median_5m_bps": 0.0,
                 "signal_n": 16, "verdict": "FAIL", "when": "2026-09-27"}
MAKER_FEE_PCT = 0.10
TAKER_FEE_PCT = 0.20


def _parse(ts):
    dt = ts if isinstance(ts, datetime) else datetime.fromisoformat(ts)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def fills(bars, limit_price, strict=True):
    """(filled, minutes_waited) for a resting buy at `limit_price`."""
    for i, bar in enumerate(bars):
        low = bar["l"]
        if (low < limit_price) if strict else (low <= limit_price):
            return True, i + 1
    return False, len(bars)


def price_after(bars, minutes):
    """Close of the last bar in the window — what a fallback would pay."""
    window = bars[:minutes]
    return window[-1]["c"] if window else None


def trial(bars, ask, limit_price, minutes, strict=True, half_spread=None):
    """One simulated entry. Returns bps of notional saved (negative = cost).

    `half_spread` (as a fraction) converts the fallback's mid/close into the ASK
    it would actually pay. Leaving it out compares an ask-inclusive benchmark
    against a mid-price fallback, which hands every non-fill a free half-spread
    it never earned — the first version of this did exactly that and reported
    every bucket positive, including one that filled 10% of the time.
    """
    window = bars[:minutes]
    if not window or not ask or ask <= 0:
        return None
    filled, waited = fills(window, limit_price, strict=strict)
    fee_saved = (TAKER_FEE_PCT - MAKER_FEE_PCT) * 100      # bps
    if filled:
        # Bought at the bid instead of the ask: the whole spread, plus the fee.
        return {"filled": True, "waited": waited,
                "bps": (ask - limit_price) / ask * 10_000 + fee_saved}
    close = price_after(window, minutes)
    if close is None:
        return None
    # Fell back to market: pays the ASK at T, and the same taker fee as today.
    # What is left is pure price drift over the wait.
    half = (ask / limit_price - 1) / 2 if half_spread is None else half_spread
    return {"filled": False, "waited": waited,
            "bps": (ask - close * (1 + half)) / ask * 10_000}


def _spread_bucket(pct):
    if pct < 0.05:
        return "tight  <0.05%"
    if pct < 0.20:
        return "mid  0.05-0.2%"
    if pct < 0.60:
        return "wide  0.2-0.6%"
    return "very wide >0.6%"


def mechanics(symbols=None, days=7, windows=WINDOWS, now=None, quotes=None,
              strict=True, step_minutes=60):
    """Fill rate and net bps for every hour x coin in the archive."""
    now = now or datetime.now(timezone.utc)
    symbols = symbols if symbols is not None else list(config.WATCHLIST)
    quotes = quotes if quotes is not None else live_spread_pct(symbols)
    start = now - timedelta(days=days)
    out = {}
    for sym in symbols:
        half = (quotes.get(sym) or 0.0) / 2 / 100.0
        if half <= 0:
            continue
        bars = bars1m.load_range(sym, start, now)
        if len(bars) < max(windows) + 2:
            continue
        index = {b["t"]: i for i, b in enumerate(bars)}
        times = sorted(index)[::step_minutes]
        for minutes in windows:
            key = (_spread_bucket(quotes[sym]), minutes)
            agg = out.setdefault(key, {"n": 0, "filled": 0, "bps": 0.0, "coins": set()})
            for t in times:
                i = index[t]
                ask = bars[i]["c"] * (1 + half)       # mid -> ask, from the quote
                limit = bars[i]["c"] * (1 - half)     # rest at the bid
                res = trial(bars[i + 1:], ask, limit, minutes, strict=strict,
                            half_spread=half)
                if not res:
                    continue
                agg["n"] += 1
                agg["filled"] += res["filled"]
                agg["bps"] += res["bps"]
                agg["coins"].add(sym)
    return out


def live_spread_pct(symbols=None, rows=None, now=None):
    """{symbol: median observed spread %}, from recorded quotes when there are any."""
    symbols = symbols if symbols is not None else list(config.WATCHLIST)
    rows = rows if rows is not None else spreads.load()
    seen = {}
    for row in rows:
        for sym, (bid, ask) in row.get("s", {}).items():
            if bid and ask and bid > 0:
                seen.setdefault(sym, []).append((ask / bid - 1) * 100)
    return {s: sorted(v)[len(v) // 2] for s, v in seen.items() if s in symbols and v}


def entries(led=None, days=7, now=None):
    """The bot's real entries inside the archived window, closed and open."""
    led = led if led is not None else ledger.load()
    now = now or datetime.now(timezone.utc)
    cut = now - timedelta(days=days)
    out = []
    for t in led.get("closed", []):
        if t.get("entry_time") and _parse(t["entry_time"]) >= cut:
            out.append({"symbol": t["symbol"], "when": _parse(t["entry_time"]),
                        "entry_price": t["entry_price"], "qty": t["qty"],
                        "pnl": t.get("pnl"), "reason": t.get("reason"),
                        "quote": t.get("entry_quote")})
    for p in led.get("open", []):
        if p.get("entry_time") and _parse(p["entry_time"]) >= cut:
            out.append({"symbol": p["symbol"], "when": _parse(p["entry_time"]),
                        "entry_price": p["entry_price"], "qty": p["qty"],
                        "pnl": None, "reason": "open", "quote": p.get("entry_quote")})
    out.sort(key=lambda e: e["when"])
    return out


def signal_sample(led=None, days=7, windows=WINDOWS, now=None, quotes=None, strict=True):
    """The same trial, on the entries the bot actually made."""
    now = now or datetime.now(timezone.utc)
    quotes = quotes if quotes is not None else live_spread_pct()
    rows = []
    for e in entries(led, days=days, now=now):
        half = (quotes.get(e["symbol"]) or 0.0) / 2 / 100.0
        if e["quote"]:
            ask, bid = float(e["quote"]["ask"]), float(e["quote"]["bid"])
        elif half > 0:
            ask, bid = e["entry_price"] * (1 + half), e["entry_price"] * (1 - half)
        else:
            continue
        bars = bars1m.load_range(e["symbol"], e["when"],
                                 e["when"] + timedelta(minutes=max(windows) + 1))
        if not bars:
            continue
        row = dict(e, ask=ask, bid=bid, spread_pct=(ask / bid - 1) * 100, by_window={})
        for minutes in windows:
            row["by_window"][minutes] = trial(bars, ask, bid, minutes, strict=strict,
                                              half_spread=(ask / bid - 1) / 2)
        rows.append(row)
    return rows


def render(mech, signal, windows=WINDOWS):
    lines = ["MAKER-ENTRY EXPERIMENT — Phase 1 (offline, nothing was placed)", ""]
    lines.append(f"  fee: taker {TAKER_FEE_PCT}% -> maker {MAKER_FEE_PCT}% "
                 f"= {int((TAKER_FEE_PCT - MAKER_FEE_PCT) * 100)} bps saved on a fill")
    lines += ["", "MECHANICS — every hour x coin, no signal involved", "",
              f"  {'spread bucket':18} {'wait':>5} {'n':>6} {'fill%':>7} {'net bps':>9}"]
    for (bucket, minutes) in sorted(mech, key=lambda k: (k[0], k[1])):
        a = mech[(bucket, minutes)]
        if not a["n"]:
            continue
        lines.append(f"  {bucket:18} {minutes:>4}m {a['n']:>6} "
                     f"{a['filled'] / a['n'] * 100:>6.1f}% {a['bps'] / a['n']:>+8.1f}")
    lines += ["", f"SIGNAL — the bot's own entries ({len(signal)} of them)", ""]
    if not signal:
        lines.append("  (no archived entries in the window)")
    else:
        lines.append(f"  {'entry':26} {'spread':>7} " +
                     " ".join(f"{m:>9}m" for m in windows))
        for row in signal:
            cells = []
            for m in windows:
                r = row["by_window"].get(m)
                cells.append("        -" if not r else
                             f"{'F' if r['filled'] else 'x'}{r['bps']:>+8.0f}")
            lines.append(f"  {row['symbol'] + ' ' + row['when'].strftime('%m-%d %H:%M'):26} "
                         f"{row['spread_pct']:>6.2f}% " + " ".join(cells))
        lines += ["", "  F = the limit filled (saved the spread + fee)",
                  "  x = never filled, fell back to market at T (bps vs buying at once)"]
        for m in windows:
            got = [r["by_window"][m] for r in signal if r["by_window"].get(m)]
            if got:
                fill_rate = sum(g["filled"] for g in got) / len(got) * 100
                lines.append(f"  {m:>3}m wait: {fill_rate:5.1f}% filled, "
                             f"net {sum(g['bps'] for g in got) / len(got):+.1f} bps/trade")
    return "\n".join(lines)


def gate(mech, signal, windows=WINDOWS):
    """Grade the run against the registered rule. Returns the verdict and why.

    Phase 1 scored mechanics +5.4 bps at best and a 5m signal median of -0.0 on
    16 trades, and failed. A re-run is graded identically: a bigger sample is a
    reason to look again, never a reason to move the bar.
    """
    import statistics as _st
    best = max((a["bps"] / a["n"] for a in mech.values() if a["n"]), default=0.0)
    medians = {}
    for m in windows:
        got = [r["by_window"][m]["bps"] for r in signal if r["by_window"].get(m)]
        if got:
            medians[m] = _st.median(got)
    best_median = max(medians.values(), default=0.0)
    ok_mech = best >= GATE_MECHANICS_BPS
    ok_signal = best_median > GATE_MIN_SIGNAL_MEDIAN_BPS
    return {"mechanics_best_bps": round(best, 1),
            "signal_best_median_bps": round(best_median, 1),
            "signal_n": len(signal),
            "medians": {m: round(v, 1) for m, v in medians.items()},
            "passes_mechanics": ok_mech, "passes_signal": ok_signal,
            "verdict": "PASS — design the live pilot" if (ok_mech and ok_signal)
                       else "FAIL — ship nothing"}


def summary(g, windows=WINDOWS):
    """The short form, for Telegram."""
    p1 = PHASE1_RESULT
    lines = ["MAKER-ENTRY RE-TEST (offline, nothing placed)", "",
             f"  mechanics best   {g['mechanics_best_bps']:+.1f} bps"
             f"   (gate >= {GATE_MECHANICS_BPS:.0f})"
             f" {'ok' if g['passes_mechanics'] else 'FAILS'}",
             f"  signal median    {g['signal_best_median_bps']:+.1f} bps"
             f"   over {g['signal_n']} entries"
             f" {'ok' if g['passes_signal'] else 'FAILS'}",
             "", f"  VERDICT: {g['verdict']}", "",
             f"  Phase 1 ({p1['when']}) was {p1['mechanics_best_bps']:+.1f} bps "
             f"on {p1['signal_n']} entries -> {p1['verdict']}."]
    if not (g["passes_mechanics"] and g["passes_signal"]):
        lines += ["", "  A bigger sample is a reason to look again, never a reason",
                  "  to move the bar. Nothing ships."]
    return "\n".join(lines)


def _main(argv=None):
    quotes = live_spread_pct()
    if not quotes:
        from . import data
        quotes = {s: (t["sell"] / t["buy"] - 1) * 100
                  for s in config.WATCHLIST
                  if (t := data.fetch_tickers().get(config.pair_id(s))) and t["buy"]}
        print("(no recorded quotes yet — using a live sample; this is an estimate)\n")
    argv = list(argv if argv is not None else sys.argv[1:])
    days = 21 if "--since-phase1" in argv else 7
    mech = mechanics(quotes=quotes)
    sig = signal_sample(quotes=quotes, days=days)
    verdict = gate(mech, sig)
    print(render(mech, sig))
    print("")
    print(summary(verdict))
    if "--telegram" in argv:
        from . import notify
        notify.send("CryptoIndodaxBot\n" + summary(verdict))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
