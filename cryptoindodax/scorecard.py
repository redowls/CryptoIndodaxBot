"""Pre-registered decision scorecard for the live account.

Registered 2026-09-17, before the evaluation window opened, so the criterion
cannot be moved afterwards to fit the result. That is the entire point: this
session showed how easy it is to find a convincing story in a dozen trades, and
a rule written after seeing the data is not a rule.

THE CRITERION

    At the decision point, STOP live trading and return to paper if BOTH hold:
      1. true win rate  < 35%   (take-profits only; a stop is a failed trade
                                 whatever its P&L sign, per standing doctrine)
      2. the account trails an equal-weight hold of the watchlist over the same
         period, measured at the bot's own average exposure

    Decision point: 2026-10-17 or 40 closed trades, whichever comes LATER.

Both conditions are required. One alone is survivable — a low win rate is fine
if the payoff is large enough, and trailing a raging bull market while barely
invested proves little. Both together say the strategy is neither picking
winners nor paying for its own costs.

AMENDMENT 2026-09-18 — written before any such exit existed

The profit-lock ladder (config.PROFIT_LOCK_RUNGS) added a new exit reason,
"lock". It is scored by the doctrine's own bucket rule, not by its name:
a lock exit is a WIN only when its r_multiple is at least LOCK_WIN_R (+1.0R);
below that it is a failed trade exactly like a stop. The tp-only figure is
still printed beside it so the original definition never disappears. The
decision constants did not move.

AMENDMENT 2026-09-24 — a correction, not a change of criterion

The account leg was computed as `(equity - START_EQUITY) / START_EQUITY`, which
silently treats a deposit as profit. A Rp500.823 top-up on 2026-09-23 therefore
read as +98% on an account that was down, and the "trails an equal-weight hold"
leg reported "ahead" while the account trailed by 24 points. External cash is
now recorded in `cashflow.py` and subtracted first. The criterion's constants
did not move; the number it was reading was wrong, and is now right.

WHAT THIS DELIBERATELY DOES NOT DO

It does not suggest a fix, and it does not tune anything. If the criterion
trips, the action is to stop and go back to paper — not to search for the
parameter that would have passed.
"""
import json
import sys
from datetime import datetime, timezone

from . import cashflow, config

DECISION_DATE = "2026-10-17"
MIN_TRADES = 40
TRUE_WIN_FLOOR = 35.0
LOCK_WIN_R = 1.0            # a "lock" exit is a win only at or above this R
START_EQUITY = 500_861.0
START_DATE = "2026-09-01"


def load_ledger():
    try:
        return json.loads((config.TRADES_DIR / "trades.json").read_text())
    except (OSError, ValueError):
        return {"open": [], "closed": []}


def metrics(led=None, equity=None, benchmark_pct=None, now=None,
            deposits=None, account_pct=None):
    """Everything the criterion needs, plus the verdict.

    `deposits` is net external cash contributed since START_DATE (cashflow.py);
    it is capital, never profit, so it is added to the base and subtracted from
    the gain. `account_pct` lets a caller that owns an equity curve supply the
    time-weighted return, which is the figure comparable with a percentage
    benchmark; without one this falls back to simple return on contributed
    capital, and says which it used.
    """
    led = led if led is not None else load_ledger()
    if deposits is None:
        deposits = cashflow.total(cashflow.load())
    now = now or datetime.now(timezone.utc)
    closed = led.get("closed", [])
    n = len(closed)
    tps = [t for t in closed if t.get("reason") == "tp"]
    locks = [t for t in closed if t.get("reason") == "lock"]
    lock_wins = [t for t in locks if (t.get("r_multiple") or 0.0) >= LOCK_WIN_R]
    stops = [t for t in closed if t.get("reason") == "stop"]
    net = sum(t.get("pnl", 0.0) for t in closed)
    gross = sum(t.get("pnl_gross", t.get("pnl", 0.0)) for t in closed)
    fees = sum(t.get("fees", 0.0) for t in closed)
    estimated = sum(1 for t in closed if t.get("fees_estimated"))
    true_win = ((len(tps) + len(lock_wins)) / n * 100) if n else 0.0
    tp_only = (len(tps) / n * 100) if n else 0.0

    days = (now - datetime.fromisoformat(START_DATE + "T00:00:00+00:00")).days
    due = now.date().isoformat() >= DECISION_DATE and n >= MIN_TRADES

    invested = START_EQUITY + deposits
    net_pnl = (equity - invested) if equity else None
    roi_pct = (net_pnl / invested * 100) if (equity and invested > 0) else None
    method = "time-weighted" if account_pct is not None else "simple"
    if account_pct is None:
        account_pct = roi_pct
    trails = (account_pct is not None and benchmark_pct is not None
              and account_pct < benchmark_pct)

    fails_win = true_win < TRUE_WIN_FLOOR
    if not due:
        verdict = "COLLECTING"
    elif fails_win and trails:
        verdict = "STOP — return to paper"
    else:
        verdict = "CONTINUE"
    return {
        "trades": n, "tp": len(tps), "stops": len(stops),
        "locks": len(locks), "lock_wins": len(lock_wins),
        "true_win_pct": round(true_win, 1), "tp_only_win_pct": round(tp_only, 1),
        "stop_pct": round(len(stops) / n * 100, 1) if n else 0.0,
        "net_realised": round(net, 2), "gross_realised": round(gross, 2),
        "fees": round(fees, 2), "fees_estimated_trades": estimated,
        "days_live": days, "decision_due": due,
        "account_pct": account_pct, "benchmark_pct": benchmark_pct,
        "account_pct_method": method, "roi_pct": roi_pct,
        "deposits": round(deposits, 2), "invested_capital": round(invested, 2),
        "net_pnl": round(net_pnl, 2) if net_pnl is not None else None,
        "trails_benchmark": trails, "fails_win_floor": fails_win,
        "verdict": verdict,
    }


def render(m):
    lines = [
        "CryptoIndodaxBot — pre-registered scorecard",
        f"registered 2026-09-17 | decision at {DECISION_DATE} AND >= {MIN_TRADES} trades",
        "",
        f"  closed trades      : {m['trades']}  ({m['tp']} tp, {m['locks']} lock, "
        f"{m['stops']} stop)",
        f"  true win rate      : {m['true_win_pct']}%   floor {TRUE_WIN_FLOOR}%"
        f"   -> {'FAILS' if m['fails_win_floor'] else 'ok'}",
        f"                       = tp {m['tp']} + lock at/above +{LOCK_WIN_R:.0f}R "
        f"{m['lock_wins']} of {m['locks']}   (tp-only {m['tp_only_win_pct']}%)",
        f"  stop rate          : {m['stop_pct']}%",
        f"  realised net       : {config.fmt_idr(m['net_realised'])}"
        f"   (gross {config.fmt_idr(m['gross_realised'])},"
        f" fees {config.fmt_idr(m['fees'])})",
    ]
    if m["fees_estimated_trades"]:
        lines.append(f"  NOTE: {m['fees_estimated_trades']} trade(s) carry modelled fees, "
                     "not exchange-reported ones")
    if m["deposits"]:
        lines.append(f"  deposits           : {config.fmt_idr(m['deposits'])}"
                     f"   (capital, not profit — contributed "
                     f"{config.fmt_idr(m['invested_capital'])} in total)")
    if m["account_pct"] is not None:
        lines.append(f"  account since start: {m['account_pct']:+.2f}%"
                     f"   ({m['account_pct_method']})")
    if m["net_pnl"] is not None and m["deposits"]:
        lines.append(f"                       = {config.fmt_idr(m['net_pnl'])} on "
                     f"{config.fmt_idr(m['invested_capital'])} contributed "
                     f"({m['roi_pct']:+.2f}% simple)")
    if m["benchmark_pct"] is not None:
        lines.append(f"  equal-weight hold  : {m['benchmark_pct']:+.2f}%"
                     f"   -> {'TRAILS' if m['trails_benchmark'] else 'ahead'}")
    lines += ["", f"  days live          : {m['days_live']}",
              f"  VERDICT            : {m['verdict']}"]
    if m["verdict"] == "COLLECTING":
        lines.append("  (criterion is not evaluated until BOTH the date and the "
                     "trade count are reached)")
    return "\n".join(lines)


def _main(argv=None):
    equity = benchmark = None
    from . import replay
    history = replay.load_history()
    try:
        from . import broker
        # get_account only values coins whose price is supplied, so without
        # marks it silently reports CASH as equity and an open position looks
        # like a 10% loss that never happened.
        marks = replay._closes(history[-1][1]) if history else {}
        equity = broker.get_account(price_by_symbol=marks)["equity"]
    except Exception:
        pass
    try:
        if history:
            # Only coins present in the FIRST snapshot count. A coin added
            # last week would otherwise contribute a few days of return to a
            # 16-day benchmark and quietly flatter or punish the comparison.
            opening = replay._closes(history[0][1])
            closing = replay._closes(history[-1][1])
            moves = [(closing[sym] / opening[sym] - 1) * 100
                     for sym in config.WATCHLIST
                     if opening.get(sym) and closing.get(sym)]
            if moves:
                benchmark = sum(moves) / len(moves)
    except Exception:
        pass
    account_pct = None
    try:
        # The time-weighted figure needs an equity curve, which dashboard owns.
        # Imported here, not at module scope: dashboard imports this file.
        from . import dashboard, ledger
        flows = cashflow.load()
        curve = dashboard.equity_curve(history, ledger.load(), flows=flows)
        if curve:
            account_pct = cashflow.twr(curve)
    except Exception:
        pass
    print(render(metrics(equity=equity, benchmark_pct=benchmark,
                         account_pct=account_pct)))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
