import pytest
from cryptoindodax import config, strategy


def _tf(ema8=110, ema20=105, ema55=100, rsi=55, adx=30, atr=2.0, close=112):
    return {"status": "ok", "ema8": ema8, "ema20": ema20, "ema55": ema55,
            "rsi14": rsi, "adx14": adx, "atr14": atr, "last_close": close}


def _coin(sym="SOL", h1=None, h4=None, d1=None):
    return {"symbol": sym, "status": "ok", "timeframes": {
        "1H": h1 or _tf(), "4H": h4 or _tf(), "1D": d1 or _tf()}}


def _extras(day=1.0, last=112, prev=110):
    return {"day_change_pct": day, "last_1h_close": last, "prev_1h_close": prev}


def _snap(*coins):
    return {"symbols": list(coins)}


# --- stack ---

def test_stack_up_down_mixed():
    assert strategy.stack(_tf(3, 2, 1)) == "UP"
    assert strategy.stack(_tf(1, 2, 3)) == "DOWN"
    assert strategy.stack(_tf(2, 3, 1)) == "MIXED"
    assert strategy.stack({"ema8": 1, "ema20": 2, "ema55": None}) == "MIXED"


# --- regime ---

def test_regime_risk_on():
    btc = _coin("BTC", d1=_tf(3, 2, 1, adx=25))
    assert strategy.regime(_snap(btc)) == "risk_on"


def test_regime_risk_off():
    btc = _coin("BTC", d1=_tf(1, 2, 3, adx=30))
    assert strategy.regime(_snap(btc)) == "risk_off"


def test_regime_neutral_low_adx_or_missing_btc():
    btc = _coin("BTC", d1=_tf(3, 2, 1, adx=15))
    assert strategy.regime(_snap(btc)) == "neutral"
    assert strategy.regime(_snap(_coin("ETH"))) == "neutral"


def test_effective_regime_only_tightens():
    assert strategy.effective_regime("risk_on", "neutral") == "neutral"
    assert strategy.effective_regime("risk_off", "risk_on") == "risk_off"
    assert strategy.effective_regime("neutral", "auto") == "neutral"


# --- entry filters, one per insight ---

def test_entry_passes_clean_setup():
    ok, reason = strategy.evaluate_entry(_coin(), _extras(), "risk_on")
    assert ok, reason


def test_entry_rejects_atr_below_the_fee_drag_floor():
    """BTC-shaped setup: perfect trend, ATR too small to pay the round trip."""
    coin = _coin(h1=_tf(atr=0.5, close=112))          # ATR 0.45% < 0.75% floor
    ok, reason = strategy.evaluate_entry(coin, _extras(), "risk_on")
    assert not ok
    assert "fee drag" in reason and "ATR" in reason


def test_entry_allows_atr_above_the_floor():
    coin = _coin(h1=_tf(atr=1.0, close=112))          # ATR 0.89% > 0.75% floor
    assert strategy.evaluate_entry(coin, _extras(), "risk_on")[0]


def test_atr_floor_is_derived_from_the_fee_drag_cap():
    """MIN_ATR_PCT is not a magic number — it is the ATR at which a round trip
    costs exactly MAX_FEE_DRAG_R of 1R, so changing the fee or the cap moves it."""
    implied = (config.OBSERVED_ROUND_TRIP_PCT
               / (config.STOP_ATR_MULT * config.MIN_ATR_PCT))
    assert abs(implied - config.MAX_FEE_DRAG_R) < 1e-9


def test_atr_floor_is_reported_before_adx():
    """A coin failing both should name the economic reason, not a downstream one."""
    coin = _coin(h1=_tf(atr=0.5, close=112, adx=5))
    assert "fee drag" in strategy.evaluate_entry(coin, _extras(), "risk_on")[1]


def test_entry_rejects_low_adx():
    coin = _coin(h1=_tf(adx=18))
    ok, reason = strategy.evaluate_entry(coin, _extras(), "risk_on")
    assert not ok and "ADX" in reason


def test_entry_needs_higher_adx_when_not_risk_on():
    coin = _coin(h1=_tf(adx=22))
    assert strategy.evaluate_entry(coin, _extras(), "risk_on")[0]
    assert not strategy.evaluate_entry(coin, _extras(), "neutral")[0]


def test_entry_rejects_bad_stack():
    coin = _coin(h1=_tf(ema8=100, ema20=105, ema55=110))
    ok, reason = strategy.evaluate_entry(coin, _extras(), "risk_on")
    assert not ok and "stack" in reason


def test_entry_rejects_4h_downtrend():
    coin = _coin(h4=_tf(ema8=100, ema20=105, ema55=110))
    ok, reason = strategy.evaluate_entry(coin, _extras(), "risk_on")
    assert not ok and "4H" in reason


def test_entry_rejects_rsi_out_of_band_and_blowoff():
    assert not strategy.evaluate_entry(_coin(h1=_tf(rsi=75)), _extras(), "risk_on")[0]
    assert not strategy.evaluate_entry(_coin(h1=_tf(rsi=40)), _extras(), "risk_on")[0]
    ok, reason = strategy.evaluate_entry(_coin(h1=_tf(rsi=85)), _extras(), "risk_on")
    assert not ok and "blow-off" in reason


def test_entry_rejects_late_entry():
    ok, reason = strategy.evaluate_entry(_coin(), _extras(day=6.2), "risk_on")
    assert not ok and "late" in reason


def test_entry_rejects_red_1h_close():
    ok, reason = strategy.evaluate_entry(_coin(), _extras(last=100, prev=101), "risk_on")
    assert not ok and "green" in reason


def test_entry_risk_off_requires_green_outlier():
    coin = _coin(h1=_tf(adx=35, rsi=55))
    assert not strategy.evaluate_entry(coin, _extras(day=-0.5), "risk_off")[0]
    assert strategy.evaluate_entry(coin, _extras(day=1.5), "risk_off")[0]
    coin_hot = _coin(h1=_tf(adx=35, rsi=68))
    assert not strategy.evaluate_entry(coin_hot, _extras(day=1.5), "risk_off")[0]


def test_entry_rejects_missing_data():
    coin = {"symbol": "SOL", "timeframes": {"1H": {"status": "error"}}}
    ok, reason = strategy.evaluate_entry(coin, _extras(), "risk_on")
    assert not ok and "1H" in reason
    ok, reason = strategy.evaluate_entry(_coin(), {}, "risk_on")
    assert not ok


# --- candidate ranking ---

def test_candidates_ranked_by_adx_and_filtered():
    a = _coin("SOL", h1=_tf(adx=28))
    b = _coin("LINK", h1=_tf(adx=40))
    c = _coin("DOGE", h1=_tf(adx=10))
    extras = {s: _extras() for s in ("SOL", "LINK", "DOGE", "LTC")}
    cands, rejects = strategy.entry_candidates(
        _snap(a, b, c, _coin("LTC")), extras, open_syms={"LTC"}, reg="risk_on")
    syms = [s for s, _ in cands]
    assert syms[0] == "LINK" and "SOL" in syms and "LTC" not in syms
    reasons = dict(rejects)
    assert reasons["LTC"] == "already open"
    assert "ADX" in reasons["DOGE"]


def test_candidates_respect_policy_block_and_btc_risk_off():
    btc = _coin("BTC", h1=_tf(adx=35), d1=_tf(1, 2, 3, adx=30))
    sol = _coin("SOL", h1=_tf(adx=35))
    extras = {s: _extras() for s in ("BTC", "SOL")}
    cands, rejects = strategy.entry_candidates(
        _snap(btc, sol), extras, open_syms=set(), reg="risk_off", blocked={"SOL"})
    assert cands == []
    reasons = dict(rejects)
    assert "blocked" in reasons["SOL"] and "regime" in reasons["BTC"]


# --- exits ---

def _pos(entry=100.0, stop=94.0, hw=None):
    return {"symbol": "SOL", "qty": 1.0, "entry_price": entry,
            "initial_stop": stop, "stop": stop, "high_water": hw or entry}


def test_exit_stop_hit():
    action, pos = strategy.check_exit(_pos(), _tf(close=93.0), "risk_on", 5)
    assert action == "stop"


def test_exit_take_profit_at_2_5r():
    action, pos = strategy.check_exit(_pos(), _tf(close=115.5), "risk_on", 5)
    assert action == "tp"


def test_exit_time_stop():
    action, pos = strategy.check_exit(_pos(), _tf(close=101.0), "risk_on", 121)
    assert action == "time"


def test_exit_trail_activates_after_1r():
    # +1R = 106; close 108 with atr 0.5 → trail = 108 - TRAIL_ATR_MULT*0.5 > 94
    action, pos = strategy.check_exit(_pos(), _tf(close=108.0, atr=0.5), "risk_on", 5)
    assert action is None
    assert pos["stop"] == 108.0 - config.TRAIL_ATR_MULT * 0.5
    assert pos["high_water"] == 108.0


def test_exit_trail_tighter_in_risk_off():
    action, pos = strategy.check_exit(_pos(), _tf(close=108.0, atr=0.5), "risk_off", 5)
    assert pos["stop"] == 108.0 - config.RISK_OFF_TRAIL_ATR_MULT * 0.5


def test_trail_is_not_tighter_than_1r():
    """The inverse of what this test asserted until 2026-09-15.

    It used to require TRAIL < STOP on the theory that a wider trail can never
    bind. It cannot — but TP_R is what exits a winner here, and a trail tighter
    than 1R (TRAIL_ATR_MULT < STOP_ATR_MULT) exits on any 1R-sized pullback,
    amputating moves on their way to +2.5R. Replaying 335 snapshots put trail
    2.0 at +0.41% and everything from 3.0 up at +2.67%, so the floor is 1R."""
    assert config.TRAIL_ATR_MULT >= config.STOP_ATR_MULT
    assert config.RISK_OFF_TRAIL_ATR_MULT <= config.TRAIL_ATR_MULT


def test_exit_trail_never_lowers_stop():
    p = _pos(hw=110.0)
    p["stop"] = 107.0
    action, pos = strategy.check_exit(p, _tf(close=108.0, atr=2.0), "risk_on", 5)
    assert pos["stop"] == 107.0


# --- profit-lock ladder ---

def test_lock_rungs_are_on_and_activate_at_or_above_1_5r():
    """The refuted 2026-09-07 geometry (2 ATR from +1R) must not be reachable
    as a rung, and a lock under +1R can only ever deliver a SCRATCH."""
    assert config.PROFIT_LOCK_RUNGS
    assert min(a for a, _ in config.PROFIT_LOCK_RUNGS) >= 1.5
    assert all(t > 0 for _, t in config.PROFIT_LOCK_RUNGS)


def test_profit_lock_trail_picks_the_tightest_rung_reached():
    rungs = ((1.5, 2.0), (2.0, 1.0))
    assert strategy.profit_lock_trail(8.0, 6.0, rungs) is None      # +1.33R: nothing armed
    assert strategy.profit_lock_trail(9.0, 6.0, rungs) == 2.0       # +1.5R
    assert strategy.profit_lock_trail(12.0, 6.0, rungs) == 1.0      # +2.0R
    assert strategy.profit_lock_trail(12.0, 0.0, rungs) is None     # broken geometry


def test_lock_is_inactive_below_the_first_rung(monkeypatch):
    monkeypatch.setattr(config, "PROFIT_LOCK_RUNGS", ((1.5, 1.0),))
    monkeypatch.setattr(config, "PROFIT_LOCK_PCT_RUNGS", ())   # isolate the ATR ladder
    action, pos = strategy.check_exit(_pos(), _tf(close=108.9, atr=1.0), "risk_on", 5)
    assert action is None and "lock" not in pos


def test_lock_arms_on_the_peak_and_only_ratchets_up(monkeypatch):
    monkeypatch.setattr(config, "PROFIT_LOCK_RUNGS", ((1.5, 1.0),))
    _, p = strategy.check_exit(_pos(), _tf(close=109.0, atr=1.0), "risk_on", 5)   # +1.5R arms
    assert p["lock"] == 108.0
    action, p = strategy.check_exit(p, _tf(close=108.5, atr=2.0), "risk_on", 6)   # pullback, wider ATR
    assert action is None and p["lock"] == 108.0                                   # never lowered
    _, p = strategy.check_exit(p, _tf(close=111.0, atr=1.0), "risk_on", 7)        # new high
    assert p["lock"] == 110.0


def test_lock_exit_is_booked_as_lock(monkeypatch):
    monkeypatch.setattr(config, "PROFIT_LOCK_RUNGS", ((1.5, 1.0),))
    _, p = strategy.check_exit(_pos(), _tf(close=109.0, atr=1.0), "risk_on", 5)
    action, _ = strategy.check_exit(p, _tf(close=107.9, atr=1.0), "risk_on", 6)
    assert action == "lock"


def test_a_bar_through_both_levels_is_a_stop_not_a_lock(monkeypatch):
    monkeypatch.setattr(config, "PROFIT_LOCK_RUNGS", ((1.5, 1.0),))
    _, p = strategy.check_exit(_pos(), _tf(close=109.0, atr=1.0), "risk_on", 5)
    assert p["stop"] == 109.0 - config.TRAIL_ATR_MULT * 1.0
    action, _ = strategy.check_exit(p, _tf(close=104.0, atr=1.0), "risk_on", 6)
    assert action == "stop"


def test_lock_never_fires_on_a_new_high(monkeypatch):
    monkeypatch.setattr(config, "PROFIT_LOCK_RUNGS", ((1.5, 1.0),))
    _, p = strategy.check_exit(_pos(), _tf(close=109.0, atr=1.0), "risk_on", 5)
    action, _ = strategy.check_exit(p, _tf(close=112.0, atr=1.0), "risk_on", 6)
    assert action is None


def test_tp_is_untouched_by_the_ladder(monkeypatch):
    monkeypatch.setattr(config, "PROFIT_LOCK_RUNGS", ((1.5, 1.0),))
    action, _ = strategy.check_exit(_pos(), _tf(close=115.5, atr=1.0), "risk_on", 5)
    assert action == "tp"


def test_ladder_off_is_the_old_engine(monkeypatch):
    monkeypatch.setattr(config, "PROFIT_LOCK_RUNGS", ())
    monkeypatch.setattr(config, "PROFIT_LOCK_PCT_RUNGS", ())
    monkeypatch.setattr(config, "BREAKEVEN_AT_R", None)
    _, p = strategy.check_exit(_pos(), _tf(close=109.0, atr=1.0), "risk_on", 5)
    action, p = strategy.check_exit(p, _tf(close=107.0, atr=1.0), "risk_on", 6)
    assert action is None and "lock" not in p


UNI_2026_09_17 = [  # (1H close, ATR14) from the stored snapshots, 03:07 .. 23:07 UTC
    (119527, 3098), (119371, 2957), (119610, 2762), (119610, 2565), (119000, 2462),
    (119335, 2445), (123424, 2542), (122150, 2456), (119781, 2450), (121875, 2290),
    (122000, 2137), (127999, 2511), (127537, 2530), (126937, 2628), (136054, 3170),
    (135888, 3098), (132553, 3041), (132852, 2824), (135000, 3013), (136000, 2798),
    (125674, 3309),
]


def _walk_uni():
    """The live UNI position of 2026-09-17: filled 121.172, stop 111.447 (1R = 9.725),
    policy regime risk_off, through the real check_exit bar by bar."""
    pos = {"symbol": "UNI", "qty": 0.37178557, "entry_price": 121172.0,
           "initial_stop": 111447.0, "stop": 111447.0, "high_water": 121172.0}
    for i, (close, atr) in enumerate(UNI_2026_09_17):
        action, pos = strategy.check_exit(pos, _tf(close=close, atr=atr), "risk_off", i + 1)
        if action:
            return i, action, close, pos
    return None, None, None, pos


def test_uni_2026_09_17_without_the_ladder_left_on_the_trail_after_the_dip(monkeypatch):
    """What happened live: +1.53R peak (+12.24%), one -7.6% bar at 23:07, out
    on the 3xATR risk_off trail at +0.49R — Rp1.485 net on a position that
    had been up Rp5.5k. TP_R sat at +20%, never in play."""
    monkeypatch.setattr(config, "PROFIT_LOCK_RUNGS", ())
    i, action, close, _ = _walk_uni()
    assert (i, action, close) == (20, "stop", 125674)


def test_uni_2026_09_17_with_the_ladder_locks_before_the_dip():
    """Same bars through the shipped rungs: the 17:07 peak arms the lock, the
    19:07 close under it exits at +1.17R (+9.4%), four hours before the bar
    that took the trail. This is the trade the ladder was built for — and
    replay --ladder shows the price of it on other trades; read both."""
    i, action, close, _ = _walk_uni()
    assert (i, action, close) == (16, "lock", 132553)
    assert (close - 121172.0) / (121172.0 - 111447.0) > 1.0


# --- break-even floor (config.BREAKEVEN_AT_R) -----------------------------

def test_the_dead_zone_exists_without_the_floor():
    """The defect the floor is for: the trail ARMS at +1R but sits
    TRAIL/STOP = 1.33R below the peak, so a position peaking between +1.00R and
    +1.33R has an armed stop BELOW its own entry. FARTCOIN died here on
    2026-09-23: peak +1.04R, exit -0.39R."""
    assert config.TRAIL_ATR_MULT / config.STOP_ATR_MULT > 1.0
    assert config.PROFIT_LOCK_RUNGS[0][0] > config.TRAIL_ATR_MULT / config.STOP_ATR_MULT, \
        "the first lock rung sits above the dead zone, so it cannot close it"


def test_floor_stays_off_because_it_amputates_winners():
    """Tried at 1.0R on 2026-09-24 and rejected on the trade-level diff: it
    turned LINK's +3.69R take-profit (+Rp9.478) into a +0.16R stop (-Rp189),
    paying 3.53R to save at most 0.33R twice. The aggregate looked fine, which
    is exactly why the per-trade view is the one that decides."""
    assert config.BREAKEVEN_AT_R is None


def test_floor_lifts_the_stop_to_entry_once_the_peak_clears_1r(monkeypatch):
    monkeypatch.setattr(config, "BREAKEVEN_AT_R", 1.0)
    monkeypatch.setattr(config, "BREAKEVEN_INCLUDES_FEES", False)
    # entry 100, stop 94, 1R = 6. close 106 = exactly +1R.
    _, pos = strategy.check_exit(_pos(), _tf(close=106.0, atr=2.0), "risk_on", 5)
    assert pos["stop"] == 100.0, "the trail alone would have left it at 98"


def test_floor_can_include_the_round_trip(monkeypatch):
    monkeypatch.setattr(config, "BREAKEVEN_AT_R", 1.0)
    monkeypatch.setattr(config, "BREAKEVEN_INCLUDES_FEES", True)
    _, pos = strategy.check_exit(_pos(), _tf(close=106.0, atr=2.0), "risk_on", 5)
    assert pos["stop"] == 100.0 * (1 + config.OBSERVED_ROUND_TRIP_PCT / 100.0)


def test_floor_does_not_arm_below_the_threshold(monkeypatch):
    monkeypatch.setattr(config, "BREAKEVEN_AT_R", 1.0)
    _, pos = strategy.check_exit(_pos(), _tf(close=105.0, atr=2.0), "risk_on", 5)   # +0.83R
    assert pos["stop"] == 94.0


def test_floor_never_lowers_a_stop(monkeypatch):
    monkeypatch.setattr(config, "BREAKEVEN_AT_R", 1.0)
    p = _pos(hw=115.0)
    p["stop"] = 112.0                       # trail already well above break-even
    _, pos = strategy.check_exit(p, _tf(close=113.0, atr=0.5), "risk_on", 5)
    assert pos["stop"] >= 112.0


def test_floor_turns_the_fartcoin_loss_into_a_scratch(monkeypatch):
    """Regression for the real trade. Entry 3462, initial stop 3158.665
    (1R = 303.34 = 8.76% of price), peak 3779 = +1.04R, ATR ~107.

    Without the floor the trail settles 1.42R under the peak, which is BELOW
    the entry, and the position exited at -0.39R (-Rp3.516) after being up
    double digits. With the floor the stop LEVEL cannot go under the entry once
    +1R has printed — note the FILL can still be lower, since a level is not a
    guaranteed price, which is why the harness gain is about Rp2.700 and not
    the full Rp3.516."""
    base = {"symbol": "FARTCOIN", "qty": 25.42, "entry_price": 3462.0,
            "initial_stop": 3158.665, "stop": 3158.665, "high_water": 3462.0}
    bars = [(3779.0, 107.0), (3600.0, 107.0), (3450.0, 107.0), (3345.0, 107.0)]

    def run(floor):
        monkeypatch.setattr(config, "BREAKEVEN_AT_R", floor)
        monkeypatch.setattr(config, "BREAKEVEN_INCLUDES_FEES", True)
        monkeypatch.setattr(config, "PROFIT_LOCK_PCT_RUNGS", ())   # isolate the floor
        pos = dict(base)
        for i, (c, a) in enumerate(bars):
            action, pos = strategy.check_exit(pos, _tf(close=c, atr=a), "neutral", i + 1)
            if action:
                return action, c, pos
        return None, None, pos

    act, px, pos = run(None)
    assert act == "stop"
    assert pos["stop"] < base["entry_price"], "the armed trail is under water"
    assert px < base["entry_price"]

    act2, px2, pos2 = run(1.0)
    assert act2 == "stop"
    assert pos2["stop"] >= base["entry_price"], "the floor holds the stop at or above entry"
    assert px2 > px, "and it gets out earlier, before the deeper bar"


# --- percent profit ladder (config.PROFIT_LOCK_PCT_RUNGS) -----------------

def test_pct_rungs_are_configured_and_only_ever_ratchet_up():
    rungs = config.PROFIT_LOCK_PCT_RUNGS
    assert rungs, "the percent ladder is live; () would disable it"
    peaks = [p for p, _ in rungs]
    stops = [s for _, s in rungs]
    assert peaks == sorted(peaks) and stops == sorted(stops)
    for peak, stop in rungs:
        assert 0 < stop < peak, "a rung must lock in profit, below its own trigger"


def test_pct_level_is_none_below_the_first_rung():
    assert strategy.profit_lock_pct_level(1000.0, 1049.0) is None      # +4.9%


def test_pct_level_holds_the_highest_rung_reached():
    assert strategy.profit_lock_pct_level(1000.0, 1050.0) == 1025.0    # +5%  -> +2.5%
    assert strategy.profit_lock_pct_level(1000.0, 1099.0) == 1025.0    # still the first
    assert strategy.profit_lock_pct_level(1000.0, 1100.0) == pytest.approx(1065.0)
    assert strategy.profit_lock_pct_level(1000.0, 1500.0) == 1160.0    # past the last


def test_pct_level_survives_a_misordered_rung_set():
    """Taking the max means a typo can only ever hold a HIGHER floor."""
    assert strategy.profit_lock_pct_level(1000.0, 1200.0,
                                          rungs=((10.0, 6.5), (5.0, 2.5))) == pytest.approx(1065.0)


def test_pct_level_ignores_broken_inputs():
    assert strategy.profit_lock_pct_level(0, 1100.0) is None
    assert strategy.profit_lock_pct_level(1000.0, None) is None
    assert strategy.profit_lock_pct_level(1000.0, 1100.0, rungs=()) is None


def test_pct_ladder_sets_the_lock_and_needs_no_atr(monkeypatch):
    """It must work on an hour where the indicator went missing, because the
    ATR ladder cannot and that is when a position is most exposed."""
    monkeypatch.setattr(config, "PROFIT_LOCK_PCT_RUNGS", ((5.0, 2.5),))
    monkeypatch.setattr(config, "PROFIT_LOCK_RUNGS", ((1.5, 1.0),))
    p = _pos(entry=100.0, stop=94.0, hw=100.0)
    _, pos = strategy.check_exit(p, {"status": "ok", "last_close": 106.0, "atr14": None},
                                 "risk_on", 5)
    assert pos["lock"] == pytest.approx(102.5), "peak +6% arms the +2.5% rung"


def test_pct_ladder_takes_the_higher_of_the_two_ladders(monkeypatch):
    """Both ladders write one `lock` level and the HIGHER one wins, so adding
    the percent ladder can only ever tighten, never loosen."""
    monkeypatch.setattr(config, "PROFIT_LOCK_RUNGS", ((1.5, 1.0),))
    # entry 100, 1R = 6, close 109 = +1.5R -> ATR rung locks at 109 - 1*ATR = 108
    monkeypatch.setattr(config, "PROFIT_LOCK_PCT_RUNGS", ((5.0, 4.5),))   # 104.5, lower
    _, pos = strategy.check_exit(_pos(), _tf(close=109.0, atr=1.0), "risk_on", 5)
    assert pos["lock"] == pytest.approx(108.0)
    # now make the percent rung the tighter of the two
    monkeypatch.setattr(config, "PROFIT_LOCK_PCT_RUNGS", ((5.0, 8.5),))   # 108.5, higher
    _, pos = strategy.check_exit(_pos(), _tf(close=109.0, atr=1.0), "risk_on", 5)
    assert pos["lock"] == pytest.approx(108.5)


def test_pct_ladder_never_lowers_an_existing_lock(monkeypatch):
    monkeypatch.setattr(config, "PROFIT_LOCK_PCT_RUNGS", ((5.0, 2.5),))
    p = _pos(entry=100.0, stop=94.0, hw=106.0)
    p["lock"] = 105.0                       # ATR ladder already set a tighter one
    _, pos = strategy.check_exit(p, _tf(close=106.0, atr=1.0), "risk_on", 5)
    assert pos["lock"] == 105.0


def test_pct_lock_exit_is_reported_as_lock(monkeypatch):
    monkeypatch.setattr(config, "PROFIT_LOCK_PCT_RUNGS", ((5.0, 2.5),))
    p = _pos(entry=100.0, stop=94.0, hw=106.0)
    _, p = strategy.check_exit(p, {"status": "ok", "last_close": 106.0, "atr14": None},
                               "risk_on", 5)
    action, _ = strategy.check_exit(p, {"status": "ok", "last_close": 102.0, "atr14": None},
                                    "risk_on", 6)
    assert action == "lock"


def test_the_five_minute_watcher_honours_a_percent_lock(monkeypatch):
    """check_levels reads the same `lock` field, so the fast exit watcher gets
    the percent ladder without knowing it exists."""
    monkeypatch.setattr(config, "PROFIT_LOCK_PCT_RUNGS", ((5.0, 2.5),))
    p = _pos(entry=100.0, stop=94.0, hw=106.0)
    _, p = strategy.check_exit(p, {"status": "ok", "last_close": 106.0, "atr14": None},
                               "risk_on", 5)
    assert strategy.check_levels(p, 102.4) == "lock"
    assert strategy.check_levels(p, 102.6) is None


def test_mog_and_fartcoin_regression():
    """The two real trades the ladder was measured on. MOG peaked +15.6% and
    exited at -1.76R (-Rp14.109); FARTCOIN peaked +9.2% and exited at -0.49R.
    Both are high-ATR memes where 1R is ~8-9% of price, which is exactly why a
    +5% rung reaches them and never reaches BTC."""
    # MOG: peak +15.6% clears the third rung, so the floor is entry +11%
    assert strategy.profit_lock_pct_level(100.0, 115.6) == pytest.approx(111.0)
    # FARTCOIN: peak +9.2% clears only the first, floor is entry +2.5%
    assert strategy.profit_lock_pct_level(3462.0, 3779.0) == pytest.approx(3462.0 * 1.025)
    # BTC on the same day peaked +2.38% — the ladder never arms on a major
    assert strategy.profit_lock_pct_level(1_513_998_000.0, 1_550_000_000.0) is None


# --- the peak, read off a real traded high --------------------------------

def test_the_peak_ratchets_off_the_bar_high_when_one_is_supplied():
    """LINK 2026-09-24: the 18:00 hour reached +5,40% and the 18:07 reading said
    +0,96%, so the ladder's +5% rung never armed on a peak that really happened."""
    pos = {"symbol": "LINK", "qty": 1.0, "entry_price": 100.0, "initial_stop": 94.0,
           "stop": 94.0, "high_water": 100.0}
    h1 = {"last_close": 101.0, "high_1h": 106.0, "atr14": 2.0}
    _, updated = strategy.check_exit(pos, h1, "risk_on", 1.0)
    assert updated["high_water"] == 106.0
    assert updated["lock"] == pytest.approx(102.5)      # the +5% rung's +2,5% stop


def test_a_snapshot_without_the_high_field_replays_exactly_as_before():
    """Every stored snapshot predates the field; none may change behaviour."""
    pos = {"symbol": "LINK", "qty": 1.0, "entry_price": 100.0, "initial_stop": 94.0,
           "stop": 94.0, "high_water": 100.0}
    _, updated = strategy.check_exit(dict(pos), {"last_close": 101.0, "atr14": 2.0},
                                     "risk_on", 1.0)
    assert updated["high_water"] == 101.0
    assert updated.get("lock") is None


def test_a_bar_high_below_the_close_cannot_lower_the_peak():
    pos = {"symbol": "LINK", "qty": 1.0, "entry_price": 100.0, "initial_stop": 94.0,
           "stop": 94.0, "high_water": 108.0}
    _, updated = strategy.check_exit(pos, {"last_close": 103.0, "high_1h": 99.0,
                                           "atr14": 2.0}, "risk_on", 1.0)
    assert updated["high_water"] == 108.0


def test_the_bar_high_peak_can_be_switched_off_and_the_old_behaviour_returns(monkeypatch):
    """It is registered exit geometry, so it must be revertible without a code edit."""
    monkeypatch.setattr(config, "PEAK_FROM_BAR_HIGH", False)
    pos = {"symbol": "LINK", "qty": 1.0, "entry_price": 100.0, "initial_stop": 94.0,
           "stop": 94.0, "high_water": 100.0}
    h1 = {"last_close": 101.0, "high_1h": 106.0, "atr14": 2.0}
    _, updated = strategy.check_exit(pos, h1, "risk_on", 1.0)
    assert updated["high_water"] == 101.0
