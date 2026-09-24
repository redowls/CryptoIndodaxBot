"""External cash movements, and the detector that finds the ones nobody recorded.

The bug these exist to prevent: a Rp500.823 deposit on 2026-09-23 read as +98%
profit, because every return was divided by a hardcoded day-one balance. The
worst failure mode is not a crash — it is a number that flatters.
"""
from datetime import datetime, timedelta, timezone

import pytest

from cryptoindodax import cashflow

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _at(hours):
    return (T0 + timedelta(hours=hours)).isoformat()


# --- the record -----------------------------------------------------------

def test_a_recorded_flow_survives_a_round_trip(tmp_path):
    path = tmp_path / "flows.json"
    cashflow.add(500_823, at=_at(10), note="top-up", path=path)
    flows = cashflow.load(path)
    assert [f["amount"] for f in flows] == [500_823.0]
    assert flows[0]["note"] == "top-up"


def test_flows_come_back_oldest_first_however_they_were_written(tmp_path):
    path = tmp_path / "flows.json"
    cashflow.add(200, at=_at(20), path=path)
    cashflow.add(100, at=_at(5), path=path)
    assert [f["amount"] for f in cashflow.load(path)] == [100.0, 200.0]


def test_a_missing_file_is_no_flows_not_an_error(tmp_path):
    assert cashflow.load(tmp_path / "nothing.json") == []


def test_a_withdrawal_is_a_negative_flow(tmp_path):
    path = tmp_path / "flows.json"
    cashflow.add(-150_000, at=_at(3), path=path)
    assert cashflow.total(cashflow.load(path)) == -150_000.0


def test_cash_contributed_counts_only_what_had_landed_by_then():
    flows = [{"at": _at(10), "amount": 500_000.0}]
    assert cashflow.net_at(flows, T0 + timedelta(hours=9)) == 0.0
    assert cashflow.net_at(flows, T0 + timedelta(hours=10)) == 500_000.0


def test_indonesian_thousand_separators_are_accepted_on_the_command_line():
    assert cashflow._amount("500.823") == 500_823.0
    assert cashflow._amount("500823") == 500_823.0
    assert cashflow._amount("-1.250.000") == -1_250_000.0


# --- the return -----------------------------------------------------------

def test_a_deposit_on_its_own_returns_nothing():
    """The whole point. Doubling the account by transfer is not a 100% gain."""
    curve = [{"equity": 500_000.0, "capital": 500_000.0},
             {"equity": 1_000_000.0, "capital": 1_000_000.0}]
    assert cashflow.twr(curve) == pytest.approx(0.0)


def test_the_return_chains_across_a_deposit_instead_of_diluting_it():
    """+10% on 100, then a 100 deposit, then +10% again -> 21%, not 15.5%."""
    curve = [{"equity": 100.0, "capital": 100.0},
             {"equity": 110.0, "capital": 100.0},
             {"equity": 210.0, "capital": 200.0},
             {"equity": 231.0, "capital": 200.0}]
    assert cashflow.twr(curve) == pytest.approx(21.0)
    # the money-weighted view of the same account disagrees, and should
    assert (231.0 - 200.0) / 200.0 * 100 == pytest.approx(15.5)


def test_the_series_ends_where_the_headline_figure_does():
    curve = [{"equity": 100.0, "capital": 100.0},
             {"equity": 110.0, "capital": 100.0},
             {"equity": 210.0, "capital": 200.0}]
    series = cashflow.twr_series(curve)
    assert len(series) == len(curve)
    assert series[-1] == pytest.approx(cashflow.twr(curve))


def test_a_curve_with_nothing_on_it_returns_zero_not_a_crash():
    assert cashflow.twr([]) == 0.0
    assert cashflow.twr([{"equity": 1.0, "capital": 1.0}]) == 0.0


# --- the detector ---------------------------------------------------------

def _obs(cash_by_hour):
    return [(T0 + timedelta(hours=h), 0.0, cash, 0) for h, cash in cash_by_hour]


def test_cash_that_appears_with_no_trade_behind_it_is_flagged():
    gaps = cashflow.unexplained(_obs([(0, 69_673.0), (1, 570_496.0)]), [],
                                {"open": [], "closed": []})
    assert len(gaps) == 1
    assert gaps[0]["residual"] == pytest.approx(500_823.0)


def test_recording_the_transfer_silences_the_warning():
    obs = _obs([(0, 69_673.0), (1, 570_496.0)])
    flows = [{"at": _at(1), "amount": 500_823.0}]
    assert cashflow.unexplained(obs, flows, {"open": [], "closed": []}) == []


def test_a_sell_that_explains_the_cash_is_not_mistaken_for_a_deposit():
    """Otherwise the fix would erase real profit by relabelling it as capital."""
    led = {"open": [], "closed": [{"symbol": "DOT", "qty": 10.0, "entry_price": 100.0,
                                   "exit_price": 50_000.0, "fees": 0.0,
                                   "entry_time": _at(-5), "exit_time": _at(1)}]}
    obs = _obs([(0, 69_673.0), (1, 569_673.0)])
    assert cashflow.unexplained(obs, [], led) == []


def test_ordinary_fill_slippage_stays_below_the_alarm():
    gaps = cashflow.unexplained(_obs([(0, 100_000.0), (1, 101_000.0)]), [],
                                {"open": [], "closed": []})
    assert gaps == []


def test_the_trader_log_is_read_back_as_observations(tmp_path):
    log = tmp_path / "trader.log"
    log.write_text(
        "2026-09-23T01:14:02+00:00 account equity Rp528.623 (cash Rp69.673), 5 coin positions\n"
        "2026-09-23T01:14:02+00:00 regime: computed risk_on\n"
        "2026-09-23T02:14:05+00:00 account equity Rp1.027.379 (cash Rp570.496), 5 coin positions\n")
    obs = cashflow.observations(log)
    assert [o[2] for o in obs] == [69_673.0, 570_496.0]
    assert obs[0][1] == 528_623.0


def test_a_missing_log_is_no_observations_not_an_error(tmp_path):
    assert cashflow.observations(tmp_path / "nope.log") == []
