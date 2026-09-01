"""Indodax public market data.

Indodax serves OHLC through its TradingView bridge, which differs from Alpaca's
crypto bars in three ways that matter:

  1. The window is expressed as from/to unix seconds, not a page cursor. One
     request returns the whole range, so there is no pagination to follow (the
     bug that silently staled every CryptoAutoBot 4H snapshot cannot recur here).
  2. Bars come back as {"Time","Open","High","Low","Close","Volume"} with Time in
     unix seconds and Volume as a *string*.
  3. A request with no User-Agent is answered with 403 by their edge.

`fetch_bars` normalises the payload to Alpaca's {"t","o","h","l","c","v"} shape
so indicators/snapshot/digest/strategy port over untouched.
"""
import time
from datetime import datetime, timezone

import requests

from . import config, net


class FetchError(Exception):
    pass


if config.FORCE_IPV4:
    net.force_ipv4()


def _to_epoch(value):
    """Accept unix seconds, a datetime, or an ISO-8601 string."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, datetime):
        return int(value.timestamp())
    return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())


def normalize_bar(raw):
    """Indodax bar -> Alpaca-shaped bar. Volume may be a string or absent."""
    return {
        "t": datetime.fromtimestamp(int(raw["Time"]), timezone.utc).isoformat(),
        "o": float(raw["Open"]),
        "h": float(raw["High"]),
        "l": float(raw["Low"]),
        "c": float(raw["Close"]),
        "v": float(raw.get("Volume") or 0.0),
    }


def fetch_bars(pair: str, timeframe: str, start=None, end=None, session=None):
    """Fetch OHLCV bars for one symbol/timeframe. Returns list (possibly empty).

    `pair` is the chart symbol ("BTCIDR"); `timeframe` is an Indodax tf code
    ("60", "240", "1D"). `start`/`end` accept unix seconds, datetimes or ISO
    strings; `end` defaults to now.
    """
    to_ts = _to_epoch(end) or int(time.time())
    from_ts = _to_epoch(start)
    if from_ts is None:
        from_ts = to_ts - 86400 * 10
    params = {"from": from_ts, "to": to_ts, "tf": timeframe, "symbol": pair}
    getter = (session or requests).get
    try:
        r = getter(config.BARS_URL, params=params,
                   headers={"User-Agent": config.USER_AGENT}, timeout=20)
        r.raise_for_status()
        payload = r.json()
    except Exception as e:  # network, HTTP, JSON
        raise FetchError(f"{pair} {timeframe}: {e}") from e
    # An unknown symbol yields null or an error object rather than a list.
    if not isinstance(payload, list):
        raise FetchError(f"{pair} {timeframe}: unexpected payload {payload!r:.120}")
    out = []
    for raw in payload:
        try:
            out.append(normalize_bar(raw))
        except (KeyError, TypeError, ValueError):
            continue  # skip malformed bar rather than lose the whole series
    return out


def fetch_pairs(session=None):
    """Pair metadata: order minimums and volume precision, keyed by ticker_id."""
    getter = (session or requests).get
    try:
        r = getter(config.PAIRS_URL, headers={"User-Agent": config.USER_AGENT}, timeout=20)
        r.raise_for_status()
        payload = r.json()
    except Exception as e:
        raise FetchError(f"pairs: {e}") from e
    return {p["ticker_id"]: p for p in payload if isinstance(p, dict) and "ticker_id" in p}
