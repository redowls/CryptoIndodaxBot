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
    """risk_off halves the RISK BUDGET, always — and halves the position too
    whenever the notional cap is not in the way (here a 4.0 ATR, wide enough
    that neither size gets clipped)."""
    _, _, full_risk = risk.position_size(10000, 100.0, 2.0)
    _, _, half_risk = risk.position_size(10000, 100.0, 2.0, half=True)
    assert abs(half_risk - full_risk / 2) < 1e-9
    full, _, _ = risk.position_size(10000, 100.0, 4.0)
    half, _, _ = risk.position_size(10000, 100.0, 4.0, half=True)
    assert abs(half - full / 2) < 1e-9


def test_the_notional_cap_compresses_the_risk_off_half_size():
    """Half size is half the risk, NOT always half the position — and raising
    MAX_POSITIONS made that bite harder.

    When the stop is narrow enough that equity/MAX_POSITIONS clips the full
    trade but not the half one, a risk_off entry comes out at 62.5% of a full
    entry rather than 50%. At MAX_POSITIONS=4 this same fixture was not clipped
    and the ratio was exactly half; the 4 -> 5 change on 2026-09-21 introduced
    the compression. It is a genuine weakening of the risk_off brake, pinned
    here so it is a known property rather than a surprise."""
    full, _, _ = risk.position_size(10000, 100.0, 2.0)
    half, _, _ = risk.position_size(10000, 100.0, 2.0, half=True)
    cap = 10000 / config.MAX_POSITIONS
    assert abs(full * 100.0 - cap) < 1e-6, "the full-size trade is clipped by the cap"
    assert half * 100.0 < cap, "the half-size trade is not"
    assert 0.5 < half / full < 0.7


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


def test_ledger_close_records_pnl_net_of_fees(tmp_path):
    """`pnl` is the headline every consumer reads, so it must be NET.

    It was gross until 2026-09-17, which is how the ledger came to report
    +Rp22.260 realised on an account that was down Rp7.035."""
    led = ledger.load(tmp_path / "t.json")
    pos = ledger.open_position(led, "SOL", 2.0, 100.0, 2.0, "oid1", now=NOW)
    trade = ledger.close_position(led, pos, 110.0, "tp", now=NOW)
    assert trade["pnl_gross"] == 20.0
    assert trade["pnl"] < trade["pnl_gross"]
    assert trade["pnl"] == round(trade["pnl_gross"] - trade["fees"], 2)
    assert trade["fees"] > 0 and trade["fees_estimated"] is True
    assert led["open"] == []
    assert led["closed"][0]["reason"] == "tp"


def test_ledger_close_books_r_geometry_and_peak(tmp_path):
    """The doctrine buckets and the giveback view read these off the trade,
    so the trade has to carry them rather than leave them to be guessed."""
    led = ledger.load(tmp_path / "t.json")
    pos = ledger.open_position(led, "SOL", 2.0, 100.0, 2.0, "oid1", now=NOW)   # stop 94, 1R = 6
    pos["high_water"] = 112.0
    trade = ledger.close_position(led, pos, 109.0, "lock", now=NOW)
    assert trade["initial_stop"] == 94.0
    assert trade["r_multiple"] == 1.5
    assert trade["peak_price"] == 112.0 and trade["peak_r"] == 2.0


def test_ledger_uses_real_commissions_when_given(tmp_path):
    led = ledger.load(tmp_path / "t.json")
    pos = ledger.open_position(led, "SOL", 2.0, 100.0, 2.0, "oid1", now=NOW, entry_fee=7.0)
    trade = ledger.close_position(led, pos, 110.0, "tp", now=NOW, exit_fee=9.0)
    assert trade["fees"] == 16.0
    assert trade["pnl"] == 4.0
    assert trade["fees_estimated"] is False


def test_ledger_flags_a_half_measured_fee_as_estimated(tmp_path):
    """One real side and one modelled side is still an estimate."""
    led = ledger.load(tmp_path / "t.json")
    pos = ledger.open_position(led, "SOL", 2.0, 100.0, 2.0, "oid1", now=NOW, entry_fee=7.0)
    trade = ledger.close_position(led, pos, 110.0, "tp", now=NOW)
    assert trade["fees_estimated"] is True


def test_ledger_keeps_the_order_id_on_the_closed_trade(tmp_path):
    """Without it a closed trade cannot be reconciled against exchange fills."""
    led = ledger.load(tmp_path / "t.json")
    pos = ledger.open_position(led, "SOL", 2.0, 100.0, 2.0, "oid1", now=NOW)
    assert ledger.close_position(led, pos, 110.0, "tp", now=NOW)["order_id"] == "oid1"


def test_circuit_breaker_sees_net_losses(tmp_path):
    """The breaker sums `pnl`; now that it is net, fees count toward the halt."""
    led = ledger.load(tmp_path / "t.json")
    pos = ledger.open_position(led, "SOL", 2.0, 100.0, 2.0, "oid1", now=NOW)
    trade = ledger.close_position(led, pos, 100.0, "stop", now=NOW)
    assert trade["pnl"] < 0, "a flat round trip is a loss once fees are counted"


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
