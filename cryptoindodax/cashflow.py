"""External cash movements — data/cashflows.json.

A DEPOSIT IS NOT PROFIT

Every return figure this bot prints was computed against one hardcoded number,
`scorecard.START_EQUITY`, on the assumption that the rupiah in the account on
day one is the only rupiah that would ever go in. On 2026-09-23 the user topped
up Rp500.823 and the assumption broke: the page went on dividing live equity by
the day-one balance and reported **+98%** on an account that was actually down.
The pre-registered criterion reads the same field, so the leg that asks whether
the bot trails a buy-and-hold was reading "ahead" while the account trailed by
24 points. That is the worst kind of bug — not a crash, a flattering number.

This module is the missing input: an explicit, timestamped record of money
moving in or out, which the scorecard and the dashboard subtract before they
compute a return.

MEASURING RETURN ONCE THE BASE MOVES

    Two honest numbers, and they answer different questions:

      roi   = (equity - contributed) / contributed
              what the money in the account actually did, all cash weighted
              by how much of it there is. This is the human question.

      twr   = chained per-period returns (`twr()` below)
              what the *strategy* did, with the timing of deposits removed.
              This is the one that may be compared with a percentage
              benchmark, because a buy-and-hold has no deposits to time.

    The scorecard's benchmark leg compares against an equal-weight hold, so it
    uses the time-weighted figure; both are published so neither can hide.

WHY FLOWS ARE RECORDED BY HAND, NOT DETECTED

`unexplained()` can find a cash jump no trade accounts for, and it found this
one. It still does not write the record itself. A sell the ledger missed looks
exactly like a deposit from the outside, and auto-classifying it as capital
would quietly *remove* a real loss from the P&L — flattering the numbers again,
by the same mechanism, through the tool built to stop it. So detection warns
and a human confirms.
"""
import json
import re
import sys
from datetime import datetime, timezone

from . import config

PATH = config.ROOT / "data" / "cashflows.json"

# A cash move smaller than this is noise: fills land a little off the price the
# ledger booked, and fees are deducted in-asset. Rp50.000 is ~5% of the account
# and ~2.5x the entire lifetime fill-slippage gap, so nothing but a real
# transfer reaches it.
DETECT_TOLERANCE = 50_000.0


def _parse(ts):
    """ISO timestamp -> aware datetime (naive input is read as UTC)."""
    dt = ts if isinstance(ts, datetime) else datetime.fromisoformat(ts)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def load(path=None):
    """Recorded flows, oldest first. Missing file is simply no flows."""
    path = path or PATH
    try:
        flows = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(flows, list):
        return []
    return sorted(flows, key=lambda f: f.get("at") or "")


def save(flows, path=None):
    path = path or PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(flows, key=lambda f: f.get("at") or ""), indent=2))


def add(amount, at=None, note="", path=None):
    """Record one movement. Positive = deposit in, negative = withdrawal out."""
    at = _parse(at) if at else datetime.now(timezone.utc)
    flow = {"at": at.isoformat(), "amount": round(float(amount), 2), "note": note}
    flows = load(path)
    flows.append(flow)
    save(flows, path)
    return flow


def net_at(flows, when):
    """Net external cash contributed at or before `when`."""
    when = _parse(when)
    return sum(float(f.get("amount") or 0.0) for f in flows
               if f.get("at") and _parse(f["at"]) <= when)


def total(flows):
    return sum(float(f.get("amount") or 0.0) for f in flows)


def twr(curve):
    """Time-weighted return %, from a curve whose points carry `capital`.

    Each point is {equity, capital}; the strategy's P&L at that point is
    `equity - capital`, so a deposit moves both by the same amount and
    contributes nothing to the return. The step's denominator is the value at
    the start of the step plus any cash added during it, which is the standard
    treatment of a flow landing at period open — at hourly resolution the
    timing error is far below the noise in the marks.
    """
    series = twr_series(curve)
    return series[-1] if series else 0.0


def twr_series(curve):
    """Cumulative time-weighted return % at every point of the curve.

    One implementation, so the headline figure and the line on the chart can
    never disagree about what the account returned.
    """
    out, growth, prev = [], 1.0, None
    for point in curve:
        if prev is not None:
            added = point["capital"] - prev["capital"]
            base = prev["equity"] + added
            gain = ((point["equity"] - point["capital"])
                    - (prev["equity"] - prev["capital"]))
            if base > 0:
                growth *= 1 + gain / base
        prev = point
        out.append((growth - 1) * 100)
    return out


# --- detection ------------------------------------------------------------

def observations(path=None):
    """[(when, equity, cash, positions)] from the trader's own hourly log line.

    The trader has printed `account equity Rp... (cash Rp...)` on every run
    since 2026-09-01. It is a text log, not a series store, so it is no use as
    an equity curve — but it is a genuine record of the account's cash balance
    at 546 past instants, which is exactly what is needed to notice that cash
    once moved without a trade behind it.
    """
    path = path or config.LOG_DIR / "trader.log"
    rx = re.compile(r"^(\S+) account equity Rp([\d.]+) \(cash Rp([\d.]+)\), (\d+) coin")
    out = []
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return out
    for line in lines:
        m = rx.match(line)
        if m:
            out.append((_parse(m.group(1)),
                        float(m.group(2).replace(".", "")),
                        float(m.group(3).replace(".", "")),
                        int(m.group(4))))
    return out


def _trade_cash(led, lo, hi):
    """Cash the ledger says trading moved in (lo, hi]: sells in, buys out."""
    moved = 0.0
    for t in led.get("closed", []):
        if t.get("exit_time") and lo < _parse(t["exit_time"]) <= hi:
            moved += t["qty"] * t["exit_price"] - float(t.get("fees") or 0.0) / 2
        if t.get("entry_time") and lo < _parse(t["entry_time"]) <= hi:
            moved -= t["qty"] * t["entry_price"] + float(t.get("fees") or 0.0) / 2
    for p in led.get("open", []):
        if p.get("entry_time") and lo < _parse(p["entry_time"]) <= hi:
            moved -= p["qty"] * p["entry_price"] + float(p.get("entry_fee") or 0.0)
    return moved


def unexplained(obs, flows, led, tolerance=DETECT_TOLERANCE):
    """Cash moves no trade and no recorded flow accounts for.

    Returns [{from, to, moved, explained, residual}]. A residual here means one
    of two things and the tool cannot tell which: money was transferred in or
    out, or a fill happened that the ledger never saw. Both are worth a look;
    neither is written to the flow record automatically.
    """
    gaps = []
    for prev, point in zip(obs, obs[1:]):
        lo, hi = prev[0], point[0]
        moved = point[2] - prev[2]
        explained = _trade_cash(led, lo, hi) + (net_at(flows, hi) - net_at(flows, lo))
        residual = moved - explained
        if abs(residual) > tolerance:
            gaps.append({"from": lo.isoformat(), "to": hi.isoformat(),
                         "moved": round(moved, 2), "explained": round(explained, 2),
                         "residual": round(residual, 2)})
    return gaps


def render(flows, gaps=None):
    lines = ["CryptoIndodaxBot — external cash movements", ""]
    if not flows:
        lines.append("  (none recorded — every return is measured against the day-one balance)")
    for f in flows:
        lines.append(f"  {f['at'][:19]}  {config.fmt_idr(f['amount']):>16}"
                     f"   {f.get('note') or ''}")
    if flows:
        lines += ["", f"  net contributed    : {config.fmt_idr(total(flows))}"]
    if gaps is not None:
        lines += ["", f"  unrecorded moves   : {len(gaps)}"]
        for g in gaps:
            lines.append(f"    !! {g['from'][:19]} -> {g['to'][:19]}  cash moved "
                         f"{config.fmt_idr(g['moved'])}, trades explain "
                         f"{config.fmt_idr(g['explained'])}, "
                         f"UNEXPLAINED {config.fmt_idr(g['residual'])}")
        if gaps:
            lines += ["",
                      "  Every return figure is wrong by the unexplained amount until this is",
                      "  resolved. If it was a transfer, record it:",
                      "    python -m cryptoindodax.cashflow add <amount> --at <ISO> --note '...'",
                      "  If it was not, the ledger has missed a fill and the trade history is",
                      "  short one trade — do NOT record it as cash."]
    return "\n".join(lines)


def _amount(text):
    """Accept 500823 or the Indonesian 500.823 — dots are thousand separators."""
    text = text.strip()
    if re.fullmatch(r"-?\d{1,3}(\.\d{3})+", text):
        text = text.replace(".", "")
    return float(text.replace(",", "."))


def _main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    cmd = argv[0] if argv else "check"
    if cmd == "add":
        if len(argv) < 2:
            print("usage: cashflow add <amount> [--at ISO] [--note TEXT]")
            return 2
        amount = _amount(argv[1])
        at = note = None
        for flag, target in (("--at", "at"), ("--note", "note")):
            if flag in argv:
                value = argv[argv.index(flag) + 1]
                if target == "at":
                    at = value
                else:
                    note = value
        flow = add(amount, at=at, note=note or "")
        print(f"recorded {config.fmt_idr(flow['amount'])} at {flow['at']}")
        print(render(load()))
        return 0
    flows = load()
    gaps = None
    if cmd in ("check", "list"):
        gaps = unexplained(observations(), flows, _led()) if cmd == "check" else None
    print(render(flows, gaps))
    return 0


def _led():
    from . import ledger
    return ledger.load()


if __name__ == "__main__":
    sys.exit(_main())
