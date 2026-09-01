"""Per-pair trading constraints.

Alpaca accepted any fractional qty, so CryptoAutoBot never needed this. Indodax
rejects an order that breaches the pair's minimum notional or carries more
decimal places than the pair allows, so sizing has to be snapped to the pair's
own rules before an order is sent.

Metadata is cached to data/pairs.json; a stale cache is preferable to failing a
cycle, so a refresh error falls back to whatever is on disk, then to defaults.
"""
import json
import math
import time

from . import config, data

CACHE_MAX_AGE_S = 24 * 3600

# Field-name trap: on /api/pairs it is `price_round` that gives the decimals
# allowed on the COIN quantity (8 for the majors, 6 for SHIB) — the value that
# `trade_min_traded_currency` is quoted to. `volume_precision` is 0 on every
# pair; it describes the IDR side, which has no sub-unit. Rounding a quantity
# by volume_precision would floor 0.0087 BTC to 0.
DEFAULTS = {"trade_min_base_currency": config.MIN_ORDER_IDR,
            "trade_min_traded_currency": 0.0,
            "price_round": 8,
            "trade_fee_percent_taker": config.TAKER_FEE_PCT * 100}

_cache = None


def _read_cache():
    try:
        blob = json.loads(config.PAIRS_CACHE.read_text())
        return blob.get("fetched_at", 0), blob.get("pairs", {})
    except (OSError, ValueError, AttributeError):
        return 0, {}


def _write_cache(pairs):
    try:
        config.PAIRS_CACHE.parent.mkdir(parents=True, exist_ok=True)
        config.PAIRS_CACHE.write_text(
            json.dumps({"fetched_at": int(time.time()), "pairs": pairs}, indent=2))
    except OSError:
        pass


def load(force=False, now=None):
    """Return {ticker_id: metadata}, refreshing the cache when it ages out."""
    global _cache
    now = now or time.time()
    if _cache is not None and not force:
        return _cache
    fetched_at, cached = _read_cache()
    if cached and not force and now - fetched_at < CACHE_MAX_AGE_S:
        _cache = cached
        return _cache
    try:
        fresh = data.fetch_pairs()
        if fresh:
            _write_cache(fresh)
            _cache = fresh
            return _cache
    except data.FetchError:
        pass
    _cache = cached  # stale beats empty
    return _cache


def meta(symbol):
    """Constraints for one coin symbol ("BTC"), falling back to DEFAULTS."""
    entry = (load() or {}).get(config.pair_id(symbol)) or {}
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        if entry.get(k) is not None:
            out[k] = entry[k]
    return out


def min_order_idr(symbol):
    try:
        return max(float(meta(symbol)["trade_min_base_currency"]), 0.0)
    except (TypeError, ValueError):
        return float(config.MIN_ORDER_IDR)


def round_qty(symbol, qty):
    """Snap a coin quantity DOWN to the pair's allowed precision (`price_round`).

    Rounding down matters: rounding up can exceed the free balance on a sell and
    get the order rejected for insufficient funds.
    """
    try:
        precision = int(meta(symbol)["price_round"])
    except (TypeError, ValueError):
        precision = 8
    precision = max(0, min(precision, 8))
    factor = 10 ** precision
    return math.floor(float(qty) * factor) / factor


def meets_minimums(symbol, qty, price):
    """(ok, reason) — does this order clear the pair's notional/qty floors?"""
    notional = float(qty) * float(price)
    floor_idr = min_order_idr(symbol)
    if notional < floor_idr:
        return False, (f"notional {config.fmt_idr(notional)} below pair minimum "
                       f"{config.fmt_idr(floor_idr)}")
    try:
        min_coin = float(meta(symbol)["trade_min_traded_currency"] or 0.0)
    except (TypeError, ValueError):
        min_coin = 0.0
    if min_coin and float(qty) < min_coin:
        return False, f"qty {qty:.8f} below pair minimum {min_coin:.8f} {symbol}"
    return True, "ok"


def taker_fee_pct(symbol):
    """Taker fee as a fraction (Indodax quotes it in percent, e.g. 0.2 -> 0.002)."""
    try:
        return float(meta(symbol)["trade_fee_percent_taker"]) / 100.0
    except (TypeError, ValueError):
        return config.TAKER_FEE_PCT
