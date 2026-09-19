"""Fast exit watcher — cron */5, between the hourly trader cycles.

WHY THIS EXISTS

Exits used to be evaluated once an hour, at :14, against a price sampled at
:07. On 2026-09-19 PEPE's profit lock sat at Rp0,066823 and the market traded
0,36% under it for most of an hour while the bot held, because nothing looked.
Any level — stop, lock or take-profit — could be crossed and recovered inside
the hour and leave no trace. The same blind spot cost the UNI exit on 09-17.

WHAT IT DELIBERATELY DOES NOT DO — read before extending it

It moves nothing. `high_water`, `stop` and `lock` stay exactly where the hourly
cycle put them. Entries, reconciliation, sizing, the policy overlay and the
circuit breaker are not its business either.

That restraint is the whole design. Feeding 5-minute prices into the trail
would ratchet every stop off intra-hour spikes, fire locks earlier and more
often, and pay the ~0,63% round trip each time. That is a different strategy,
not a faster one — and the stored history is hourly snapshots, so `replay`
cannot score it. Until there is data to justify it, the hourly cycle decides
WHERE the levels sit and this only notices they were breached.

No time stop here either: that is a clock, not a price, and being 55 minutes
late on a 120-hour limit changes nothing.

COST

One /api/ticker_all call per run — every pair arrives in a single response, so
the cost is flat whether one position is open or four — and no call at all when
the book is empty.
"""
import argparse
import sys
from datetime import datetime, timezone

from . import broker, config, data, ledger, lock, strategy, trader

LEVEL_KEY = {"stop": "stop", "lock": "lock"}


def log(msg):
    print(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {msg}", flush=True)


def _level_text(pos, action):
    """The level that was breached, for the log line."""
    key = LEVEL_KEY.get(action)
    if key:
        return f"{action} {config.fmt_price(pos.get(key))}"
    r = pos["entry_price"] - pos["initial_stop"]
    return f"tp {config.fmt_price(pos['entry_price'] + config.TP_R * r)}"


def breaches(led, tickers):
    """[(pos, price, action)] for open positions whose live price broke a level."""
    out = []
    for pos in led.get("open", []):
        ticker = tickers.get(config.pair_id(pos["symbol"])) or {}
        price = ticker.get("last")
        if not price:
            log(f"{pos['symbol']}: no live price this pass — leaving it to the hourly cycle")
            continue
        action = strategy.check_levels(pos, price)
        if action:
            out.append((pos, price, action))
    return out


def run(dry_run=False):
    if not dry_run and not config.TRADING_ENABLED:
        return 0
    # Cheap pre-check outside the lock: no position means no work and no HTTP.
    if not ledger.load().get("open"):
        return 0

    with lock.held() as acquired:
        if not acquired:
            log("hourly trader holds the lock — skipping this pass")
            return 0
        # Re-read inside the lock: the trader may have just closed something.
        led = ledger.load()
        if not led.get("open"):
            return 0
        try:
            tickers = data.fetch_tickers()
        except data.FetchError as e:
            log(f"ticker fetch failed ({e}) — skipping this pass")
            return 0

        hits = breaches(led, tickers)
        if not hits:
            return 0

        positions_by_sym = {}
        if not dry_run:
            marks = {s: t["last"] for s in config.WATCHLIST
                     if (t := tickers.get(config.pair_id(s)))}
            try:
                positions_by_sym = {p["symbol"]: p
                                    for p in broker.get_positions(price_by_symbol=marks)}
            except broker.BrokerError as e:
                log(f"cannot reach Indodax ({e}) — leaving the exit to the hourly cycle")
                return 0

        for pos, price, action in hits:
            log(f"{action} breach {pos['symbol']}: live {config.fmt_price(price)} "
                f"vs {_level_text(pos, action)} (hourly cycle set it)")
            trader._exit_position(led, pos, price, action, dry_run, positions_by_sym)
        if not dry_run:
            ledger.save(led)
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="CryptoIndodaxBot fast exit watcher")
    p.add_argument("--dry-run", action="store_true",
                   help="report breaches without selling anything")
    args = p.parse_args(argv)
    return run(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
