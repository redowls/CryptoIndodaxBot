"""Backtest harness — drives the REAL engines over stored snapshot history.

The point of this module is that it does not reimplement the strategy. It calls
`strategy.regime`, `strategy.entry_candidates`, `strategy.check_exit` and
`risk.position_size` — the same functions the live trader calls — so a replay
result cannot drift from live behaviour the way a parallel implementation
would. Anything it gets wrong is wrong in the bot too.

It exists because two exit recommendations were shipped on reasoning alone, and
the second one (TRAIL_ATR_MULT 6.0 -> 2.0) turned out to cost about 2R. Tuning
without a harness is guessing.

WHAT IT MODELS
  * the hourly cycle in trader.run(): regime -> exits -> circuit breaker ->
    entries, with slot contention, the re-entry throttle and position sizing
  * fees on both sides, defaulting to the observed round trip rather than the
    configured one, because the ledger's own pnl field is gross (see below)
  * equity compounding, so sizing shrinks after losses the way it does live

WHAT IT CANNOT MODEL — read before trusting a number
  * **Hourly closes only.** Snapshots store one close per hour, so an intrabar
    spike through a stop is invisible. Live has the same blind spot (exits are
    evaluated on the 1H close), so this understates slippage equally in both.
  * **Fills at the close.** Live fills at market after the close, and the
    recorded fill price is currently the snapshot close anyway because
    avg_fill_price is broken, so replay and live share this error.
  * **Short history.** Snapshots begin 2026-09-01, and only 5 of the 10 coins
    existed in them before 2026-09-07. Any result covering less than a few
    hundred trades is directional, not proof.
  * **Approximated day change.** The live trader reads 1D bars for
    `day_change_pct`; replay derives it from the snapshot series (now versus
    the last close before today's 00:00 UTC). Close, not identical.
  * **No policy overlay by default.** Live had hand-written blocks on most
    days. Replay runs the pure engine unless `blocked` is passed, so it answers
    "what does the strategy do", not "what did this account do". Pass
    `use_policy_history=True` (CLI `--historical-policy`) to replay the daily
    overlay exactly as it stood, reconstructed from git.
  * **Today's config over yesterday's history.** Entry rules changed on
    2026-09-07 (ADX 25 -> 20, MIN_ATR_PCT added, trail 6.0 -> 2.0). Replay
    applies whatever config is loaded NOW to the whole window, so only the
    period from 2026-09-07 17:00 onward can be compared against live at all.
    Use `--validate` for that comparison and `--from` to window a run.
"""
import argparse
import glob
import json
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from . import config, ledger, regime_lab, risk, scorecard, strategy


# --- history --------------------------------------------------------------

def load_history(pattern=None):
    """[(datetime, snapshot), ...] ascending by capture time."""
    pattern = pattern or str(config.DATA_DIR / "*" / "*.json")
    out = []
    for path in sorted(glob.glob(pattern)):
        try:
            snap = json.loads(open(path).read())
            out.append((datetime.fromisoformat(snap["captured_at"]), snap))
        except (OSError, ValueError, KeyError):
            continue
    out.sort(key=lambda x: x[0])
    return out


def _closes(snap):
    out = {}
    for coin in snap.get("symbols", []):
        tf = coin.get("timeframes", {}).get("1H", {})
        if tf.get("status") == "ok" and tf.get("last_close"):
            out[coin["symbol"]] = tf["last_close"]
    return out


def build_extras(history):
    """Per-index {symbol: extras} mirroring trader.fetch_extras.

    `prev_1h_close` is the previous snapshot's close and `day_change_pct` is
    measured against the last close before the current UTC day began — the
    documented approximation of the live 1D-bar read.
    """
    per_index = []
    day_open = {}
    current_day = None
    prev_closes = {}
    for dt, snap in history:
        closes = _closes(snap)
        if dt.date() != current_day:
            current_day = dt.date()
            day_open = dict(prev_closes) or dict(closes)
        extras = {}
        for sym, close in closes.items():
            base = day_open.get(sym)
            extras[sym] = {
                "day_change_pct": ((close / base - 1) * 100) if base else None,
                "last_1h_close": close,
                "prev_1h_close": prev_closes.get(sym),
            }
        per_index.append(extras)
        prev_closes.update(closes)
    return per_index


def load_policy_history():
    """[(active_from, policy_dict)] reconstructed from git history of policy.json.

    A policy file becomes effective when it is committed, not on the date it
    names, so the commit timestamp is what the replay keys on.
    """
    try:
        log = subprocess.run(["git", "log", "--format=%H %cI", "--", "memory/policy.json"],
                             cwd=str(config.ROOT), capture_output=True, text=True,
                             timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return []
    out = []
    for line in log.split("\n"):
        if not line.strip():
            continue
        sha, when = line.split()
        try:
            blob = subprocess.run(["git", "show", f"{sha}:memory/policy.json"],
                                  cwd=str(config.ROOT), capture_output=True, text=True,
                                  timeout=30).stdout
            pol = json.loads(blob)
            out.append((datetime.fromisoformat(when), pol))
        except (ValueError, OSError, subprocess.SubprocessError):
            continue
    out.sort(key=lambda x: x[0])
    return out


def policy_at(pol_history, when):
    """The overlay in force at `when` — the latest one committed at or before it."""
    active = {"regime_hint": "auto", "blocked_symbols": [],
              "max_positions": config.MAX_POSITIONS}
    for committed, pol in pol_history:
        if committed <= when:
            active = pol
        else:
            break
    return active


@contextmanager
def overrides(**kwargs):
    """Temporarily set config knobs (TRAIL_ATR_MULT=4.0, TP_R=3.0, ...)."""
    saved = {k: getattr(config, k) for k in kwargs}
    try:
        for k, v in kwargs.items():
            setattr(config, k, v)
        yield
    finally:
        for k, v in saved.items():
            setattr(config, k, v)


# --- the simulation -------------------------------------------------------

def run(history=None, start_equity=500_861.0, fee_pct=None, blocked=(),
        regime_hint="auto", max_positions=None, use_policy_history=False,
        regime_fn=None):
    """Replay every hourly snapshot. Returns a result dict."""
    history = history if history is not None else load_history()
    if not history:
        return {"trades": [], "equity_curve": [], "start_equity": start_equity,
                "end_equity": start_equity, "hours": 0, "fee_pct": 0.0}
    fee = config.OBSERVED_ROUND_TRIP_PCT / 100.0 / 2 if fee_pct is None else fee_pct / 100.0 / 2
    extras_by_index = build_extras(history)
    cap = min(max_positions or config.MAX_POSITIONS, config.MAX_POSITIONS)
    pol_history = load_policy_history() if use_policy_history else []
    # Swap the regime gate without touching strategy.py — see regime_lab. A
    # stateful variant must be a fresh instance per run or hysteresis leaks
    # between scenarios.
    gate = regime_fn or strategy.regime

    led = {"open": [], "closed": [], "last_entry_attempt": {}}
    cash = start_equity
    curve = []

    for i, (now, snap) in enumerate(history):
        closes = _closes(snap)
        equity = cash + sum(p["qty"] * closes.get(p["symbol"], p["entry_price"])
                            for p in led["open"])
        curve.append((now.isoformat(), equity))

        if pol_history:
            pol = policy_at(pol_history, now)
            hint = pol.get("regime_hint", "auto")
            blocked_now = set(pol.get("blocked_symbols") or [])
            cap_now = min(pol.get("max_positions") or cap, config.MAX_POSITIONS)
        else:
            hint, blocked_now, cap_now = regime_hint, set(blocked), cap
        reg = strategy.effective_regime(gate(snap), hint)

        # --- exits (same order as trader.run) ---
        for pos in list(led["open"]):
            h1 = next((c.get("timeframes", {}).get("1H", {})
                       for c in snap.get("symbols", []) if c.get("symbol") == pos["symbol"]), None)
            if not h1 or h1.get("status") != "ok":
                continue
            action, updated = strategy.check_exit(pos, h1, reg, ledger.hours_held(pos, now))
            if action:
                price = h1["last_close"]
                proceeds = updated["qty"] * price * (1 - fee)
                cost = updated["qty"] * updated["entry_price"] * (1 + fee)
                cash += proceeds
                trade = ledger.close_position(led, updated, price, action, now=now)
                trade["pnl_net"] = round(proceeds - cost, 2)
                # r_multiple and peak_r are booked by ledger.close_position now,
                # so live trades and replayed ones carry identical geometry.
                trade["fees"] = round(updated["qty"] * (price + updated["entry_price"]) * fee, 2)
            else:
                ledger.update_position(led, updated)

        equity = cash + sum(p["qty"] * closes.get(p["symbol"], p["entry_price"])
                            for p in led["open"])
        if equity <= 0 or risk.circuit_breaker_tripped(
                [dict(t, pnl=t.get("pnl_net", t["pnl"])) for t in led["closed"]], equity, now=now):
            continue

        # --- entries ---
        open_syms = {p["symbol"] for p in led["open"]}
        slots = cap_now - len(open_syms)
        if slots <= 0:
            continue
        candidates, _ = strategy.entry_candidates(
            snap, extras_by_index[i], open_syms, reg, blocked=blocked_now)
        entered = 0
        for sym, coin in candidates:
            if entered >= slots:
                break
            if ledger.throttled(led, sym, now=now):
                continue
            h1 = coin["timeframes"]["1H"]
            price, atr = h1["last_close"], h1.get("atr14")
            qty, stop, _ = risk.position_size(equity, price, atr, symbol=sym)
            if qty <= 0:
                continue
            spend = qty * price * (1 + fee)
            if spend > cash:
                continue
            cash -= spend
            ledger.open_position(led, sym, qty, price, atr, order_id=f"replay-{i}", now=now)
            ledger.record_entry_attempt(led, sym, now=now)
            entered += 1

    final_closes = _closes(history[-1][1])
    end_equity = cash + sum(p["qty"] * final_closes.get(p["symbol"], p["entry_price"])
                            for p in led["open"])
    return {"trades": led["closed"], "open": led["open"], "equity_curve": curve,
            "start_equity": start_equity, "end_equity": end_equity,
            "hours": len(history), "fee_pct": fee * 2 * 100,
            "span": (history[0][0].isoformat()[:16], history[-1][0].isoformat()[:16])}


# --- reporting ------------------------------------------------------------

def summarize(result):
    """Headline stats, including the stop-exit doctrine view.

    A stop-triggered exit counts as a FAILURE whatever its P&L sign, so the
    true win rate is take-profits only. A break-even stop is not a win.
    """
    trades = result["trades"]
    n = len(trades)
    wins = [t for t in trades if t.get("pnl_net", t["pnl"]) > 0]
    tps = [t for t in trades if t["reason"] == "tp"]
    stops = [t for t in trades if t["reason"] == "stop"]
    # A profit-lock exit is scored by the doctrine bucket, not by its name: a
    # win at or above +1R, a failed trade below it (scorecard.LOCK_WIN_R).
    locks = [t for t in trades if t["reason"] == "lock"]
    lock_wins = [t for t in locks if (t.get("r_multiple") or 0) >= scorecard.LOCK_WIN_R]
    # Giveback: how much of its peak a trade that got to +1R handed back. This
    # is the number the ladder exists to lower, so it belongs beside the money.
    reached = [t for t in trades if (t.get("peak_r") or 0) >= 1.0]
    giveback = (sum(t["peak_r"] - (t.get("r_multiple") or 0) for t in reached) / len(reached)
                if reached else 0.0)
    gross_win = sum(t.get("pnl_net", t["pnl"]) for t in wins)
    gross_loss = -sum(t.get("pnl_net", t["pnl"]) for t in trades
                      if t.get("pnl_net", t["pnl"]) <= 0)
    peak, dd = result["start_equity"], 0.0
    for _, eq in result["equity_curve"]:
        peak = max(peak, eq)
        dd = max(dd, (peak - eq) / peak if peak else 0)
    net = result["end_equity"] - result["start_equity"]
    return {
        "trades": n,
        "net_idr": round(net, 2),
        "return_pct": round(net / result["start_equity"] * 100, 2) if result["start_equity"] else 0,
        "total_r": round(sum((t.get("r_multiple") or 0) for t in trades), 2),
        "headline_win_pct": round(len(wins) / n * 100, 1) if n else 0,
        "true_win_pct": round((len(tps) + len(lock_wins)) / n * 100, 1) if n else 0,
        "tp_only_win_pct": round(len(tps) / n * 100, 1) if n else 0,
        "lock_pct": round(len(locks) / n * 100, 1) if n else 0,
        "stop_pct": round(len(stops) / n * 100, 1) if n else 0,
        "reached_1r": len(reached),
        "giveback_r": round(giveback, 2),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "max_dd_pct": round(dd * 100, 2),
        "fees_idr": round(sum(t.get("fees", 0) for t in trades), 2),
        "open_at_end": len(result.get("open", [])),
    }


def buy_and_hold(history, symbol="BTC", start_equity=500_861.0):
    """The benchmark a long-only strategy has to beat."""
    first = next((_closes(s).get(symbol) for _, s in history if _closes(s).get(symbol)), None)
    last = next((_closes(s).get(symbol) for _, s in reversed(history) if _closes(s).get(symbol)), None)
    if not first or not last:
        return None
    return round((last / first - 1) * 100, 2)


def _fmt(label, s):
    return (f"{label:<27}{s['trades']:>4}{s['net_idr']:>12,.0f}{s['return_pct']:>8.2f}%"
            f"{s['total_r']:>8.2f}R{s['true_win_pct']:>8.1f}%{s['lock_pct']:>7.1f}%"
            f"{s['stop_pct']:>7.1f}%{s['giveback_r']:>8.2f}R"
            f"{str(s['profit_factor']):>7}{s['max_dd_pct']:>8.2f}%")


def _header():
    return (f"{'variant':<27}{'n':>4}{'net IDR':>12}{'return':>9}{'total R':>8}"
            f"{'trueWin':>8}{'lock%':>7}{'stop%':>7}{'gvback':>9}{'PF':>7}{'maxDD':>9}\n"
            + "-" * 107)


def main(argv=None):
    p = argparse.ArgumentParser(description="CryptoIndodaxBot replay harness")
    p.add_argument("--trail", type=float, help="override TRAIL_ATR_MULT")
    p.add_argument("--tp", type=float, help="override TP_R")
    p.add_argument("--stop", type=float, help="override STOP_ATR_MULT")
    p.add_argument("--adx", type=float, help="override ENTRY_ADX_MIN")
    p.add_argument("--atr-floor", type=float, help="override MIN_ATR_PCT")
    p.add_argument("--equity", type=float, default=500_861.0)
    p.add_argument("--fee", type=float, help="round-trip fee %% (default: observed)")
    p.add_argument("--sweep", choices=["trail", "tp", "adx", "atr"],
                   help="scan one knob instead of a single run")
    p.add_argument("--trades", action="store_true", help="list every trade")
    p.add_argument("--historical-policy", action="store_true",
                   help="apply the daily overlay as it actually stood (from git)")
    p.add_argument("--from", dest="start", help="only replay snapshots from this ISO date")
    p.add_argument("--regimes", action="store_true",
                   help="score every experimental regime gate on the bull window, "
                        "the bear window and the whole history")
    p.add_argument("--ladder", action="store_true",
                   help="score the pre-declared profit-lock ladder variants, plus "
                        "flat-percent comparison rows, on bull/bear/full windows")
    p.add_argument("--validate", action="store_true",
                   help="replay 2026-09-07T17:00 onward with the historical policy and "
                        "compare entries against the live ledger")
    args = p.parse_args(argv)

    if args.regimes:
        return _regime_lab(args)
    if args.ladder:
        return _ladder_lab(args)
    if args.validate:
        return _validate()

    history = load_history()
    if args.start:
        cutoff = datetime.fromisoformat(args.start)
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=timezone.utc)
        history = [(d, s_) for d, s_ in history if d >= cutoff]
    if not history:
        print("no snapshots under data/snapshots/ — nothing to replay")
        return 1
    ov = {k: v for k, v in (("TRAIL_ATR_MULT", args.trail), ("TP_R", args.tp),
                            ("STOP_ATR_MULT", args.stop), ("ENTRY_ADX_MIN", args.adx),
                            ("MIN_ATR_PCT", args.atr_floor)) if v is not None}

    span = (history[0][0].isoformat()[:16], history[-1][0].isoformat()[:16])
    print(f"replaying {len(history)} hourly snapshots  {span[0]} -> {span[1]}")
    bh = buy_and_hold(history)
    print(f"benchmark: BTC buy-and-hold over the same window {bh:+.2f}%\n")

    if args.sweep:
        values = {"trail": [1.5, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0],
                  "tp": [1.5, 2.0, 2.5, 3.0, 4.0, 5.0],
                  "adx": [15, 18, 20, 22, 25, 30],
                  "atr": [0.0, 0.5, 0.75, 1.0, 1.25]}[args.sweep]
        knob = {"trail": "TRAIL_ATR_MULT", "tp": "TP_R", "adx": "ENTRY_ADX_MIN",
                "atr": "MIN_ATR_PCT"}[args.sweep]
        print(_header())
        for v in values:
            with overrides(**{**ov, knob: v}):
                s = summarize(run(history, start_equity=args.equity, fee_pct=args.fee,
                                  use_policy_history=args.historical_policy))
            live = "  <- live" if v == getattr(config, knob) else ""
            print(_fmt(f"{knob}={v}", s) + live)
    else:
        with overrides(**ov):
            res = run(history, start_equity=args.equity, fee_pct=args.fee,
                      use_policy_history=args.historical_policy)
            s = summarize(res)
        print(_header())
        print(_fmt("config as given", s))
        print(f"\nfees paid {config.fmt_idr(s['fees_idr'])} at {res['fee_pct']:.2f}% round trip"
              f" | {s['open_at_end']} still open at the end")
        if args.trades:
            print(f"\n{'exit':<17}{'coin':<6}{'why':<6}{'entry':>14}{'exit px':>14}"
                  f"{'peak R':>8}{'R':>8}{'net IDR':>11}")
            for t in res["trades"]:
                print(f"{t['exit_time'][:16]:<17}{t['symbol']:<6}{t['reason']:<6}"
                      f"{t['entry_price']:>14,.0f}{t['exit_price']:>14,.0f}"
                      f"{(t.get('peak_r') or 0):>+8.2f}{(t.get('r_multiple') or 0):>+8.2f}"
                      f"{t.get('pnl_net', 0):>+11,.0f}")

    print("\nCAVEAT: hourly closes only, no intrabar; snapshots start 2026-09-01 and "
          "carried 5 of 10 coins before 09-07. Treat as directional, not proof.")
    return 0


# The bull window ran to the 09-07 peak (equity Rp531.150); everything after is
# the decline. Splitting there is what exposes a gate that only works on one.
REGIME_SPLIT = "2026-09-07T17:00:00+00:00"


def _regime_lab(args):
    """Score each experimental gate on bull, bear and full windows."""
    history = load_history()
    if not history:
        print("no snapshots to replay")
        return 1
    split = datetime.fromisoformat(REGIME_SPLIT)
    windows = [("BULL  01-07", [h for h in history if h[0] < split]),
               ("BEAR  07-16", [h for h in history if h[0] >= split]),
               ("FULL  01-16", history)]
    ov = {k: v for k, v in (("TRAIL_ATR_MULT", args.trail), ("TP_R", args.tp),
                            ("STOP_ATR_MULT", args.stop), ("ENTRY_ADX_MIN", args.adx),
                            ("MIN_ATR_PCT", args.atr_floor)) if v is not None}
    print("REGIME GATE VARIANTS — scored on each window separately.\n"
          "A gate that only wins on BEAR is fitted to the decline, not better.\n")
    for label, hist in windows:
        if not hist:
            continue
        print(f"=== {label} ({len(hist)} snapshots) ===")
        print(f"{'gate':<34}{'n':>4}{'net IDR':>11}{'return':>9}{'trueWin':>9}{'stop%':>8}{'maxDD':>9}")
        print("-" * 84)
        for variant in regime_lab.variants():
            with overrides(**ov):
                res = run(hist, use_policy_history=args.historical_policy,
                          regime_fn=variant)
                st = summarize(res)
            tag = "  <- live" if isinstance(variant, regime_lab.Current) else ""
            print(f"{variant.name:<34}{st['trades']:>4}{st['net_idr']:>11,.0f}"
                  f"{st['return_pct']:>8.2f}%{st['true_win_pct']:>8.1f}%"
                  f"{st['stop_pct']:>7.1f}%{st['max_dd_pct']:>8.2f}%{tag}")
        print()
    print("CAVEAT: one bull run and one decline, 16 days, low double-digit trade\n"
          "counts per cell. This ranks hypotheses; it does not confirm any of them.")
    return 0


# --- profit-lock ladder lab ----------------------------------------------
# Pre-declared 2026-09-18 BEFORE any of these rows was run, and the decision
# rule is written down here so that a result cannot pick the rule afterwards:
#   1. no rung may activate below +1.5R (structural, see config.py)
#   2. ship the SIMPLEST rung set that (a) does not turn the BULL window from a
#      profit into a loss and (b) lowers average giveback on FULL against live
#   3. the flat-percent rows answer "would a flat 2% or 3% have done it?" — they
#      are comparison only and never ship, because a fixed percentage is one
#      ordinary bar on a meme coin and four bars on BTC
#   4. the last row is the refuted 2026-09-07 tight trail expressed as a rung.
#      The harness has to reproduce that collapse or nothing above it is trusted.
LADDER_VARIANTS = [
    ("live: no ladder", ()),
    ("1.5R -> 1.0 ATR", ((1.5, 1.0),)),
    ("1.5R -> 1.5 ATR", ((1.5, 1.5),)),
    ("1.5R -> 2.0 ATR", ((1.5, 2.0),)),
    ("1.5R->2.0, 2.0R->1.0", ((1.5, 2.0), (2.0, 1.0))),
    ("1.5R->1.5, 2R->1, 2.25R->.5", ((1.5, 1.5), (2.0, 1.0), (2.25, 0.5))),
    ("control: 1.0R -> 2.0 ATR", ((1.0, 2.0),)),
]
FLAT_PCT_VARIANTS = [(8.0, 2.0), (8.0, 3.0), (5.0, 2.0), (10.0, 2.0)]


@contextmanager
def _flat_pct_lock(activate_pct, giveback_pct):
    """Swap strategy.check_exit for one that locks a flat PERCENTAGE under the
    peak, so the ATR ladder can be compared against the intuitive version.
    Lab only — nothing in the engine reads percentages."""
    real = strategy.check_exit

    def patched(position, h1, reg, hours_held):
        action, pos = real(position, h1, reg, hours_held)
        if action is None:
            hw, entry, close = pos["high_water"], pos["entry_price"], h1["last_close"]
            if ((hw / entry - 1) * 100 >= activate_pct
                    and close <= hw * (1 - giveback_pct / 100)):
                return "lock", pos
        return action, pos

    strategy.check_exit = patched
    try:
        yield
    finally:
        strategy.check_exit = real


def _ladder_lab(args):
    """Score every pre-declared ladder on the bull window, the bear window and
    the whole history. A ladder that only wins on BEAR is fitted to the
    decline: locking profit is trivially right in a market that keeps falling,
    and the question is what it costs when the move keeps going."""
    history = load_history()
    if not history:
        print("no snapshots to replay")
        return 1
    split = datetime.fromisoformat(REGIME_SPLIT)
    windows = [("BULL  to 09-07", [h for h in history if h[0] < split]),
               ("BEAR  09-07 on", [h for h in history if h[0] >= split]),
               ("FULL", history)]
    ov = {k: v for k, v in (("TRAIL_ATR_MULT", args.trail), ("TP_R", args.tp),
                            ("STOP_ATR_MULT", args.stop), ("ENTRY_ADX_MIN", args.adx),
                            ("MIN_ATR_PCT", args.atr_floor)) if v is not None}
    print("PROFIT-LOCK LADDER — rungs are (activate at +R of PEAK gain, trail in ATR).\n"
          "gvback = average R handed back from the peak by trades that reached +1R.\n"
          "The decision rule is pre-declared in replay.py; a row does not get to pick it.\n")
    for label, hist in windows:
        if not hist:
            continue
        print(f"=== {label} ({len(hist)} snapshots) ===")
        print(_header())
        for name, rungs in LADDER_VARIANTS:
            with overrides(PROFIT_LOCK_RUNGS=rungs, **ov):
                st = summarize(run(hist, use_policy_history=args.historical_policy))
            tag = ("  <- live" if not rungs
                   else "  <- refuted geometry" if rungs[0][0] < 1.5 else "")
            print(_fmt(name, st) + tag)
        for act, give in FLAT_PCT_VARIANTS:
            with overrides(PROFIT_LOCK_RUNGS=(), **ov), _flat_pct_lock(act, give):
                st = summarize(run(hist, use_policy_history=args.historical_policy))
            print(_fmt(f"flat: peak>={act:.0f}%, give {give:.0f}%", st) + "  (comparison only)")
        print()
    print("CAVEAT: hourly closes only, one bull run and one decline, low double-digit\n"
          "trade counts per cell. This ranks hypotheses; it does not confirm any of them.")
    return 0


VALIDATE_FROM = "2026-09-07T17:00:00+00:00"


def _validate():
    """Compare a historical-policy replay against the live ledger.

    Only the window after 2026-09-07T17:00 is comparable: that is when the
    current entry rules (ADX 20, MIN_ATR_PCT) went live, and replay always runs
    today's config. Before it, replay and live are different strategies and a
    mismatch proves nothing.
    """
    cutoff = datetime.fromisoformat(VALIDATE_FROM)
    history = [(d, s) for d, s in load_history() if d >= cutoff]
    if not history:
        print("no snapshots in the validation window")
        return 1
    try:
        live = json.loads((config.TRADES_DIR / "trades.json").read_text())
    except (OSError, ValueError):
        print("no live ledger to compare against")
        return 1

    res = run(history, use_policy_history=True)
    live_entries = [(t["symbol"], t["entry_time"], t["entry_price"])
                    for t in live["closed"] + live["open"]
                    if t["entry_time"] >= VALIDATE_FROM]
    rep_entries = [(t["symbol"], t["entry_time"], t["entry_price"])
                   for t in res["trades"] + res["open"]]

    print(f"VALIDATION — replay vs live, {VALIDATE_FROM[:16]} onward "
          f"({len(history)} snapshots)\n")
    print(f"  live entries   : {len(live_entries)}")
    print(f"  replay entries : {len(rep_entries)}")
    # Match on coin and nearest entry time rather than exact price: live acts at
    # :14 on freshly fetched bars, replay at :07 on the stored snapshot, so the
    # same decision lands a fraction of a bar apart. Price delta is the signal.
    unmatched = list(rep_entries)
    print(f"\n  {'coin':<6}{'live entry':>16}{'replay entry':>16}{'delta':>9}   live time")
    matched = 0
    deltas = []
    for sym, when, px in sorted(live_entries, key=lambda x: x[1]):
        cand = [r for r in unmatched if r[0] == sym]
        if not cand:
            print(f"  {sym:<6}{px:>16,.0f}{'— not taken —':>16}{'':>9}   {when[:16]}")
            continue
        best = min(cand, key=lambda r: abs(
            (datetime.fromisoformat(r[1]) - datetime.fromisoformat(when)).total_seconds()))
        unmatched.remove(best)
        d = (best[2] / px - 1) * 100
        deltas.append(abs(d))
        matched += 1
        print(f"  {sym:<6}{px:>16,.0f}{best[2]:>16,.0f}{d:>+8.2f}%   {when[:16]}")
    for r in unmatched:
        print(f"  {r[0]:<6}{'— live skipped —':>16}{r[2]:>16,.0f}{'':>9}   {r[1][:16]}")
    if deltas:
        print(f"\n  matched {matched}/{len(live_entries)} live entries by coin; "
              f"mean entry-price difference {sum(deltas)/len(deltas):.2f}% "
              f"(one 1H bar is ~{config.MIN_ATR_PCT:.2f}%+)")
    print("\n  Divergence is expected once any single decision differs: a filled slot "
          "changes\n  every later cycle. Read the overlap as a sanity check on the "
          "mechanics, not\n  as proof the harness reproduces live trade for trade.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
