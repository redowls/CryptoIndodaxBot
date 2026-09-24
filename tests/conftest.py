import pytest

from cryptoindodax import cashflow, pairs

PERMISSIVE = {"trade_min_base_currency": 10_000, "trade_min_traded_currency": 0.0,
              "price_round": 8, "trade_fee_percent_taker": 0.2}


class _AnyPair(dict):
    """Pair metadata for any ticker_id, so tests don't break when the watchlist
    changes. Real per-pair constraints are exercised in test_pairs.py."""

    def get(self, key, default=None):
        return PERMISSIVE


@pytest.fixture(autouse=True)
def _no_network_pair_metadata(monkeypatch, request):
    """Keep pair metadata off the network for every test.

    `pairs.round_qty`/`meets_minimums` sit on the sizing path, so without this
    the suite would hit https://indodax.com/api/pairs. test_pairs.py drives the
    cache logic itself and opts out.
    """
    if request.node.fspath.basename == "test_pairs.py":
        return
    monkeypatch.setattr(pairs, "load", lambda *a, **k: _AnyPair())


@pytest.fixture(autouse=True)
def _isolate_cashflows(monkeypatch, tmp_path):
    """No test may read or write the live deposit record.

    `equity_curve`, `daily_pnl` and `scorecard.metrics` all fall back to
    `cashflow.load()` when no flows are passed, which is right in production and
    a trap in a test: the suite would silently price its fixtures against a real
    Rp500.823 top-up. Pointing the path at tmp makes the default "no flows"
    everywhere unless a test says otherwise.
    """
    monkeypatch.setattr(cashflow, "PATH", tmp_path / "cashflows.json")
