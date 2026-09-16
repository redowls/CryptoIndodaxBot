"""Experimental regime gates — for the replay harness ONLY.

Nothing here is wired into live trading. `trader.py` calls `strategy.regime`
and will keep doing so until one of these variants earns its place in the
harness first. Treat every class below as a hypothesis, not a recommendation.

WHY THIS EXISTS

Between 2026-09-07 and 2026-09-16 every watchlist coin fell (equal-weight
-7.67%, LINK -16.6%) and the live gate reported `risk_on` for 24 of 24 hours on
all 14 days. It never went neutral once.

The cause is the shape of the test, not a bug: `strategy.regime` asks whether
BTC's daily EMA stack has fully INVERTED to 8<20<55. After a strong rally that
ordering survives weeks of decline — BTC's stack read UP with ADX 42-52 the
entire way down. The gate is a trend-reversal detector being used as a risk
switch, and reversal confirmation arrives long after the damage.

Daily RSI did track the damage over the same window (70.8 -> 49.2) and the
daily close dipped under its own EMA20 on 09-11. Those are the signals these
variants are built from.

WHAT THE HARNESS ACTUALLY SAID — read this before reviving the idea

Every variant here scored IDENTICALLY to the live gate on all three windows.
The idea does not work, and the reason is worth keeping:

The two largest losing positions were opened 2026-09-07T18 and 2026-09-08T19,
when breadth was 100% (every coin above its own daily EMA20) and BTC was
comfortably above its EMA20 with RSI 63-67. No gate can be defensive on data
that says nothing is wrong yet. The gates that do fire — from 09-11 — fire at
hours when either no entry was taken or the entry was UNI, the one winner.

So an earlier claim that "close < EMA20 would have fired on 09-11, ahead of the
last three losers" was wrong: it was read off daily samples, and at the actual
hourly entry timestamps BTC was above its EMA20 in four of five cases.

What DID beat the live configuration was the AlwaysRiskOff control, in all
three windows including the bull run. That is not a timing result — it is the
risk_off ENTRY conditions (green on the day, RSI <= 65, ADX bar 25, BTC not a
candidate) being better entry conditions in every regime. The lead is in the
entry filter, not the regime clock.

THE TRAP THESE VARIANTS MUST AVOID

Any gate tuned on a decline will avoid that decline. The question is whether it
also destroys the bull window that preceded it, where four take-profits landed
in three days. Every variant is therefore scored on the bull window, the bear
window and the whole history separately. A variant that only wins on the bear
window is curve-fitted, not better.
"""
from . import strategy


def _btc_1d(snap):
    btc = next((s for s in snap.get("symbols", []) if s.get("symbol") == "BTC"), None)
    if not btc:
        return None
    tf = btc.get("timeframes", {}).get("1D", {})
    return tf if tf.get("status") == "ok" and tf.get("ema20") else None


class Current:
    """The live rule, reproduced here so it is scored on identical footing."""
    name = "current (EMA stack inversion)"

    def __call__(self, snap):
        return strategy.regime(snap)


class RsiGate:
    """Risk off on BTC's daily RSI, which registered this decline in real time.

    RSI is bounded and mean-reverting, so it cannot lag the way an EMA ordering
    does. The cost is that it says nothing about trend direction: a quiet
    sideways market sits near 50 and would be treated as defensive.
    """
    name = "BTC 1D RSI"

    def __init__(self, off_below=50.0, cautious_below=55.0):
        self.off, self.cautious = off_below, cautious_below

    def __call__(self, snap):
        tf = _btc_1d(snap)
        if not tf or tf.get("rsi14") is None:
            return "neutral"
        rsi = tf["rsi14"]
        if rsi < self.off:
            return "risk_off"
        if rsi < self.cautious:
            return "neutral"
        return strategy.regime(snap)


class Ema20Gate:
    """Risk off while BTC's daily close is under its own EMA20.

    This is the earliest signal in the data — it fired 2026-09-11, ahead of the
    ETH, UNI and XRP entries that lost Rp18.010 between them. It is also the
    most whipsaw-prone: over that window it toggled on 09-11, off 09-12, on
    09-13 and 09-14, off 09-15, on 09-16.
    """
    name = "BTC 1D close < EMA20"

    def __call__(self, snap):
        tf = _btc_1d(snap)
        if not tf or not tf.get("last_close"):
            return "neutral"
        if tf["last_close"] < tf["ema20"]:
            return "risk_off"
        return strategy.regime(snap)


class Ema20Confirmed:
    """Ema20Gate with hysteresis: N consecutive snapshots before it flips.

    Whipsaw is what makes a fast gate expensive — each flip can close a position
    and pay the round trip again. Requiring confirmation in both directions
    trades a few hours of lateness for far fewer flips. Stateful, so a fresh
    instance is required per replay run.
    """
    def __init__(self, bars=6):
        self.bars = bars
        self.below = 0
        self.above = 0
        self.state = None
        self.name = f"BTC 1D < EMA20, {bars}h confirm"

    def __call__(self, snap):
        tf = _btc_1d(snap)
        if not tf or not tf.get("last_close"):
            return "neutral"
        if tf["last_close"] < tf["ema20"]:
            self.below += 1
            self.above = 0
        else:
            self.above += 1
            self.below = 0
        if self.below >= self.bars:
            self.state = "risk_off"
        elif self.above >= self.bars:
            self.state = None
        return "risk_off" if self.state == "risk_off" else strategy.regime(snap)


class BreadthGate:
    """Regime from how many watchlist coins are in an uptrend, not from BTC alone.

    The live gate stakes everything on one symbol. Breadth asks the whole book,
    which is what actually deteriorated first here: alts were falling harder
    than BTC (LINK -16.6% against BTC -4.9%) while the BTC-only gate saw nothing.
    """
    name = "breadth of 1D uptrends"

    def __init__(self, off_below=0.35, cautious_below=0.55):
        self.off, self.cautious = off_below, cautious_below

    def __call__(self, snap):
        ups = total = 0
        for coin in snap.get("symbols", []):
            tf = coin.get("timeframes", {}).get("1D", {})
            if tf.get("status") != "ok" or not tf.get("ema55"):
                continue
            total += 1
            if strategy.stack(tf) == "UP":
                ups += 1
        if not total:
            return "neutral"
        frac = ups / total
        if frac < self.off:
            return "risk_off"
        if frac < self.cautious:
            return "neutral"
        return strategy.regime(snap)


class RsiOrEma20:
    """Either early warning is enough — the union of the two fastest signals."""
    name = "RSI<50 OR close<EMA20"

    def __init__(self):
        self.rsi = RsiGate()
        self.ema = Ema20Gate()

    def __call__(self, snap):
        a, b = self.rsi(snap), self.ema(snap)
        order = {"risk_off": 2, "neutral": 1, "risk_on": 0}
        return a if order[a] >= order[b] else b


class BreadthFast:
    """Breadth measured on price-vs-EMA20, not on stack ordering.

    BreadthGate inherited the very lag it was meant to fix: it counts coins
    whose 1D EMA stack is UP, and those stacks stayed UP through the whole
    decline (0 of 13 live entries had a 1D stack down). Comparing each coin's
    daily close to its own EMA20 responds in days instead of weeks, and it is
    the alts that led this drawdown — LINK -16.6% against BTC -4.9% — so asking
    the whole book is the point.
    """
    def __init__(self, off_below=0.4, cautious_below=0.6):
        self.off, self.cautious = off_below, cautious_below
        self.name = f"breadth: close>EMA20 (<{off_below:.0%} off)"

    def _frac(self, snap):
        up = total = 0
        for coin in snap.get("symbols", []):
            tf = coin.get("timeframes", {}).get("1D", {})
            if tf.get("status") != "ok" or not tf.get("ema20") or not tf.get("last_close"):
                continue
            total += 1
            if tf["last_close"] > tf["ema20"]:
                up += 1
        return (up / total) if total else None

    def __call__(self, snap):
        frac = self._frac(snap)
        if frac is None:
            return "neutral"
        if frac < self.off:
            return "risk_off"
        if frac < self.cautious:
            return "neutral"
        return strategy.regime(snap)


class BreadthRsi:
    """Breadth on daily RSI — how much of the book still has momentum."""
    def __init__(self, off_below=0.4, cautious_below=0.6):
        self.off, self.cautious = off_below, cautious_below
        self.name = f"breadth: 1D RSI>50 (<{off_below:.0%} off)"

    def __call__(self, snap):
        up = total = 0
        for coin in snap.get("symbols", []):
            tf = coin.get("timeframes", {}).get("1D", {})
            if tf.get("status") != "ok" or tf.get("rsi14") is None:
                continue
            total += 1
            if tf["rsi14"] > 50:
                up += 1
        if not total:
            return "neutral"
        frac = up / total
        if frac < self.off:
            return "risk_off"
        if frac < self.cautious:
            return "neutral"
        return strategy.regime(snap)


class AlwaysRiskOff:
    """The control. Not a proposal — it isolates how much of any improvement
    comes from the risk_off ENTRY conditions (green on the day, RSI <= 65,
    ADX bar 25) rather than from the gate's timing."""
    name = "control: always risk_off"

    def __call__(self, snap):
        return "risk_off"


def variants():
    """Fresh instances every call — several of these carry state."""
    return [Current(), RsiGate(), Ema20Gate(), Ema20Confirmed(6),
            BreadthGate(), BreadthFast(), BreadthFast(0.6, 0.8), BreadthRsi(),
            BreadthRsi(0.6, 0.8), RsiOrEma20(), AlwaysRiskOff()]
