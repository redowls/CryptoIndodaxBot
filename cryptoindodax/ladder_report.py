"""Daily ladder monitor — what the profit ladders did, and whether they moved.

Two jobs, both for the 00:00 WIB routine.

1. GUARD. The exit geometry is a set of registered values. A prompt telling a
   model not to loosen them is a wish; this is a check. Anything that differs
   from REGISTERED is printed as a loud diff, so a change has to be noticed and
   explained rather than quietly carried forward. It does not block anything —
   the values live in config.py and are meant to be changeable — it just makes
   a change impossible to miss.

2. MONITOR. For the past day: every closed trade with the peak it reached, the
   rung that armed, and what the ladder actually did to it; and every open
   position with its live distance to the lock and to the take-profit.

The point of the monitor is to keep an honest count of two things that decide
whether the percent ladder earns its place:
  * how many trades it CONVERTS (a loss that became a small lock), and
  * how many winners it CAPS (a trade locked out that was still running).
Only the first was measurable when it shipped, from 2 trades of 26.
"""
import json
import sys
from datetime import datetime, timedelta, timezone

from . import config

# Registered 2026-09-24. See config.py for the evidence behind each.
REGISTERED = {
    "PROFIT_LOCK_PCT_RUNGS": ((5.0, 2.5), (10.0, 6.5), (15.0, 11.0), (20.0, 16.0)),
    "PROFIT_LOCK_RUNGS": ((1.5, 1.0),),
    "BREAKEVEN_AT_R": None,
    "TP_R": 2.5,
    "STOP_ATR_MULT": 3.0,
    "TRAIL_ATR_MULT": 4.0,
}

DEAD_ZONE = (1.0, config.TRAIL_ATR_MULT / config.STOP_ATR_MULT)


def drift():
    """[(name, registered, current)] for every knob that has moved."""
    out = []
    for name, expected in REGISTERED.items():
        actual = getattr(config, name, None)
        if isinstance(expected, tuple) and isinstance(actual, (list, tuple)):
            actual = tuple(tuple(x) if isinstance(x, (list, tuple)) else x for x in actual)
        if actual != expected:
            out.append((name, expected, actual))
    return out


def armed_rung(entry, peak, rungs=None):
    """(peak %, armed stop %) for the highest percent rung the peak reached."""
    rungs = config.PROFIT_LOCK_PCT_RUNGS if rungs is None else rungs
    if not entry or not peak or not rungs:
        return None
    peak_pct = (peak / entry - 1) * 100.0
    reached = [(p, s) for p, s in rungs if peak_pct >= p]
    return max(reached, key=lambda x: x[1]) if reached else None


def classify(trade):
    """What the ladder did to one closed trade, in one word."""
    r, peak_r = trade.get("r_multiple"), trade.get("peak_r")
    if trade.get("reason") == "lock":
        if r is None:
            return "locked"
        return "locked-win" if r >= 1.0 else "converted"
    if peak_r is not None and DEAD_ZONE[0] <= peak_r < DEAD_ZONE[1]:
        return "dead-zone"
    return trade.get("reason") or "?"


def day(led=None, now=None, hours=24):
    now = now or datetime.now(timezone.utc)
    led = led if led is not None else _load()
    since = (now - timedelta(hours=hours)).isoformat()
    closed = [t for t in led.get("closed", []) if (t.get("exit_time") or "") >= since]
    return {"closed": closed, "open": led.get("open", []), "now": now,
            "drift": drift(),
            "converted": [t for t in closed if classify(t) == "converted"],
            "locked_wins": [t for t in closed if classify(t) == "locked-win"],
            "dead_zone": [t for t in closed if classify(t) == "dead-zone"]}


def render(d, marks=None):
    marks = marks or {}
    L = ["PROFIT LADDER — past 24h"]
    if d["drift"]:
        L += ["", "!! EXIT GEOMETRY HAS MOVED since it was registered 2026-09-24 !!"]
        for name, exp, act in d["drift"]:
            L.append(f"   {name}: registered {exp}  ->  now {act}")
        L.append("   Explain this in the digest. Do not carry it forward silently.")
    else:
        L.append("  geometry unchanged since registration (rungs, TP_R, stop, trail)")

    L += ["", f"  closed in the window: {len(d['closed'])}"]
    if d["closed"]:
        L.append(f"    {'coin':<9}{'why':<7}{'peak':>8}{'exit':>8}{'rung':>10}   verdict")
        for t in sorted(d["closed"], key=lambda x: x.get("exit_time") or ""):
            rung = armed_rung(t.get("entry_price"), t.get("peak_price"))
            rtxt = f"+{rung[0]:.0f}%->+{rung[1]:.1f}%" if rung else "none"
            pk = f"{t['peak_r']:+.2f}R" if t.get("peak_r") is not None else "   ?"
            ex = f"{t['r_multiple']:+.2f}R" if t.get("r_multiple") is not None else "   ?"
            L.append(f"    {t['symbol']:<9}{t.get('reason','?'):<7}{pk:>8}{ex:>8}{rtxt:>10}"
                     f"   {classify(t)}   {config.fmt_idr(t.get('pnl', 0))}")
    L += ["",
          f"  ladder converted a loss into a lock : {len(d['converted'])}",
          f"  lock exits at or above +1R          : {len(d['locked_wins'])}",
          f"  died in the dead zone (+1.00..{DEAD_ZONE[1]:.2f}R) : {len(d['dead_zone'])}"]
    if d["dead_zone"]:
        L.append("    (BREAKEVEN_AT_R is off by design — it capped a +3.69R winner."
                 " Count these; do not reopen it without a better rule.)")

    L += ["", f"  open positions: {len(d['open'])}"]
    for p in d["open"]:
        e, hw = p.get("entry_price"), p.get("high_water") or p.get("entry_price")
        px = marks.get(p["symbol"])
        rung = armed_rung(e, hw)
        lock = p.get("lock")
        bits = [f"    {p['symbol']:<9}peak +{(hw/e-1)*100:.2f}%"]
        bits.append(f"rung {'+%.0f%%->+%.1f%%' % rung if rung else 'none'}")
        bits.append(f"lock {config.fmt_price(lock) if lock else '-'}")
        if px:
            bits.append(f"live {config.fmt_price(px)} ({(px/e-1)*100:+.2f}%)")
            if lock:
                bits.append(f"{(px/lock-1)*100:+.2f}% to lock")
        L.append("  ".join(bits))
    return "\n".join(L)


def _load():
    try:
        return json.loads((config.TRADES_DIR / "trades.json").read_text())
    except (OSError, ValueError):
        return {"open": [], "closed": []}


def _main(argv=None):
    marks = {}
    try:
        from . import data
        tickers = data.fetch_tickers()
        marks = {s: tickers[config.pair_id(s)]["last"] for s in config.WATCHLIST
                 if config.pair_id(s) in tickers}
    except Exception:
        pass
    print(render(day(), marks=marks))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
