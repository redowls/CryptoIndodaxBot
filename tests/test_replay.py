from datetime import datetime, timezone

from cryptoindodax import config, replay


def _tf(close=100.0, ema8=110, ema20=105, ema55=100, rsi=55, adx=30, atr=2.0):
    return {"status": "ok", "last_close": close, "ema8": ema8, "ema20": ema20,
            "ema55": ema55, "rsi14": rsi, "adx14": adx, "atr14": atr}


def _snap(when, **closes):
    """One snapshot where every named coin has a clean uptrend at that close."""
    return (datetime.fromisoformat(when), {
        "captured_at": when,
        "symbols": [{"symbol": s, "status": "ok",
                     "timeframes": {"1H": _tf(close=c), "4H": _tf(close=c), "1D": _tf(close=c)}}
                    for s, c in closes.items()]})


# --- history and extras ---------------------------------------------------

def test_build_extras_uses_the_previous_snapshot_close():
    h = [_snap("2026-09-01T01:00:00+00:00", BTC=100.0),
         _snap("2026-09-01T02:00:00+00:00", BTC=110.0)]
    extras = replay.build_extras(h)
    assert extras[0]["BTC"]["prev_1h_close"] is None      # nothing before the first
    assert extras[1]["BTC"]["prev_1h_close"] == 100.0
    assert extras[1]["BTC"]["last_1h_close"] == 110.0


def test_build_extras_measures_day_change_from_the_previous_day():
    h = [_snap("2026-09-01T23:00:00+00:00", BTC=100.0),
         _snap("2026-09-02T01:00:00+00:00", BTC=105.0)]
    extras = replay.build_extras(h)
    assert round(extras[1]["BTC"]["day_change_pct"], 2) == 5.0


def test_load_history_is_sorted_and_skips_unreadable(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "02.json").write_text(
        '{"captured_at": "2026-09-01T02:00:00+00:00", "symbols": []}')
    (tmp_path / "a" / "01.json").write_text(
        '{"captured_at": "2026-09-01T01:00:00+00:00", "symbols": []}')
    (tmp_path / "a" / "bad.json").write_text("not json")
    out = replay.load_history(str(tmp_path / "*" / "*.json"))
    assert [d.hour for d, _ in out] == [1, 2]


# --- policy reconstruction ------------------------------------------------

def test_policy_at_picks_the_latest_committed_before_the_hour():
    hist = [(datetime.fromisoformat("2026-09-01T17:00:00+00:00"),
             {"regime_hint": "neutral", "blocked_symbols": ["UNI"], "max_positions": 2}),
            (datetime.fromisoformat("2026-09-05T17:00:00+00:00"),
             {"regime_hint": "auto", "blocked_symbols": [], "max_positions": 4})]
    early = replay.policy_at(hist, datetime.fromisoformat("2026-09-03T00:00:00+00:00"))
    late = replay.policy_at(hist, datetime.fromisoformat("2026-09-06T00:00:00+00:00"))
    assert early["blocked_symbols"] == ["UNI"] and early["max_positions"] == 2
    assert late["blocked_symbols"] == [] and late["max_positions"] == 4


def test_policy_at_before_any_commit_is_permissive():
    hist = [(datetime.fromisoformat("2026-09-05T17:00:00+00:00"),
             {"regime_hint": "risk_off", "blocked_symbols": ["BTC"], "max_positions": 1})]
    pol = replay.policy_at(hist, datetime.fromisoformat("2026-09-01T00:00:00+00:00"))
    assert pol["blocked_symbols"] == [] and pol["regime_hint"] == "auto"


# --- config overrides -----------------------------------------------------

def test_overrides_restore_config_even_on_error():
    before = config.TRAIL_ATR_MULT
    try:
        with replay.overrides(TRAIL_ATR_MULT=99.0):
            assert config.TRAIL_ATR_MULT == 99.0
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert config.TRAIL_ATR_MULT == before


# --- the simulation -------------------------------------------------------

def test_run_on_empty_history_is_flat_not_an_error():
    res = replay.run([], start_equity=1000.0)
    assert res["trades"] == [] and res["end_equity"] == 1000.0


def test_run_charges_fees_on_both_sides():
    """A position opened and closed at the same price must lose exactly the
    round trip — this is what the live ledger's gross pnl field does not do."""
    h = [_snap(f"2026-09-0{d}T0{t}:00:00+00:00", BTC=1_000_000.0, DOT=1_000_000.0)
         for d in (1, 2) for t in range(1, 9)]
    res = replay.run(h, start_equity=10_000_000.0, fee_pct=1.0)
    for t in res["trades"]:
        if abs(t["exit_price"] - t["entry_price"]) < 1e-9:
            assert t["pnl_net"] < 0, "a flat round trip must still cost the fee"


def test_run_respects_the_position_cap():
    coins = {s: 100_000.0 for s in config.WATCHLIST}
    h = [_snap(f"2026-09-01T{h_:02d}:00:00+00:00", **coins) for h_ in range(1, 12)]
    res = replay.run(h, start_equity=100_000_000.0, max_positions=2)
    assert len(res["open"]) <= 2


def test_summarize_counts_a_profitable_stop_as_a_failure():
    """Stop-exit doctrine: a stop is a failed trade whatever its P&L sign, so a
    break-even or profitable stop lifts the headline win rate but not the true one."""
    res = {"trades": [{"reason": "stop", "pnl": 10.0, "pnl_net": 10.0, "r_multiple": 0.1},
                      {"reason": "tp", "pnl": 100.0, "pnl_net": 100.0, "r_multiple": 2.5}],
           "equity_curve": [("t", 1000.0)], "start_equity": 1000.0, "end_equity": 1110.0}
    s = replay.summarize(res)
    assert s["headline_win_pct"] == 100.0
    assert s["true_win_pct"] == 50.0
    assert s["stop_pct"] == 50.0


def test_summarize_reports_max_drawdown():
    res = {"trades": [], "equity_curve": [("a", 1000.0), ("b", 800.0), ("c", 900.0)],
           "start_equity": 1000.0, "end_equity": 900.0}
    assert replay.summarize(res)["max_dd_pct"] == 20.0


def test_buy_and_hold_benchmark():
    h = [_snap("2026-09-01T01:00:00+00:00", BTC=100.0),
         _snap("2026-09-02T01:00:00+00:00", BTC=110.0)]
    assert replay.buy_and_hold(h, "BTC") == 10.0
