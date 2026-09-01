import json
from datetime import datetime, timezone

from . import config


def load_day(date_str):
    d = config.DATA_DIR / date_str
    if not d.exists():
        return []
    snaps = []
    for f in sorted(d.glob("*.json")):
        try:
            snaps.append(json.loads(f.read_text()))
        except Exception:
            continue
    return snaps


def _tf(symbol_entry, tf="1H"):
    return symbol_entry.get("timeframes", {}).get(tf, {})


def summarize(snaps):
    if not snaps:
        return {"date": None, "snapshot_count": 0, "coins": []}
    first, last = snaps[0], snaps[-1]
    first_by = {s["symbol"]: s for s in first.get("symbols", [])}
    coins = []
    for s in last.get("symbols", []):
        sym = s["symbol"]
        tf = _tf(s, "1H")
        if tf.get("status") != "ok":
            coins.append({"symbol": sym, "status": "error"})
            continue
        close = tf["last_close"]
        f_tf = _tf(first_by.get(sym, {}), "1H")
        start = f_tf.get("last_close") if f_tf.get("status") == "ok" else None
        change = ((close - start) / start * 100) if start else None
        e8, e20, e55 = tf.get("ema8"), tf.get("ema20"), tf.get("ema55")
        if e8 and e20 and e55:
            trend = "up" if e8 > e20 > e55 else "down" if e8 < e20 < e55 else "mixed"
        else:
            trend = "unknown"
        atr = tf.get("atr14")
        atr_pct = (atr / close * 100) if (atr and close) else None
        rsi = tf.get("rsi14")
        adx = tf.get("adx14")
        coins.append({
            "symbol": sym, "status": "ok", "close": close,
            "change_pct_day": round(change, 2) if change is not None else None,
            "trend": trend,
            "rsi14": round(rsi, 1) if rsi is not None else None,
            "adx14": round(adx, 1) if adx is not None else None,
            "atr_pct": round(atr_pct, 2) if atr_pct is not None else None,
        })
    return {"date": last.get("captured_at"), "snapshot_count": len(snaps), "coins": coins}


def main():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print(json.dumps(summarize(load_day(today)), indent=2))


if __name__ == "__main__":
    main()
