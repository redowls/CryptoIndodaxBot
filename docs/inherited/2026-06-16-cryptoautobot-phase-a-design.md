# CryptoAutoBot — Phase A Design (Analysis + Daily Digest)

**Date:** 2026-06-16
**Status:** Approved (design); pending spec review → implementation plan
**Repo:** https://github.com/redowls/CryptoAutoBot.git
**Location:** `/root/CryptoAutoBot`

## Goal

Run an automated crypto-analysis system on the VPS for ~1 month. Every hour it
captures a deterministic market snapshot; once daily it runs a Claude-powered
analysis that produces insights, commits them to git as durable "memory," and
sends a Telegram digest. The accumulated `memory/insights.md` becomes the
foundation for **Phase B** — a paper-trading bot — designed separately later.

Two sub-projects:
- **Phase A (this spec):** analysis + daily digest. Build now.
- **Phase B (deferred):** Alpaca paper-trading bot informed by Phase A data.

## Decisions (locked)

| Decision | Choice |
|---|---|
| Hourly cadence | **Hybrid** — cheap deterministic hourly snapshot + one Claude analysis/day |
| Universe (10) | `BTC ETH SOL XRP DOGE AVAX LINK DOT LTC UNI` (Alpaca-tradable; BNB/ADA dropped, swapped for LTC/UNI) |
| Data source | Alpaca public crypto OHLCV (keyless for historical bars) |
| Daily digest time | **00:00 WIB = 17:00 UTC** → cron `0 17 * * *` |
| Hourly snapshot time | `5 * * * *` (every hour, 24/7 incl. weekends) |
| Telegram | New bot token `8900684488:…` (chat ID pending user messaging the bot) |
| Git auth | Reuse existing fine-grained GitHub PAT (HTTPS), like the other bots |
| Claude cost target | ~30 Claude runs/month (daily digest only) |

## Architecture

Self-contained Python project mirroring `CryptoTradeWisBot` conventions but
lightweight (no database — git + JSON files are the durable store).

```
/root/CryptoAutoBot/
├── cryptoauto/
│   ├── __init__.py
│   ├── config.py          # watchlist, paths, Alpaca endpoint, constants
│   ├── data.py            # Alpaca OHLCV fetch (1H/4H/1D), per-symbol resilient
│   ├── indicators.py      # EMA(8/20/55), RSI(14), ATR(14), ADX(14), vol20
│   ├── snapshot.py        # hourly entrypoint: fetch → compute → write JSON
│   └── digest.py          # deterministic aggregation of a day's snapshots
├── data/snapshots/YYYY-MM-DD/HH.json   # hourly captures (committed daily)
├── memory/
│   ├── analysis/YYYY-MM-DD.md          # per-day Claude reasoning (durable)
│   └── insights.md                     # rolling accumulating "logic" → Phase B
├── tests/                               # pytest: indicators, schema, aggregation
├── logs/
├── requirements.txt
├── .env.example                         # optional Alpaca keys; no secrets in git
├── .gitignore                           # .env, .venv, logs, __pycache__
└── README.md
```

Routine assets live with the existing scaffold in `/root/claude-routines/`:
- `cryptoauto-daily.md` — the Claude prompt (prints ONLY the Telegram text to stdout)
- `cryptoauto-daily.conf` — overrides MODEL, ALLOWED_TOOLS (Write/Edit + Bash for
  git push), and TELEGRAM_TOKEN/TELEGRAM_CHAT_ID for the new bot

## Components

### `snapshot.py` (hourly, deterministic, no Claude)
1. For each of the 10 symbols, fetch 1H/4H/1D OHLCV from Alpaca's public crypto
   bars endpoint.
2. Compute EMA(8/20/55), RSI(14), ATR(14), ADX(14), and the 20-bar volume
   average per timeframe.
3. Write one `data/snapshots/<date>/<HH>.json` with prices, indicators, and a
   per-symbol fetch status. Idempotent (re-running the same hour overwrites).
4. Per-symbol failures are caught, logged, and recorded as `status: "error"` —
   the run never aborts because one symbol failed.

### `digest.py` (deterministic helper, no Claude)
Loads the day's snapshots and produces a compact, structured summary (per-coin
price change, trend vs EMAs, RSI/ADX regime, volatility, notable moves) that the
Claude routine reads. Keeps the LLM prompt small and the math testable.

### `cryptoauto-daily.md` (daily Claude routine)
1. Run `python -m cryptoauto.digest` to get the structured day summary.
2. Analyze regime/trend/momentum/volatility per coin; identify the day's best
   setups and any risk flags.
3. Write `memory/analysis/<date>.md` (full reasoning) and append the key,
   reusable learnings to `memory/insights.md`.
4. `git add -A && git commit && git push` to CryptoAutoBot (bundles the day's
   snapshots + analysis in one commit).
5. Print ONLY the Telegram digest text to stdout (`run-routine.sh` sends it).

## Data Flow

```
cron 5 * * * *   → snapshot.py        → data/snapshots/<date>/<HH>.json (local)
cron 0 17 * * *  → run-routine.sh cryptoauto-daily
                     → python -m cryptoauto.digest (reads ~24 snapshots)
                     → Claude analysis → memory/analysis + memory/insights
                     → git commit + push
                     → Telegram digest (via new bot)
```

## Error Handling

- `snapshot.py`: per-symbol try/except; logs to `logs/`, continues; exit 0 even
  on partial failure so cron stays quiet (failures visible in the JSON status).
- `run-routine.sh`: existing flock (no overlap), timeout (no hang), and a short
  Telegram failure alert if the digest run errors or times out.
- Git push failure in the routine: log + still send the digest (data is safe on
  disk; next run re-pushes).

## Testing

- `tests/test_indicators.py` — EMA/RSI/ATR/ADX/vol against known fixtures.
- `tests/test_snapshot.py` — JSON schema + per-symbol error handling (mocked fetch).
- `tests/test_digest.py` — deterministic aggregation over fixture snapshots.
- Manual: one live `snapshot.py` run, then one manual `run-routine.sh
  cryptoauto-daily` to validate the prompt, git push, and Telegram delivery
  before enabling the cron entries.

## Secrets & Config

- Alpaca: historical crypto bars are keyless; optional keys via `.env` (gitignored)
  if rate limits require, used automatically when present.
- Telegram: new bot token + chat ID in `cryptoauto-daily.conf` (not committed to
  CryptoAutoBot; lives in `/root/claude-routines/`). Chat ID captured after the
  user sends the bot a message.
- GitHub: reuse the existing fine-grained PAT for HTTPS push.

## Out of Scope (Phase A)

- No trading, no orders, no Alpaca account actions — analysis only.
- No database (git + JSON is the store).
- Phase B (paper-trading bot) is a separate spec built after ~1 month of data.

## Success Criteria

- Hourly snapshots accumulate under `data/snapshots/` for all reachable symbols.
- A daily Telegram digest arrives at 00:00 WIB with per-coin analysis.
- Each day's reasoning is committed to `memory/` in the GitHub repo.
- After ~1 month, `memory/insights.md` holds a usable body of strategy logic to
  seed Phase B.
