"""Bid/ask observations.

The maker-entry experiment turns entirely on the spread: BTC quotes 0,003% and
UNI 1,250%, so a half-spread can be six times the 0,10% a maker fee saves. These
pin the recorder, and above all that it cannot throw on a live path.
"""
from datetime import datetime, timedelta, timezone

from cryptoindodax import config, spreads

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


def _ticks(**by_symbol):
    return {config.pair_id(s): {"last": v[0], "buy": v[0], "sell": v[1]}
            for s, v in by_symbol.items()}


def test_a_quote_is_recorded_per_symbol():
    row = spreads.observe(_ticks(BTC=(100.0, 101.0)), symbols=["BTC"], now=NOW)
    assert row["s"] == {"BTC": [100.0, 101.0]}


def test_a_symbol_that_did_not_quote_is_left_out_not_zeroed():
    row = spreads.observe({}, symbols=["BTC"], now=NOW)
    assert row["s"] == {}


def test_observations_survive_the_round_trip_and_come_back_in_order(tmp_path):
    path = tmp_path / "s.jsonl"
    for i in (2, 0, 1):
        spreads.append(spreads.observe(_ticks(BTC=(100.0 + i, 101.0 + i)),
                                       symbols=["BTC"], now=NOW + timedelta(minutes=i)),
                       path)
    rows = spreads.load(path)
    assert [r["s"]["BTC"][0] for r in rows] == [100.0, 101.0, 102.0]


def test_loading_can_be_bounded_to_a_window(tmp_path):
    path = tmp_path / "s.jsonl"
    for i in range(4):
        spreads.append(spreads.observe(_ticks(BTC=(100.0, 101.0)), symbols=["BTC"],
                                       now=NOW + timedelta(hours=i)), path)
    assert len(spreads.load(path, start=NOW + timedelta(hours=1),
                            end=NOW + timedelta(hours=3))) == 2


def test_the_nearest_observation_wins_not_the_latest_before(tmp_path):
    """An entry at :14 is better served by the :07 reading than by last hour's."""
    path = tmp_path / "s.jsonl"
    spreads.append(spreads.observe(_ticks(BTC=(90.0, 91.0)), symbols=["BTC"],
                                   now=NOW - timedelta(minutes=53)), path)
    spreads.append(spreads.observe(_ticks(BTC=(100.0, 101.0)), symbols=["BTC"],
                                   now=NOW + timedelta(minutes=7)), path)
    assert spreads.at(spreads.load(path), "BTC", NOW) == (100.0, 101.0)


def test_a_quote_too_old_to_mean_anything_is_refused(tmp_path):
    path = tmp_path / "s.jsonl"
    spreads.append(spreads.observe(_ticks(BTC=(90.0, 91.0)), symbols=["BTC"],
                                   now=NOW - timedelta(days=2)), path)
    assert spreads.at(spreads.load(path), "BTC", NOW) is None


def test_a_corrupt_line_is_skipped_rather_than_losing_the_file(tmp_path):
    path = tmp_path / "s.jsonl"
    spreads.append(spreads.observe(_ticks(BTC=(100.0, 101.0)), symbols=["BTC"], now=NOW), path)
    with path.open("a") as fh:
        fh.write("{not json\n")
    assert len(spreads.load(path)) == 1


def test_record_never_raises_on_a_live_path(tmp_path):
    """It is called from the snapshot and from the entry path. It may lose an
    observation; it may not take anything else down with it."""
    assert spreads.record(None, symbols=["BTC"], path=tmp_path / "s.jsonl") is not None
    assert spreads.record({"x": "not a dict"}, symbols=["BTC"],
                          path=tmp_path / "s.jsonl") is not None
