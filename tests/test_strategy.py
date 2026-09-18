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
