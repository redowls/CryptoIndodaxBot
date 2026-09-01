import json
from datetime import datetime, timedelta, timezone

from . import config, data, indicators


def _start_for(tf_key):
    days = config.LOOKBACK_DAYS.get(tf_key, 30)
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def compute_for_bars(bars):
    if not bars:
        return None
    closes = [b["c"] for b in bars]
    highs = [b["h"] for b in bars]
    lows = [b["l"] for b in bars]
    vols = [b["v"] for b in bars]
    return {
        "last_close": closes[-1],
        "last_time": bars[-1]["t"],
        "ema8": indicators.ema(closes, 8),
        "ema20": indicators.ema(closes, 20),
        "ema55": indicators.ema(closes, 55),
        "rsi14": indicators.rsi(closes, config.RSI_PERIOD),
        "atr14": indicators.atr(highs, lows, closes, config.ATR_PERIOD),
        "adx14": indicators.adx(highs, lows, closes, config.ADX_PERIOD),
        "vol20": indicators.vol_avg(vols, config.VOL_PERIOD),
        "last_vol": vols[-1],
        "bar_count": len(bars),
    }


def snapshot_symbol(sym):
    pair = config.pair(sym)
    out = {"symbol": sym, "pair": pair, "status": "ok", "timeframes": {}}
    for tf_key, tf_api in config.TIMEFRAMES.items():
        try:
            bars = data.fetch_bars(pair, tf_api, start=_start_for(tf_key))
            ind = compute_for_bars(bars)
            if ind is None:
                out["timeframes"][tf_key] = {"status": "no_data"}
            else:
                out["timeframes"][tf_key] = {"status": "ok", **ind}
        except Exception as e:
            out["status"] = "partial"
            out["timeframes"][tf_key] = {"status": "error", "error": str(e)}
    return out


def run():
    now = datetime.now(timezone.utc)
    date_dir = config.DATA_DIR / now.strftime("%Y-%m-%d")
    date_dir.mkdir(parents=True, exist_ok=True)
    snap = {"captured_at": now.isoformat(), "symbols": []}
    for sym in config.WATCHLIST:
        try:
            snap["symbols"].append(snapshot_symbol(sym))
        except Exception as e:
            snap["symbols"].append({"symbol": sym, "status": "error", "error": str(e)})
    path = date_dir / f"{now.strftime('%H')}.json"
    path.write_text(json.dumps(snap, indent=2))
    return path


if __name__ == "__main__":
    print(f"wrote {run()}")
