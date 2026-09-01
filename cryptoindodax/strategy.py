"""Deterministic trading rules distilled from memory/insights.md.

Pure functions over snapshot dicts (see snapshot.py output shape). The trader
supplies `extras` per symbol for fields the snapshot does not store:
{"day_change_pct": float, "last_1h_close": float, "prev_1h_close": float}.
"""
from . import config

SEVERITY = {"risk_on": 0, "neutral": 1, "risk_off": 2}


def _tf(coin, key):
    tf = coin.get("timeframes", {}).get(key, {})
    return tf if tf.get("status") == "ok" else None


def stack(tf):
    """EMA stack direction for one timeframe dict: UP / DOWN / MIXED."""
    e8, e20, e55 = tf.get("ema8"), tf.get("ema20"), tf.get("ema55")
    if None in (e8, e20, e55):
        return "MIXED"
    if e8 > e20 > e55:
        return "UP"
    if e8 < e20 < e55:
        return "DOWN"
    return "MIXED"


def regime(snap):
    """BTC regime gate over the 1D timeframe (insights 06-18, 06-20, 07-10)."""
    btc = next((s for s in snap.get("symbols", []) if s.get("symbol") == "BTC"), None)
    if not btc:
        return "neutral"
    d1 = _tf(btc, "1D")
    if not d1 or d1.get("adx14") is None:
        return "neutral"
    st, adx = stack(d1), d1["adx14"]
    if st == "DOWN" and adx >= 25:
        return "risk_off"
    if st == "UP" and adx >= 20:
        return "risk_on"
    return "neutral"


def effective_regime(computed, policy_hint):
    """Policy hint may only make the regime MORE conservative, never less."""
    if policy_hint not in SEVERITY:
        return computed
    return computed if SEVERITY[computed] >= SEVERITY[policy_hint] else policy_hint


def evaluate_entry(coin, extras, reg):
    """Return (ok, reason). Applies the insight filters in order; the first
    failing filter is the reported reason (rejected-entry logging)."""
    h1 = _tf(coin, "1H")
    if not h1:
        return False, "no 1H data"
    adx, rsi = h1.get("adx14"), h1.get("rsi14")
    if adx is None or rsi is None:
        return False, "indicators not warm"
    adx_min = config.ENTRY_ADX_MIN if reg == "risk_on" else config.ENTRY_ADX_MIN_CAUTIOUS
    if adx < adx_min:
        return False, f"ADX {adx:.1f} < {adx_min:.0f}"
    if stack(h1) != "UP":
        return False, f"1H stack {stack(h1)}"
    h4 = _tf(coin, "4H")
    if h4 and stack(h4) == "DOWN":
        return False, "4H stack DOWN"
    if rsi > config.BLOWOFF_RSI:
        return False, f"blow-off RSI {rsi:.1f}"
    if not (config.ENTRY_RSI_MIN <= rsi <= config.ENTRY_RSI_MAX):
        return False, f"RSI {rsi:.1f} outside [{config.ENTRY_RSI_MIN:.0f},{config.ENTRY_RSI_MAX:.0f}]"
    day_pct = extras.get("day_change_pct")
    if day_pct is None:
        return False, "no day-change data"
    if day_pct > config.LATE_ENTRY_DAY_PCT:
        return False, f"late entry +{day_pct:.1f}% on day"
    if reg == "risk_off":
        if day_pct <= 0:
            return False, "risk_off: not green on day"
        if rsi > 65:
            return False, f"risk_off: RSI {rsi:.1f} > 65"
    last, prev = extras.get("last_1h_close"), extras.get("prev_1h_close")
    if last is None or prev is None:
        return False, "no 1H close history"
    if last <= prev:
        return False, "1H close not green"
    return True, "ok"


def entry_candidates(snap, extras_by_sym, open_syms, reg, blocked=()):
    """Rank passing coins by 1H ADX desc. Returns (candidates, rejections):
    candidates = [(symbol, coin_dict)], rejections = [(symbol, reason)]."""
    candidates, rejections = [], []
    for coin in snap.get("symbols", []):
        sym = coin.get("symbol")
        if sym == "BTC" and reg == "risk_off":
            # BTC in confirmed downtrend is the regime, not a long candidate
            rejections.append((sym, "risk_off regime driver"))
            continue
        if sym in open_syms:
            rejections.append((sym, "already open"))
            continue
        if sym in blocked:
            rejections.append((sym, "blocked by policy"))
            continue
        ok, reason = evaluate_entry(coin, extras_by_sym.get(sym, {}), reg)
        if ok:
            candidates.append((sym, coin))
        else:
            rejections.append((sym, reason))
    candidates.sort(key=lambda c: _tf(c[1], "1H")["adx14"], reverse=True)
    return candidates, rejections


def check_exit(position, h1, reg, hours_held):
    """Evaluate one open position against the current 1H data.

    Returns (action, updated) where action is None|'stop'|'tp'|'time' and
    updated is the position dict with refreshed high_water/stop (trailing).
    """
    pos = dict(position)
    close, atr = h1["last_close"], h1.get("atr14")
    r = pos["entry_price"] - pos["initial_stop"]
    pos["high_water"] = max(pos.get("high_water", pos["entry_price"]), close)
    if atr and close - pos["entry_price"] >= r:
        mult = config.RISK_OFF_TRAIL_ATR_MULT if reg == "risk_off" else config.TRAIL_ATR_MULT
        pos["stop"] = max(pos["stop"], pos["high_water"] - mult * atr)
    if close <= pos["stop"]:
        return "stop", pos
    if close >= pos["entry_price"] + config.TP_R * r:
        return "tp", pos
    if hours_held >= config.TIME_STOP_HOURS:
        return "time", pos
    return None, pos
