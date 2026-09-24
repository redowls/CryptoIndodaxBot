from datetime import datetime, timedelta, timezone

from cryptoindodax import dashboard

START = datetime(2026, 9, 1, tzinfo=timezone.utc)
NOW = datetime(2026, 9, 5, tzinfo=timezone.utc)


def _hours(n, prices):
    """n hourly snapshots; `prices` is {symbol: [close per hour]}."""
    out = []
    for i in range(n):
        syms = [{"symbol": s, "timeframes": {"1H": {"status": "ok", "last_close": p[i]}}}
                for s, p in prices.items()]
        out.append((START + timedelta(hours=i), {"symbols": syms}))
    return out


def _closed(symbol="BTC", reason="tp", pnl=1000.0, gross=None, fees=100.0,
            entry=100.0, exit_=110.0, qty=10.0, hour_in=0, hour_out=2, r=1.5):
    return {"symbol": symbol, "reason": reason, "pnl": pnl,
            "pnl_gross": gross if gross is not None else pnl + fees, "fees": fees,
            "fees_estimated": False, "qty": qty, "entry_price": entry, "exit_price": exit_,
            "entry_time": (START + timedelta(hours=hour_in)).isoformat(),
            "exit_time": (START + timedelta(hours=hour_out)).isoformat(),
            "r_multiple": r, "peak_r": r}


def _open(symbol="ETH", qty=2.0, entry=1000.0, hour_in=0, stop=900.0, fee=50.0):
    return {"symbol": symbol, "qty": qty, "entry_price": entry, "initial_stop": stop,
            "stop": stop, "high_water": entry, "entry_fee": fee,
            "entry_time": (START + timedelta(hours=hour_in)).isoformat()}


# --- the equity curve -----------------------------------------------------

def test_curve_opens_at_the_starting_equity():
    curve = dashboard.equity_curve(_hours(3, {"BTC": [100, 100, 100]}),
                                   {"open": [], "closed": []}, start_equity=500_000.0)
    assert curve[0]["equity"] == 500_000.0


def test_realised_pnl_lands_on_the_hour_the_trade_closed_not_the_hour_it_opened():
    """A curve that books profit at entry would show a rise that never happened."""
    led = {"open": [], "closed": [_closed(pnl=1000.0, hour_in=0, hour_out=2)]}
    curve = dashboard.equity_curve(_hours(4, {"BTC": [100, 100, 100, 100]}), led,
                                   start_equity=500_000.0)
    by_hour = [p["equity"] for p in curve]
    assert by_hour[1] == 500_000.0          # still open, price flat -> no change
    assert by_hour[3] == 501_000.0          # closed at hour 2


def test_an_open_position_is_marked_to_the_snapshot_close():
    led = {"open": [_open(symbol="ETH", qty=2.0, entry=1000.0, fee=0.0)], "closed": []}
    curve = dashboard.equity_curve(_hours(3, {"ETH": [1000, 1100, 1200]}), led,
                                   start_equity=500_000.0)
    assert curve[1]["equity"] == 500_200.0   # 2 * (1100 - 1000)
    assert curve[2]["equity"] == 500_400.0


def test_a_position_is_not_marked_before_it_was_opened():
    led = {"open": [_open(symbol="ETH", qty=2.0, entry=1000.0, hour_in=2, fee=0.0)],
           "closed": []}
    curve = dashboard.equity_curve(_hours(4, {"ETH": [1000, 5000, 1000, 1000]}), led,
                                   start_equity=500_000.0)
    assert curve[1]["equity"] == 500_000.0   # the 5000 spike predates the entry


def test_the_entry_fee_is_charged_while_the_position_is_open():
    led = {"open": [_open(qty=2.0, entry=1000.0, fee=50.0)], "closed": []}
    curve = dashboard.equity_curve(_hours(2, {"ETH": [1000, 1000]}), led,
                                   start_equity=500_000.0)
    assert curve[0]["equity"] == 499_950.0


def test_a_coin_missing_from_a_snapshot_carries_its_last_known_close():
    history = _hours(3, {"ETH": [1000, 1100, 1200]})
    history[1][1]["symbols"] = []            # the hour Indodax returned nothing
    led = {"open": [_open(qty=2.0, entry=1000.0, fee=0.0)], "closed": []}
    curve = dashboard.equity_curve(history, led, start_equity=500_000.0)
    assert curve[1]["equity"] == 500_000.0   # held at the hour-0 close, not dropped


def test_exposure_is_reported_per_point():
    led = {"open": [_open(qty=2.0, entry=1000.0, fee=0.0)], "closed": []}
    curve = dashboard.equity_curve(_hours(2, {"ETH": [1000, 1000]}), led,
                                   start_equity=500_000.0)
    assert curve[0]["invested"] == 2000.0
    assert curve[0]["positions"] == 1


# --- the benchmark --------------------------------------------------------

def test_benchmark_is_an_equal_weight_hold_of_the_opening_snapshot():
    history = _hours(2, {"BTC": [100, 110], "ETH": [200, 200]})   # +10% and 0%
    curve = dashboard.equity_curve(history, {"open": [], "closed": []},
                                   start_equity=500_000.0, watchlist=["BTC", "ETH"])
    assert curve[1]["benchmark"] == 525_000.0                      # mean +5%


def test_a_coin_added_after_the_window_opened_is_left_out_of_the_benchmark():
    """Otherwise a late arrival contributes a few days of return to a 20-day
    comparison and quietly flatters or punishes it."""
    history = _hours(2, {"BTC": [100, 110], "PEPE": [0, 0]})
    history[0][1]["symbols"] = [s for s in history[0][1]["symbols"] if s["symbol"] != "PEPE"]
    history[1][1]["symbols"] = [{"symbol": "PEPE",
                                 "timeframes": {"1H": {"status": "ok", "last_close": 999.0}}},
                                *history[1][1]["symbols"]]
    curve = dashboard.equity_curve(history, {"open": [], "closed": []},
                                   start_equity=500_000.0, watchlist=["BTC", "PEPE"])
    assert curve[1]["benchmark"] == 550_000.0                      # BTC's +10% alone


# --- per-asset rollup: the standing stop doctrine -------------------------

def test_a_profitable_stop_is_still_a_failed_trade_for_its_asset():
    led = {"open": [], "closed": [_closed("BTC", "stop", pnl=500.0),
                                  _closed("BTC", "tp", pnl=100.0)]}
    btc = dashboard.per_asset(led, {})[0]
    assert btc["trades"] == 2
    assert btc["wins"] == 1
    assert btc["true_win_pct"] == 50.0
    assert btc["stop_pct"] == 50.0
    assert btc["net"] == 600.0


def test_a_lock_exit_counts_as_a_win_only_at_or_above_1r():
    led = {"open": [], "closed": [_closed("SOL", "lock", r=1.7),
                                  _closed("SOL", "lock", r=0.6)]}
    sol = dashboard.per_asset(led, {})[0]
    assert sol["wins"] == 1
    assert sol["locks"] == 2


def test_per_asset_net_reconciles_with_the_scorecard():
    from cryptoindodax import scorecard
    led = {"open": [], "closed": [_closed("BTC", "tp", pnl=1000.0),
                                  _closed("ETH", "stop", pnl=-400.0),
                                  _closed("BTC", "stop", pnl=-100.0)]}
    assert (round(sum(a["net"] for a in dashboard.per_asset(led, {})), 2)
            == scorecard.metrics(led, now=NOW)["net_realised"])


def test_assets_are_ordered_by_net_contribution():
    led = {"open": [], "closed": [_closed("BTC", "stop", pnl=-900.0),
                                  _closed("ETH", "tp", pnl=1200.0),
                                  _closed("SOL", "tp", pnl=300.0)]}
    assert [a["symbol"] for a in dashboard.per_asset(led, {})] == ["ETH", "SOL", "BTC"]


def test_an_asset_with_only_an_open_position_still_appears():
    led = {"open": [_open("UNI", qty=4.0, entry=100.0, fee=0.0)], "closed": []}
    uni = dashboard.per_asset(led, {"UNI": 120.0})[0]
    assert uni["trades"] == 0
    assert uni["open"] is True
    assert uni["unrealised"] == 80.0


def test_cost_basis_gives_the_percent_view_its_own_denominator():
    """Percent-only mode must not divide a coin's result by account equity — a
    Rp90.000 position doubling is not a 0.2% result."""
    led = {"open": [], "closed": [_closed("SOL", "tp", pnl=450.0, qty=10.0, entry=900.0)]}
    sol = dashboard.per_asset(led, {})[0]
    assert sol["cost_basis"] == 9000.0
    assert sol["contribution_pct"] == 5.0


def test_cost_basis_counts_closed_and_open_risk_for_the_same_coin():
    led = {"open": [_open("ETH", qty=1.0, entry=2000.0, fee=0.0)],
           "closed": [_closed("ETH", "tp", pnl=100.0, qty=2.0, entry=1000.0)]}
    assert dashboard.per_asset(led, {})[0]["cost_basis"] == 4000.0


def test_fee_drag_is_reported_against_gross():
    led = {"open": [], "closed": [_closed("BTC", "tp", pnl=900.0, gross=1000.0, fees=100.0)]}
    assert dashboard.per_asset(led, {})[0]["fee_drag_pct"] == 10.0


# --- open positions -------------------------------------------------------

def test_an_open_position_without_a_mark_reports_no_pnl_rather_than_a_fake_one():
    """Marking an unpriced coin at its entry invents a break-even that is not real."""
    view = dashboard.open_positions(({"open": [_open("MOG", qty=1.0, entry=10.0)],
                                      "closed": []}), {}, now=NOW)
    assert view[0]["price"] is None
    assert view[0]["unrealised"] is None
    assert view[0]["unrealised_pct"] is None


def test_open_position_reports_distance_to_stop_and_r_so_far():
    led = {"open": [_open("ETH", qty=2.0, entry=1000.0, stop=900.0, fee=0.0)], "closed": []}
    pos = dashboard.open_positions(led, {"ETH": 1050.0}, now=NOW)[0]
    assert pos["unrealised"] == 100.0
    assert pos["unrealised_pct"] == 5.0
    assert pos["r_so_far"] == 0.5                 # 50 of a 100-wide R
    assert round(pos["stop_distance_pct"], 2) == 14.29


# --- the whole document ---------------------------------------------------

def test_build_publishes_the_drift_between_the_reconstruction_and_the_live_account():
    """The curve rests on 'no deposits or withdrawals'. When that rots, the page
    has to say so rather than keep drawing a confident line."""
    led = {"open": [], "closed": [_closed("BTC", "tp", pnl=1000.0)]}
    doc = dashboard.build(_hours(3, {"BTC": [100, 100, 100]}), led,
                          live_equity=520_000.0, start_equity=500_000.0, now=NOW)
    assert doc["totals"]["reconstructed_equity"] == 501_000.0
    assert doc["totals"]["live_equity"] == 520_000.0
    assert doc["totals"]["drift"] == 19_000.0


def test_build_survives_an_empty_ledger_and_an_empty_archive():
    doc = dashboard.build([], {"open": [], "closed": []}, start_equity=500_000.0, now=NOW)
    assert doc["curve"] == []
    assert doc["assets"] == []
    assert doc["totals"]["trades"] == 0


def test_build_carries_the_modelled_fee_caveat_forward():
    led = {"open": [], "closed": [_closed("BTC", "tp"),
                                  dict(_closed("ETH", "stop", pnl=-100.0),
                                       fees_estimated=True)]}
    doc = dashboard.build([], led, start_equity=500_000.0, now=NOW)
    assert doc["totals"]["fees_estimated_trades"] == 1


def test_build_reports_the_scorecard_verdict_unchanged():
    from cryptoindodax import scorecard
    led = {"open": [], "closed": [_closed("BTC", "stop", pnl=-100.0)]}
    doc = dashboard.build([], led, start_equity=500_000.0, now=NOW)
    assert doc["scorecard"]["verdict"] == scorecard.metrics(led, now=NOW)["verdict"]
    assert doc["scorecard"]["true_win_pct"] == 0.0


def test_daily_pnl_buckets_realised_trades_by_exit_day():
    led = {"open": [], "closed": [_closed("BTC", "tp", pnl=500.0, hour_out=2),
                                  _closed("ETH", "tp", pnl=300.0, hour_out=5),
                                  _closed("SOL", "stop", pnl=-200.0, hour_out=30)]}
    days = dashboard.daily_pnl(led)
    from cryptoindodax import scorecard
    cap = scorecard.START_EQUITY
    assert days == [{"date": "2026-09-01", "net": 800.0, "trades": 2, "capital": cap},
                    {"date": "2026-09-02", "net": -200.0, "trades": 1, "capital": cap}]


# --- deposits -------------------------------------------------------------
#
# A Rp500.823 top-up on 2026-09-23 read as +98% profit on an account that was
# down, because live equity was divided by a hardcoded day-one balance. These
# pin the shape of the fix: cash in raises the base, never the return.

def _flow(hour, amount=500_000.0):
    return [{"at": (START + timedelta(hours=hour)).isoformat(), "amount": amount}]


def test_a_deposit_raises_the_base_and_returns_nothing():
    curve = dashboard.equity_curve(_hours(4, {"BTC": [100, 100, 100, 100]}),
                                   {"open": [], "closed": []},
                                   start_equity=500_000.0, flows=_flow(2))
    assert [p["capital"] for p in curve] == [500_000.0, 500_000.0, 1_000_000.0, 1_000_000.0]
    assert [p["pnl"] for p in curve] == [0.0, 0.0, 0.0, 0.0]
    assert curve[-1]["equity"] == 1_000_000.0
    assert curve[-1]["twr_pct"] == 0.0


def test_the_benchmark_takes_the_same_cash_at_the_same_hour():
    """An equal-weight holder topped up too. Comparing against one who did not
    would hand the bot a 500k head start it never earned."""
    curve = dashboard.equity_curve(_hours(4, {"BTC": [100, 100, 100, 100]}),
                                   {"open": [], "closed": []},
                                   start_equity=500_000.0, flows=_flow(2))
    assert curve[-1]["benchmark"] == 1_000_000.0
    assert curve[-1]["benchmark_pct"] == 0.0


def test_a_deposit_is_never_reported_as_profit():
    doc = dashboard.build(_hours(4, {"BTC": [100, 100, 100, 100]}),
                          {"open": [], "closed": []},
                          live_equity=990_000.0, start_equity=500_000.0,
                          now=NOW, flows=_flow(2))
    t = doc["totals"]
    assert t["deposits"] == 500_000.0
    assert t["invested_capital"] == 1_000_000.0
    assert t["net_pnl"] == -10_000.0
    assert t["return_pct"] == -1.0          # the old arithmetic said +98.0


def test_drift_measures_the_ledger_against_the_account_not_the_deposit():
    """Drift is the reconciliation alarm. A top-up must not set it off."""
    doc = dashboard.build(_hours(4, {"BTC": [100, 100, 100, 100]}),
                          {"open": [], "closed": []},
                          live_equity=990_000.0, start_equity=500_000.0,
                          now=NOW, flows=_flow(2))
    assert doc["totals"]["drift"] == -10_000.0


def test_the_time_weighted_figure_is_anchored_to_the_account_not_the_ledger():
    """The ledger overstates by Rp21.994 of unrecorded slippage. Left alone it
    would publish profit that was never in the account."""
    led = {"open": [], "closed": [_closed("BTC", "tp", pnl=20_000.0, hour_in=0, hour_out=1)]}
    doc = dashboard.build(_hours(4, {"BTC": [100, 100, 100, 100]}), led,
                          live_equity=1_000_000.0, start_equity=500_000.0,
                          now=NOW, flows=_flow(2))
    assert doc["totals"]["reconstructed_equity"] == 1_020_000.0
    assert doc["totals"]["twr_pct"] == 1.96      # 4.0 if it trusted the ledger


def test_an_unrecorded_cash_move_is_published_rather_than_averaged_in():
    gap = {"from": "x", "to": "y", "moved": 1.0, "explained": 0.0, "residual": 1.0}
    doc = dashboard.build(_hours(2, {"BTC": [100, 100]}), {"open": [], "closed": []},
                          start_equity=500_000.0, now=NOW, flows=[], unrecorded=[gap])
    assert doc["meta"]["unrecorded_cashflows"] == [gap]
