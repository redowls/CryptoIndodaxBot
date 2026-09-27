"""Bid/ask observations — data/spreads.jsonl.

An OHLC bar records where price traded; it never records what it would have COST
to trade. The gap between bid and ask is the whole subject of the maker-entry
experiment, and on this watchlist it is not a rounding error: BTC quotes 0,003%
while UNI quotes 1,250%. A half-spread of 0,625% is six times the 0,10% the
maker fee saves, so any estimate of that experiment made without real spreads is
an estimate of the wrong thing.

Nothing here is read by a trading decision. It records, and it is wrapped by
every caller, because a recorder that can break the snapshot is worse than no
recorder at all.
"""
import json
import sys
from datetime import datetime, timezone

from . import config

PATH = config.ROOT / "data" / "spreads.jsonl"


def observe(tickers, symbols=None, now=None):
    """One row: {"t": iso, "s": {SYMBOL: [bid, ask]}} for the coins that quoted."""
    now = now or datetime.now(timezone.utc)
    symbols = symbols if symbols is not None else list(config.WATCHLIST)
    seen = {}
    for sym in symbols:
        tick = (tickers or {}).get(config.pair_id(sym))
        if tick and tick.get("buy") and tick.get("sell"):
            seen[sym] = [float(tick["buy"]), float(tick["sell"])]
    return {"t": now.isoformat(timespec="seconds"), "s": seen}


def append(row, path=None):
    path = path or PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(row, separators=(",", ":")) + "\n")


def load(path=None, start=None, end=None):
    """Rows oldest first, optionally bounded. A malformed line is skipped."""
    path = path or PATH
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            row = json.loads(line)
            when = datetime.fromisoformat(row["t"])
        except (ValueError, KeyError, TypeError):
            continue
        if (start and when < start) or (end and when >= end):
            continue
        row["_when"] = when
        out.append(row)
    out.sort(key=lambda r: r["_when"])
    return out


def at(rows, symbol, when, max_age_s=5400):
    """(bid, ask) from the observation nearest `when`, or None if none is close.

    Nearest rather than latest-before: an entry at :14 is better served by the
    :07 reading seven minutes earlier than by one from the previous hour.
    """
    best = None
    for row in rows:
        if symbol not in row.get("s", {}):
            continue
        gap = abs((row["_when"] - when).total_seconds())
        if gap <= max_age_s and (best is None or gap < best[0]):
            best = (gap, row["s"][symbol])
    return tuple(best[1]) if best else None


def record(tickers, symbols=None, now=None, path=None):
    """Observe and append in one call. Never raises — callers are on live paths."""
    try:
        row = observe(tickers, symbols=symbols, now=now)
        if row["s"]:
            append(row, path)
        return row
    except Exception:                          # noqa: BLE001
        return None


def _main(argv=None):
    from . import data
    row = record(data.fetch_tickers())
    if not row:
        print("no observation recorded")
        return 1
    print(f"{row['t']} recorded {len(row['s'])} quotes")
    for sym, (bid, ask) in sorted(row["s"].items()):
        print(f"  {sym:9} bid {config.fmt_price(bid):>14} ask {config.fmt_price(ask):>14}"
              f"   spread {(ask / bid - 1) * 100:6.3f}%")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
