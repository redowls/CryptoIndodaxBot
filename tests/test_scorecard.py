from datetime import datetime, timezone

from cryptoindodax import scorecard

NOW = datetime(2026, 9, 17, tzinfo=timezone.utc)
DUE = datetime(2026, 11, 1, tzinfo=timezone.utc)


def _trade(reason="tp", pnl=100.0, gross=None, fees=10.0, estimated=False, r=None):
    return {"reason": reason, "pnl": pnl, "pnl_gross": gross if gross is not None else pnl + fees,
            "fees": fees, "fees_estimated": estimated, "r_multiple": r}


def _led(tps=0, stops=0, **kw):
    return {"closed": [_trade("tp", **kw) for _ in range(tps)]
                      + [_trade("stop", pnl=-50.0, **kw) for _ in range(stops)]}


# --- the doctrine ---------------------------------------------------------

def test_a_profitable_stop_is_still_not_a_win():
    """Standing rule: a stop is a failed trade whatever its P&L sign."""
    led = {"closed": [{"reason": "stop", "pnl": 500.0, "pnl_gross": 510.0, "fees": 10.0},
                      {"reason": "tp", "pnl": 100.0, "pnl_gross": 110.0, "fees": 10.0}]}
    m = scorecard.metrics(led, now=NOW)
    assert m["true_win_pct"] == 50.0
    assert m["stop_pct"] == 50.0


def test_net_and_gross_are_reported_separately():
    m = scorecard.metrics(_led(tps=2, stops=1), now=NOW)
    assert m["net_realised"] < m["gross_realised"]
    assert m["fees"] > 0


def test_a_lock_exit_is_a_win_only_at_or_above_1r():
    """Amendment 2026-09-18, made before any lock exit existed: the ladder's
    'lock' reason is scored by the doctrine bucket, never by its name."""
    led = {"closed": [_trade("lock", r=1.7), _trade("lock", r=0.6), _trade("stop", pnl=-50.0)]}
    m = scorecard.metrics(led, now=NOW)
    assert (m["locks"], m["lock_wins"]) == (2, 1)
    assert m["true_win_pct"] == 33.3
    assert m["tp_only_win_pct"] == 0.0
    assert "lock" in scorecard.render(m)


def test_a_lock_without_geometry_is_not_a_win():
    m = scorecard.metrics({"closed": [_trade("lock")]}, now=NOW)     # r_multiple None
    assert m["lock_wins"] == 0 and m["true_win_pct"] == 0.0


# --- the criterion --------------------------------------------------------

def test_verdict_is_collecting_before_the_decision_point():
    """Neither the date nor the trade count is reached yet."""
    m = scorecard.metrics(_led(tps=1, stops=9), equity=400_000.0,
                          benchmark_pct=5.0, now=NOW)
    assert m["fails_win_floor"] and m["trails_benchmark"]
    assert m["verdict"] == "COLLECTING", "must not judge before the window closes"


def test_date_alone_does_not_open_the_decision():
    m = scorecard.metrics(_led(tps=1, stops=9), equity=400_000.0,
                          benchmark_pct=5.0, now=DUE)
    assert m["trades"] == 10 < scorecard.MIN_TRADES
    assert m["verdict"] == "COLLECTING"


def test_stop_requires_both_conditions():
    led = _led(tps=8, stops=32)                       # 20% true win, 40 trades
    m = scorecard.metrics(led, equity=400_000.0, benchmark_pct=5.0, now=DUE)
    assert m["trades"] == 40 and m["fails_win_floor"] and m["trails_benchmark"]
    assert m["verdict"].startswith("STOP")


def test_a_low_win_rate_alone_does_not_stop_it():
    """A small win rate is survivable if the payoff is large enough."""
    m = scorecard.metrics(_led(tps=8, stops=32), equity=900_000.0,
                          benchmark_pct=5.0, now=DUE)
    assert m["fails_win_floor"] and not m["trails_benchmark"]
    assert m["verdict"] == "CONTINUE"


def test_trailing_the_benchmark_alone_does_not_stop_it():
    m = scorecard.metrics(_led(tps=20, stops=20), equity=400_000.0,
                          benchmark_pct=5.0, now=DUE)
    assert m["trails_benchmark"] and not m["fails_win_floor"]
    assert m["verdict"] == "CONTINUE"


def test_criterion_constants_are_the_registered_ones():
    """Changing these after the fact defeats the purpose of pre-registering."""
    assert scorecard.DECISION_DATE == "2026-10-17"
    assert scorecard.MIN_TRADES == 40
    assert scorecard.TRUE_WIN_FLOOR == 35.0


# --- rendering and robustness --------------------------------------------

def test_render_flags_modelled_fees():
    m = scorecard.metrics(_led(tps=1, estimated=True), now=NOW)
    assert "modelled fees" in scorecard.render(m)


def test_render_without_equity_or_benchmark_still_works():
    text = scorecard.render(scorecard.metrics(_led(tps=1), now=NOW))
    assert "VERDICT" in text


def test_empty_ledger_does_not_divide_by_zero():
    m = scorecard.metrics({"closed": []}, now=DUE)
    assert m["trades"] == 0 and m["true_win_pct"] == 0.0
    assert m["verdict"] == "COLLECTING"
