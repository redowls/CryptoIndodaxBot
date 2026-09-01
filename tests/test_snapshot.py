from cryptoindodax import snapshot


def _bars(n=60, base=100.0):
    out = []
    for i in range(n):
        c = base + i
        out.append({"o": c, "h": c + 1, "l": c - 1, "c": c, "v": 10.0 + i, "t": f"t{i}"})
    return out


def test_compute_for_bars_has_indicator_keys():
    res = snapshot.compute_for_bars(_bars())
    for key in ("last_close", "ema8", "ema20", "ema55", "rsi14", "atr14", "adx14", "vol20"):
        assert key in res
    assert res["last_close"] == res["bar_count"] - 1 + 100.0


def test_compute_for_bars_empty_returns_none():
    assert snapshot.compute_for_bars([]) is None


def test_snapshot_symbol_handles_fetch_error(monkeypatch):
    def boom(pair, tf, *a, **k):
        from cryptoindodax import data
        raise data.FetchError("down")
    monkeypatch.setattr(snapshot.data, "fetch_bars", boom)
    res = snapshot.snapshot_symbol("BTC")
    assert res["status"] == "partial"
    assert res["timeframes"]["1H"]["status"] == "error"


def test_snapshot_symbol_ok(monkeypatch):
    monkeypatch.setattr(snapshot.data, "fetch_bars", lambda pair, tf, *a, **k: _bars())
    res = snapshot.snapshot_symbol("ETH")
    assert res["status"] == "ok"
    assert res["timeframes"]["1H"]["status"] == "ok"


def test_run_writes_json(monkeypatch, tmp_path):
    monkeypatch.setattr(snapshot.config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(snapshot.config, "WATCHLIST", ["BTC"])
    monkeypatch.setattr(snapshot.data, "fetch_bars", lambda pair, tf, *a, **k: _bars())
    path = snapshot.run()
    assert path.exists()
    import json
    data = json.loads(path.read_text())
    assert data["symbols"][0]["symbol"] == "BTC"
    assert "captured_at" in data
