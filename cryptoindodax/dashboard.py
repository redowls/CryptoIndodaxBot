"""Dashboard data builder — writes data/dashboard.json for the static web page.

Strictly read-only. It places no orders, mutates no ledger and writes nowhere
but its own output file, so it is safe to run from cron alongside the trader.

THE EQUITY CURVE IS RECONSTRUCTED, NOT RECORDED

Nothing in this bot has ever logged account equity over time: `broker.get_account`
answers for *now*, and the answer is gone when the process exits. So the curve is
rebuilt from the two things that were kept — the hourly snapshot archive (one
close per watchlist coin since START_DATE) and the trade ledger:

    equity(t) = START_EQUITY + realised_net(exit_time <= t)
                             + unrealised(positions open at t, marked at t)

It reuses the ledger's own net `pnl` — the audited, fee-inclusive field — so the
curve cannot quietly disagree with the scorecard about how much was made.

WHAT THE CURVE ASSUMES, AND WHERE IT BREAKS

  * **No deposits or withdrawals since START_DATE.** Move rupiah into or out of
    the Indodax account and every point after it is wrong by that amount.
  * **Hourly resolution.** An intra-hour spike is invisible — exactly as it is
    to the trader, which also only sees 1H closes.
  * **Fees land at the exit.** A closed trade stores one combined `fees` figure,
    so the whole round trip is charged when the trade closes. Final equity is
    right; the dip *during* a closed trade is understated by its entry fee.
    Still-open positions do carry their own recorded `entry_fee`.
  * **A coin missing from a snapshot** is carried at its last known close rather
    than dropped, which would otherwise print a cliff the account never took.

Because those assumptions can rot silently, `build()` also asks the broker for
live equity and publishes the gap as `totals.drift`. The page renders that gap
instead of hiding it: a curve that has desynced should say so on its face.

SCORING

Win/loss buckets come from the standing doctrine, not from the sign of the P&L:
a stop is a failed trade whatever it paid, and a "lock" exit is a win only at or
above +1R. `scorecard.LOCK_WIN_R` is the single source of that threshold, so the
page and the pre-registered criterion can never drift apart.
"""
import json
import sys
from collections import OrderedDict
from datetime import datetime, timezone

from . import config, scorecard

OUTPUT_PATH = config.ROOT / "data" / "dashboard.json"
RECENT_TRADES = 30


def _parse(ts):
    """ISO timestamp -> aware datetime (naive input is read as UTC)."""
    dt = datetime.fromisoformat(ts)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _closes(snap):
    """{symbol: 1H close} for the coins this snapshot actually priced."""
    out = {}
    for coin in snap.get("symbols", []):
        tf = coin.get("timeframes", {}).get("1H", {})
        if tf.get("status") == "ok" and tf.get("last_close"):
            out[coin["symbol"]] = float(tf["last_close"])
    return out


def _is_win(trade):
    """Doctrine bucket: tp always, lock only at or above LOCK_WIN_R, stop never."""
    reason = trade.get("reason")
    if reason == "tp":
        return True
    if reason == "lock":
        return (trade.get("r_multiple") or 0.0) >= scorecard.LOCK_WIN_R
    return False


def latest_marks(history):
    """Last known close per coin across the whole archive, carried forward."""
    marks = {}
    for _, snap in history:
        marks.update(_closes(snap))
    return marks


# --- the curve ------------------------------------------------------------

def equity_curve(history, led, start_equity=None, watchlist=None):
    """[{t, equity, benchmark, invested, positions}, ...], one point per snapshot.

    `benchmark` is an equal-weight buy-and-hold of the coins priced in the FIRST
    snapshot — the same construction the scorecard uses, so the two agree. A coin
    that only appears later is excluded rather than credited with a partial-window
    return it never had to earn.
    """
    start_equity = start_equity if start_equity is not None else scorecard.START_EQUITY
    watchlist = watchlist if watchlist is not None else config.WATCHLIST
    if not history:
        return []

    closed = [dict(t, _in=_parse(t["entry_time"]), _out=_parse(t["exit_time"]))
              for t in led.get("closed", []) if t.get("entry_time") and t.get("exit_time")]
    opens = [dict(p, _in=_parse(p["entry_time"]))
             for p in led.get("open", []) if p.get("entry_time")]

    opening = {s: p for s, p in _closes(history[0][1]).items() if s in watchlist}
    marks, curve = {}, []
    for when, snap in history:
        marks.update(_closes(snap))          # carry the last known close forward

        realised = sum(t.get("pnl", 0.0) for t in closed if t["_out"] <= when)

        unrealised = invested = 0.0
        held = 0
        for t in closed:                     # a trade that was open at this hour
            if t["_in"] <= when < t["_out"] and marks.get(t["symbol"]):
                unrealised += t["qty"] * (marks[t["symbol"]] - t["entry_price"])
                invested += t["qty"] * marks[t["symbol"]]
                held += 1
        for p in opens:                      # a trade still open now
            if p["_in"] <= when and marks.get(p["symbol"]):
                unrealised += (p["qty"] * (marks[p["symbol"]] - p["entry_price"])
                               - float(p.get("entry_fee") or 0.0))
                invested += p["qty"] * marks[p["symbol"]]
                held += 1

        moves = [marks[s] / opening[s] - 1 for s in opening if marks.get(s)]
        benchmark = start_equity * (1 + sum(moves) / len(moves)) if moves else start_equity

        curve.append({
            "t": when.isoformat(),
            "equity": round(start_equity + realised + unrealised, 2),
            "benchmark": round(benchmark, 2),
            "invested": round(invested, 2),
            "positions": held,
        })
    return curve


def daily_pnl(led):
    """Realised P&L bucketed by the UTC day the trade closed, ascending."""
    days = OrderedDict()
    for t in sorted(led.get("closed", []), key=lambda x: x.get("exit_time") or ""):
        if not t.get("exit_time"):
            continue
        key = _parse(t["exit_time"]).date().isoformat()
        row = days.setdefault(key, {"date": key, "net": 0.0, "trades": 0})
        row["net"] = round(row["net"] + t.get("pnl", 0.0), 2)
        row["trades"] += 1
    return list(days.values())


# --- per-asset ------------------------------------------------------------

def per_asset(led, marks):
    """One row per coin the account has ever traded or still holds.

    Ordered by total contribution (realised + unrealised), so the page opens on
    what actually moved the account rather than on alphabetical order.
    """
    rows = OrderedDict()

    def row(sym):
        return rows.setdefault(sym, {
            "symbol": sym, "trades": 0, "tp": 0, "locks": 0, "stops": 0, "other": 0,
            "wins": 0, "net": 0.0, "gross": 0.0, "fees": 0.0, "fees_estimated": 0,
            "r_sum": 0.0, "r_count": 0, "best": None, "worst": None,
            # cost_basis is every rupiah this coin has ever had at risk, so the
            # percent-only view has an honest denominator of its own rather than
            # borrowing account equity and calling a small coin a small result.
            "cost_basis": 0.0,
            "open": False, "qty": None, "entry_price": None, "price": None,
            "unrealised": None,
        })

    for t in led.get("closed", []):
        r = row(t["symbol"])
        r["trades"] += 1
        reason = t.get("reason")
        r["tp" if reason == "tp" else "locks" if reason == "lock"
          else "stops" if reason == "stop" else "other"] += 1
        r["wins"] += 1 if _is_win(t) else 0
        pnl = t.get("pnl", 0.0)
        r["net"] = round(r["net"] + pnl, 2)
        r["gross"] = round(r["gross"] + t.get("pnl_gross", pnl), 2)
        r["fees"] = round(r["fees"] + t.get("fees", 0.0), 2)
        r["fees_estimated"] += 1 if t.get("fees_estimated") else 0
        if t.get("r_multiple") is not None:
            r["r_sum"] += t["r_multiple"]
            r["r_count"] += 1
        r["best"] = pnl if r["best"] is None else max(r["best"], pnl)
        r["worst"] = pnl if r["worst"] is None else min(r["worst"], pnl)
        if t.get("qty") and t.get("entry_price"):
            r["cost_basis"] = round(r["cost_basis"] + t["qty"] * t["entry_price"], 2)

    for p in led.get("open", []):
        r = row(p["symbol"])
        price = marks.get(p["symbol"])
        r.update({"open": True, "qty": p["qty"], "entry_price": p["entry_price"],
                  "price": price})
        r["cost_basis"] = round(r["cost_basis"] + p["qty"] * p["entry_price"], 2)
        if price:
            r["unrealised"] = round(p["qty"] * (price - p["entry_price"])
                                    - float(p.get("entry_fee") or 0.0), 2)

    out = []
    for r in rows.values():
        n = r["trades"]
        r["true_win_pct"] = round(r["wins"] / n * 100, 1) if n else None
        r["stop_pct"] = round(r["stops"] / n * 100, 1) if n else None
        r["avg_r"] = round(r["r_sum"] / r["r_count"], 2) if r["r_count"] else None
        r["fee_drag_pct"] = round(r["fees"] / r["gross"] * 100, 1) if r["gross"] > 0 else None
        r["contribution"] = round(r["net"] + (r["unrealised"] or 0.0), 2)
        r["contribution_pct"] = (round(r["contribution"] / r["cost_basis"] * 100, 2)
                                 if r["cost_basis"] else None)
        for gone in ("r_sum", "r_count"):
            r.pop(gone)
        out.append(r)
    out.sort(key=lambda r: -r["contribution"])
    return out


def open_positions(led, marks, now=None):
    """Live view of what is held, marked at the latest close.

    A coin with no mark reports `None` P&L rather than being valued at its entry
    price — an invented break-even is worse than an admitted gap.
    """
    now = now or datetime.now(timezone.utc)
    out = []
    for p in led.get("open", []):
        price = marks.get(p["symbol"])
        entry, qty = p["entry_price"], p["qty"]
        cost = qty * entry
        initial_stop = p.get("initial_stop")
        one_r = (entry - initial_stop) if initial_stop else None
        pnl = (qty * (price - entry) - float(p.get("entry_fee") or 0.0)) if price else None
        out.append({
            "symbol": p["symbol"], "qty": qty, "entry_price": entry, "price": price,
            "cost": round(cost, 2),
            "value": round(qty * price, 2) if price else None,
            "unrealised": round(pnl, 2) if pnl is not None else None,
            "unrealised_pct": round(qty * (price - entry) / cost * 100, 2)
                              if price and cost else None,
            "stop": p.get("stop"), "initial_stop": initial_stop,
            "stop_distance_pct": round((price - p["stop"]) / price * 100, 4)
                                 if price and p.get("stop") else None,
            "r_so_far": round((price - entry) / one_r, 2) if price and one_r else None,
            "peak_r": round((p["high_water"] - entry) / one_r, 2)
                      if one_r and p.get("high_water") else None,
            "hours_held": round((now - _parse(p["entry_time"])).total_seconds() / 3600, 1),
            "entry_time": p["entry_time"],
            "half_size": bool(p.get("half_size")),
            "adopted": bool(p.get("adopted")),
        })
    out.sort(key=lambda p: -(p["value"] or 0))
    return out


def recent_trades(led, limit=RECENT_TRADES):
    """Most recent closed trades first, only the fields the page renders."""
    closed = sorted((t for t in led.get("closed", []) if t.get("exit_time")),
                    key=lambda t: t["exit_time"], reverse=True)
    return [{
        "symbol": t["symbol"], "reason": t.get("reason"),
        "win": _is_win(t),
        "entry_time": t.get("entry_time"), "exit_time": t["exit_time"],
        "entry_price": t.get("entry_price"), "exit_price": t.get("exit_price"),
        "qty": t.get("qty"),
        "cost": round(t["qty"] * t["entry_price"], 2)
                if t.get("qty") and t.get("entry_price") else None,
        "pnl": t.get("pnl"), "pnl_gross": t.get("pnl_gross"), "fees": t.get("fees"),
        "fees_estimated": bool(t.get("fees_estimated")),
        "pnl_pct": round((t["exit_price"] / t["entry_price"] - 1) * 100, 2)
                   if t.get("entry_price") and t.get("exit_price") else None,
        "r_multiple": t.get("r_multiple"), "peak_r": t.get("peak_r"),
        "hours_held": round((_parse(t["exit_time"]) - _parse(t["entry_time"])).total_seconds()
                            / 3600, 1) if t.get("entry_time") else None,
    } for t in closed[:limit]]


# --- the whole document ---------------------------------------------------

def build(history, led, live_equity=None, start_equity=None, now=None, watchlist=None):
    """Everything the page renders, in one JSON-serialisable dict."""
    now = now or datetime.now(timezone.utc)
    start_equity = start_equity if start_equity is not None else scorecard.START_EQUITY
    watchlist = watchlist if watchlist is not None else config.WATCHLIST

    marks = latest_marks(history)
    curve = equity_curve(history, led, start_equity=start_equity, watchlist=watchlist)
    assets = per_asset(led, marks)
    positions = open_positions(led, marks, now=now)

    closed = led.get("closed", [])
    realised = round(sum(t.get("pnl", 0.0) for t in closed), 2)
    gross = round(sum(t.get("pnl_gross", t.get("pnl", 0.0)) for t in closed), 2)
    fees = round(sum(t.get("fees", 0.0) for t in closed), 2)
    unrealised = round(sum(p["unrealised"] or 0.0 for p in positions), 2)

    reconstructed = curve[-1]["equity"] if curve else round(start_equity + realised + unrealised, 2)
    benchmark_equity = curve[-1]["benchmark"] if curve else None
    benchmark_pct = (round((benchmark_equity / start_equity - 1) * 100, 2)
                     if benchmark_equity else None)
    equity = live_equity if live_equity is not None else reconstructed

    peak = ddown = 0.0
    for point in curve:                       # max drawdown off the reconstructed curve
        peak = max(peak, point["equity"])
        if peak:
            ddown = min(ddown, (point["equity"] / peak - 1) * 100)

    card = scorecard.metrics(led, equity=live_equity, benchmark_pct=benchmark_pct, now=now)

    return {
        "meta": {
            "generated_at": now.isoformat(timespec="seconds"),
            "start_date": scorecard.START_DATE,
            "start_equity": start_equity,
            "watchlist": list(watchlist),
            "trading_enabled": config.TRADING_ENABLED,
            "snapshots": len(history),
            "decision_date": scorecard.DECISION_DATE,
            "min_trades": scorecard.MIN_TRADES,
            "lock_win_r": scorecard.LOCK_WIN_R,
            "currency": config.QUOTE,
        },
        "totals": {
            "equity": round(equity, 2),
            "reconstructed_equity": reconstructed,
            "live_equity": round(live_equity, 2) if live_equity is not None else None,
            "drift": round(live_equity - reconstructed, 2) if live_equity is not None else None,
            "return_pct": round((equity / start_equity - 1) * 100, 2),
            "benchmark_equity": benchmark_equity,
            "benchmark_pct": benchmark_pct,
            "realised_net": realised,
            "realised_gross": gross,
            "fees": fees,
            "fee_drag_pct": round(fees / gross * 100, 1) if gross > 0 else None,
            "fees_estimated_trades": sum(1 for t in closed if t.get("fees_estimated")),
            "unrealised": unrealised,
            "invested": round(sum(p["value"] or 0.0 for p in positions), 2),
            "open_positions": len(positions),
            "max_positions": config.MAX_POSITIONS,
            "trades": len(closed),
            "wins": sum(1 for t in closed if _is_win(t)),
            "stops": sum(1 for t in closed if t.get("reason") == "stop"),
            "true_win_pct": card["true_win_pct"],
            "tp_only_win_pct": card["tp_only_win_pct"],
            "stop_pct": card["stop_pct"],
            "max_drawdown_pct": round(ddown, 2),
            "days_live": card["days_live"],
        },
        "scorecard": card,
        "curve": curve,
        "daily": daily_pnl(led),
        "assets": assets,
        "positions": positions,
        "trades": recent_trades(led),
    }


def _live_equity(marks):
    """Live account equity, or None when Indodax cannot be reached.

    Never fatal: the page is more useful with a reconstructed curve and a missing
    checkpoint than not published at all.
    """
    try:
        from . import broker
        return broker.get_account(price_by_symbol=marks)["equity"]
    except Exception as e:                    # noqa: BLE001 - any failure is non-fatal
        print(f"live equity unavailable ({e}) — publishing reconstruction only", flush=True)
        return None


def _main(argv=None):
    from . import ledger, replay
    argv = argv or sys.argv
    out = OUTPUT_PATH if len(argv) < 2 else __import__("pathlib").Path(argv[1])

    history = replay.load_history()
    led = ledger.load()
    doc = build(history, led, live_equity=_live_equity(latest_marks(history)))

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=1))
    t = doc["totals"]
    print(f"{doc['meta']['generated_at']} wrote {out} — equity {config.fmt_idr(t['equity'])} "
          f"({t['return_pct']:+.2f}%), {t['trades']} closed, {t['open_positions']} open, "
          f"drift {config.fmt_idr(t['drift']) if t['drift'] is not None else 'n/a'}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
