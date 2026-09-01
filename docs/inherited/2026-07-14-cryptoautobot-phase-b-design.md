# CryptoAutoBot Phase B — Paper-Trading Bot (design)

Date: 2026-07-14. Status: approved by user.

## Purpose

Phase A (live since 2026-06-16) produces hourly indicator snapshots and a daily
Claude digest, accumulating trading logic in `memory/insights.md` (~29 insights
over 27 trading days). Phase B turns that logic into an automated **Alpaca
paper**-trading bot.

## Decisions (locked, user-approved 2026-07-14)

| Decision | Choice |
|---|---|
| Brain | **Hybrid** — deterministic Python rules distilled from insights.md + daily Claude policy overlay (`memory/policy.json`) |
| Cadence | **Hourly**, right after the :05 snapshot (trader cron at :12) |
| Account | **New dedicated Alpaca paper account** (keys provided at credentials checkpoint) |
| Fills | **Alpaca paper is the execution record** — mitigated by order-status polling, unfilled-order cancel, per-run position reconciliation |
| Direction | Long-only (Alpaca crypto cannot short) |
| Claude cost | Unchanged (~30 runs/month; policy written by the existing daily digest routine) |

## Strategy rules (traceable to insights.md)

Inputs: the current hour's snapshot JSON (1H/4H/1D × EMA8/20/55, RSI14, ATR14,
ADX14, vol20 per coin).

### BTC regime gate (insights 06-18, 06-20, 07-10)

- `risk_on` — BTC 1D EMA stack UP (8>20>55) and 1D ADX ≥ 20 → normal rules
- `neutral` — BTC trendless (1D ADX < 20 or mixed stack) → alt entries need 1H ADX ≥ 30
- `risk_off` — BTC 1D stack DOWN and 1D ADX ≥ 25 → entries only for
  relative-strength outliers (green on day, 1H ADX ≥ 30, RSI ≤ 65) at **half size**

### Entry filter, per coin (insights 06-16, 06-21, 06-22, 06-29, 07-10)

1. **ADX first:** 1H ADX ≥ 25 (≥ 30 when regime is not `risk_on`)
2. **Trend:** 1H EMA stack UP (8>20>55); 4H stack not DOWN
3. **RSI headroom:** 1H RSI in [45, 70]; hard blow-off guard: RSI > 80 never enters
4. **Late-entry guard:** skip coins already up > 5% on the day
5. **Green-close confirm:** last 1H close > previous 1H close
6. Rank survivors by 1H ADX desc; enter until `MAX_POSITIONS` (3);
   one entry attempt per coin per 24h (re-entry throttle)

### Exits (evaluated hourly, before entries)

- Initial stop: entry − 3×ATR(1H at entry)  → defines 1R
- Trail: once unrealized ≥ +1R, stop = max(stop, high-water close − 6×ATR(1H))
- Hard take-profit: +2.5R
- Time stop: close after 120 hours in position
- Regime flush: while regime is `risk_off`, trail distance tightens to 3×ATR

### Risk

- Sizing: 1.5% of account equity at risk per trade → qty = risk$ / (3×ATR)
- MAX_POSITIONS = 3, max one position per coin
- Circuit breaker: rolling 24h realized loss ≥ 4% of equity → no new entries 24h

### Claude policy overlay — `memory/policy.json`

Written daily by the existing 17:00 UTC digest routine:

```json
{"date": "YYYY-MM-DD", "regime_hint": "auto",
 "blocked_symbols": [], "max_positions": 3, "notes": "..."}
```

Safety clamps in the engine: file missing/invalid/older than 48h → ignored
(pure deterministic mode). `regime_hint` may only make the engine MORE
conservative than the computed BTC gate (risk_on hint cannot override a
computed risk_off). `blocked_symbols` only removes candidates.
`max_positions` may only lower the built-in cap.

## Architecture

New modules in `cryptoauto/` (same functional style as Phase A):

| Module | Responsibility |
|---|---|
| `broker.py` | Alpaca paper REST via `requests`: account, positions, market order, order poll, cancel. Paper URL hardcoded — no live path exists. Keys from `.env` (`APCA_API_KEY_ID`/`APCA_API_SECRET_KEY`). |
| `strategy.py` | Pure functions: `regime(snap)`, `entry_candidates(snap, policy, open_syms, regime)`, `check_exit(position, coin_snap, regime)` |
| `risk.py` | Sizing, stop/trail/TP math, circuit-breaker check over closed trades |
| `policy.py` | Load/validate/clamp `memory/policy.json` |
| `ledger.py` | `data/trades/trades.json`: open positions (entry, stop, R, high-water) + closed history; reconciles against Alpaca positions each run |
| `trader.py` | Hourly entrypoint: load snapshot → reconcile → exits → circuit breaker → entries → notify → log. `--dry-run` logs decisions, places no orders. |
| `notify.py` | Telegram sendMessage (existing Phase A bot token/chat) |

Order lifecycle: market order → poll status ≤ 90s → unfilled ⇒ cancel + record
`unfilled` (never assume a fill). Alpaca positions are the source of truth for
quantity; the ledger adds entry context.

Staleness guard: if the current-hour snapshot is missing or `captured_at` is
older than 70 minutes, the cycle logs and exits without trading.

Config additions (`config.py`): `MAX_POSITIONS=3`, `RISK_PCT=0.015`,
`STOP_ATR_MULT=3.0`, `TRAIL_ATR_MULT=6.0`, `TP_R=2.5`, `TIME_STOP_HOURS=120`,
`CIRCUIT_BREAKER_PCT=0.04`, `TRADING_ENABLED` (env, default false),
`PAPER_BASE_URL="https://paper-api.alpaca.markets"`.

Cron: `12 * * * * cd /root/CryptoAutoBot && .venv/bin/python -m cryptoauto.trader >> logs/trader.log 2>&1`

Daily routine change: `cryptoauto-daily.md` additionally writes
`memory/policy.json` and reports open positions + realized P&L in the digest.

## Testing

pytest, mirroring Phase A conventions (monkeypatch, no network):
regime gate branches; each entry filter individually + ranking/cap;
sizing math incl. half-size in risk_off; trail/TP/time-stop transitions;
policy staleness + clamps; ledger round-trip + reconciliation
(position missing on Alpaca, position unknown to ledger); broker order
poll/cancel paths (mocked); trader dry-run end-to-end with fixture snapshot.

## Rollout

1. Modules + tests (TDD)
2. Credentials checkpoint — user supplies new paper key+secret → `.env`
3. Live `--dry-run` reviewed with user
4. `TRADING_ENABLED=true` + cron, observe first cycle
5. Daily routine update, push, memory update

## Out of scope

Shorting, live trading, backtest harness (follow-up IMP), Phase A changes.
