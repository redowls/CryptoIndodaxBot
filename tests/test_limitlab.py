"""Phase 1 of the maker-entry experiment.

The arithmetic these pin is the whole experiment: get the benchmark wrong by
half a spread and every bucket reports positive, including one that fills a
tenth of the time. That is exactly what the first version of `trial` did.
"""
import pytest

from cryptoindodax import limitlab


def _bars(lows, close=None):
    return [{"t": f"2026-09-27T00:{i:02d}:00+00:00", "o": 100.0, "h": 100.0,
             "l": low, "c": close if close is not None else low}
            for i, low in enumerate(lows)]


# --- the fill model -------------------------------------------------------

def test_price_must_trade_through_the_bid_not_merely_touch_it():
    """Touching the bid does not clear a queue joined at the back of it."""
    assert limitlab.fills(_bars([99.0]), 99.0, strict=True)[0] is False
    assert limitlab.fills(_bars([99.0]), 99.0, strict=False)[0] is True


def test_the_wait_is_reported_in_minutes_to_the_filling_bar():
    filled, waited = limitlab.fills(_bars([101.0, 100.5, 98.0]), 99.0)
    assert (filled, waited) == (True, 3)


# --- the arithmetic -------------------------------------------------------

def test_a_fill_banks_the_whole_spread_plus_the_fee():
    """Market pays the ask, a resting bid pays the bid: the saving is the spread."""
    r = limitlab.trial(_bars([98.0]), ask=101.0, limit_price=99.0, minutes=5,
                       half_spread=0.01)
    assert r["filled"] is True
    assert r["bps"] == pytest.approx((101.0 - 99.0) / 101.0 * 10_000 + 10, rel=1e-6)


def test_a_non_fill_is_scored_against_the_ask_it_would_pay_not_the_mid():
    """THE BUG. The fallback buys at market, so it pays the ask at T. Comparing
    an ask-inclusive benchmark against a mid-price fallback gives every non-fill
    a free half-spread and turns a losing idea into a winning table."""
    flat = _bars([100.0, 100.0], close=100.0)
    r = limitlab.trial(flat, ask=101.0, limit_price=99.0, minutes=2, half_spread=0.01)
    assert r["filled"] is False
    assert r["bps"] == pytest.approx(0.0, abs=1.0)      # price never moved -> no edge


def test_waiting_while_price_runs_away_is_a_cost():
    rising = _bars([100.0, 105.0], close=105.0)
    r = limitlab.trial(rising, ask=101.0, limit_price=99.0, minutes=2, half_spread=0.01)
    assert r["filled"] is False and r["bps"] < -300


def test_waiting_while_price_falls_without_reaching_the_bid_is_a_gain():
    falling = [{"t": "t0", "o": 100.0, "h": 100.0, "l": 99.5, "c": 99.5}]
    r = limitlab.trial(falling, ask=101.0, limit_price=99.0, minutes=1, half_spread=0.01)
    assert r["filled"] is False and r["bps"] > 0


def test_an_empty_window_scores_nothing_rather_than_guessing():
    assert limitlab.trial([], ask=101.0, limit_price=99.0, minutes=5) is None


# --- bucketing ------------------------------------------------------------

def test_spread_buckets_separate_the_coins_the_prize_lives_in():
    assert limitlab._spread_bucket(0.003) == "tight  <0.05%"
    assert limitlab._spread_bucket(0.17) == "mid  0.05-0.2%"
    assert limitlab._spread_bucket(0.50) == "wide  0.2-0.6%"
    assert limitlab._spread_bucket(1.78) == "very wide >0.6%"


def test_the_median_observed_spread_is_used_not_the_latest():
    rows = [{"s": {"BTC": [100.0, 100.5]}, "_when": 1},
            {"s": {"BTC": [100.0, 110.0]}, "_when": 2},
            {"s": {"BTC": [100.0, 101.0]}, "_when": 3}]
    assert limitlab.live_spread_pct(["BTC"], rows=rows)["BTC"] == pytest.approx(1.0)
