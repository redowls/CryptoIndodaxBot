"""The 1-minute archive.

Indodax serves ~7 days of 1m history and then it is gone, so the archive is the
only reason minute-resolution questions can be asked about last month. These
mostly pin the properties that make an unattended recorder trustworthy: re-runs
must not corrupt, a gap must self-heal, and one bad coin must not stop the rest.
"""
import json
from datetime import datetime, timedelta, timezone

from cryptoindodax import bars1m

DAY = datetime(2026, 9, 20, tzinfo=timezone.utc)


def _bars(n, start=DAY, price=100.0):
    return [{"t": (start + timedelta(minutes=i)).isoformat(), "o": price, "h": price + 1,
             "l": price - 1, "c": price, "v": 1.0} for i in range(n)]


def test_a_day_of_bars_survives_the_round_trip(tmp_path):
    bars1m.archive(["LINK"], now=DAY + timedelta(hours=2), root=tmp_path,
                   fetch=lambda *a, **k: _bars(60))
    assert len(bars1m.load_day(DAY, "LINK", tmp_path)) == 60


def test_rerunning_the_archive_changes_nothing(tmp_path):
    """Cron overlap is the normal case, not the exception."""
    for _ in range(3):
        bars1m.archive(["LINK"], now=DAY + timedelta(hours=2), root=tmp_path,
                       fetch=lambda *a, **k: _bars(60))
    assert len(bars1m.load_day(DAY, "LINK", tmp_path)) == 60


def test_a_later_run_folds_in_the_minutes_the_earlier_one_missed(tmp_path):
    bars1m.archive(["LINK"], now=DAY, root=tmp_path, fetch=lambda *a, **k: _bars(30))
    bars1m.archive(["LINK"], now=DAY, root=tmp_path, fetch=lambda *a, **k: _bars(90))
    assert len(bars1m.load_day(DAY, "LINK", tmp_path)) == 90


def test_merge_keeps_one_bar_per_minute_and_prefers_the_fresh_one():
    stale = [{"t": "2026-09-20T00:00:00+00:00", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}]
    fresh = [{"t": "2026-09-20T00:00:00+00:00", "o": 2, "h": 2, "l": 2, "c": 2, "v": 2}]
    merged = bars1m.merge(stale, fresh)
    assert len(merged) == 1 and merged[0]["c"] == 2


def test_a_range_spanning_midnight_comes_back_in_order(tmp_path):
    bars1m.archive(["LINK"], now=DAY + timedelta(days=1, hours=1), root=tmp_path,
                   fetch=lambda *a, **k: _bars(2880))          # two whole days
    got = bars1m.load_range("LINK", DAY + timedelta(hours=23),
                            DAY + timedelta(hours=25), tmp_path)
    assert len(got) == 120
    assert [b["t"] for b in got] == sorted(b["t"] for b in got)


def test_one_dead_symbol_does_not_stop_the_others(tmp_path):
    def flaky(pair, tf, **k):
        if pair.startswith("LINK"):
            raise RuntimeError("feed down")
        return _bars(10)
    bars1m.archive(["LINK", "DOT"], now=DAY, root=tmp_path, fetch=flaky)
    assert bars1m.load_day(DAY, "LINK", tmp_path) == []
    assert len(bars1m.load_day(DAY, "DOT", tmp_path)) == 10


def test_folders_past_the_retention_window_are_pruned(tmp_path):
    bars1m.archive(["LINK"], now=DAY, root=tmp_path, fetch=lambda *a, **k: _bars(10))
    gone = bars1m.prune(now=DAY + timedelta(days=90), root=tmp_path, keep_days=45)
    assert gone == ["2026-09-20"]
    assert bars1m.load_day(DAY, "LINK", tmp_path) == []


def test_a_missing_archive_reads_as_empty_not_an_error(tmp_path):
    assert bars1m.load_day(DAY, "NOPE", tmp_path) == []
    assert bars1m.prune(now=DAY, root=tmp_path / "absent") == []


def test_bars_are_stored_as_arrays_because_repeated_keys_are_most_of_the_file(tmp_path):
    bars1m.archive(["LINK"], now=DAY, root=tmp_path, fetch=lambda *a, **k: _bars(5))
    raw = json.loads(bars1m.path_for(DAY, "LINK", tmp_path).read_text())
    assert isinstance(raw[0], list) and len(raw[0]) == 6
