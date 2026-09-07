"""Telegram command listener — currently one command, /saldo.

Delivery model: cron polls getUpdates once a minute, exactly like every other
moving part of this bot. There is no webhook and no long-running process, so
there is nothing new to keep alive, and a missed minute self-heals on the next
run because the update offset is persisted.

Two things this module is strict about:

  * **Only whitelisted chats get an answer.** /saldo discloses the account's
    balances and cost basis. Anyone who finds the bot on Telegram can type it,
    so a message from a chat id outside TELEGRAM_CHAT_IDS is logged and
    dropped without a reply — not even an error, which would confirm the bot
    is live to a stranger.
  * **Cost basis is never invented.** A spot balance carries no entry price;
    the only honest source is the ledger's open positions. A coin the ledger
    has never seen (adopted balance, manual buy on the Indodax app) is shown
    with its value but no P/L, and is excluded from the totals rather than
    being marked at its current price for a fake break-even.
"""
import json
import sys
from datetime import datetime, timedelta, timezone

import requests

from . import broker, config, data, notify

STATE_PATH = config.ROOT / "data" / "telegram_state.json"
WIB = timezone(timedelta(hours=7), "WIB")


def log(msg):
    print(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {msg}", flush=True)


# --- formatting -----------------------------------------------------------

def fmt_pct(value):
    """Percent the way Indonesian users read it: +1,23% / -0,55%."""
    if value is None:
        return "-"
    return f"{value:+.2f}%".replace(".", ",")


def fmt_qty(qty):
    """Up to 8 decimals, trailing zeros trimmed, comma as the decimal mark."""
    s = f"{qty:.8f}".rstrip("0").rstrip(".")
    return (s or "0").replace(".", ",")


# --- report ---------------------------------------------------------------

def build_saldo_report(cash, holdings, now=None):
    """Render the /saldo message.

    `holdings` is a list of dicts: symbol, qty, price (current, IDR) and
    entry_price (IDR, or None when the ledger has no record of the buy).
    Pure function — the poller supplies the data so this can be tested without
    touching Telegram or Indodax.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(WIB)
    lines = ["💰 CryptoIndodaxBot — Saldo",
             now.strftime("%d %b %Y %H:%M WIB"), ""]

    coin_value = sum(h["qty"] * h["price"] for h in holdings if h.get("price"))
    priced = [h for h in holdings if h.get("price") and h.get("entry_price")]
    cost = sum(h["qty"] * h["entry_price"] for h in priced)
    now_value = sum(h["qty"] * h["price"] for h in priced)
    pnl = now_value - cost
    pnl_pct = (pnl / cost * 100) if cost else None

    lines += [f"Total aset      : {config.fmt_idr(cash + coin_value)}",
              f"Saldo tersedia  : {config.fmt_idr(cash)}",
              f"Nilai koin      : {config.fmt_idr(coin_value)}"]
    if priced:
        lines.append(f"Untung/rugi     : {config.fmt_idr(pnl)} ({fmt_pct(pnl_pct)})")
    lines.append("")

    if not holdings:
        lines.append("Tidak ada koin yang dipegang — semua dalam rupiah.")
        return "\n".join(lines)

    untracked = False
    for h in sorted(holdings, key=lambda x: -(x["qty"] * (x.get("price") or 0))):
        price, entry = h.get("price"), h.get("entry_price")
        value = h["qty"] * price if price else None
        lines.append(f"📊 {h['symbol']}")
        lines.append(f"   Qty        : {fmt_qty(h['qty'])}")
        if entry:
            lines.append(f"   Harga beli : {config.fmt_idr(entry)}")
            lines.append(f"   Nilai beli : {config.fmt_idr(h['qty'] * entry)}")
        else:
            untracked = True
            lines.append("   Harga beli : - (tidak tercatat di ledger)")
        lines.append(f"   Harga kini : {config.fmt_idr(price) if price else '- (harga tidak tersedia)'}")
        if value is not None:
            lines.append(f"   Nilai kini : {config.fmt_idr(value)}")
        if entry and price:
            d = value - h["qty"] * entry
            lines.append(f"   Untung/rugi: {config.fmt_idr(d)} "
                         f"({fmt_pct(d / (h['qty'] * entry) * 100)})")
        lines.append("")

    if untracked:
        lines.append("Catatan: koin tanpa harga beli tidak dibeli oleh bot, "
                     "jadi tidak ikut dihitung di untung/rugi total.")
    return "\n".join(lines).rstrip()


def collect_holdings(balances, tickers, open_positions):
    """Join spot balances with live prices and the ledger's entry prices.

    Every non-IDR balance is reported, not just watchlist coins — a /saldo that
    hides an asset is worse than one that admits it has no cost basis for it.
    Dust below DUST_IDR is dropped so old fill remainders do not clutter it.
    """
    entry_by_sym = {p["symbol"].upper(): p.get("entry_price")
                    for p in open_positions or []}
    out = []
    for sym, bal in (balances or {}).items():
        if sym == "IDR":
            continue
        qty = (bal.get("free") or 0.0) + (bal.get("locked") or 0.0)
        if qty <= 0:
            continue
        t = (tickers or {}).get(config.pair_id(sym))
        price = t["last"] if t else None
        if price and qty * price < config.DUST_IDR:
            continue
        out.append({"symbol": sym, "qty": qty, "price": price,
                    "entry_price": entry_by_sym.get(sym.upper())})
    return out


def saldo_text(now=None):
    """Gather everything /saldo needs and render it. Never raises."""
    try:
        balances = broker.get_balances()
    except Exception as e:
        return f"⚠️ Gagal membaca akun Indodax: {e}"
    try:
        tickers = data.fetch_tickers()
    except Exception as e:
        tickers = {}
        log(f"ticker fetch failed, reporting without prices: {e}")
    try:
        led = json.loads((config.TRADES_DIR / "trades.json").read_text())
        open_positions = led.get("open", [])
    except (OSError, ValueError):
        open_positions = []
    idr = balances.get("IDR", {})
    cash = (idr.get("free") or 0.0) + (idr.get("locked") or 0.0)
    holdings = collect_holdings(balances, tickers, open_positions)
    text = build_saldo_report(cash, holdings, now=now)
    if not tickers:
        text += "\n\n⚠️ Harga pasar tidak bisa diambil — nilai koin tidak lengkap."
    return text


# --- polling --------------------------------------------------------------

def _load_state():
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        return {"offset": 0}


def _save_state(state):
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state))
    except OSError as e:
        log(f"could not persist offset ({e}) — commands may be answered twice")


def get_updates(offset):
    r = requests.get(
        f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/getUpdates",
        params={"offset": offset, "timeout": 0}, timeout=20)
    r.raise_for_status()
    body = r.json()
    if not body.get("ok"):
        raise RuntimeError(f"getUpdates not ok: {body}")
    return body.get("result", [])


def command_of(text):
    """First token as a bare command: '/saldo@Bot extra' -> 'saldo'."""
    if not text:
        return None
    token = text.strip().split()[0]
    if not token.startswith("/"):
        return None
    return token[1:].split("@")[0].lower()


def handle(cmd, chat_id, now=None):
    """Reply to one command. Returns True if something was sent."""
    if cmd == "saldo":
        return notify.send_to(chat_id, saldo_text(now=now))
    if cmd in ("start", "help"):
        return notify.send_to(chat_id, "CryptoIndodaxBot\n\n/saldo — total aset, "
                                       "saldo rupiah, dan rincian tiap koin "
                                       "(qty, harga beli, harga kini, untung/rugi).")
    return False


def poll(now=None):
    """One polling pass. Returns the number of commands answered."""
    if not config.TELEGRAM_TOKEN:
        log("TELEGRAM_TOKEN not configured — nothing to poll")
        return 0
    state = _load_state()
    try:
        updates = get_updates(int(state.get("offset", 0)))
    except Exception as e:
        log(f"getUpdates failed: {e}")
        return 0
    answered = 0
    allowed = {str(c) for c in config.TELEGRAM_CHAT_IDS}
    for u in updates:
        state["offset"] = int(u["update_id"]) + 1
        msg = u.get("message") or u.get("edited_message") or {}
        chat_id = str((msg.get("chat") or {}).get("id") or "")
        cmd = command_of(msg.get("text"))
        if not cmd:
            continue
        if chat_id not in allowed:
            # Silence, not an error: never confirm this bot to an unknown chat.
            log(f"ignoring /{cmd} from unauthorised chat {chat_id}")
            continue
        if handle(cmd, chat_id, now=now):
            answered += 1
            log(f"answered /{cmd} for chat {chat_id}")
    _save_state(state)
    return answered


def _main(argv):
    if len(argv) > 1 and argv[1] == "print":
        print(saldo_text())          # render locally without touching Telegram
        return 0
    poll()
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
