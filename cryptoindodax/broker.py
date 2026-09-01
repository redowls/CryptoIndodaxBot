"""Indodax TAPI v2 REST client.

Replaces CryptoAutoBot's Alpaca client. The differences that shape this module:

  * Auth is HMAC-SHA256 over the query string (GET/DELETE) or the urlencoded
    body (POST), sent as `Sign`, with the key in `X-APIKEY`. The legacy
    https://indodax.com/tapi (v1, SHA512, `Key` header) rejects v2-generation
    keys with invalid_version_key.
  * Indodax is a *spot* exchange: there are no positions, only balances. A
    balance carries no entry price, so the ledger — not the exchange — is the
    source of truth for what a position cost.
  * There is no close-position endpoint; exiting is a market SELL.
  * A market BUY is sized in IDR (`quoteOrderQty`); a market SELL is sized in
    coin (`quantity`). That asymmetry is why buy/sell have separate entrypoints.
  * Orders carry no average fill price; it is derived from myTrades fills.

⚠️ Indodax has no paper endpoint. Every call here moves real money.
"""
import hashlib
import hmac
import time
import urllib.parse

import requests

from . import config, net


class BrokerError(Exception):
    pass


if config.FORCE_IPV4:
    net.force_ipv4()


def _credentials():
    if not (config.INDODAX_KEY and config.INDODAX_SECRET):
        raise BrokerError(
            "Indodax credentials not configured (INDODAX_API_KEY/INDODAX_API_SECRET)")
    return config.INDODAX_KEY, config.INDODAX_SECRET


def _sign(secret, payload):
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def _request(method, path, params=None, session=None):
    """Signed TAPI v2 call. Returns the decoded JSON body.

    Params are sent in the query string for GET/DELETE and in the body for POST;
    the signature covers exactly the string that is sent.
    """
    key, secret = _credentials()
    params = dict(params or {})
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = config.RECV_WINDOW_MS
    encoded = urllib.parse.urlencode(params)
    headers = {
        "X-APIKEY": key,
        "Sign": _sign(secret, encoded),
        "Accept": "application/json",
        "User-Agent": config.USER_AGENT,
    }
    url = f"{config.TAPI_BASE_URL}{path}"
    kwargs = {"headers": headers, "timeout": 20}
    if method.upper() == "POST":
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        kwargs["data"] = encoded
    else:
        url = f"{url}?{encoded}"
    requester = (session or requests).request
    try:
        r = requester(method, url, **kwargs)
        body = r.json() if r.text else {}
    except ValueError as e:
        raise BrokerError(f"{method} {path}: non-JSON response") from e
    except requests.RequestException as e:
        raise BrokerError(f"{method} {path}: {e}") from e
    # v2 signals failure with a `code` field, regardless of HTTP status.
    if isinstance(body, dict) and body.get("code") not in (None, 0, 200):
        msg = body.get("msg") or body.get("error") or body
        raise BrokerError(f"{method} {path}: [{body.get('code')}] {msg}")
    return body


# --- account --------------------------------------------------------------

def get_balances(session=None):
    """{asset_upper: {"free": float, "locked": float}} for every non-zero asset."""
    body = _request("GET", "/api/v2/account", {"omitZeroBalances": "true"}, session)
    out = {}
    for b in body.get("balances", []) or []:
        try:
            out[str(b["asset"]).upper()] = {
                "free": float(b.get("free") or 0.0),
                "locked": float(b.get("locked") or 0.0),
            }
        except (KeyError, TypeError, ValueError):
            continue
    return out


def get_account(price_by_symbol=None, session=None):
    """Account snapshot with equity marked in IDR.

    Equity = free+locked IDR plus every watchlist coin balance valued at the
    price supplied in `price_by_symbol` (normally the current snapshot's 1H
    close). Coins with no supplied price are ignored rather than guessed.
    """
    balances = get_balances(session=session)
    prices = price_by_symbol or {}
    idr = balances.get("IDR", {"free": 0.0, "locked": 0.0})
    cash = idr["free"] + idr["locked"]
    equity = cash
    for sym, bal in balances.items():
        if sym == "IDR":
            continue
        price = prices.get(sym)
        if price:
            equity += (bal["free"] + bal["locked"]) * float(price)
    return {"equity": equity, "cash": cash, "balances": balances,
            "status": "ACTIVE" if cash or equity else "EMPTY"}


def get_positions(price_by_symbol=None, session=None, balances=None):
    """Derive positions from spot balances.

    A spot balance has no entry price or open/closed notion — anything the
    account holds above dust in a watchlist coin counts as a position. The
    ledger supplies the entry context; `avg_entry_price` here is only a
    fallback used when adopting a balance the ledger has never seen.
    """
    balances = get_balances(session=session) if balances is None else balances
    prices = price_by_symbol or {}
    out = []
    for sym in config.WATCHLIST:
        bal = balances.get(sym.upper())
        if not bal:
            continue
        qty = bal["free"] + bal["locked"]
        price = prices.get(sym)
        if qty <= 0:
            continue
        if price and qty * float(price) < config.DUST_IDR:
            continue  # dust left over from a previous fill, not a position
        out.append({
            "symbol": sym,
            "qty": qty,
            "free_qty": bal["free"],
            "avg_entry_price": float(price) if price else None,
            "current_price": float(price) if price else None,
        })
    return out


# --- orders ---------------------------------------------------------------

def _order_id(body):
    oid = body.get("orderId")
    return str(oid) if oid is not None else None


def market_buy_idr(symbol, idr_amount, client_order_id=None, session=None):
    """Market BUY sized in rupiah. Indodax requires quoteOrderQty (int) here."""
    params = {
        "symbol": config.pair(symbol),
        "side": "BUY",
        "type": "MARKET",
        "quoteOrderQty": int(idr_amount),
    }
    if client_order_id:
        params["newClientOrderId"] = client_order_id
    return _order_id(_request("POST", "/api/v2/order", params, session))


def market_sell_qty(symbol, qty, client_order_id=None, session=None):
    """Market SELL sized in coin."""
    params = {
        "symbol": config.pair(symbol),
        "side": "SELL",
        "type": "MARKET",
        "quantity": f"{float(qty):.8f}".rstrip("0").rstrip("."),
    }
    if client_order_id:
        params["newClientOrderId"] = client_order_id
    return _order_id(_request("POST", "/api/v2/order", params, session))


def get_order(symbol, order_id, session=None):
    """Normalised order state. Fill price comes from myTrades, not the order."""
    try:
        o = _request("GET", "/api/v2/order",
                     {"symbol": config.pair(symbol), "orderId": str(order_id)}, session)
    except BrokerError as e:
        if "-2013" in str(e) or "not found" in str(e).lower():
            return {"status": "not_found", "filled_avg_price": None, "filled_qty": 0.0}
        raise
    status = str(o.get("status") or "").upper()
    filled_qty = float(o.get("executedQty") or 0.0)
    price = None
    if filled_qty > 0:
        price = avg_fill_price(symbol, order_id, session=session)
        if price is None and o.get("price"):
            price = float(o["price"])  # LIMIT fallback; MARKET has no price field
    return {"status": status.lower(), "filled_avg_price": price, "filled_qty": filled_qty}


def avg_fill_price(symbol, order_id, session=None):
    """Quantity-weighted average price across an order's fills, or None."""
    try:
        body = _request("GET", "/api/v2/myTrades",
                        {"symbol": config.pair(symbol).lower(), "orderId": str(order_id)},
                        session)
    except BrokerError:
        return None
    fills = body.get("data") if isinstance(body, dict) else body
    total_qty = total_quote = 0.0
    for f in fills or []:
        try:
            qty = float(f.get("qty") or 0.0)
            price = float(f.get("price") or 0.0)
        except (TypeError, ValueError):
            continue
        if qty > 0 and price > 0:
            total_qty += qty
            total_quote += qty * price
    return (total_quote / total_qty) if total_qty > 0 else None


def cancel_order(symbol, order_id, session=None):
    _request("DELETE", "/api/v2/order",
             {"symbol": config.pair(symbol), "orderId": str(order_id)}, session)


def wait_for_fill(symbol, order_id, timeout_s=90, poll_s=3, sleep=time.sleep, session=None):
    """Poll until filled; on timeout cancel and report what (if anything) filled.

    Returns (status, filled_avg_price, filled_qty) — status: filled|canceled|partial.
    A market order normally fills instantly, but never assume it did.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        o = get_order(symbol, order_id, session=session)
        if o["status"] == "filled":
            return "filled", o["filled_avg_price"], o["filled_qty"]
        if o["status"] in ("cancelled", "canceled", "rejected", "expired", "not_found"):
            status = "partial" if o["filled_qty"] > 0 else "canceled"
            return status, o["filled_avg_price"], o["filled_qty"]
        sleep(poll_s)
    try:
        cancel_order(symbol, order_id, session=session)
    except BrokerError:
        pass
    o = get_order(symbol, order_id, session=session)
    if o["filled_qty"] > 0:
        return "partial", o["filled_avg_price"], o["filled_qty"]
    return "canceled", None, 0.0


def close_position(symbol, qty, session=None):
    """Exit a position by market-selling `qty`. Returns the order id.

    There is no Alpaca-style close endpoint on a spot exchange; the caller is
    responsible for passing a quantity that does not exceed the free balance.
    """
    return market_sell_qty(symbol, qty, session=session)
