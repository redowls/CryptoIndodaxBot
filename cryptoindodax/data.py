"""Indodax public market data.

Indodax serves OHLC through its TradingView bridge, which differs from Alpaca's
crypto bars in three ways that matter:

  1. The window is expressed as from/to unix seconds, not a page cursor. One
     request returns the whole range, so there is no pagination to follow (the
     bug that silently staled every CryptoAutoBot 4H snapshot cannot recur here).
  2. Bars come back as {"Time","Open","High","Low","Close","Volume"} with Time in
     unix seconds and Volume as a *string*.
  3. A request with no User-Agent is answered with 403 by their edge.
  4. A range ending "now" includes the bar for the hour still in progress, whose
     Close is just the price at the moment of the request. Reading that as "the
     last close" turns every decision into a single point sample taken minutes
     past the hour. On 2026-09-24 LINK's 18:00 bar CLOSED at +5,40% from entry
     and the bot recorded +0,96%, because it had looked at 18:07 and never
     again — so the profit ladder's +5% rung never armed and a stop that should
     have been sitting at +2,5% stayed at -6,60%. `fetch_bars` therefore drops
     the forming bar unless a caller explicitly asks for it.

`fetch_bars` normalises the payload to Alpaca's {"t","o","h","l","c","v"} shape
so indicators/snapshot/digest/strategy port over untouched.
"""
import time
from datetime import datetime, timezone

import requests

from . import config, net


class FetchError(Exception):
    pass


# Indodax tf code -> bar length in seconds. Used only to recognise the bar that
# has not finished yet; a code missing from here is left alone rather than
# guessed at, so an unknown timeframe degrades to the old behaviour.
PERIOD_SECONDS = {"60": 3600, "240": 14400, "1D": 86400}


def _bar_epoch(bar):
    """Bar time as unix seconds, or None when it cannot be read."""
    try:
        return int(datetime.fromisoformat(bar["t"]).timestamp())
    except (KeyError, TypeError, ValueError):
        return None


def drop_forming_bar(bars, timeframe, now=None):
    """Bars up to the last COMPLETED period.

    The current period's bar is not a close, it is a snapshot of a price still
    moving. Indicators computed over it are computed over a partial sample, and
    a peak read off it is whatever the price happened to be when the cron fired.
    """
    secs = PERIOD_SECONDS.get(str(timeframe))
    if not secs or not bars:
        return bars
    stamps = [_bar_epoch(b) for b in bars]
    if any(t is None for t in stamps):
        # Undateable bars cannot be judged, and this runs on the snapshot path:
        # losing a coin's whole 1H block over one odd timestamp would cost more
        # than keeping a bar that may still be forming.
        return bars
    epoch = int((now or datetime.now(timezone.utc)).timestamp())
    cutoff = epoch - (epoch % secs)
    return [b for b, t in zip(bars, stamps) if t < cutoff]


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


def fetch_bars(pair: str, timeframe: str, start=None, end=None, session=None,
               include_partial=True, now=None):
    """Fetch OHLCV bars for one symbol/timeframe. Returns list (possibly empty).

    `pair` is the chart symbol ("BTCIDR"); `timeframe` is an Indodax tf code
    ("60", "240", "1D"). `start`/`end` accept unix seconds, datetimes or ISO
    strings; `end` defaults to now.

    The forming bar IS included by default, because that is what 24 days of live
    trading was measured on and a backtest of the alternative did not support
    changing it (replay --bars arm B: 27 trades +Rp15.446 became 31 trades
    -Rp10.998 — though that arm recomputed its indicators from refetched bars,
    so it is inconclusive rather than damning). Pass `include_partial=False`, or
    call `drop_forming_bar`, where a finished period is what the question needs —
    a peak, for instance, which is what `snapshot` does.
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
    return out if include_partial else drop_forming_bar(out, timeframe, now=now)


def fetch_tickers(session=None):
    """Last/bid/ask for every pair in one call, keyed by ticker_id.

    /api/ticker_all returns all ~500 pairs in a single response, so a balance in
    any coin — watchlist or not — can be priced without a request per asset.
    Values arrive as strings; anything unparseable is skipped rather than
    poisoning a report with a NaN.
    """
    getter = (session or requests).get
    try:
        r = getter(f"{config.PUBLIC_BASE_URL}/api/ticker_all",
                   headers={"User-Agent": config.USER_AGENT}, timeout=20)
        r.raise_for_status()
        payload = r.json()
    except Exception as e:
        raise FetchError(f"tickers: {e}") from e
    out = {}
    for ticker_id, t in (payload.get("tickers") or {}).items():
        try:
            out[ticker_id] = {"last": float(t["last"]),
                              "buy": float(t["buy"]),
                              "sell": float(t["sell"])}
        except (KeyError, TypeError, ValueError):
            continue
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
