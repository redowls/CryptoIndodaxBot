from datetime import datetime, timezone

import pytest
from cryptoindodax import config, data


class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


BAR = {"Time": 1699328700, "Open": 1000, "High": 1200, "Low": 900,
       "Close": 1100, "Volume": "14814.00000000"}


def test_fetch_bars_normalizes_to_ohlcv_shape(monkeypatch):
    monkeypatch.setattr(data.requests, "get", lambda *a, **k: _FakeResp([BAR]))
    bars = data.fetch_bars("BTCIDR", "60")
    assert len(bars) == 1
    b = bars[0]
    assert (b["o"], b["h"], b["l"], b["c"]) == (1000.0, 1200.0, 900.0, 1100.0)
    assert b["v"] == 14814.0                      # Volume arrives as a string
    assert b["t"] == datetime.fromtimestamp(1699328700, timezone.utc).isoformat()


def test_fetch_bars_zero_volume_string_becomes_float(monkeypatch):
    monkeypatch.setattr(data.requests, "get",
                        lambda *a, **k: _FakeResp([{**BAR, "Volume": "0"}]))
    assert data.fetch_bars("BTCIDR", "60")[0]["v"] == 0.0


def test_fetch_bars_empty_list_is_empty(monkeypatch):
    monkeypatch.setattr(data.requests, "get", lambda *a, **k: _FakeResp([]))
    assert data.fetch_bars("UNIIDR", "60") == []


def test_fetch_bars_sends_from_to_tf_symbol(monkeypatch):
    captured = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        captured["headers"] = headers
        return _FakeResp([])

    monkeypatch.setattr(data.requests, "get", fake_get)
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    end = datetime(2026, 6, 11, tzinfo=timezone.utc)
    data.fetch_bars("BTCIDR", "240", start=start, end=end)
    p = captured["params"]
    assert p["symbol"] == "BTCIDR" and p["tf"] == "240"
    assert p["from"] == int(start.timestamp()) and p["to"] == int(end.timestamp())
    assert captured["url"] == config.BARS_URL


def test_fetch_bars_sends_user_agent(monkeypatch):
    """A request without a User-Agent is answered with 403 by Indodax's edge."""
    captured = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured["headers"] = headers or {}
        return _FakeResp([])

    monkeypatch.setattr(data.requests, "get", fake_get)
    data.fetch_bars("BTCIDR", "60")
    assert captured["headers"].get("User-Agent") == config.USER_AGENT


def test_fetch_bars_accepts_iso_and_epoch_start(monkeypatch):
    captured = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured.setdefault("froms", []).append(params["from"])
        return _FakeResp([])

    monkeypatch.setattr(data.requests, "get", fake_get)
    data.fetch_bars("BTCIDR", "60", start="2026-06-01T00:00:00Z", end=1780000000)
    data.fetch_bars("BTCIDR", "60", start=1780000000, end=1780086400)
    assert captured["froms"][0] == int(datetime(2026, 6, 1, tzinfo=timezone.utc).timestamp())
    assert captured["froms"][1] == 1780000000


def test_fetch_bars_skips_malformed_bar_without_losing_series(monkeypatch):
    monkeypatch.setattr(data.requests, "get",
                        lambda *a, **k: _FakeResp([BAR, {"Time": "x"}, BAR]))
    assert len(data.fetch_bars("BTCIDR", "60")) == 2


def test_fetch_bars_non_list_payload_raises(monkeypatch):
    """An unknown symbol yields null/an error object rather than a list."""
    monkeypatch.setattr(data.requests, "get",
                        lambda *a, **k: _FakeResp({"error": "unknown symbol"}))
    with pytest.raises(data.FetchError):
        data.fetch_bars("NOPEIDR", "60")


def test_fetch_bars_http_error_raises_fetcherror(monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("dns fail")
    monkeypatch.setattr(data.requests, "get", boom)
    with pytest.raises(data.FetchError):
        data.fetch_bars("BTCIDR", "60")


def test_fetch_pairs_keys_by_ticker_id(monkeypatch):
    payload = [{"ticker_id": "btc_idr", "trade_min_base_currency": 10000},
               {"ticker_id": "eth_idr", "trade_min_base_currency": 10000},
               {"no_ticker": True}]
    monkeypatch.setattr(data.requests, "get", lambda *a, **k: _FakeResp(payload))
    pairs = data.fetch_pairs()
    assert set(pairs) == {"btc_idr", "eth_idr"}


def test_pair_helpers_use_idr_forms():
    assert config.pair("btc") == "BTCIDR"
    assert config.pair_id("BTC") == "btc_idr"


def test_watchlist_is_the_fourteen_selected_coins():
    assert config.WATCHLIST == ["BTC", "ETH", "SOL", "XRP", "DOGE", "AVAX",
                                "LINK", "DOT", "LTC", "UNI", "PEPE", "FARTCOIN",
                                "USELESS", "MOG"]


def test_watchlist_entries_are_live_indodax_idr_pairs():
    """Every watchlist symbol must resolve to a pair id that exists in the
    cached /api/pairs metadata — a symbol Indodax does not list would pass
    every filter and then fail at order time."""
    from cryptoindodax import pairs as pairs_mod
    known = pairs_mod.load() or {}
    if not known:  # no cache in this environment; nothing to assert against
        return
    missing = [s for s in config.WATCHLIST if config.pair_id(s) not in known]
    assert not missing, f"not listed on Indodax: {missing}"


def test_watchlist_contains_btc_for_the_regime_gate():
    """strategy.regime() reads BTC's 1D to set risk_on/neutral/risk_off for
    every coin. Without BTC in the watchlist there is no BTC in the snapshot,
    regime() falls back to 'neutral', and the ADX entry bar silently rises
    from 25 to 30 across the board."""
    assert "BTC" in config.WATCHLIST


# --- the forming bar ------------------------------------------------------
#
# Reading the hour still in progress as "the last close" turned every decision
# into a point sample taken minutes past the hour. LINK's 18:00 bar on
# 2026-09-24 closed at +5,40% from entry; the bot recorded +0,96% and its
# profit-ladder rung never armed.

def _raw(hour, close, day=24):
    ts = int(datetime(2026, 9, day, hour, tzinfo=timezone.utc).timestamp())
    return {"Time": ts, "Open": close, "High": close, "Low": close,
            "Close": close, "Volume": "1"}


NOW = datetime(2026, 9, 24, 18, 7, tzinfo=timezone.utc)


def test_the_hour_still_running_is_not_a_close():
    bars = [data.normalize_bar(_raw(16, 100)), data.normalize_bar(_raw(17, 110)),
            data.normalize_bar(_raw(18, 120))]
    kept = data.drop_forming_bar(bars, "60", now=NOW)
    assert [b["c"] for b in kept] == [100, 110]


def test_a_finished_hour_survives_even_seconds_after_it_closed():
    bars = [data.normalize_bar(_raw(17, 110))]
    just_after = datetime(2026, 9, 24, 18, 0, 1, tzinfo=timezone.utc)
    assert len(data.drop_forming_bar(bars, "60", now=just_after)) == 1


def test_four_hour_bars_use_their_own_period_not_the_hour():
    bars = [data.normalize_bar(_raw(8, 100)), data.normalize_bar(_raw(12, 110)),
            data.normalize_bar(_raw(16, 120))]
    kept = data.drop_forming_bar(bars, "240", now=NOW)   # 16:00 bar runs to 20:00
    assert [b["c"] for b in kept] == [100, 110]


def test_an_unknown_timeframe_is_left_alone_rather_than_guessed_at():
    bars = [data.normalize_bar(_raw(18, 120))]
    assert data.drop_forming_bar(bars, "7m", now=NOW) == bars


def test_fetch_bars_keeps_the_forming_bar_unless_asked_to_drop_it():
    """The live default, unchanged: 24 days of trading was measured on it and the
    backtest of the alternative (replay --bars arm B) did not support a change."""
    class _R:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return [_raw(16, 100), _raw(17, 110), _raw(18, 120)]

    class _S:
        def get(self, *a, **k): return _R()

    whole = data.fetch_bars("LINKIDR", "60", session=_S(), now=NOW)
    assert [b["c"] for b in whole] == [100, 110, 120]
    kept = data.fetch_bars("LINKIDR", "60", session=_S(), now=NOW, include_partial=False)
    assert [b["c"] for b in kept] == [100, 110]


def test_bars_with_unreadable_timestamps_are_kept_rather_than_lost():
    """This runs on the snapshot path; one odd bar must not cost a coin's hour."""
    bars = [{"o": 1, "h": 2, "l": 1, "c": 1, "v": 1, "t": "t0"}]
    assert data.drop_forming_bar(bars, "60", now=NOW) == bars
