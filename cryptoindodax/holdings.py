"""The hourly holdings message: what is held, where it exits, and at what price.

Sent by the trader at the end of every cycle, so the numbers in Telegram are
the ones the bot just finished acting on rather than a separate opinion.

WHICH STOP IT SHOWS

A position can carry two floors at once: the ATR trail (`stop`) and the profit
lock the ladder armed (`lock`). Whichever is HIGHER is the one that will fire,
so that is the one shown, labelled with which it is. Showing the trail while a
lock sits above it would tell the reader they have further to fall than they do.

WHICH PRICE IT USES

Live tickers, not the hourly snapshot close the exits were judged against. The
snapshot can be ten minutes old by the time the trader finishes, and a report
that disagrees with the Indodax app is a report nobody trusts. The prices are
therefore a few minutes fresher than the decisions above them — fine for
reading, which is all this is for. It is a message, not a trigger: nothing here
buys, sells or moves a level.
"""
import math
from datetime import datetime, timedelta, timezone

from . import config

WIB = timezone(timedelta(hours=7), "WIB")


def levels(pos):
    """Exit geometry for one open position: take-profit and the live floor."""
    entry = pos.get("entry_price")
    initial = pos.get("initial_stop")
    r = (entry - initial) if (entry and initial) else None
    tp = entry + config.TP_R * r if (r and r > 0) else None

    stop, lock = pos.get("stop"), pos.get("lock")
    floor, kind = stop, "trail"
    if lock and (not stop or lock > stop):
        floor, kind = lock, "profit lock"
    return {"entry": entry, "tp": tp, "floor": floor, "floor_kind": kind}


def _decimals(entry):
    """Decimal places that give one coin's prices a common, readable scale.

    `config.fmt_price` trims trailing zeros, which is right for a one-off line
    but ragged in a block: MOG would print 0,00218 above 0,002225 above
    0,00258452, and nothing lines up. Four significant figures off the entry
    price fixes every number in that position to the same width.
    """
    if not entry or entry >= 100:
        return 0
    if entry >= 1:
        return 2
    return min(8, int(math.floor(-math.log10(abs(entry)))) + 4)


def _price(value, decimals):
    """Rupiah at a fixed scale, Indonesian separators, nothing trimmed."""
    if value is None:
        return "-"
    text = f"{float(value):,.{decimals}f}"
    return "Rp" + text.replace(",", "|").replace(".", ",").replace("|", ".")


def _from_entry(price, entry):
    return ((price / entry - 1) * 100) if (price and entry) else None


def render(positions, prices, cash=None, equity=None, now=None, max_positions=None):
    """The Telegram message. Pure — the trader supplies the data.

    `positions` are ledger open positions; `prices` is {symbol: current price}.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(WIB)
    max_positions = config.MAX_POSITIONS if max_positions is None else max_positions
    lines = ["📈 CryptoIndodaxBot — holdings",
             now.strftime("%d %b %Y %H:%M WIB"), ""]

    if not positions:
        lines.append("No open positions — everything is in rupiah.")
        if cash is not None:
            lines.append(f"Cash {config.fmt_idr(cash)}")
        return "\n".join(lines)

    for pos in sorted(positions, key=lambda p: p["symbol"]):
        sym = pos["symbol"]
        price = prices.get(sym)
        lv = levels(pos)
        dp = _decimals(lv["entry"])
        gain = _from_entry(price, lv["entry"])

        lines.append(f"{sym}  {config.fmt_pct(gain)}")
        lines.append(f"   now  {_price(price, dp)}   (buy {_price(lv['entry'], dp)})")
        if lv["tp"]:
            lines.append(f"   TP   {_price(lv['tp'], dp)}"
                         f"   {config.fmt_pct(_from_entry(lv['tp'], lv['entry']))} from buy")
        else:
            lines.append("   TP   - (no stop distance recorded)")
        if lv["floor"]:
            away = _from_entry(lv["floor"], price) if price else None
            tail = f"   {abs(away):.2f}% away".replace(".", ",") if away is not None else ""
            lines.append(f"   SL   {_price(lv['floor'], dp)}"
                         f"   {config.fmt_pct(_from_entry(lv['floor'], lv['entry']))} from buy"
                         f"   · {lv['floor_kind']}{tail}")
        else:
            lines.append("   SL   - (no stop on record)")
        lines.append("")

    tail = [f"{len(positions)} of {max_positions} slots"]
    if cash is not None:
        tail.append(f"cash {config.fmt_idr(cash)}")
    if equity is not None:
        tail.append(f"equity {config.fmt_idr(equity)}")
    lines.append(" · ".join(tail))
    return "\n".join(lines).rstrip()


def prices_for(positions, tickers, fallback=None):
    """{symbol: live last price}, falling back to the snapshot close per coin."""
    fallback = fallback or {}
    out = {}
    for pos in positions:
        sym = pos["symbol"]
        tick = (tickers or {}).get(config.pair_id(sym))
        out[sym] = tick["last"] if tick else fallback.get(sym)
    return out
