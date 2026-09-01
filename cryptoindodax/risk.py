"""Position sizing and account-level risk controls.

Sizing is the same Van-Tharp-style rule CryptoAutoBot used — risk RISK_PCT of
equity against a STOP_ATR_MULT*ATR stop — with two Indodax-specific additions:
the quantity is snapped to the pair's volume precision, and an order that would
fall under the pair's minimum notional is refused rather than sent to be
rejected by the exchange.

All money amounts are IDR.
"""
from datetime import datetime, timedelta, timezone

from . import config, pairs


def position_size(equity, entry_price, atr, half=False, symbol=None):
    """Risk RISK_PCT of equity with a STOP_ATR_MULT*ATR stop.

    Returns (qty, initial_stop, risk_idr) or (0, None, 0) if unsizable.
    """
    if not atr or atr <= 0 or not entry_price or entry_price <= 0 or equity <= 0:
        return 0.0, None, 0.0
    stop_dist = config.STOP_ATR_MULT * atr
    if stop_dist >= entry_price:
        return 0.0, None, 0.0  # stop below zero — volatility too wide to size
    risk_idr = equity * config.RISK_PCT
    if half:
        risk_idr /= 2
    qty = risk_idr / stop_dist
    # never exceed the cash a single position may use (cap notional at 1/MAX_POSITIONS)
    max_notional = equity / config.MAX_POSITIONS
    if qty * entry_price > max_notional:
        qty = max_notional / entry_price
    if symbol:
        qty = pairs.round_qty(symbol, qty)
        ok, _ = pairs.meets_minimums(symbol, qty, entry_price)
        if not ok:
            return 0.0, None, 0.0
    else:
        qty = round(qty, 8)
    if qty <= 0:
        return 0.0, None, 0.0
    return qty, entry_price - stop_dist, risk_idr


def sizing_reason(equity, entry_price, atr, symbol=None):
    """Human-readable explanation for a refused size (logging only)."""
    if not atr or atr <= 0:
        return "no ATR"
    if not entry_price or entry_price <= 0:
        return "no price"
    if equity <= 0:
        return "no equity"
    if config.STOP_ATR_MULT * atr >= entry_price:
        return "stop distance exceeds price"
    if symbol:
        qty = pairs.round_qty(symbol, (equity * config.RISK_PCT) / (config.STOP_ATR_MULT * atr))
        ok, why = pairs.meets_minimums(symbol, qty, entry_price)
        if not ok:
            return why
    return "unsizable"


def circuit_breaker_tripped(closed_trades, equity, now=None):
    """True when realized losses over the trailing 24h reach CIRCUIT_BREAKER_PCT."""
    if equity <= 0:
        return True
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)
    realized = 0.0
    for t in closed_trades:
        try:
            exit_time = datetime.fromisoformat(t["exit_time"])
        except (KeyError, ValueError):
            continue
        if exit_time >= cutoff:
            realized += t.get("pnl", 0.0)
    return realized <= -config.CIRCUIT_BREAKER_PCT * equity
