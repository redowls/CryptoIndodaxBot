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

# Indodax-tradable universe. Every one is a live *_idr pair with a 10.000 IDR
# minimum and a 0.2% taker fee. 5 coins on 2026-09-01, 10 on 2026-09-06, and 12
# on 2026-09-15 when the user asked for meme-coin exposure.
#
# PEPE and FARTCOIN were picked over the other memes Indodax lists on total
# round-trip cost, not on volume alone. Bid/ask spread is an invisible fee the
# MIN_ATR_PCT floor does not see (that floor only knows OBSERVED_ROUND_TRIP_PCT),
# and on thin meme books it dwarfs the commission:
#
#     coin      spread   fee+spread   vs a 3*ATR stop     verdict
#     PEPE       0.19%      0.82%          29% of 1R      taken
#     FARTCOIN   0.32%      0.95%          27% of 1R      taken
#     SHIB       0.48%      1.11%          43% of 1R      rejected
#     PENGU      0.58%      1.21%          42% of 1R      rejected
#
# SHIB is the more established name and was the obvious pick until the spread
# was measured; at 43% of 1R it is as uneconomic as BTC was. All four clear the
# ATR floor and carry a full 200-day history, so cost was the deciding screen.
#
# USELESS and MOG were added 2026-09-15 on the same screen, widened to 19 meme
# pairs with spreads sampled 8 times over two minutes (a single reading is not
# trustworthy — WIF ranged 0.85% to 4.43%). They are the ONLY two candidates
# that come in under MAX_FEE_DRAG_R once the spread is counted:
#
#     coin      ATR%    stop   spread   drag of 1R   24h volume
#     USELESS   4.29%  12.87%   0.41%       8.1%     Rp2.70bn   taken
#     MOG       2.44%   7.32%   0.68%      17.9%     Rp53m      taken
#     MOODENG   1.57%   4.71%   0.86%      31.6%     Rp556m     over cap
#     SPX       1.20%   3.60%   0.56%      33.0%     Rp68m      over cap
#     PIPPIN    1.15%   3.45%   0.53%      33.6%     Rp1.62bn   over cap
#     WIF       1.17%   3.50%   3.67%     122.9%     Rp147m     unusable
#     DOGS      1.21%   3.63%   8.12%     241.3%     Rp9.5m     unusable
#
# MOG's 24h turnover looks thin next to PIPPIN's, and that nearly ruled it out.
# It should not have: the bot exits with MARKET orders, so what matters is the
# resting book, and MOG holds Rp25.2m of bids within 1% of top — a Rp127k exit
# is 0.50% of it. Depth, not turnover, is the liquidity question at this size.
#
# USELESS carries the widest stop in the book (12.87%), so it is the first coin
# where the equity/MAX_POSITIONS notional cap does NOT bind and a trade risks
# the full RISK_PCT. Its spread also spiked to 3.78% in one of the eight
# samples, so the 8.1% drag is a median, not a guarantee.
#
# BTC must stay on this list whatever else changes: strategy.regime() reads
# BTC's 1D timeframe to set the risk_on/neutral/risk_off gate for every other
# coin, and falls back to "neutral" if BTC is absent — which silently raises
# the ADX entry bar across the board.
WATCHLIST = ["BTC", "ETH", "SOL", "XRP", "DOGE", "AVAX", "LINK", "DOT", "LTC", "UNI",
             "PEPE", "FARTCOIN", "USELESS", "MOG"]

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

# Raised 4 -> 5 on 2026-09-21 at the user's request. This knob does TWO jobs,
# and the second one is easy to miss: it is the slot count AND the divisor in
# the per-trade notional cap (risk.position_size caps notional at
# equity / MAX_POSITIONS). So every position is now capped at 20% of equity
# instead of 25% — a 20% smaller trade wherever that cap binds, which on this
# book is nearly always. Total possible exposure is unchanged at 100%.
#
# Replayed over 488 snapshots (2026-09-01 -> 09-21), pure engine, no overlay:
#   cap 3   20 trades  +14.78%   maxDD 9.97%
#   cap 4   27 trades  +18.82%   maxDD 8.42%
#   cap 5   31 trades  +22.04%   maxDD 7.59%   <- best return AND lowest DD
#   cap 6   34 trades  +20.07%   maxDD 6.80%
# More, smaller positions diversified the book faster than the shrinking size
# cost it, up to 5; at 6 the per-trade size starts losing more than breadth
# adds. That is a peak, not a plateau, so 5 is not a safe default to drift past.
#
# CAVEAT measured at the same time: replayed against the daily policy overlay
# AS IT ACTUALLY STOOD, cap 5 scored WORSE than cap 4 (+1.63% vs +2.27%) on the
# identical 22 trades. The routine wrote 2-4 on most days, the engine takes the
# smaller of policy and config, so the extra slot never opened while the 20%
# size cut applied to every trade. Raising this number is only an improvement
# if the daily routine actually writes 5 — which is why that prompt was changed
# in the same commit. If the routine starts capping at 4 again, this should go
# back to 4 rather than sit here paying the size cut for nothing.
MAX_POSITIONS = 5          # hard cap; the daily policy may lower it, never raise it
RISK_PCT = 0.015            # equity fraction risked per trade
STOP_ATR_MULT = 3.0         # initial stop distance = 1R
# REVERTED 2026-09-15 from 2.0 back to 4.0, and the reasoning that produced 2.0
# is refuted. That change argued a trail wider than the stop "can never protect
# anything"; true, but irrelevant — TP_R does the exiting here, and a trail
# tighter than 1R just amputates winners on their way to it.
#
# TRAIL_ATR_MULT is measured in ATR while 1R = STOP_ATR_MULT * ATR, so a trail
# of 2.0 sits only 0.67R below the high water. Any 0.67R pullback exits, and a
# move heading to +2.5R retraces that routinely. Measured over 335 snapshots
# with the daily policy overlay replayed from git (cryptoindodax.replay):
#
#     trail 1.5  -0.25%   0% true win     trail 4.0  +2.67%   40% true win
#     trail 2.0  +0.41%   9% true win     trail 6.0  +2.67%   40% true win
#     trail 3.0  +2.67%  40% true win     trail 8.0  +2.67%   40% true win
#
# Everything from 3.0 up is identical because the trail simply never binds
# before TP; everything at or below 2.0 collapses. That is a plateau with a
# cliff, not a peak, so 4.0 sits mid-plateau rather than on the 3.0 edge.
# RISK_OFF stays below the normal trail to keep its tighten-in-a-downtrend
# purpose, though no risk_off trade exists yet to test it on.
TRAIL_ATR_MULT = 4.0        # trail distance once >= +1R
RISK_OFF_TRAIL_ATR_MULT = 3.0  # tighter trail while BTC regime is risk_off
TP_R = 2.5                  # hard take-profit in R multiples

# --- profit-lock ladder: the take-profit side of the trail ----------------
# Added 2026-09-18 after UNI: filled 121.172 on 09-17 02:14 UTC, peaked
# +12.24% (+1.53R) at 22:07, and one -7.6% hour later left on the 3xATR
# risk_off trail at +3.94% — then bounced to +15% within two hours. TP_R=2.5
# needs 7.5 ATR from entry (+20% on UNI at that day's ATR) so it never came
# into play; the only exits available were the trail, sitting 8% under the
# peak, and the stop.
#
# Rungs are (activate at +R of PEAK gain, trail distance in current ATR).
# Once the high-water mark has reached a rung, a lock level is kept at
# high_water - trail*ATR and only ever ratchets up. A 1H close at or under it
# exits with reason "lock". The initial stop, the +1R trail and TP_R are all
# untouched; a rung can only tighten, never loosen.
#
# Activation is in R (like TP_R) and distance in ATR (like the trail) so one
# rule scales to every coin: 1 ATR is ~0.5% on BTC and ~4% on USELESS. A flat
# "2% giveback" would be a single ordinary bar on a meme coin and four bars
# on BTC — replay --ladder scores the flat version beside this one for
# comparison, and it is not what ships.
#
# No rung activates below +1.5R by design. The refuted 2026-09-07 trail
# (2 ATR from +1R) lives below that line, and so does everything the doctrine
# scores as SCRATCH: a lock exit under +1R is not a win (see scorecard.py),
# so there is no point arming one that can only deliver that.
#
# WHAT THE HARNESS SAID (replay --ladder, 2026-09-18, 403 snapshots), kept here
# so nobody has to rediscover it: this is a variance trade, not an edge.
#   historical policy, FULL:  live +2.65% / true win 33%  ->  +0.82% / 38%
#     it changed exactly two trades, both +2.5R take-profits cut to locks at
#     +1.34R (UNI 09-12) and +1.19R (DOT 09-17), about Rp10k between them
#   pure engine, FULL (19 tr): +8.91% / 39%  ->  +6.95% / 47%,  PF 1.50 -> 1.56
#   pure engine, BULL:         +7.82% -> +8.44%, maxDD 4.2% -> 2.5%  (helps)
#   pure engine, BEAR:         +0.56% -> -1.93%
# The live UNI trade it was built for is in neither path. The pure engine
# entered UNI two hours earlier on the normal 4xATR trail, HELD through the
# 23:07 dip and was still open at +2.24R when the history ends; live left at
# +0.49R because the policy hint had the regime at risk_off, which uses the
# 3xATR trail. A lock would have cut that pure-path trade at +1.49R as well.
# The pre-declared rule in replay.py would not ship this. It ships on the
# user's explicit instruction of 2026-09-18 — a preference for smaller, more
# frequent wins over the occasional full +2.5R — not on evidence.
# To turn it off: PROFIT_LOCK_RUNGS = ()
PROFIT_LOCK_RUNGS = ((1.5, 1.0),)
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


def fmt_price(value) -> str:
    """A price that stays readable below Rp1.

    fmt_idr rounds to whole rupiah, which is right for balances and useless for
    prices: PEPE trades at Rp0,064 a token, so every stop, lock and fill it
    logged came out as a flat "Rp0". Anything at or above Rp100 keeps the
    familiar rounded form; below that the decimals are the information.
    """
    if value is None:
        return "-"
    v = float(value)
    if abs(v) >= 100:
        return fmt_idr(v)
    text = f"{v:.8f}".rstrip("0").rstrip(".") if abs(v) < 1 else f"{v:,.2f}"
    return "Rp" + text.replace(",", "|").replace(".", ",").replace("|", ".")


def fmt_idr(amount) -> str:
    """Format a rupiah amount the way Indonesian users read it: Rp1.234.567."""
    if amount is None:
        return "-"
    sign = "-" if amount < 0 else ""
    return f"{sign}Rp{abs(round(amount)):,}".replace(",", ".")
