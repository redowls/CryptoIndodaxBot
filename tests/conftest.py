import pytest

from cryptoindodax import pairs


@pytest.fixture(autouse=True)
def _no_network_pair_metadata(monkeypatch, request):
    """Keep pair metadata off the network for every test.

    `pairs.round_qty`/`meets_minimums` sit on the sizing path, so without this
    the suite would hit https://indodax.com/api/pairs. test_pairs.py drives the
    cache logic itself and opts out.
    """
    if request.node.fspath.basename == "test_pairs.py":
        return
    permissive = {"trade_min_base_currency": 10_000, "trade_min_traded_currency": 0.0,
                  "price_round": 8, "trade_fee_percent_taker": 0.2}
    monkeypatch.setattr(pairs, "load",
                        lambda *a, **k: {"btc_idr": permissive, "eth_idr": permissive,
                                         "sol_idr": permissive, "doge_idr": permissive,
                                         "xrp_idr": permissive})
