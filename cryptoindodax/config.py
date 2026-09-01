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

# Indodax-tradable universe. Every one of these is a live *_idr pair (verified
# against /api/pairs) — same 10 coins CryptoAutoBot tracked on Alpaca, so the
# strategy thresholds distilled in memory/insights.md carry over unchanged.
# BNB and ADA are also listed on Indodax (they were not on Alpaca) if you want
# to widen the universe later.
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

MAX_POSITIONS = 3
RISK_PCT = 0.015            # equity fraction risked per trade
STOP_ATR_MULT = 3.0         # initial stop distance = 1R
TRAIL_ATR_MULT = 6.0        # trail distance once >= +1R
RISK_OFF_TRAIL_ATR_MULT = 3.0  # tighter trail while BTC regime is risk_off
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
ENTRY_ADX_MIN = 25.0
ENTRY_ADX_MIN_CAUTIOUS = 30.0  # when regime is neutral/risk_off
ENTRY_RSI_MIN = 45.0
ENTRY_RSI_MAX = 70.0
BLOWOFF_RSI = 80.0
LATE_ENTRY_DAY_PCT = 5.0    # skip coins already up more than this on the day

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


def fmt_idr(amount) -> str:
    """Format a rupiah amount the way Indonesian users read it: Rp1.234.567."""
    if amount is None:
        return "-"
    sign = "-" if amount < 0 else ""
    return f"{sign}Rp{abs(round(amount)):,}".replace(",", ".")
