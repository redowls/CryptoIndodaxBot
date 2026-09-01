import json

from cryptoindodax import digest


def _snap(captured, btc_close, ema_stack="up"):
    if ema_stack == "up":
        e8, e20, e55 = 110.0, 105.0, 100.0
    else:
        e8, e20, e55 = 100.0, 105.0, 110.0
    return {
        "captured_at": captured,
        "symbols": [{
            "symbol": "BTC", "pair": "BTCIDR", "status": "ok",
            "timeframes": {"1H": {
                "status": "ok", "last_close": btc_close,
                "ema8": e8, "ema20": e20, "ema55": e55,
                "rsi14": 60.0, "adx14": 30.0, "atr14": 2.0,
            }},
        }],
    }


def test_summarize_empty():
    out = digest.summarize([])
    assert out["snapshot_count"] == 0 and out["coins"] == []


def test_summarize_computes_change_and_trend():
    snaps = [_snap("2026-06-16T00:00:00Z", 100.0), _snap("2026-06-16T05:00:00Z", 110.0)]
    out = digest.summarize(snaps)
    assert out["snapshot_count"] == 2
    coin = out["coins"][0]
    assert coin["symbol"] == "BTC"
    assert coin["change_pct_day"] == 10.0
    assert coin["trend"] == "up"
    assert coin["atr_pct"] == round(2.0 / 110.0 * 100, 2)


def test_summarize_marks_error_status():
    snaps = [{"captured_at": "t", "symbols": [
        {"symbol": "UNI", "status": "partial",
         "timeframes": {"1H": {"status": "error", "error": "x"}}}]}]
    out = digest.summarize(snaps)
    assert out["coins"][0]["status"] == "error"


def test_load_day_reads_sorted(tmp_path, monkeypatch):
    monkeypatch.setattr(digest.config, "DATA_DIR", tmp_path)
    d = tmp_path / "2026-06-16"
    d.mkdir()
    (d / "05.json").write_text(json.dumps(_snap("2026-06-16T05:00:00Z", 110.0)))
    (d / "00.json").write_text(json.dumps(_snap("2026-06-16T00:00:00Z", 100.0)))
    snaps = digest.load_day("2026-06-16")
    assert len(snaps) == 2
    assert snaps[0]["captured_at"].endswith("00:00:00Z")  # sorted by filename
