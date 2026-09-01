import json
from datetime import datetime, timedelta, timezone

from cryptoindodax import config, ledger, policy, risk

NOW = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)


# --- risk.position_size ---

def test_position_size_risks_1_5_pct():
    qty, stop, risk_d = risk.position_size(10000, 100.0, 2.0)
    assert risk_d == 150.0
    assert stop == 100.0 - 6.0
    assert qty * 6.0 == risk_d or qty * 100.0 == 10000 / config.MAX_POSITIONS


def test_position_size_half_in_risk_off():
    full, _, _ = risk.position_size(10000, 100.0, 2.0)
    half, _, _ = risk.position_size(10000, 100.0, 2.0, half=True)
    assert abs(half - full / 2) < 1e-9


def test_position_size_caps_notional():
    # tiny ATR would produce a huge position; notional capped at equity/MAX_POSITIONS
    qty, stop, _ = risk.position_size(9000, 100.0, 0.01)
    assert qty * 100.0 <= 9000 / config.MAX_POSITIONS + 1e-6


def test_position_size_unsizable():
    assert risk.position_size(10000, 100.0, 0)[0] == 0.0
    assert risk.position_size(10000, 100.0, 40.0)[0] == 0.0  # stop would be < 0
    assert risk.position_size(0, 100.0, 2.0)[0] == 0.0


# --- risk.circuit_breaker ---

def _closed(pnl, hours_ago):
    return {"pnl": pnl, "exit_time": (NOW - timedelta(hours=hours_ago)).isoformat()}


def test_circuit_breaker_trips_on_24h_losses():
    trades = [_closed(-250, 2), _closed(-200, 10)]
    assert risk.circuit_breaker_tripped(trades, 10000, now=NOW)


def test_circuit_breaker_ignores_old_and_wins():
    trades = [_closed(-500, 30), _closed(-100, 2), _closed(+200, 3)]
    assert not risk.circuit_breaker_tripped(trades, 10000, now=NOW)


# --- policy ---

def _write_policy(tmp_path, **kw):
    p = tmp_path / "policy.json"
    body = {"date": NOW.strftime("%Y-%m-%d"), "regime_hint": "neutral",
            "blocked_symbols": ["DOGE"], "max_positions": 2}
    body.update(kw)
    p.write_text(json.dumps(body))
    return p


def test_policy_loads_and_clamps(tmp_path):
    p = _write_policy(tmp_path, max_positions=99)
    pol = policy.load(p, now=NOW)
    assert pol["regime_hint"] == "neutral"
    assert pol["blocked_symbols"] == ["DOGE"]
    assert pol["max_positions"] == config.MAX_POSITIONS  # clamped down to cap


def test_policy_stale_or_missing_ignored(tmp_path):
    p = _write_policy(tmp_path, date="2026-07-01")
    assert policy.load(p, now=NOW) == policy.DEFAULT
    assert policy.load(tmp_path / "nope.json", now=NOW) == policy.DEFAULT


def test_policy_invalid_fields_degrade(tmp_path):
    p = _write_policy(tmp_path, regime_hint="yolo", blocked_symbols="DOGE", max_positions=-1)
    pol = policy.load(p, now=NOW)
    assert pol == policy.DEFAULT


def test_policy_garbage_file(tmp_path):
    p = tmp_path / "policy.json"
    p.write_text("not json{")
    assert policy.load(p, now=NOW) == policy.DEFAULT


# --- ledger ---

def test_ledger_round_trip(tmp_path):
    path = tmp_path / "trades.json"
    led = ledger.load(path)
    pos = ledger.open_position(led, "SOL", 2.0, 100.0, 2.0, "oid1", now=NOW)
    assert pos["stop"] == 100.0 - config.STOP_ATR_MULT * 2.0
    ledger.save(led, path)
    led2 = ledger.load(path)
    assert led2["open"][0]["symbol"] == "SOL"


def test_ledger_close_records_pnl(tmp_path):
    led = ledger.load(tmp_path / "t.json")
    pos = ledger.open_position(led, "SOL", 2.0, 100.0, 2.0, "oid1", now=NOW)
    trade = ledger.close_position(led, pos, 110.0, "tp", now=NOW)
    assert trade["pnl"] == 20.0
    assert led["open"] == []
    assert led["closed"][0]["reason"] == "tp"


def test_ledger_throttle(tmp_path):
    led = ledger.load(tmp_path / "t.json")
    assert not ledger.throttled(led, "SOL", now=NOW)
    ledger.record_entry_attempt(led, "SOL", now=NOW)
    assert ledger.throttled(led, "SOL", now=NOW + timedelta(hours=23))
    assert not ledger.throttled(led, "SOL", now=NOW + timedelta(hours=25))


def test_ledger_hours_held():
    pos = {"entry_time": (NOW - timedelta(hours=6)).isoformat()}
    assert abs(ledger.hours_held(pos, now=NOW) - 6) < 1e-6


def test_reconcile_drops_missing_and_adopts_unknown(tmp_path):
    led = ledger.load(tmp_path / "t.json")
    ledger.open_position(led, "SOL", 2.0, 100.0, 2.0, "o1", now=NOW)
    ledger.open_position(led, "LTC", 1.0, 90.0, 1.5, "o2", now=NOW)
    held = [
        {"symbol": "LTC", "qty": 0.5, "current_price": 95.0, "avg_entry_price": 90.0},
        {"symbol": "UNI", "qty": 10.0, "current_price": 8.0, "avg_entry_price": 7.9},
    ]
    notes = ledger.reconcile(led, held, now=NOW)
    syms = {p["symbol"] for p in led["open"]}
    assert syms == {"LTC", "UNI"}
    ltc = next(p for p in led["open"] if p["symbol"] == "LTC")
    assert ltc["qty"] == 0.5  # the exchange wins
    uni = next(p for p in led["open"] if p["symbol"] == "UNI")
    assert uni.get("adopted") and uni["stop"] < 8.0
    assert any("SOL" in n for n in notes)


def test_reconcile_skips_balance_with_no_mark_price(tmp_path):
    """A held coin the snapshot could not price is left alone, not adopted blind."""
    led = ledger.load(tmp_path / "t.json")
    notes = ledger.reconcile(
        led, [{"symbol": "XRP", "qty": 100.0, "current_price": None,
               "avg_entry_price": None}], now=NOW)
    assert led["open"] == []
    assert any("no price" in n for n in notes)
