"""The hourly holdings message.

These are readability tests as much as correctness ones: the message exists to
be read at a glance on a phone, and a number that is right but ragged (or a
stop that is right but not the one that will actually fire) defeats the point.
"""
import pytest

from cryptoindodax import config, holdings

NOW = __import__("datetime").datetime(2026, 9, 24, 5, 14, tzinfo=__import__("datetime").timezone.utc)


def _pos(symbol="DOT", entry=20_347.0, initial=18_812.0, stop=18_812.0, lock=None):
    pos = {"symbol": symbol, "qty": 10.0, "entry_price": entry,
           "initial_stop": initial, "stop": stop, "high_water": entry,
           "entry_time": "2026-09-23T02:00:00+00:00"}
    if lock is not None:
        pos["lock"] = lock
    return pos


# --- levels ---------------------------------------------------------------

def test_take_profit_sits_at_tp_r_times_the_entry_risk():
    lv = holdings.levels(_pos(entry=100.0, initial=90.0))
    assert lv["tp"] == pytest.approx(100.0 + config.TP_R * 10.0)


def test_the_floor_shown_is_the_profit_lock_once_it_is_above_the_trail():
    """The lock is what fires. Showing the trail would overstate the room left."""
    lv = holdings.levels(_pos(entry=100.0, initial=90.0, stop=92.0, lock=105.0))
    assert (lv["floor"], lv["floor_kind"]) == (105.0, "profit lock")


def test_the_floor_shown_is_the_trail_while_it_is_still_the_higher_of_the_two():
    lv = holdings.levels(_pos(entry=100.0, initial=90.0, stop=98.0, lock=95.0))
    assert (lv["floor"], lv["floor_kind"]) == (98.0, "trail")


def test_no_recorded_entry_risk_means_no_invented_take_profit():
    pos = _pos()
    pos["initial_stop"] = None
    assert holdings.levels(pos)["tp"] is None


# --- the message ----------------------------------------------------------

def test_flat_account_says_so_instead_of_printing_an_empty_block():
    text = holdings.render([], {}, cash=847_863.0, now=NOW)
    assert "No open positions" in text and "Rp847.863" in text


def test_gain_is_measured_from_the_entry_price():
    text = holdings.render([_pos(entry=100.0, initial=90.0)], {"DOT": 112.0}, now=NOW)
    assert "+12,00%" in text


def test_every_price_for_one_coin_prints_at_the_same_scale():
    """The MOG case: 0,00218 over 0,002225 over 0,00258452 lines up for nobody."""
    pos = _pos("MOG", entry=0.002225, initial=0.002081, stop=0.002081)
    text = holdings.render([pos], {"MOG": 0.00218}, now=NOW)
    shown = [w.strip("()") for line in text.splitlines() for w in line.split()
             if w.startswith("Rp0,") or w.startswith("(Rp0,")]
    assert shown, text
    assert len({len(w) for w in shown}) == 1, shown


def test_the_stop_line_says_how_far_the_price_has_to_fall():
    text = holdings.render([_pos(entry=100.0, initial=90.0, stop=90.0)],
                           {"DOT": 100.0}, now=NOW)
    assert "10,00% away" in text


def test_a_coin_with_no_live_price_is_reported_not_skipped():
    text = holdings.render([_pos()], {}, now=NOW)
    assert "DOT" in text and "now  -" in text


def test_the_footer_counts_the_slots_actually_in_use():
    text = holdings.render([_pos("DOT"), _pos("LINK")], {}, now=NOW, max_positions=5)
    assert "2 of 5 slots" in text


def test_the_timestamp_is_wib_not_utc():
    text = holdings.render([], {}, now=NOW)
    assert "12:14 WIB" in text          # 05:14 UTC


# --- price sourcing -------------------------------------------------------

def test_live_ticker_beats_the_snapshot_close():
    prices = holdings.prices_for([_pos("DOT")],
                                 {config.pair_id("DOT"): {"last": 21_000.0}},
                                 fallback={"DOT": 20_000.0})
    assert prices["DOT"] == 21_000.0


def test_a_missing_ticker_falls_back_to_the_snapshot_rather_than_dropping_the_coin():
    prices = holdings.prices_for([_pos("DOT")], {}, fallback={"DOT": 20_000.0})
    assert prices["DOT"] == 20_000.0
