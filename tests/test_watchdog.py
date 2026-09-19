"""The fast exit watcher, its lock, and the frozen level check.

The defining property under test is what the watcher must NOT do: it may notice
a breach of a level the hourly cycle set, and it may never move that level.
"""
import json

import pytest

from cryptoindodax import config, ledger, lock, strategy, trader, watchdog


def _pos(symbol="PEPE", entry=0.064, initial=0.062, stop=0.0652, lock_level=0.06682,
         hw=0.06762):
    return {"symbol": symbol, "qty": 1_000_000.0, "entry_price": entry,
            "initial_stop": initial, "stop": stop, "lock": lock_level,
            "high_water": hw, "entry_time": "2026-09-18T01:14:30+00:00",
            "atr_at_entry": 0.0006, "order_id": "1"}


# --- the frozen level check ----------------------------------------------

def test_check_levels_never_mutates_the_position():
    """The entire reason this function exists instead of reusing check_exit."""
    pos = _pos()
    before = json.dumps(pos, sort_keys=True)
    for price in (0.05, 0.064, 0.0668, 0.09):
        strategy.check_levels(pos, price)
    assert json.dumps(pos, sort_keys=True) == before


def test_check_levels_orders_stop_before_lock():
    """A price under both is the stop it is, not a flattering lock exit."""
    assert strategy.check_levels(_pos(), 0.0651) == "stop"
    assert strategy.check_levels(_pos(), 0.0668) == "lock"


def test_check_levels_take_profit():
    pos = _pos(entry=100.0, initial=94.0, stop=94.0, lock_level=None, hw=100.0)
    assert strategy.check_levels(pos, 115.5) == "tp"      # +2.5R
    assert strategy.check_levels(pos, 114.9) is None


def test_check_levels_holds_between_the_levels():
    assert strategy.check_levels(_pos(), 0.0670) is None


def test_check_levels_ignores_a_missing_lock():
    pos = _pos(lock_level=None)
    assert strategy.check_levels(pos, 0.0668) is None


def test_check_levels_has_no_time_stop():
    """A clock is not a price; the hourly cycle owns TIME_STOP_HOURS."""
    assert strategy.check_levels(_pos(), 0.0670) is None


def test_check_levels_rejects_an_unusable_price():
    for bad in (0, None, -1.0):
        assert strategy.check_levels(_pos(), bad) is None


def test_check_exit_still_agrees_with_check_levels():
    """check_exit delegates its comparisons here, so they cannot drift."""
    h1 = {"status": "ok", "last_close": 0.0651, "atr14": 0.0006}
    action, _ = strategy.check_exit(_pos(), h1, "risk_on", 5)
    assert action == "stop" == strategy.check_levels(_pos(), 0.0651)


# --- the lock -------------------------------------------------------------

def test_lock_is_exclusive_and_released(tmp_path):
    path = tmp_path / "t.lock"
    with lock.held(path=path) as first:
        assert first
        with lock.held(path=path) as second:
            assert second is False, "a second holder must not get the lock"
    with lock.held(path=path) as again:
        assert again, "the lock must be free once the holder exits"


def test_lock_waiting_gives_up_without_raising(tmp_path):
    path = tmp_path / "t.lock"
    slept = []
    with lock.held(path=path):
        with lock.held(path=path, wait_s=0.01, poll_s=0.001,
                       sleep=slept.append) as got:
            assert got is False
    assert slept, "a waiting caller should have polled at least once"


# --- the watcher ----------------------------------------------------------

@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TRADES_DIR", tmp_path)
    monkeypatch.setattr(lock, "LOCK_PATH", tmp_path / "t.lock")
    monkeypatch.setattr(config, "TRADING_ENABLED", True)
    calls = {"tickers": 0, "exits": [], "saved": 0}

    def fake_exit(led, pos, price, reason, dry_run, by_sym):
        calls["exits"].append((pos["symbol"], price, reason))
        led["open"] = [p for p in led["open"] if p["symbol"] != pos["symbol"]]

    monkeypatch.setattr(trader, "_exit_position", fake_exit)
    monkeypatch.setattr(watchdog.broker, "get_positions", lambda **kw: [])
    monkeypatch.setattr(ledger, "save", lambda led, path=None: calls.__setitem__(
        "saved", calls["saved"] + 1))
    return calls


def _tickers(monkeypatch, calls, price):
    def fake(session=None):
        calls["tickers"] += 1
        return {config.pair_id("PEPE"): {"last": price, "buy": price, "sell": price}}
    monkeypatch.setattr(watchdog.data, "fetch_tickers", fake)


def _write_ledger(tmp_path, positions):
    (tmp_path / "trades.json").write_text(json.dumps({"open": positions, "closed": []}))


def test_empty_book_costs_nothing(tmp_path, monkeypatch, env):
    _write_ledger(tmp_path, [])
    _tickers(monkeypatch, env, 0.0668)
    watchdog.run()
    assert env["tickers"] == 0, "no position means no HTTP call at all"


def test_a_breach_exits_at_the_live_price(tmp_path, monkeypatch, env):
    _write_ledger(tmp_path, [_pos()])
    _tickers(monkeypatch, env, 0.06655)          # under the 0,06682 lock
    watchdog.run()
    assert env["exits"] == [("PEPE", 0.06655, "lock")]
    assert env["saved"] == 1


def test_a_price_between_levels_holds(tmp_path, monkeypatch, env):
    _write_ledger(tmp_path, [_pos()])
    _tickers(monkeypatch, env, 0.0670)
    watchdog.run()
    assert env["exits"] == [] and env["saved"] == 0


def test_the_watcher_never_moves_a_level(tmp_path, monkeypatch, env):
    """A new high between hourly cycles must NOT ratchet the lock — that is the
    strategy change this module exists to avoid making."""
    _write_ledger(tmp_path, [_pos()])
    _tickers(monkeypatch, env, 0.0900)           # far above the peak
    watchdog.run()
    after = json.loads((tmp_path / "trades.json").read_text())["open"][0]
    assert after["high_water"] == 0.06762
    assert after["lock"] == 0.06682
    assert after["stop"] == 0.0652


def test_it_skips_while_the_hourly_trader_holds_the_lock(tmp_path, monkeypatch, env):
    _write_ledger(tmp_path, [_pos()])
    _tickers(monkeypatch, env, 0.06655)
    with lock.held(path=lock.LOCK_PATH):
        watchdog.run()
    assert env["exits"] == [], "must not sell under the hourly cycle"
    assert env["tickers"] == 0


def test_a_coin_without_a_live_price_is_left_alone(tmp_path, monkeypatch, env):
    _write_ledger(tmp_path, [_pos(symbol="MOG")])
    _tickers(monkeypatch, env, 0.06655)          # only PEPE is in the feed
    watchdog.run()
    assert env["exits"] == []


def test_a_ticker_failure_is_not_an_exit(tmp_path, monkeypatch, env):
    _write_ledger(tmp_path, [_pos()])

    def boom(session=None):
        raise watchdog.data.FetchError("down")
    monkeypatch.setattr(watchdog.data, "fetch_tickers", boom)
    watchdog.run()
    assert env["exits"] == [] and env["saved"] == 0


def test_trading_disabled_does_nothing(tmp_path, monkeypatch, env):
    monkeypatch.setattr(config, "TRADING_ENABLED", False)
    _write_ledger(tmp_path, [_pos()])
    _tickers(monkeypatch, env, 0.06655)
    watchdog.run()
    assert env["tickers"] == 0 and env["exits"] == []


def test_dry_run_reports_without_selling(tmp_path, monkeypatch, env):
    _write_ledger(tmp_path, [_pos()])
    _tickers(monkeypatch, env, 0.06655)
    watchdog.run(dry_run=True)
    assert env["exits"] == [("PEPE", 0.06655, "lock")]
    assert env["saved"] == 0, "dry run must not persist the ledger"


# --- sub-rupiah formatting ------------------------------------------------

def test_prices_below_one_rupiah_keep_their_decimals():
    """fmt_idr rendered every PEPE level as 'Rp0', which made the lock and
    trail log lines useless on exactly the coins that needed them."""
    assert config.fmt_idr(0.064) == "Rp0"
    assert config.fmt_price(0.064) == "Rp0,064"
    assert config.fmt_price(0.0668227180317669) == "Rp0,06682272"
    assert config.fmt_price(121407) == "Rp121.407"
    assert config.fmt_price(None) == "-"
