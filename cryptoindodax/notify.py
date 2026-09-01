"""Telegram notifications (best-effort — a notify failure never blocks trading).

Run `python -m cryptoindodax.notify resolve` after pressing Start on the bot to
discover the chat id, or `... test` to send a delivery check.
"""
import sys

import requests

from . import config


def send(text):
    if not (config.TELEGRAM_TOKEN and config.TELEGRAM_CHAT_ID):
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": config.TELEGRAM_CHAT_ID, "text": text},
            timeout=15,
        )
        return r.status_code == 200
    except requests.RequestException:
        return False


def resolve_chat_id():
    """Chat ids that have messaged this bot. Empty until someone presses Start."""
    if not config.TELEGRAM_TOKEN:
        return []
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/getUpdates", timeout=15)
        updates = r.json().get("result", [])
    except (requests.RequestException, ValueError):
        return []
    seen = {}
    for u in updates:
        chat = (u.get("message") or u.get("channel_post") or {}).get("chat") or {}
        if chat.get("id"):
            seen[chat["id"]] = chat.get("username") or chat.get("title") or chat.get("first_name")
    return sorted(seen.items())


def _main(argv):
    cmd = argv[1] if len(argv) > 1 else "resolve"
    if cmd == "resolve":
        found = resolve_chat_id()
        if not found:
            print("No chats yet — open Telegram, find the bot, and press Start.")
            return 1
        for cid, who in found:
            print(f"TELEGRAM_CHAT_ID={cid}  ({who})")
        return 0
    if cmd == "test":
        ok = send("CryptoIndodaxBot: delivery test ✅")
        print("sent" if ok else "FAILED (check TELEGRAM_TOKEN / TELEGRAM_CHAT_ID)")
        return 0 if ok else 1
    print(f"usage: python -m cryptoindodax.notify [resolve|test]")
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
