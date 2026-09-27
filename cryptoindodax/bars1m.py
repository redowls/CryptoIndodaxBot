"""Archive of 1-minute bars — data/bars1m/<date>/<SYMBOL>.json.

WHY THIS EXISTS, AND WHY IT RUNS BEFORE ANY DECISION IS MADE

Indodax serves 1-minute history for roughly SEVEN DAYS and then it is gone. Any
question that needs minute resolution — did a resting limit order fill, how far
did price travel inside the hour the bot only sampled once — can only be asked
about the last week unless someone kept the bars.

So this is deliberately built first, before the experiment that needs it. It
changes no behaviour, places no order and reads nothing the bot decides on. It
is a recorder, and its only job is that the data still exists later.

Storage is compact arrays, [t, o, h, l, c, v] per bar, because dict keys
repeated 1440 times a day per coin are most of the file. About 1,2 MB a day for
the whole watchlist; the directory is gitignored.
"""
import json
import sys
from datetime import datetime, timedelta, timezone

from . import config, data

TF = "1"
ARCHIVE = config.ROOT / "data" / "bars1m"
RETENTION_DAYS = 45


def path_for(day, symbol, root=None):
    root = root or ARCHIVE
    return root / day.strftime("%Y-%m-%d") / f"{symbol}.json"


def _pack(bar):
    return [bar["t"], bar["o"], bar["h"], bar["l"], bar["c"], bar["v"]]


def _unpack(row):
    return {"t": row[0], "o": row[1], "h": row[2], "l": row[3], "c": row[4], "v": row[5]}


def load_day(day, symbol, root=None):
    """[bar, ...] for one symbol on one UTC day, oldest first. [] when absent."""
    try:
        rows = json.loads(path_for(day, symbol, root).read_text())
    except (OSError, ValueError):
        return []
    return [_unpack(r) for r in rows]


def load_range(symbol, start, end, root=None):
    """Every archived bar for `symbol` in [start, end), oldest first."""
    out, day = [], start.date()
    while day <= end.date():
        for bar in load_day(datetime.combine(day, datetime.min.time(), timezone.utc),
                            symbol, root):
            when = datetime.fromisoformat(bar["t"])
            if start <= when < end:
                out.append(bar)
        day += timedelta(days=1)
    out.sort(key=lambda b: b["t"])
    return out


def merge(existing, fresh):
    """Union by bar time, newest write winning. Re-runs are safe and idempotent."""
    by_time = {b["t"]: b for b in existing}
    by_time.update({b["t"]: b for b in fresh})
    return [by_time[k] for k in sorted(by_time)]


def archive(symbols=None, days=7, now=None, root=None, fetch=None):
    """Pull the served 1m window and fold it into the archive. Returns a summary.

    Overlapping re-runs are the normal case — the window is re-fetched whole and
    merged by bar time, so a missed day self-heals on the next run as long as it
    is still inside the exchange's seven.
    """
    now = now or datetime.now(timezone.utc)
    root = root or ARCHIVE
    fetch = fetch or data.fetch_bars
    symbols = symbols if symbols is not None else list(config.WATCHLIST)
    start = now - timedelta(days=days)
    wrote = {}
    for sym in symbols:
        try:
            bars = fetch(config.pair(sym), TF, start=start, end=now,
                         include_partial=False)
        except Exception as e:                # noqa: BLE001 - one coin must not stop the rest
            print(f"  {sym}: {e}", flush=True)
            continue
        by_day = {}
        for bar in bars:
            by_day.setdefault(datetime.fromisoformat(bar["t"]).date(), []).append(bar)
        for day, fresh in by_day.items():
            when = datetime.combine(day, datetime.min.time(), timezone.utc)
            target = path_for(when, sym, root)
            merged = merge(load_day(when, sym, root), fresh)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps([_pack(b) for b in merged], separators=(",", ":")))
            wrote[sym] = wrote.get(sym, 0) + len(fresh)
    return wrote


def prune(now=None, root=None, keep_days=RETENTION_DAYS):
    """Drop day folders older than the retention window. Returns what went."""
    now = now or datetime.now(timezone.utc)
    root = root or ARCHIVE
    cutoff = (now - timedelta(days=keep_days)).date()
    gone = []
    if not root.exists():
        return gone
    for folder in sorted(root.iterdir()):
        try:
            day = datetime.strptime(folder.name, "%Y-%m-%d").date()
        except ValueError:
            continue
        if day < cutoff:
            for f in folder.iterdir():
                f.unlink()
            folder.rmdir()
            gone.append(folder.name)
    return gone


def _main(argv=None):
    now = datetime.now(timezone.utc)
    print(f"{now.isoformat(timespec='seconds')} archiving 1m bars", flush=True)
    wrote = archive(now=now)
    total = sum(wrote.values())
    print(f"  {len(wrote)} symbols, {total} bars folded in", flush=True)
    for name in prune(now=now):
        print(f"  pruned {name}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
