import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data" / "snapshots"
MEMORY_DIR = ROOT / "memory"
LOG_DIR = ROOT / "logs"
TRADES_DIR = ROOT / "data" / "trades"
POLICY_PATH = MEMORY_DIR / "policy.json"
PAIRS_CACHE = ROOT / "data" / "pairs.json"


def _load_dotenv(path=ROOT / ".env"):
    # cron runs without a login shell; pick up keys from .env ourselves
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()

# Indodax-tradable universe. Every one of these is a live *_idr pair (all ten
# re-verified against /api/pairs on 2026-09-06: min order 10.000 IDR, taker fee
# 0.2%). Narrowed to 5 on 2026-09-01, widened back to 10 on 2026-09-06 at the
# user's request — SOL, XRP, DOGE, AVAX and LTC rejoin BTC/ETH/UNI/DOT/LINK.
#
# BTC must stay on this list whatever else changes: strategy.regime() reads
# BTC's 1D timeframe to set the risk_on/neutral/risk_off gate for every other
# coin, and falls back to "neutral" if BTC is absent — which silently raises
# the ADX entry bar from 25 to 30 across the board.
WATCHLIST = ["BTC", "ETH", "SOL", "XRP", "DOGE", "AVAX", "LINK", "DOT", "LTC", "UNI"]

# Everything is quoted in Indonesian Rupiah.
QUOTE = "IDR"


def pair(sym: str) -> str:
    """Chart/TAPIv2 symbol form: BTC -> BTCIDR."""
    return f"{sym.upper()}{QUOTE}"


def pair_id(sym: str) -> str:
    """Public-API pair id form: BTC -> btc_idr (used by /api/pairs, /api/ticker)."""
    return f"{sym.lower()}_{QUOTE.lower()}"


# --- Public market data ---------------------------------------------------
# Indodax serves OHLC through its TradingView bridge. Unlike Alpaca there is no
# pagination: one from/to window returns the whole range in a single response.
BARS_URL = "https://indodax.com/tradingview/history_v2"
PAIRS_URL = "https://indodax.com/api/pairs"
PUBLIC_BASE_URL = "https://indodax.com"

# `tf` is minutes, or D/W codes — not Alpaca's "1Hour" strings.
TIMEFRAMES = {"1H": "60", "4H": "240", "1D": "1D"}

# A bare urllib/no-UA request gets a 403 from Indodax's edge; any real UA is
# accepted. Set one explicitly so the bot is identifiable in their logs.
USER_AGENT = "CryptoIndodaxBot/1.0 (+https://github.com/redowls/CryptoIndodaxBot)"

# How far back to request per timeframe, sized so EMA55/ADX14 are always warm
# (verified: 10d/1H = 241 bars, 30d/4H = 181 bars, 200d/1D = 201 bars).
LOOKBACK_DAYS = {"1H": 10, "4H": 30, "1D": 200}

EMA_PERIODS = (8, 20, 55)
RSI_PERIOD = 14
ATR_PERIOD = 14
ADX_PERIOD = 14
VOL_PERIOD = 20

# --- Private trade API (TAPI v2) -----------------------------------------
# The legacy https://indodax.com/tapi (v1, HMAC-SHA512, `Key` header) rejects
# v2-generation keys with error_code=invalid_version_key. v2 is the live path.
TAPI_BASE_URL = "https://api.indodax.com"
RECV_WINDOW_MS = 5000

INDODAX_KEY = os.getenv("INDODAX_API_KEY")
INDODAX_SECRET = os.getenv("INDODAX_API_SECRET")

# This host has both IPv4 and IPv6, and api.indodax.com publishes both A and
# AAAA records — so requests leave over IPv6 by default and the key's IPv4
# whitelist never matches ([-2015] Unauthorized IP address). Pin to IPv4 so the
# source address is deterministic and whitelistable. See net.py.
FORCE_IPV4 = os.getenv("INDODAX_FORCE_IPV4", "true").lower() == "true"

# --- Phase B trading ------------------------------------------------------
# NOTE: Indodax has no usable paper/sandbox endpoint (demo-indodax.com sits
# behind a Cloudflare Access sign-in wall), so unlike CryptoAutoBot — which
# traded an Alpaca *paper* account — every order this bot places is REAL money
# in a REAL IDR account. TRADING_ENABLED must be set true deliberately; use
# `python -m cryptoindodax.trader --dry-run` to preview decisions safely.
TRADING_ENABLED = os.getenv("TRADING_ENABLED", "false").lower() == "true"

MAX_POSITIONS = 4          # hard cap; the daily policy may lower it, never raise it
RISK_PCT = 0.015            # equity fraction risked per trade
STOP_ATR_MULT = 3.0         # initial stop distance = 1R
# The trail must be TIGHTER than the stop or it can never protect anything.
# At the old 6.0 (2x STOP_ATR_MULT) the trail stop only reached entry+0.5R by
# the time price hit the 2.5R take-profit, so it never bound: DOT ran +9% on
# 2026-09-06 with its stop pinned at the original 1R the whole way, because a
# rising ATR pushed `high_water - 6*ATR` back below the initial stop. At 2.0 the
# trail sits inside the 3.0 stop, locks in ~+1.8R at target, and lets a runner
# continue past TP instead of being capped by it.
TRAIL_ATR_MULT = 2.0        # trail distance once >= +1R
RISK_OFF_TRAIL_ATR_MULT = 1.5  # tighter trail while BTC regime is risk_off
TP_R = 2.5                  # hard take-profit in R multiples
TIME_STOP_HOURS = 120
CIRCUIT_BREAKER_PCT = 0.04  # rolling 24h realized loss halts new entries
REENTRY_THROTTLE_HOURS = 24
SNAPSHOT_MAX_AGE_MIN = 70   # never trade on a stale snapshot
POLICY_MAX_AGE_HOURS = 48   # stale policy.json is ignored

# Indodax enforces a per-pair minimum order (10,000 IDR on btc_idr). Anything
# smaller is rejected, so treat sub-minimum sizing as unsizable. Coin dust below
# this notional is ignored when deriving positions from spot balances.
MIN_ORDER_IDR = 10_000
DUST_IDR = 10_000

# Fallback taker fee on Indodax spot (0.2% for the majors). The live per-pair
# value comes from /api/pairs via pairs.taker_fee_pct(); fills report their own
# commission regardless.
TAKER_FEE_PCT = 0.002

# Equity used by --dry-run when no API credentials are configured (IDR).
DRY_RUN_EQUITY_IDR = 10_000_000

# entry filter thresholds (distilled from memory/insights.md)
# Lowered 25 -> 20 on 2026-09-07: ADX was the single largest brake in the live
# funnel (244 of 720 coin-hours rejected on it). With MIN_ATR_PCT now filtering
# the uneconomic names, 20 restores candidate flow to ~12.9/day — the same rate
# as the old unfiltered 25 bar, but on coins that can pay their own fees. The
# cautious bar keeps its original +5 offset.
ENTRY_ADX_MIN = 20.0
ENTRY_ADX_MIN_CAUTIOUS = 25.0  # when regime is neutral/risk_off
ENTRY_RSI_MIN = 45.0
ENTRY_RSI_MAX = 70.0
BLOWOFF_RSI = 80.0
LATE_ENTRY_DAY_PCT = 5.0    # skip coins already up more than this on the day

# --- volatility floor: refuse trades the fees eat ------------------------
# Indodax charges ~0.2% a side and real fills have run ~0.63% round trip. Against
# a STOP_ATR_MULT*ATR stop that cost is a fixed share of 1R, and it is brutal on
# the low-volatility majors: at a 1H ATR of 0.28% BTC's stop is 0.84% and the
# round trip is 75% of everything risked. The trade has to be right by a huge
# margin just to break even.
#
# The first four live trades separated perfectly on this axis — BTC (ATR 0.54%)
# and ETH (0.65%) both stopped out, DOT (0.84%) and LINK (0.92%) both hit take
# profit — and replaying the two losers against 4x/5x/6x ATR stops showed a wider
# stop would have avoided both losses while producing zero wins (BTC never got
# above -0.60R in the 62h after its stop, ETH peaked at +0.11R). The stop width
# was never the problem; entering those coins at all was.
#
# So the veto is economic, not a magic number: cap the round trip at
# MAX_FEE_DRAG_R of 1R and solve for the ATR that implies. This used to be
# re-applied by hand in policy.json every night (180 "blocked by policy"
# rejections in the first week) — making it structural is what frees the daily
# digest to stop hand-blocking BTC/ETH.
OBSERVED_ROUND_TRIP_PCT = 0.63   # measured on real fills, not the 0.4% configured
MAX_FEE_DRAG_R = 0.28            # fees may not exceed 28% of 1R
MIN_ATR_PCT = OBSERVED_ROUND_TRIP_PCT / MAX_FEE_DRAG_R / STOP_ATR_MULT  # = 0.75%

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

# One or more chat ids, comma-separated — every alert goes to all of them.
# A single id keeps working unchanged.
TELEGRAM_CHAT_IDS = [c.strip() for c in (os.getenv("TELEGRAM_CHAT_ID") or "").split(",")
                     if c.strip()]

# Back-compat alias: the first configured chat.
TELEGRAM_CHAT_ID = TELEGRAM_CHAT_IDS[0] if TELEGRAM_CHAT_IDS else None


def fmt_idr(amount) -> str:
    """Format a rupiah amount the way Indonesian users read it: Rp1.234.567."""
    if amount is None:
        return "-"
    sign = "-" if amount < 0 else ""
    return f"{sign}Rp{abs(round(amount)):,}".replace(",", ".")
