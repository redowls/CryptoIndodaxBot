from cryptoindodax import strategy


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


def test_entry_rejects_low_adx():
    coin = _coin(h1=_tf(adx=20))
    ok, reason = strategy.evaluate_entry(coin, _extras(), "risk_on")
    assert not ok and "ADX" in reason


def test_entry_needs_higher_adx_when_not_risk_on():
    coin = _coin(h1=_tf(adx=27))
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
    # +1R = 106; close 108 with atr 0.5 → trail = 108 - 6*0.5 = 105 > 94
    action, pos = strategy.check_exit(_pos(), _tf(close=108.0, atr=0.5), "risk_on", 5)
    assert action is None
    assert pos["stop"] == 108.0 - 6 * 0.5
    assert pos["high_water"] == 108.0


def test_exit_trail_tighter_in_risk_off():
    action, pos = strategy.check_exit(_pos(), _tf(close=108.0, atr=0.5), "risk_off", 5)
    assert pos["stop"] == 108.0 - 3 * 0.5


def test_exit_trail_never_lowers_stop():
    p = _pos(hw=110.0)
    p["stop"] = 107.0
    action, pos = strategy.check_exit(p, _tf(close=108.0, atr=2.0), "risk_on", 5)
    assert pos["stop"] == 107.0
