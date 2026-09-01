import pytest

from cryptoindodax import pairs

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
