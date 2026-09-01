"""Hourly trading entrypoint (cron :12, right after the :05 snapshot).

Order of operations: load snapshot → reconcile with Indodax balances → exits →
circuit breaker → entries → persist ledger + notify. `--dry-run` logs every
decision but places no orders and mutates nothing.

⚠️ Unlike CryptoAutoBot, which traded an Alpaca *paper* account, Indodax has no
sandbox: with TRADING_ENABLED=true this spends real rupiah. Preview with
`python -m cryptoindodax.trader --dry-run` first.
"""
import argparse
import json
from datetime import datetime, timedelta, timezone

from . import broker, config, data, ledger, notify, pairs, policy, risk, strategy


def log(msg):
    print(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {msg}", flush=True)


def load_current_snapshot(now=None):
    """Latest snapshot no older than SNAPSHOT_MAX_AGE_MIN, else None."""
    now = now or datetime.now(timezone.utc)
    for candidate in (now, now - timedelta(hours=1)):
        path = config.DATA_DIR / candidate.strftime("%Y-%m-%d") / f"{candidate.strftime('%H')}.json"
        if not path.exists():
            continue
        try:
            snap = json.loads(path.read_text())
            captured = datetime.fromisoformat(snap["captured_at"])
        except (ValueError, KeyError):
            continue
        if now - captured <= timedelta(minutes=config.SNAPSHOT_MAX_AGE_MIN):
            return snap
    return None


def prices_from_snapshot(snap):
    """{symbol: last 1H close} — the marks used to value balances as equity."""
    out = {}
    for coin in snap.get("symbols", []):
        tf = coin.get("timeframes", {}).get("1H", {})
        if tf.get("status") == "ok" and tf.get("last_close"):
            out[coin["symbol"]] = tf["last_close"]
    return out


def fetch_extras(symbols, now=None):
    """Fields the snapshot lacks: day change %, last two 1H closes."""
    now = now or datetime.now(timezone.utc)
    out = {}
    for sym in symbols:
        extras = {"day_change_pct": None, "last_1h_close": None, "prev_1h_close": None}
        try:
            d1 = data.fetch_bars(config.pair(sym), config.TIMEFRAMES["1D"],
                                 start=now - timedelta(days=4), end=now)
            if len(d1) >= 2:
                prev, last = d1[-2]["c"], d1[-1]["c"]
                if prev:
                    extras["day_change_pct"] = (last / prev - 1) * 100
            h1 = data.fetch_bars(config.pair(sym), config.TIMEFRAMES["1H"],
                                 start=now - timedelta(hours=12), end=now)
            if len(h1) >= 2:
                extras["last_1h_close"] = h1[-1]["c"]
                extras["prev_1h_close"] = h1[-2]["c"]
        except data.FetchError as e:
            log(f"extras: {sym}: {e}")
        out[sym] = extras
    return out


def _coin_h1(snap, symbol):
    for coin in snap.get("symbols", []):
        if coin.get("symbol") == symbol:
            tf = coin.get("timeframes", {}).get("1H", {})
            return tf if tf.get("status") == "ok" else None
    return None


def _sellable_qty(pos, positions_by_sym):
    """Quantity we may actually sell: the free balance, snapped to precision.

    Fees are taken in-asset, so the held quantity drifts a little below what the
    entry fill reported; selling the ledger's number would be rejected.
    """
    sym = pos["symbol"]
    live = positions_by_sym.get(sym) or {}
    qty = live.get("free_qty", pos["qty"])
    return pairs.round_qty(sym, min(qty, pos["qty"]) if live else pos["qty"])


def _exit_position(led, pos, price_hint, reason, dry_run, positions_by_sym):
    sym = pos["symbol"]
    qty = _sellable_qty(pos, positions_by_sym)
    if dry_run:
        log(f"DRY-RUN exit {sym}: {reason} qty {qty} @ ~{config.fmt_idr(price_hint)}")
        return None
    if qty <= 0:
        log(f"exit {sym}: {reason} but no sellable balance — dropping from ledger")
        return ledger.close_position(led, pos, price_hint, f"{reason}/no-balance")
    ok, why = pairs.meets_minimums(sym, qty, price_hint)
    if not ok:
        log(f"exit {sym}: {reason} but {why} — cannot sell, dropping from ledger")
        return ledger.close_position(led, pos, price_hint, f"{reason}/below-minimum")
    exit_price = price_hint
    try:
        order_id = broker.close_position(sym, qty)
        status, fill_price, filled_qty = broker.wait_for_fill(sym, order_id)
        if fill_price:
            exit_price = fill_price
        log(f"exit {sym}: {reason}, sell order {status} qty {filled_qty} "
            f"@ {config.fmt_idr(exit_price)}")
        if status == "canceled" and not filled_qty:
            log(f"exit {sym}: sell did not fill — keeping position, will retry next cycle")
            return None
    except broker.BrokerError as e:
        log(f"exit {sym}: sell FAILED ({e}) — keeping position, will retry next cycle")
        notify.send(f"CryptoIndodaxBot EXIT FAILED {sym} ({reason}): {e}")
        return None
    trade = ledger.close_position(led, pos, exit_price, reason)
    notify.send(f"CryptoIndodaxBot EXIT {sym} ({reason}) @ {config.fmt_idr(exit_price)} "
                f"P&L {config.fmt_idr(trade['pnl'])}")
    return trade


def _enter_position(led, sym, coin, equity, reg, dry_run):
    h1 = coin["timeframes"]["1H"]
    price, atr = h1["last_close"], h1["atr14"]
    qty, stop, risk_idr = risk.position_size(equity, price, atr,
                                             half=(reg == "risk_off"), symbol=sym)
    if qty <= 0:
        log(f"entry {sym}: unsizable — {risk.sizing_reason(equity, price, atr, sym)}")
        return None
    notional = qty * price
    if dry_run:
        log(f"DRY-RUN entry {sym}: qty {qty} @ ~{config.fmt_idr(price)} "
            f"(notional {config.fmt_idr(notional)}), stop {config.fmt_idr(stop)}, "
            f"risk {config.fmt_idr(risk_idr)}")
        return None
    ledger.record_entry_attempt(led, sym)
    try:
        # A market BUY on Indodax is sized in rupiah, not coin.
        order_id = broker.market_buy_idr(sym, int(notional))
        status, fill_price, filled_qty = broker.wait_for_fill(sym, order_id)
    except broker.BrokerError as e:
        log(f"entry {sym}: order FAILED ({e})")
        notify.send(f"CryptoIndodaxBot ENTRY FAILED {sym}: {e}")
        return None
    if status == "canceled" or not filled_qty:
        log(f"entry {sym}: order {status} unfilled — skipped")
        return None
    entry_price = fill_price or price
    pos = ledger.open_position(led, sym, filled_qty, entry_price, atr, order_id,
                               half_size=(reg == "risk_off"))
    log(f"entry {sym}: {status} qty {filled_qty} @ {config.fmt_idr(entry_price)}, "
        f"stop {config.fmt_idr(pos['stop'])}")
    notify.send(f"CryptoIndodaxBot ENTRY {sym} qty {filled_qty} @ {config.fmt_idr(entry_price)} "
                f"stop {config.fmt_idr(pos['stop'])} (regime {reg})")
    return pos


def run(dry_run=False, now=None):
    now = now or datetime.now(timezone.utc)
    if not dry_run and not config.TRADING_ENABLED:
        log("TRADING_ENABLED is false — exiting (use --dry-run to preview decisions)")
        return
    snap = load_current_snapshot(now)
    if snap is None:
        log("no fresh snapshot (missing or older than "
            f"{config.SNAPSHOT_MAX_AGE_MIN} min) — skipping cycle")
        return

    marks = prices_from_snapshot(snap)
    have_keys = bool(config.INDODAX_KEY and config.INDODAX_SECRET)
    positions = []
    if have_keys:
        try:
            acct = broker.get_account(price_by_symbol=marks)
            positions = broker.get_positions(price_by_symbol=marks,
                                             balances=acct["balances"])
        except broker.BrokerError as e:
            log(f"cannot reach Indodax ({e}) — skipping cycle")
            return
        equity = acct["equity"]
        log(f"account equity {config.fmt_idr(equity)} "
            f"(cash {config.fmt_idr(acct['cash'])}), {len(positions)} coin positions")
    elif dry_run:
        equity, positions = config.DRY_RUN_EQUITY_IDR, None
        log(f"no Indodax keys — dry-run with simulated equity {config.fmt_idr(equity)}")
    else:
        log("no Indodax keys — cannot trade")
        return

    led = ledger.load()
    positions_by_sym = {p["symbol"]: p for p in (positions or [])}
    if positions is not None:
        for note in ledger.reconcile(led, positions):
            log(note)

    pol = policy.load(now=now)
    computed = strategy.regime(snap)
    reg = strategy.effective_regime(computed, pol["regime_hint"])
    log(f"regime: computed {computed}, policy hint {pol['regime_hint']} -> {reg}")

    # exits first
    for pos in list(led["open"]):
        h1 = _coin_h1(snap, pos["symbol"])
        if h1 is None:
            log(f"exit check {pos['symbol']}: no 1H data this hour — holding")
            continue
        action, updated = strategy.check_exit(pos, h1, reg, ledger.hours_held(pos, now))
        if action:
            _exit_position(led, updated, h1["last_close"], action, dry_run, positions_by_sym)
        else:
            ledger.update_position(led, updated)
            if updated["stop"] != pos["stop"]:
                log(f"trail {pos['symbol']}: stop -> {config.fmt_idr(updated['stop'])}")

    # An unfunded account is not a risk event — say so plainly rather than
    # letting the circuit breaker (which treats equity<=0 as tripped) claim a
    # 24h loss that never happened.
    if equity <= 0:
        log("account has no equity — deposit IDR before trading; nothing to do")
        if not dry_run:
            ledger.save(led)
        return

    if risk.circuit_breaker_tripped(led["closed"], equity, now=now):
        log(f"circuit breaker: 24h realized loss >= {config.CIRCUIT_BREAKER_PCT:.0%} "
            "of equity — no new entries")
        if not dry_run:
            ledger.save(led)
        return

    # entries
    open_syms = {p["symbol"] for p in led["open"]}
    slots = min(pol["max_positions"], config.MAX_POSITIONS) - len(open_syms)
    if slots <= 0:
        log(f"no entry slots ({len(open_syms)} open)")
    else:
        extras = fetch_extras([c["symbol"] for c in snap.get("symbols", [])], now=now)
        candidates, rejections = strategy.entry_candidates(
            snap, extras, open_syms, reg, blocked=set(pol["blocked_symbols"]))
        for sym, reason in rejections:
            log(f"reject {sym}: {reason}")
        entered = 0
        for sym, coin in candidates:
            if entered >= slots:
                break
            if ledger.throttled(led, sym, now=now):
                log(f"reject {sym}: re-entry throttle (24h)")
                continue
            if _enter_position(led, sym, coin, equity, reg, dry_run) or dry_run:
                entered += 1
        if not candidates:
            log("no entry candidates this hour")

    if not dry_run:
        ledger.save(led)
    log("cycle done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CryptoIndodaxBot hourly trader")
    parser.add_argument("--dry-run", action="store_true",
                        help="log decisions without placing orders or saving state")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
