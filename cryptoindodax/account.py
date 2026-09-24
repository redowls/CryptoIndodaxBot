"""Honest read-only account snapshot for the daily routine.

Why this module exists at all: `broker.get_account()` values coins from the
`price_by_symbol` argument and *ignores any coin it has no price for*. Called
bare it therefore returns CASH as equity, silently, with no error — and a
partly-deployed book reads as a catastrophic loss that never happened.

That trap has now fired three times in the daily digest (2026-09-12,
2026-09-22, 2026-09-24; on 09-24 it printed Rp648.537 against a true
Rp998.017, hiding Rp349.480 of open positions). Twice it was written up in
`memory/insights.md` and twice it recurred, because the insight did not edit
the thing that produces the number. This module is that edit: the routine
calls `python -m cryptoindodax.account` and cannot express the price-less
form.

Marks are the latest snapshot's 1H closes — the same source the trader,
dashboard and scorecard use, so all four agree by construction. Strictly
read-only: no orders, no ledger writes.
"""

from __future__ import annotations

from cryptoindodax import broker, config, replay


def snapshot(session=None):
    """Return the account valued at the latest snapshot's 1H closes.

    Raises if no snapshot history exists — refusing to report is better than
    reporting cash as equity, which is the whole point of this module.
    """
    history = replay.load_history()
    if not history:
        raise RuntimeError(
            "no snapshot history: cannot value open positions, and reporting "
            "cash as equity is exactly the bug this module prevents"
        )
    marks = replay._closes(history[-1][1])
    acct = broker.get_account(price_by_symbol=marks, session=session)

    coins = []
    unpriced = []
    for sym, bal in acct["balances"].items():
        if sym == "IDR":
            continue
        qty = bal["free"] + bal["locked"]
        if qty <= 0:
            continue
        price = marks.get(sym)
        if price:
            coins.append((sym, qty, qty * float(price)))
        else:
            unpriced.append((sym, qty))

    coins.sort(key=lambda r: r[2], reverse=True)
    return {
        "equity": acct["equity"],
        "cash": acct["cash"],
        "coin_value": sum(c[2] for c in coins),
        "coins": coins,
        "unpriced": unpriced,
        "status": acct["status"],
        "as_of": history[-1][0],
    }


def main():
    try:
        a = snapshot()
    except Exception as exc:  # noqa: BLE001 - a live-money report must say so
        print(f"ACCOUNT READ FAILED: {exc}")
        raise SystemExit(1)

    print(
        f"equity {config.fmt_idr(a['equity'])} "
        f"| cash {config.fmt_idr(a['cash'])} "
        f"| coin {config.fmt_idr(a['coin_value'])}"
    )
    print(f"marks: {a['as_of']:%Y-%m-%dT%H:%M}Z 1H closes | status {a['status']}")
    for sym, qty, value in a["coins"]:
        print(f"  {sym:<9}{qty:>18.8f}  {config.fmt_idr(value)}")
    for sym, qty in a["unpriced"]:
        # Counted nowhere in equity — say so loudly rather than drop it.
        print(f"  {sym:<9}{qty:>18.8f}  NO MARK - excluded from equity")


if __name__ == "__main__":
    main()
