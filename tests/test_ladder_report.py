"""The daily ladder guard and monitor.

The guard is the point: a prompt telling a model not to loosen the rungs is a
wish, a diff against registered values is a check.
"""
from datetime import datetime, timedelta, timezone

from cryptoindodax import config, ladder_report as lr

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


def _t(sym="MOG", reason="lock", r=0.22, peak_r=1.9, pnl=1100.0,
       entry=100.0, peak=115.6, hours_ago=2):
    return {"symbol": sym, "reason": reason, "r_multiple": r, "peak_r": peak_r,
            "pnl": pnl, "entry_price": entry, "peak_price": peak,
            "exit_time": (NOW - timedelta(hours=hours_ago)).isoformat()}


# --- the guard ------------------------------------------------------------

def test_no_drift_when_config_matches_what_was_registered():
    assert lr.drift() == [], "shipped config must match REGISTERED"


def test_drift_names_the_knob_that_moved(monkeypatch):
    monkeypatch.setattr(config, "PROFIT_LOCK_PCT_RUNGS", ((3.0, 1.0),))
    d = dict((n, (e, a)) for n, e, a in lr.drift())
    assert "PROFIT_LOCK_PCT_RUNGS" in d
    assert d["PROFIT_LOCK_PCT_RUNGS"][1] == ((3.0, 1.0),)


def test_drift_catches_reopening_the_break_even_floor(monkeypatch):
    """It was rejected for capping a +3.69R winner; turning it back on must be
    impossible to do quietly."""
    monkeypatch.setattr(config, "BREAKEVEN_AT_R", 1.0)
    assert any(n == "BREAKEVEN_AT_R" for n, _, _ in lr.drift())


def test_drift_catches_a_loosened_take_profit(monkeypatch):
    monkeypatch.setattr(config, "TP_R", 2.0)
    assert any(n == "TP_R" for n, _, _ in lr.drift())


def test_a_list_from_json_still_compares_equal(monkeypatch):
    """config could be loaded from JSON one day; lists must not read as drift."""
    monkeypatch.setattr(config, "PROFIT_LOCK_PCT_RUNGS",
                        [[5.0, 2.5], [10.0, 6.5], [15.0, 11.0], [20.0, 16.0]])
    assert not any(n == "PROFIT_LOCK_PCT_RUNGS" for n, _, _ in lr.drift())


def test_render_shouts_about_drift(monkeypatch):
    monkeypatch.setattr(config, "TRAIL_ATR_MULT", 2.0)
    text = lr.render(lr.day({"closed": [], "open": []}, now=NOW))
    assert "HAS MOVED" in text and "TRAIL_ATR_MULT" in text


def test_render_says_so_when_nothing_moved():
    assert "geometry unchanged" in lr.render(lr.day({"closed": [], "open": []}, now=NOW))


# --- rungs and classification --------------------------------------------

def test_armed_rung_reports_the_highest_one_reached():
    assert lr.armed_rung(100.0, 104.0) is None
    assert lr.armed_rung(100.0, 106.0) == (5.0, 2.5)
    assert lr.armed_rung(100.0, 115.6) == (15.0, 11.0)


def test_classify_separates_a_converted_loss_from_a_real_win():
    """Both are 'lock' exits; only one is a win under the standing doctrine."""
    assert lr.classify(_t(r=0.22)) == "converted"
    assert lr.classify(_t(r=1.7)) == "locked-win"


def test_classify_flags_the_dead_zone():
    """Peak between +1.00R and +1.33R: the trail was armed but under water."""
    assert lr.classify(_t(reason="stop", r=-0.39, peak_r=1.04)) == "dead-zone"
    assert lr.classify(_t(reason="stop", r=-1.05, peak_r=0.65)) == "stop"


# --- the window -----------------------------------------------------------

def test_day_only_counts_the_last_24h():
    led = {"open": [], "closed": [_t(hours_ago=2), _t(sym="OLD", hours_ago=30)]}
    d = lr.day(led, now=NOW)
    assert [t["symbol"] for t in d["closed"]] == ["MOG"]


def test_day_counts_conversions_and_dead_zones():
    led = {"open": [], "closed": [
        _t(sym="MOG", r=0.22), _t(sym="XRP", r=1.7),
        _t(sym="FARTCOIN", reason="stop", r=-0.39, peak_r=1.04)]}
    d = lr.day(led, now=NOW)
    assert len(d["converted"]) == 1 and len(d["locked_wins"]) == 1
    assert len(d["dead_zone"]) == 1


def test_render_handles_an_empty_day_and_open_positions():
    led = {"closed": [], "open": [{"symbol": "DOT", "entry_price": 20347.0,
                                   "high_water": 21786.0, "lock": 20856.0}]}
    text = lr.render(lr.day(led, now=NOW), marks={"DOT": 20008.0})
    assert "DOT" in text and "closed in the window: 0" in text
