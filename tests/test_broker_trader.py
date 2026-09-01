import hashlib
import hmac
import json
import urllib.parse
from datetime import datetime, timezone

import pytest
from cryptoindodax import broker, config, ledger, trader

NOW = datetime(2026, 7, 14, 12, 30, tzinfo=timezone.utc)


# --- broker: signing / transport -----------------------------------------

@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setattr(config, "INDODAX_KEY", "TESTKEY1-TESTKEY2-TESTKEY3")
    monkeypatch.setattr(config, "INDODAX_SECRET", "topsecret")
    return "TESTKEY1-TESTKEY2-TESTKEY3", "topsecret"


class _Resp:
    def __init__(self, payload, text="x"):
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


def _capture(monkeypatch, payload):
    seen = {}

    def fake_request(method, url, headers=None, timeout=None, data=None):
        seen.update(method=method, url=url, headers=headers, data=data)
        return _Resp(payload)

    monkeypatch.setattr(broker.requests, "request", fake_request)
    return seen


def test_broker_requires_credentials(monkeypatch):
    monkeypatch.setattr(config, "INDODAX_KEY", None)
    monkeypatch.setattr(config, "INDODAX_SECRET", None)
    with pytest.raises(broker.BrokerError):
        broker._credentials()


def test_get_uses_query_string_and_sha256_sign(creds, monkeypatch):
    _, secret = creds
    seen = _capture(monkeypatch, {"balances": []})
    broker.get_balances()
    assert seen["method"] == "GET"
    assert seen["data"] is None
    qs = seen["url"].split("?", 1)[1]
    # the signature must cover exactly the string that was sent
    expected = hmac.new(secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
    assert seen["headers"]["Sign"] == expected
    assert seen["headers"]["X-APIKEY"] == creds[0]
    params = dict(urllib.parse.parse_qsl(qs))
    assert "timestamp" in params and params["recvWindow"] == str(config.RECV_WINDOW_MS)


def test_post_signs_body_not_url(creds, monkeypatch):
    _, secret = creds
    seen = _capture(monkeypatch, {"orderId": 6423})
    broker.market_buy_idr("BTC", 250_000)
    assert seen["method"] == "POST"
    assert "?" not in seen["url"]
    expected = hmac.new(secret.encode(), seen["data"].encode(), hashlib.sha256).hexdigest()
    assert seen["headers"]["Sign"] == expected
    assert seen["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    body = dict(urllib.parse.parse_qsl(seen["data"]))
    assert body["symbol"] == "BTCIDR" and body["side"] == "BUY"
    assert body["type"] == "MARKET"
    # a market BUY is sized in rupiah, never in coin
    assert body["quoteOrderQty"] == "250000" and "quantity" not in body


def test_market_sell_is_sized_in_coin(creds, monkeypatch):
    seen = _capture(monkeypatch, {"orderId": 1})
    broker.market_sell_qty("SOL", 2.5)
    body = dict(urllib.parse.parse_qsl(seen["data"]))
    assert body["side"] == "SELL" and body["quantity"] == "2.5"
    assert "quoteOrderQty" not in body


def test_error_code_in_body_raises(creds, monkeypatch):
    _capture(monkeypatch, {"code": -1002, "msg": "Invalid credentials."})
    with pytest.raises(broker.BrokerError) as e:
        broker.get_balances()
    assert "-1002" in str(e.value)


# --- broker: account / positions -----------------------------------------

def test_get_balances_parses_floats(creds, monkeypatch):
    _capture(monkeypatch, {"balances": [
        {"asset": "IDR", "free": "5000000", "locked": "0"},
        {"asset": "SOL", "free": "2.5", "locked": "0.5"}]})
    b = broker.get_balances()
    assert b["IDR"]["free"] == 5_000_000.0
    assert b["SOL"] == {"free": 2.5, "locked": 0.5}


def test_get_account_marks_equity_in_idr(creds, monkeypatch):
    _capture(monkeypatch, {"balances": [
        {"asset": "IDR", "free": "5000000", "locked": "0"},
        {"asset": "SOL", "free": "2.0", "locked": "0"}]})
    acct = broker.get_account(price_by_symbol={"SOL": 1_800_000})
    assert acct["cash"] == 5_000_000.0
    assert acct["equity"] == 5_000_000.0 + 2.0 * 1_800_000


def test_get_account_ignores_coin_with_no_mark(creds, monkeypatch):
    _capture(monkeypatch, {"balances": [
        {"asset": "IDR", "free": "1000000", "locked": "0"},
        {"asset": "SOL", "free": "2.0", "locked": "0"}]})
    # no price supplied → the coin is not guessed into equity
    assert broker.get_account(price_by_symbol={})["equity"] == 1_000_000.0


def test_get_positions_skips_dust(creds, monkeypatch):
    balances = {"SOL": {"free": 2.0, "locked": 0.0},
                "DOGE": {"free": 0.5, "locked": 0.0}}   # 0.5 * 1472 = Rp736 → dust
    pos = broker.get_positions(price_by_symbol={"SOL": 1_800_000, "DOGE": 1_472},
                               balances=balances)
    assert [p["symbol"] for p in pos] == ["SOL"]
    assert pos[0]["qty"] == 2.0 and pos[0]["free_qty"] == 2.0


def test_get_positions_counts_locked_qty(creds, monkeypatch):
    pos = broker.get_positions(price_by_symbol={"SOL": 1_800_000},
                               balances={"SOL": {"free": 1.0, "locked": 3.0}})
    assert pos[0]["qty"] == 4.0 and pos[0]["free_qty"] == 1.0


# --- broker: fills --------------------------------------------------------

def test_wait_for_fill_filled(creds, monkeypatch):
    monkeypatch.setattr(broker, "get_order", lambda s, oid, **k: {
        "status": "filled", "filled_avg_price": 1_400_000_000.0, "filled_qty": 0.001})
    assert broker.wait_for_fill("BTC", "o1", timeout_s=1, sleep=lambda s: None) == \
        ("filled", 1_400_000_000.0, 0.001)


def test_wait_for_fill_timeout_cancels(creds, monkeypatch):
    cancels = []
    monkeypatch.setattr(broker, "get_order", lambda s, oid, **k: {
        "status": "new", "filled_avg_price": None, "filled_qty": 0.0})
    monkeypatch.setattr(broker, "cancel_order",
                        lambda s, oid, **k: cancels.append(oid))
    status, price, qty = broker.wait_for_fill("BTC", "o1", timeout_s=0.05, poll_s=0.01,
                                              sleep=lambda s: None)
    assert status == "canceled" and qty == 0.0 and cancels == ["o1"]


def test_wait_for_fill_reports_partial_after_cancel(creds, monkeypatch):
    monkeypatch.setattr(broker, "get_order", lambda s, oid, **k: {
        "status": "cancelled", "filled_avg_price": 100.0, "filled_qty": 0.4})
    assert broker.wait_for_fill("BTC", "o1", timeout_s=1, sleep=lambda s: None) == \
        ("partial", 100.0, 0.4)


def test_avg_fill_price_is_quantity_weighted(creds, monkeypatch):
    _capture(monkeypatch, {"data": [
        {"qty": "1.0", "price": "100"},
        {"qty": "3.0", "price": "200"}]})
    assert broker.avg_fill_price("SOL", "o1") == pytest.approx(175.0)


def test_avg_fill_price_none_when_no_fills(creds, monkeypatch):
    _capture(monkeypatch, {"data": []})
    assert broker.avg_fill_price("SOL", "o1") is None


# --- trader helpers -------------------------------------------------------

def _tf(ema8=1_100_000, ema20=1_050_000, ema55=1_000_000, rsi=55, adx=35,
        atr=20_000, close=1_120_000):
    return {"status": "ok", "ema8": ema8, "ema20": ema20, "ema55": ema55,
            "rsi14": rsi, "adx14": adx, "atr14": atr, "last_close": close}


def _snapshot(now=NOW):
    def coin(sym, **kw):
        return {"symbol": sym, "status": "ok",
                "timeframes": {"1H": _tf(**kw), "4H": _tf(), "1D": _tf(adx=25)}}
    return {
        "captured_at": now.isoformat(),
        "symbols": [coin("BTC"), coin("SOL", adx=40), coin("DOGE", adx=10)],
    }


def _write_snapshot(tmp_path, snap, now=NOW):
    d = tmp_path / now.strftime("%Y-%m-%d")
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{now.strftime('%H')}.json").write_text(json.dumps(snap))


def _good_extras(syms):
    return {s: {"day_change_pct": 1.0, "last_1h_close": 1_120_000,
                "prev_1h_close": 1_100_000} for s in syms}


def _paths(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "snaps")
    monkeypatch.setattr(config, "TRADES_DIR", tmp_path / "trades")
    monkeypatch.setattr(config, "POLICY_PATH", tmp_path / "policy.json")
    monkeypatch.setattr(config, "INDODAX_KEY", None)
    monkeypatch.setattr(config, "INDODAX_SECRET", None)


def test_load_current_snapshot_fresh_and_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    assert trader.load_current_snapshot(NOW) is None
    _write_snapshot(tmp_path, _snapshot(), NOW)
    assert trader.load_current_snapshot(NOW) is not None
    stale = _snapshot(NOW.replace(hour=8))
    _write_snapshot(tmp_path, stale, NOW.replace(hour=8))
    assert trader.load_current_snapshot(NOW.replace(hour=10)) is None


def test_prices_from_snapshot_collects_1h_closes():
    marks = trader.prices_from_snapshot(_snapshot())
    assert marks["SOL"] == 1_120_000 and set(marks) == {"BTC", "SOL", "DOGE"}


def test_dry_run_enters_best_candidate(tmp_path, monkeypatch, capsys):
    _paths(monkeypatch, tmp_path)
    monkeypatch.setattr(trader, "fetch_extras", lambda syms, now=None: _good_extras(syms))
    _write_snapshot(tmp_path / "snaps", _snapshot(), NOW)

    trader.run(dry_run=True, now=NOW)
    out = capsys.readouterr().out
    assert "regime: computed risk_on" in out
    assert "DRY-RUN entry SOL" in out
    assert "reject DOGE" in out
    assert "Rp" in out                       # amounts are reported in rupiah
    assert not (tmp_path / "trades" / "trades.json").exists()


def test_dry_run_exit_on_stop_hit(tmp_path, monkeypatch, capsys):
    _paths(monkeypatch, tmp_path)
    monkeypatch.setattr(trader, "fetch_extras", lambda syms, now=None: _good_extras(syms))
    snap = _snapshot()
    snap["symbols"][1]["timeframes"]["1H"]["last_close"] = 900_000.0  # SOL below stop
    _write_snapshot(tmp_path / "snaps", snap, NOW)

    led = ledger.load(tmp_path / "trades" / "trades.json")
    ledger.open_position(led, "SOL", 2.0, 1_000_000.0, 20_000.0, "o1", now=NOW)
    ledger.save(led, tmp_path / "trades" / "trades.json")

    trader.run(dry_run=True, now=NOW)
    assert "DRY-RUN exit SOL: stop" in capsys.readouterr().out


def test_run_requires_trading_enabled(monkeypatch, capsys):
    monkeypatch.setattr(config, "TRADING_ENABLED", False)
    trader.run(dry_run=False, now=NOW)
    assert "TRADING_ENABLED is false" in capsys.readouterr().out


def test_circuit_breaker_blocks_entries(tmp_path, monkeypatch, capsys):
    _paths(monkeypatch, tmp_path)
    _write_snapshot(tmp_path / "snaps", _snapshot(), NOW)

    led = ledger.load(tmp_path / "trades" / "trades.json")
    led["closed"].append({"pnl": -500_000.0, "exit_time": NOW.isoformat()})
    ledger.save(led, tmp_path / "trades" / "trades.json")

    trader.run(dry_run=True, now=NOW)
    out = capsys.readouterr().out
    assert "circuit breaker" in out and "DRY-RUN entry" not in out


def test_sellable_qty_uses_free_balance_not_ledger_qty():
    """Fees are taken in-asset, so the held quantity drifts below the fill."""
    pos = {"symbol": "SOL", "qty": 2.0}
    live = {"SOL": {"symbol": "SOL", "qty": 1.994, "free_qty": 1.994}}
    assert trader._sellable_qty(pos, live) == pytest.approx(1.994)


def test_sellable_qty_falls_back_to_ledger_when_unknown():
    assert trader._sellable_qty({"symbol": "SOL", "qty": 2.0}, {}) == pytest.approx(2.0)
