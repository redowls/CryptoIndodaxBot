# CryptoIndodaxBot

Hourly crypto analysis + trading bot for **Indodax**, quoted in **IDR**.

Ported from [CryptoAutoBot](https://github.com/redowls/CryptoAutoBot) (Alpaca /
USD). The strategy, indicators, risk model and policy-overlay design are
unchanged — only the exchange and the currency are different.

---

## ⚠️ Read this before enabling trading

**Indodax has no paper-trading sandbox.** CryptoAutoBot traded an Alpaca *paper*
account, so a bug there cost nothing. Here, `TRADING_ENABLED=true` spends real
rupiah in a real account. `demo-indodax.com` exists but sits behind a Cloudflare
Access sign-in wall and cannot be driven from a server.

The safe substitute is the dry run, which makes every decision and places no
orders:

```bash
python -m cryptoindodax.trader --dry-run
```

`TRADING_ENABLED` defaults to `false`. Leave it there until you have watched the
dry run for a while and agree with what it wants to do.

## Before first run

| Item | Status |
| --- | --- |
| `INDODAX_API_KEY` | ✅ set in `.env` |
| `INDODAX_API_SECRET` | ❌ **missing — required** |
| IP whitelist on the API key | ❌ **required for trading** |
| `TELEGRAM_TOKEN` | ✅ set (`@CryptoIndodaxBot`) |
| `TELEGRAM_CHAT_ID` | ❌ press Start on the bot, then run the resolver |

1. **API secret.** Indodax shows the secret only once, when the key is created.
   Nothing private can be called without it — the key alone is not enough.
   Put it in `.env` as `INDODAX_API_SECRET=`.

2. **IP whitelist.** TAPI v2 makes IP whitelisting *mandatory* for spot trading.
   Add this server's IP to the key at Indodax → Settings → API, or every order
   will be rejected. Read-only calls work without it.

3. **Telegram chat id.** Open Telegram, message `@CryptoIndodaxBot`, press
   Start, then:

   ```bash
   python -m cryptoindodax.notify resolve   # prints TELEGRAM_CHAT_ID=...
   python -m cryptoindodax.notify test      # confirm delivery
   ```

## Layout

```
cryptoindodax/
  config.py      watchlist, IDR helpers, thresholds, endpoints
  data.py        Indodax OHLC → normalised {t,o,h,l,c,v} bars
  pairs.py       per-pair order minimums, quantity precision, taker fee
  indicators.py  EMA / RSI / ATR / ADX / volume average   (unchanged)
  snapshot.py    hourly capture → data/snapshots/DATE/HH.json
  digest.py      deterministic day aggregation
  strategy.py    regime gate + entry filters + exit rules  (unchanged)
  risk.py        position sizing, circuit breaker
  ledger.py      open/closed positions, re-entry throttle
  policy.py      Claude daily overlay, clamped so it can only tighten
  broker.py      Indodax TAPI v2 client
  trader.py      hourly cycle: reconcile → exits → entries
  notify.py      Telegram
```

## Running

```bash
python -m cryptoindodax.snapshot          # capture one hourly snapshot
python -m cryptoindodax.digest            # summarise today's snapshots
python -m cryptoindodax.trader --dry-run  # decide, place nothing
python -m pytest tests/ -q                # 105 tests
```

Suggested cron (not installed yet — mirrors CryptoAutoBot's cadence):

```cron
5  * * * * cd /root/CryptoIndodaxBot && .venv/bin/python -m cryptoindodax.snapshot >> logs/snapshot.log 2>&1
12 * * * * cd /root/CryptoIndodaxBot && .venv/bin/python -m cryptoindodax.trader  >> logs/trader.log 2>&1
```

## What changed from CryptoAutoBot

Ported unchanged: `indicators.py`, `strategy.py`, `policy.py`, `snapshot.py`,
`digest.py`. They kept working because `data.py` normalises Indodax's payload
into the bar shape the old pipeline already spoke.

| Area | Alpaca | Indodax |
| --- | --- | --- |
| Quote currency | USD | **IDR** (`Rp1.234.567`) |
| Bars | `v1beta3/crypto/us/bars`, cursor pagination | `tradingview/history_v2`, one from/to window — **no pagination** |
| Bar payload | `{t,o,h,l,c,v}` | `{Time,Open,High,Low,Close,Volume}`, `Time` unix seconds, `Volume` a string |
| User-Agent | not needed | **required** — a UA-less request gets 403 |
| Auth | key + secret headers | HMAC-**SHA256** over the sent string, `X-APIKEY` + `Sign` |
| API version | — | **TAPI v2** (`api.indodax.com`). The v1 `indodax.com/tapi` rejects v2 keys with `invalid_version_key` |
| Holdings | positions with an entry price | **spot balances only** — no entry price, so `ledger.py` is the sole record of position context |
| Closing | `DELETE /positions/{sym}` | no such endpoint — a market **SELL** |
| Market buy size | quantity in coin | **`quoteOrderQty` in IDR** |
| Market sell size | quantity in coin | quantity in coin |
| Fill price | on the order | derived from `myTrades` fills |
| Order minimums | none | **Rp10.000** notional + a per-pair coin minimum |
| Fees | separate | taken **in-asset** → held quantity drifts below the fill |
| Sandbox | paper account | **none** |

### Two traps worth remembering

**`/api/pairs` field names are misleading.** `price_round` (8, or 6 for SHIB) is
the decimals allowed on the *coin quantity*; `volume_precision` is `0` on every
pair and describes the *IDR* side. Rounding a quantity by `volume_precision`
floors `0.0087 BTC` to `0`, and the bot silently cannot buy BTC or ETH.
`pairs.round_qty` uses `price_round`; there is a regression test for it.

**Sell the free balance, not the ledger quantity.** Fees are deducted in-asset,
so after a buy the account holds slightly less than the fill reported. Selling
the ledger's number is rejected for insufficient funds — `trader._sellable_qty`
uses the exchange's free balance, floored to precision.

## Strategy

Unchanged from CryptoAutoBot: a BTC 1D regime gate (`risk_on` / `neutral` /
`risk_off`) over deterministic entry filters — 1H ADX ≥ 25 (≥ 30 outside
`risk_on`), 1H EMA stack UP, 4H not DOWN, RSI 45–70 (never above 80), not
already up more than 5% on the day, green 1H close. Exits are a 3×ATR stop,
6×ATR trail once past +1R (3× in `risk_off`), a 2.5R take-profit and a 120h
time stop. Sizing risks 1.5% of equity per trade, max 3 positions, with a
rolling 24h −4% circuit breaker and a 24h per-coin re-entry throttle.

`memory/policy.json` is a daily Claude overlay that can only make the engine
*more* conservative (block symbols, lower the position cap, worsen the regime);
a stale or malformed file degrades to pure deterministic mode.

`memory/insights.md` is seeded from CryptoAutoBot's 27+ days of observations —
those describe coin behaviour, which is the same market regardless of quote
currency.
