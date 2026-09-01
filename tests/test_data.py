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


def test_watchlist_is_the_five_selected_coins():
    assert config.WATCHLIST == ["BTC", "ETH", "UNI", "DOT", "LINK"]


def test_watchlist_contains_btc_for_the_regime_gate():
    """strategy.regime() reads BTC's 1D to set risk_on/neutral/risk_off for
    every coin. Without BTC in the watchlist there is no BTC in the snapshot,
    regime() falls back to 'neutral', and the ADX entry bar silently rises
    from 25 to 30 across the board."""
    assert "BTC" in config.WATCHLIST
