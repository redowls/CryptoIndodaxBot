"""The account report, and the price-less-equity trap it exists to close.

`broker.get_account()` ignores coins it has no mark for, so calling it bare
returns CASH as equity with no error. That silently under-reported the book in
three daily digests (2026-09-12, 2026-09-22, 2026-09-24 — Rp648.537 printed
against a true Rp998.017). These tests pin the property that matters: open
positions are counted, and when they cannot be, the module refuses rather than
flatters.
"""
from datetime import datetime, timezone

import pytest

from cryptoindodax import account

T0 = datetime(2026, 9, 24, 16, 0, tzinfo=timezone.utc)

BALANCES = {
    "IDR": {"free": 648_537.0, "locked": 0.0},
    "DOT": {"free": 2.3761734, "locked": 0.0},
    "LINK": {"free": 0.4476667, "locked": 0.0},
}


def _snap(closes):
    return {
        "symbols": [
            {"symbol": sym, "timeframes": {"1H": {"status": "ok", "last_close": px}}}
            for sym, px in closes.items()
        ]
    }


@pytest.fixture
def patched(monkeypatch):
    """Wire the module to fixed balances and one snapshot of marks."""

    def install(closes, balances=BALANCES):
        monkeypatch.setattr(
            account.replay, "load_history", lambda *a, **k: [(T0, _snap(closes))]
        )
        monkeypatch.setattr(
            account.broker, "get_balances", lambda *a, **k: balances
        )

    return install


def test_equity_counts_open_positions_not_just_cash(patched):
    patched({"DOT": 20_911.0, "LINK": 229_348.0})
    a = account.snapshot()

    assert a["cash"] == 648_537.0
    # The trap: equity must exceed cash by the value of the two open coins.
    assert a["coin_value"] == pytest.approx(2.3761734 * 20_911 + 0.4476667 * 229_348)
    assert a["equity"] == pytest.approx(a["cash"] + a["coin_value"])
    assert a["equity"] > a["cash"]


def test_coins_are_reported_largest_first(patched):
    patched({"DOT": 20_911.0, "LINK": 229_348.0})
    values = [value for _, _, value in account.snapshot()["coins"]]
    assert values == sorted(values, reverse=True)


def test_a_coin_with_no_mark_is_named_not_silently_dropped(patched):
    # LINK has no mark: the old failure was for it to vanish into thin air.
    patched({"DOT": 20_911.0})
    a = account.snapshot()

    assert [sym for sym, _ in a["unpriced"]] == ["LINK"]
    assert a["coin_value"] == pytest.approx(2.3761734 * 20_911)


def test_no_snapshot_history_refuses_rather_than_reporting_cash_as_equity(
    monkeypatch,
):
    monkeypatch.setattr(account.replay, "load_history", lambda *a, **k: [])
    monkeypatch.setattr(account.broker, "get_balances", lambda *a, **k: BALANCES)

    with pytest.raises(RuntimeError, match="cannot value open positions"):
        account.snapshot()


def test_an_all_cash_account_reports_equity_equal_to_cash(patched):
    patched({"DOT": 20_911.0}, balances={"IDR": {"free": 648_537.0, "locked": 0.0}})
    a = account.snapshot()

    # Equity == cash is only correct when there is genuinely nothing held.
    assert a["equity"] == a["cash"] == 648_537.0
    assert a["coins"] == [] and a["unpriced"] == []


def test_locked_balances_count_toward_equity(patched):
    patched(
        {"DOT": 20_000.0},
        balances={
            "IDR": {"free": 100.0, "locked": 50.0},
            "DOT": {"free": 1.0, "locked": 2.0},
        },
    )
    a = account.snapshot()

    assert a["cash"] == 150.0
    assert a["coin_value"] == pytest.approx(3.0 * 20_000)
