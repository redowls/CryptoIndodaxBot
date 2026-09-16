from cryptoindodax import regime_lab, replay, strategy


def _tf(close=100.0, ema8=110, ema20=105, ema55=100, rsi=60, adx=30, atr=2.0):
    return {"status": "ok", "last_close": close, "ema8": ema8, "ema20": ema20,
            "ema55": ema55, "rsi14": rsi, "adx14": adx, "atr14": atr}


def _snap(**per_coin):
    """_snap(BTC=dict(...), ETH=dict(...)) -> a snapshot shaped like the real ones."""
    return {"captured_at": "2026-09-10T01:00:00+00:00",
            "symbols": [{"symbol": s, "status": "ok",
                         "timeframes": {"1H": _tf(), "4H": _tf(), "1D": _tf(**kw)}}
                        for s, kw in per_coin.items()]}


# --- BTC-only gates -------------------------------------------------------

def test_current_matches_live_strategy_exactly():
    """Scored on identical footing or the comparison is meaningless."""
    snap = _snap(BTC=dict(ema8=110, ema20=105, ema55=100, adx=40))
    assert regime_lab.Current()(snap) == strategy.regime(snap)


def test_rsi_gate_goes_defensive_before_the_stack_inverts():
    """The whole point: a falling market whose EMA stack is still UP."""
    falling = _snap(BTC=dict(ema8=110, ema20=105, ema55=100, adx=40, rsi=45))
    assert strategy.regime(falling) == "risk_on"      # live gate sees nothing
    assert regime_lab.RsiGate()(falling) == "risk_off"


def test_rsi_gate_has_a_cautious_band():
    snap = _snap(BTC=dict(ema8=110, ema20=105, ema55=100, adx=40, rsi=52))
    assert regime_lab.RsiGate()(snap) == "neutral"


def test_ema20_gate_fires_on_close_below_ema20():
    below = _snap(BTC=dict(close=100, ema8=110, ema20=105, ema55=100, adx=40))
    above = _snap(BTC=dict(close=120, ema8=110, ema20=105, ema55=100, adx=40))
    assert regime_lab.Ema20Gate()(below) == "risk_off"
    assert regime_lab.Ema20Gate()(above) == "risk_on"


def test_confirmed_gate_ignores_a_single_bar_dip():
    """Hysteresis is the point — one bar below EMA20 must not flip the regime."""
    g = regime_lab.Ema20Confirmed(bars=3)
    below = _snap(BTC=dict(close=100, ema8=110, ema20=105, ema55=100, adx=40))
    assert g(below) == "risk_on"
    assert g(below) == "risk_on"
    assert g(below) == "risk_off"      # third consecutive bar confirms


def test_confirmed_gate_needs_confirmation_to_turn_back_on():
    g = regime_lab.Ema20Confirmed(bars=2)
    below = _snap(BTC=dict(close=100, ema8=110, ema20=105, ema55=100, adx=40))
    above = _snap(BTC=dict(close=120, ema8=110, ema20=105, ema55=100, adx=40))
    g(below); g(below)
    assert g(above) == "risk_off"      # one bar back above is not enough
    assert g(above) == "risk_on"


# --- breadth --------------------------------------------------------------

def test_fast_breadth_counts_coins_above_their_own_ema20():
    snap = _snap(BTC=dict(close=120, ema20=105), ETH=dict(close=100, ema20=105),
                 SOL=dict(close=100, ema20=105), DOT=dict(close=100, ema20=105),
                 UNI=dict(close=100, ema20=105))
    assert regime_lab.BreadthFast()._frac(snap) == 0.2
    assert regime_lab.BreadthFast()(snap) == "risk_off"


def test_fast_breadth_is_not_fooled_by_stale_ema_stacks():
    """BreadthGate (stack ordering) and BreadthFast (price) must disagree when
    price has broken down but the stack has not yet inverted — that gap is the
    entire failure this module was written to study."""
    broken = _snap(**{s: dict(close=100, ema8=110, ema20=105, ema55=100, adx=40)
                      for s in ("BTC", "ETH", "SOL", "DOT", "UNI")})
    assert regime_lab.BreadthGate()(broken) == "risk_on"
    assert regime_lab.BreadthFast()(broken) == "risk_off"


def test_breadth_with_no_usable_data_is_neutral_not_confident():
    assert regime_lab.BreadthFast()({"symbols": []}) == "neutral"
    assert regime_lab.BreadthRsi()({"symbols": []}) == "neutral"


# --- combinators and control ---------------------------------------------

def test_union_gate_takes_the_more_defensive_of_the_two():
    snap = _snap(BTC=dict(close=120, ema8=110, ema20=105, ema55=100, adx=40, rsi=45))
    assert regime_lab.Ema20Gate()(snap) == "risk_on"     # price still above EMA20
    assert regime_lab.RsiGate()(snap) == "risk_off"      # momentum gone
    assert regime_lab.RsiOrEma20()(snap) == "risk_off"


def test_control_is_unconditional():
    assert regime_lab.AlwaysRiskOff()(_snap(BTC=dict())) == "risk_off"


def test_missing_btc_degrades_to_neutral_never_risk_on():
    empty = {"symbols": []}
    for gate in (regime_lab.RsiGate(), regime_lab.Ema20Gate(),
                 regime_lab.Ema20Confirmed(3)):
        assert gate(empty) == "neutral"


def test_variants_are_fresh_instances_each_call():
    """Stateful gates must not leak hysteresis between replay runs."""
    a = [v for v in regime_lab.variants() if isinstance(v, regime_lab.Ema20Confirmed)][0]
    b = [v for v in regime_lab.variants() if isinstance(v, regime_lab.Ema20Confirmed)][0]
    assert a is not b and a.below == 0 and b.below == 0


# --- harness integration --------------------------------------------------

def test_replay_accepts_a_custom_gate_and_it_changes_behaviour():
    """A gate that is always risk_off must produce a different run than the
    live gate on the same history — otherwise the injection is not wired."""
    history = replay.load_history()
    if len(history) < 24:
        return                      # no stored snapshots in this environment
    live = replay.summarize(replay.run(history, regime_fn=regime_lab.Current()))
    off = replay.summarize(replay.run(history, regime_fn=regime_lab.AlwaysRiskOff()))
    assert live["trades"] != off["trades"] or live["net_idr"] != off["net_idr"]


def test_replay_defaults_to_the_live_gate():
    history = replay.load_history()
    if len(history) < 24:
        return
    a = replay.summarize(replay.run(history))
    b = replay.summarize(replay.run(history, regime_fn=regime_lab.Current()))
    assert a["net_idr"] == b["net_idr"] and a["trades"] == b["trades"]
