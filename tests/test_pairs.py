import pytest
from cryptoindodax import config, pairs


@pytest.fixture(autouse=True)
def _isolate_pair_cache(monkeypatch, tmp_path):
    """Never touch the real cache file or the network from a unit test."""
    monkeypatch.setattr(config, "PAIRS_CACHE", tmp_path / "pairs.json")
    monkeypatch.setattr(pairs, "_cache", None)
    yield
    pairs._cache = None


def _stub(monkeypatch, meta):
    monkeypatch.setattr(pairs, "load", lambda *a, **k: {"btc_idr": meta})


def test_round_qty_floors_to_pair_precision(monkeypatch):
    _stub(monkeypatch, {"price_round": 4, "trade_min_base_currency": 10000,
                        "trade_min_traded_currency": 0})
    # rounds DOWN — rounding up could exceed the free balance on a sell
    assert pairs.round_qty("BTC", 1.239999) == 1.2399
    assert pairs.round_qty("BTC", 1.23991) == 1.2399


def test_round_qty_zero_precision_gives_whole_units(monkeypatch):
    _stub(monkeypatch, {"price_round": 0, "trade_min_base_currency": 10000,
                        "trade_min_traded_currency": 0})
    assert pairs.round_qty("BTC", 7.9) == 7.0


def test_unknown_pair_falls_back_to_defaults(monkeypatch):
    monkeypatch.setattr(pairs, "load", lambda *a, **k: {})
    assert pairs.min_order_idr("DOGE") == float(config.MIN_ORDER_IDR)
    assert pairs.round_qty("DOGE", 1.123456789) == 1.12345678


def test_meets_minimums_rejects_small_notional(monkeypatch):
    _stub(monkeypatch, {"price_round": 8, "trade_min_base_currency": 10000,
                        "trade_min_traded_currency": 0})
    ok, why = pairs.meets_minimums("BTC", 0.000001, 1_000_000)  # = Rp1
    assert not ok and "below pair minimum" in why


def test_meets_minimums_rejects_small_coin_qty(monkeypatch):
    _stub(monkeypatch, {"price_round": 8, "trade_min_base_currency": 1,
                        "trade_min_traded_currency": 0.001})
    ok, why = pairs.meets_minimums("BTC", 0.0001, 1_000_000)
    assert not ok and "below pair minimum" in why


def test_meets_minimums_accepts_valid_order(monkeypatch):
    _stub(monkeypatch, {"price_round": 8, "trade_min_base_currency": 10000,
                        "trade_min_traded_currency": 0.000001})
    ok, why = pairs.meets_minimums("BTC", 0.001, 1_400_000_000)
    assert ok and why == "ok"


def test_load_uses_fresh_cache_without_network(monkeypatch, tmp_path):
    import json, time
    config.PAIRS_CACHE.write_text(json.dumps(
        {"fetched_at": int(time.time()), "pairs": {"btc_idr": {"price_round": 2}}}))

    def boom(*a, **k):
        raise AssertionError("should not hit the network with a fresh cache")

    monkeypatch.setattr(pairs.data, "fetch_pairs", boom)
    assert pairs.load()["btc_idr"]["price_round"] == 2


def test_load_falls_back_to_stale_cache_on_fetch_error(monkeypatch):
    import json
    config.PAIRS_CACHE.write_text(json.dumps(
        {"fetched_at": 0, "pairs": {"btc_idr": {"price_round": 3}}}))

    def boom(*a, **k):
        raise pairs.data.FetchError("network down")

    monkeypatch.setattr(pairs.data, "fetch_pairs", boom)
    assert pairs.load()["btc_idr"]["price_round"] == 3  # stale beats empty


def test_fmt_idr_uses_indonesian_thousands_separator():
    assert config.fmt_idr(1234567) == "Rp1.234.567"
    assert config.fmt_idr(-50000) == "-Rp50.000"
    assert config.fmt_idr(None) == "-"


def test_quantity_precision_comes_from_price_round_not_volume_precision(monkeypatch):
    """Regression: /api/pairs reports volume_precision=0 on EVERY pair (it
    describes the IDR side). Rounding a coin quantity by it floors 0.0087 BTC
    to 0 and the bot can never buy BTC or ETH."""
    _stub(monkeypatch, {"price_round": 8, "volume_precision": 0,
                        "trade_min_base_currency": 10000,
                        "trade_min_traded_currency": 7.2e-06})
    assert pairs.round_qty("BTC", 0.00874321) == 0.00874321


def test_taker_fee_converts_percent_to_fraction(monkeypatch):
    _stub(monkeypatch, {"price_round": 8, "trade_min_base_currency": 10000,
                        "trade_min_traded_currency": 0.0,
                        "trade_fee_percent_taker": 0.2})
    assert pairs.taker_fee_pct("BTC") == pytest.approx(0.002)
