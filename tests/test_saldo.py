from datetime import datetime, timezone

from cryptoindodax import config, saldo


def _h(sym="AVAX", qty=2.0, price=100_000.0, entry=80_000.0):
    return {"symbol": sym, "qty": qty, "price": price, "entry_price": entry}


# --- formatting -----------------------------------------------------------

def test_pct_uses_indonesian_decimal_comma_and_sign():
    assert saldo.fmt_pct(1.5) == "+1,50%"
    assert saldo.fmt_pct(-0.554) == "-0,55%"
    assert saldo.fmt_pct(None) == "-"


def test_qty_trims_trailing_zeros():
    assert saldo.fmt_qty(0.92686270) == "0,9268627"
    assert saldo.fmt_qty(2.0) == "2"


# --- report ---------------------------------------------------------------

def test_report_totals_cash_coins_and_pnl():
    text = saldo.build_saldo_report(384_156.0, [_h()])
    assert "Saldo tersedia  : Rp384.156" in text
    assert "Nilai koin      : Rp200.000" in text
    assert "Total aset      : Rp584.156" in text
    assert "Untung/rugi     : Rp40.000 (+25,00%)" in text


def test_report_shows_per_coin_detail():
    text = saldo.build_saldo_report(0.0, [_h(qty=2.0, price=100_000, entry=80_000)])
    assert "Qty        : 2" in text
    assert "Harga beli : Rp80.000" in text
    assert "Nilai beli : Rp160.000" in text
    assert "Harga kini : Rp100.000" in text
    assert "Nilai kini : Rp200.000" in text
    assert "Untung/rugi: Rp40.000 (+25,00%)" in text


def test_report_handles_a_loss():
    text = saldo.build_saldo_report(0.0, [_h(qty=1.0, price=90_000, entry=100_000)])
    assert "-Rp10.000 (-10,00%)" in text


def test_report_with_no_holdings():
    text = saldo.build_saldo_report(500_000.0, [])
    assert "Tidak ada koin yang dipegang" in text
    assert "Total aset      : Rp500.000" in text


def test_untracked_coin_is_shown_but_excluded_from_pnl():
    """A balance the ledger never bought has no honest cost basis, so it must
    not be marked at its current price and silently reported as break-even."""
    holdings = [_h(sym="UNI", qty=1.0, price=100_000, entry=50_000),
                {"symbol": "SHIB", "qty": 5.0, "price": 10_000, "entry_price": None}]
    text = saldo.build_saldo_report(0.0, holdings)
    assert "SHIB" in text and "tidak tercatat" in text
    assert "Nilai koin      : Rp150.000" in text      # both coins counted in value
    assert "Untung/rugi     : Rp50.000 (+100,00%)" in text  # only the tracked one
    assert "tidak ikut dihitung" in text


def test_report_survives_a_coin_with_no_price():
    text = saldo.build_saldo_report(0.0, [{"symbol": "DOT", "qty": 3.0,
                                           "price": None, "entry_price": None}])
    assert "harga tidak tersedia" in text


# --- holdings join --------------------------------------------------------

def test_collect_holdings_joins_prices_and_ledger_entries():
    balances = {"IDR": {"free": 100.0, "locked": 0.0},
                "AVAX": {"free": 1.0, "locked": 0.5}}
    tickers = {"avax_idr": {"last": 100_000.0, "buy": 99_000.0, "sell": 101_000.0}}
    out = saldo.collect_holdings(balances, tickers,
                                 [{"symbol": "AVAX", "entry_price": 90_000.0}])
    assert len(out) == 1
    assert out[0]["qty"] == 1.5                      # free + locked
    assert out[0]["price"] == 100_000.0
    assert out[0]["entry_price"] == 90_000.0


def test_collect_holdings_reports_coins_outside_the_watchlist():
    """Hiding an asset is worse than admitting there is no cost basis for it."""
    off = next(s for s in ("SHIB", "TRX", "ADA") if s not in config.WATCHLIST)
    out = saldo.collect_holdings({off: {"free": 1000.0, "locked": 0.0}},
                                 {config.pair_id(off): {"last": 500.0}}, [])
    assert [h["symbol"] for h in out] == [off]
    assert out[0]["entry_price"] is None


def test_collect_holdings_drops_idr_and_dust():
    balances = {"IDR": {"free": 5.0, "locked": 0.0},
                "DOT": {"free": 0.00001, "locked": 0.0}}
    tickers = {"dot_idr": {"last": 17_000.0}}       # ~Rp0,17 — dust
    assert saldo.collect_holdings(balances, tickers, []) == []


# --- command parsing ------------------------------------------------------

def test_command_of():
    assert saldo.command_of("/saldo") == "saldo"
    assert saldo.command_of("/saldo@CryptoIndodaxBot") == "saldo"
    assert saldo.command_of("  /SALDO now ") == "saldo"
    assert saldo.command_of("saldo") is None
    assert saldo.command_of("") is None
    assert saldo.command_of(None) is None


# --- polling --------------------------------------------------------------

def _update(uid, chat, text):
    return {"update_id": uid, "message": {"chat": {"id": chat}, "text": text}}


def test_poll_answers_an_authorised_chat(monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(saldo, "STATE_PATH", tmp_path / "s.json")
    monkeypatch.setattr(config, "TELEGRAM_TOKEN", "t")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_IDS", ["777"])
    monkeypatch.setattr(saldo, "get_updates", lambda o: [_update(5, 777, "/saldo")])
    monkeypatch.setattr(saldo, "saldo_text", lambda now=None: "report")
    monkeypatch.setattr(saldo.notify, "send_to", lambda c, t: sent.append((c, t)) or True)
    assert saldo.poll() == 1
    assert sent == [("777", "report")]


def test_poll_ignores_an_unauthorised_chat(monkeypatch, tmp_path):
    """/saldo discloses balances — a stranger must get no reply at all."""
    sent = []
    monkeypatch.setattr(saldo, "STATE_PATH", tmp_path / "s.json")
    monkeypatch.setattr(config, "TELEGRAM_TOKEN", "t")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_IDS", ["777"])
    monkeypatch.setattr(saldo, "get_updates", lambda o: [_update(5, 999, "/saldo")])
    monkeypatch.setattr(saldo.notify, "send_to", lambda c, t: sent.append((c, t)) or True)
    assert saldo.poll() == 0
    assert sent == []


def test_poll_advances_the_offset_so_a_command_answers_once(monkeypatch, tmp_path):
    state = tmp_path / "s.json"
    monkeypatch.setattr(saldo, "STATE_PATH", state)
    monkeypatch.setattr(config, "TELEGRAM_TOKEN", "t")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_IDS", ["777"])
    monkeypatch.setattr(saldo, "saldo_text", lambda now=None: "report")
    monkeypatch.setattr(saldo.notify, "send_to", lambda c, t: True)
    monkeypatch.setattr(saldo, "get_updates", lambda o: [_update(41, 777, "/saldo")])
    saldo.poll()
    seen = {}
    monkeypatch.setattr(saldo, "get_updates", lambda o: seen.setdefault("offset", o) and [])
    saldo.poll()
    assert seen["offset"] == 42


def test_poll_survives_a_telegram_outage(monkeypatch, tmp_path):
    monkeypatch.setattr(saldo, "STATE_PATH", tmp_path / "s.json")
    monkeypatch.setattr(config, "TELEGRAM_TOKEN", "t")
    def boom(offset):
        raise RuntimeError("502")
    monkeypatch.setattr(saldo, "get_updates", boom)
    assert saldo.poll() == 0


def test_saldo_text_reports_a_broker_failure_plainly(monkeypatch):
    """A silent failure on a live-money bot is worse than an ugly message."""
    def boom():
        raise RuntimeError("[-2015] Unauthorized IP address")
    monkeypatch.setattr(saldo.broker, "get_balances", boom)
    text = saldo.saldo_text()
    assert "Gagal membaca akun" in text and "-2015" in text


def test_report_timestamp_is_wib(monkeypatch):
    utc_noon = datetime(2026, 9, 7, 5, 0, tzinfo=timezone.utc)
    text = saldo.build_saldo_report(0.0, [], now=utc_noon)
    assert "12:00 WIB" in text
