# CryptoAutoBot Phase A Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an automated crypto-analysis system that captures hourly market snapshots and sends a Claude-powered daily Telegram digest, committing all analysis to git as durable memory for a future Phase B trading bot.

**Architecture:** A lightweight Python package (`cryptoauto`) runs hourly via cron to fetch Alpaca crypto OHLCV per-symbol, compute indicators, and write a JSON snapshot. Once daily (00:00 WIB / 17:00 UTC) a Claude headless routine — driven by the existing `/root/claude-routines/run-routine.sh` scaffold — reads the day's snapshots, writes reasoning to `memory/`, pushes to GitHub, and prints a Telegram digest. No database; git + JSON files are the store.

**Tech Stack:** Python 3.12, `requests`, `pytest`, Alpaca public crypto bars API (v1beta3, keyless), the existing claude-routines cron/Telegram scaffold, GitHub HTTPS+PAT.

---

## File Structure

```
/root/CryptoAutoBot/
├── cryptoauto/
│   ├── __init__.py        # package marker
│   ├── config.py          # watchlist, paths, endpoint, indicator periods
│   ├── indicators.py      # pure functions: ema, rsi, atr, adx, vol_avg
│   ├── data.py            # Alpaca per-symbol OHLCV fetch (keyless or keyed)
│   ├── snapshot.py        # hourly entrypoint: fetch → compute → write JSON
│   └── digest.py          # deterministic aggregation of a day's snapshots
├── tests/
│   ├── test_indicators.py
│   ├── test_data.py
│   ├── test_snapshot.py
│   └── test_digest.py
├── data/snapshots/<date>/<HH>.json   # hourly captures (gitignored locally? NO — committed daily)
├── memory/analysis/<date>.md         # per-day Claude reasoning
├── memory/insights.md                # rolling accumulating learnings
├── logs/                             # gitignored
├── requirements.txt
├── .gitignore
├── .env.example
└── README.md

/root/claude-routines/
├── cryptoauto-daily.md               # Claude prompt (prints ONLY digest to stdout)
└── cryptoauto-daily.conf             # MODEL/tools/Telegram overrides for new bot
```

The repo `/root/CryptoAutoBot` and the design spec already exist (git initialized, one commit). Build on it.

---

### Task 0: Project scaffold

**Files:**
- Create: `/root/CryptoAutoBot/requirements.txt`
- Create: `/root/CryptoAutoBot/.gitignore`
- Create: `/root/CryptoAutoBot/.env.example`
- Create: `/root/CryptoAutoBot/cryptoauto/__init__.py`
- Create: `/root/CryptoAutoBot/cryptoauto/config.py`

- [ ] **Step 1: Create `requirements.txt`**

```
requests>=2.31
pytest>=8.0
```

- [ ] **Step 2: Create `.gitignore`**

```
.venv/
__pycache__/
*.pyc
.env
logs/
.pytest_cache/
```

- [ ] **Step 3: Create `.env.example`**

```
# Optional — Alpaca historical crypto bars are keyless. Set these only if you
# hit rate limits; data.py uses them automatically when present.
APCA_API_KEY_ID=
APCA_API_SECRET_KEY=
```

- [ ] **Step 4: Create `cryptoauto/__init__.py`** (empty file)

- [ ] **Step 5: Create `cryptoauto/config.py`**

```python
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data" / "snapshots"
MEMORY_DIR = ROOT / "memory"
LOG_DIR = ROOT / "logs"

# Alpaca-tradable universe (top liquidity; BNB/ADA not on Alpaca → LTC/UNI)
WATCHLIST = ["BTC", "ETH", "SOL", "XRP", "DOGE", "AVAX", "LINK", "DOT", "LTC", "UNI"]

def pair(sym: str) -> str:
    return f"{sym}/USD"

TIMEFRAMES = {"1H": "1Hour", "4H": "4Hour", "1D": "1Day"}
BARS_URL = "https://data.alpaca.markets/v1beta3/crypto/us/bars"
BARS_LIMIT = 200  # warms EMA55 / ADX(14) comfortably

EMA_PERIODS = (8, 20, 55)
RSI_PERIOD = 14
ATR_PERIOD = 14
ADX_PERIOD = 14
VOL_PERIOD = 20

ALPACA_KEY = os.getenv("APCA_API_KEY_ID")
ALPACA_SECRET = os.getenv("APCA_API_SECRET_KEY")
```

- [ ] **Step 6: Create the venv and install deps**

Run:
```bash
cd /root/CryptoAutoBot && python3 -m venv .venv && .venv/bin/pip -q install -r requirements.txt && mkdir -p logs memory
```
Expected: installs requests + pytest with no error.

- [ ] **Step 7: Commit**

```bash
cd /root/CryptoAutoBot && git add requirements.txt .gitignore .env.example cryptoauto/ && \
git commit -m "scaffold: project structure, config, deps"
```

---

### Task 1: Indicators (pure functions, TDD)

**Files:**
- Create: `cryptoauto/indicators.py`
- Test: `tests/test_indicators.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_indicators.py`:
```python
from cryptoauto import indicators as ind


def test_ema_basic():
    # period 3 over [1,2,3,4,5]: SMA seed=2.0, k=0.5 → 3.0 → 4.0
    assert ind.ema([1, 2, 3, 4, 5], 3) == 4.0


def test_ema_insufficient():
    assert ind.ema([1, 2], 3) is None


def test_rsi_all_gains_is_100():
    closes = list(range(1, 17))  # strictly increasing
    assert ind.rsi(closes, 14) == 100.0


def test_rsi_all_losses_is_0():
    closes = list(range(17, 1, -1))  # strictly decreasing
    assert ind.rsi(closes, 14) == 0.0


def test_rsi_flat_is_50():
    assert ind.rsi([5] * 16, 14) == 50.0


def test_rsi_insufficient():
    assert ind.rsi([1, 2, 3], 14) is None


def test_atr_constant_range():
    n = 16
    highs = [12.0] * n
    lows = [10.0] * n
    closes = [11.0] * n  # TR each bar = 2.0
    assert ind.atr(highs, lows, closes, 14) == 2.0


def test_atr_insufficient():
    assert ind.atr([1, 2], [1, 1], [1, 1], 14) is None


def test_adx_strong_uptrend_is_high():
    n = 40
    highs = [float(i + 2) for i in range(n)]
    lows = [float(i) for i in range(n)]
    closes = [float(i + 1) for i in range(n)]
    val = ind.adx(highs, lows, closes, 14)
    assert val is not None and val > 20


def test_adx_insufficient():
    assert ind.adx([1, 2], [1, 1], [1, 1], 14) is None


def test_vol_avg():
    assert ind.vol_avg(list(range(1, 21)), 20) == 10.5


def test_vol_avg_insufficient():
    assert ind.vol_avg([1, 2, 3], 20) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /root/CryptoAutoBot && .venv/bin/python -m pytest tests/test_indicators.py -v`
Expected: FAIL — `ModuleNotFoundError` / `AttributeError` (indicators not implemented).

- [ ] **Step 3: Write `cryptoauto/indicators.py`**

```python
from typing import Optional, Sequence


def ema(values: Sequence[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = sum(values[:period]) / period  # SMA seed
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def rsi(closes: Sequence[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        ch = closes[i] - closes[i - 1]
        if ch >= 0:
            gains += ch
        else:
            losses -= ch
    avg_gain = gains / period
    avg_loss = losses / period
    for i in range(period + 1, len(closes)):
        ch = closes[i] - closes[i - 1]
        g = ch if ch > 0 else 0.0
        l = -ch if ch < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
    if avg_loss == 0 and avg_gain == 0:
        return 50.0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def atr(highs, lows, closes, period: int = 14) -> Optional[float]:
    n = len(closes)
    if n < period + 1:
        return None
    trs = []
    for i in range(1, n):
        trs.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))
    a = sum(trs[:period]) / period
    for tr in trs[period:]:
        a = (a * (period - 1) + tr) / period
    return a


def adx(highs, lows, closes, period: int = 14) -> Optional[float]:
    n = len(closes)
    if n < 2 * period + 1:
        return None
    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)
        trs.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))

    def smooth(vals):
        s = sum(vals[:period])
        out = [s]
        for v in vals[period:]:
            s = s - s / period + v
            out.append(s)
        return out

    tr_s, pdm_s, mdm_s = smooth(trs), smooth(plus_dm), smooth(minus_dm)
    dxs = []
    for tr_v, pdm_v, mdm_v in zip(tr_s, pdm_s, mdm_s):
        if tr_v == 0:
            dxs.append(0.0)
            continue
        pdi = 100 * pdm_v / tr_v
        mdi = 100 * mdm_v / tr_v
        denom = pdi + mdi
        dxs.append(0.0 if denom == 0 else 100 * abs(pdi - mdi) / denom)
    if len(dxs) < period:
        return None
    a = sum(dxs[:period]) / period
    for dx in dxs[period:]:
        a = (a * (period - 1) + dx) / period
    return a


def vol_avg(volumes, period: int = 20) -> Optional[float]:
    if len(volumes) < period:
        return None
    return sum(volumes[-period:]) / period
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /root/CryptoAutoBot && .venv/bin/python -m pytest tests/test_indicators.py -v`
Expected: PASS (12 passed).

- [ ] **Step 5: Commit**

```bash
cd /root/CryptoAutoBot && git add cryptoauto/indicators.py tests/test_indicators.py && \
git commit -m "feat: indicator functions (ema/rsi/atr/adx/vol) with tests"
```

---

### Task 2: Alpaca data fetch (TDD with mocked HTTP)

**Files:**
- Create: `cryptoauto/data.py`
- Test: `tests/test_data.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_data.py`:
```python
import pytest
from cryptoauto import data


class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def test_fetch_bars_returns_symbol_list(monkeypatch):
    payload = {"bars": {"BTC/USD": [{"c": 1, "h": 2, "l": 0.5, "o": 1, "v": 10, "t": "x"}]}}
    monkeypatch.setattr(data.requests, "get", lambda *a, **k: _FakeResp(payload))
    bars = data.fetch_bars("BTC/USD", "1Hour", limit=5)
    assert isinstance(bars, list) and bars[0]["c"] == 1


def test_fetch_bars_missing_symbol_returns_empty(monkeypatch):
    monkeypatch.setattr(data.requests, "get", lambda *a, **k: _FakeResp({"bars": {}}))
    assert data.fetch_bars("UNI/USD", "1Hour") == []


def test_fetch_bars_http_error_raises_fetcherror(monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("dns fail")
    monkeypatch.setattr(data.requests, "get", boom)
    with pytest.raises(data.FetchError):
        data.fetch_bars("BTC/USD", "1Hour")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /root/CryptoAutoBot && .venv/bin/python -m pytest tests/test_data.py -v`
Expected: FAIL — module/attribute missing.

- [ ] **Step 3: Write `cryptoauto/data.py`**

```python
import requests

from . import config


class FetchError(Exception):
    pass


def fetch_bars(pair: str, timeframe: str, limit: int = config.BARS_LIMIT):
    """Fetch OHLCV bars for one symbol/timeframe. Returns list (possibly empty)."""
    params = {"symbols": pair, "timeframe": timeframe, "limit": limit}
    headers = {}
    if config.ALPACA_KEY and config.ALPACA_SECRET:
        headers = {
            "APCA-API-KEY-ID": config.ALPACA_KEY,
            "APCA-API-SECRET-KEY": config.ALPACA_SECRET,
        }
    try:
        r = requests.get(config.BARS_URL, params=params, headers=headers, timeout=20)
        r.raise_for_status()
        payload = r.json()
    except Exception as e:  # network, HTTP, JSON
        raise FetchError(f"{pair} {timeframe}: {e}") from e
    return payload.get("bars", {}).get(pair, [])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /root/CryptoAutoBot && .venv/bin/python -m pytest tests/test_data.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
cd /root/CryptoAutoBot && git add cryptoauto/data.py tests/test_data.py && \
git commit -m "feat: resilient per-symbol Alpaca bars fetch with tests"
```

---

### Task 3: Hourly snapshot (TDD)

**Files:**
- Create: `cryptoauto/snapshot.py`
- Test: `tests/test_snapshot.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_snapshot.py`:
```python
from cryptoauto import snapshot


def _bars(n=60, base=100.0):
    out = []
    for i in range(n):
        c = base + i
        out.append({"o": c, "h": c + 1, "l": c - 1, "c": c, "v": 10.0 + i, "t": f"t{i}"})
    return out


def test_compute_for_bars_has_indicator_keys():
    res = snapshot.compute_for_bars(_bars())
    for key in ("last_close", "ema8", "ema20", "ema55", "rsi14", "atr14", "adx14", "vol20"):
        assert key in res
    assert res["last_close"] == res["bar_count"] - 1 + 100.0


def test_compute_for_bars_empty_returns_none():
    assert snapshot.compute_for_bars([]) is None


def test_snapshot_symbol_handles_fetch_error(monkeypatch):
    def boom(pair, tf, *a, **k):
        from cryptoauto import data
        raise data.FetchError("down")
    monkeypatch.setattr(snapshot.data, "fetch_bars", boom)
    res = snapshot.snapshot_symbol("BTC")
    assert res["status"] == "partial"
    assert res["timeframes"]["1H"]["status"] == "error"


def test_snapshot_symbol_ok(monkeypatch):
    monkeypatch.setattr(snapshot.data, "fetch_bars", lambda pair, tf, *a, **k: _bars())
    res = snapshot.snapshot_symbol("ETH")
    assert res["status"] == "ok"
    assert res["timeframes"]["1H"]["status"] == "ok"


def test_run_writes_json(monkeypatch, tmp_path):
    monkeypatch.setattr(snapshot.config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(snapshot.config, "WATCHLIST", ["BTC"])
    monkeypatch.setattr(snapshot.data, "fetch_bars", lambda pair, tf, *a, **k: _bars())
    path = snapshot.run()
    assert path.exists()
    import json
    data = json.loads(path.read_text())
    assert data["symbols"][0]["symbol"] == "BTC"
    assert "captured_at" in data
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /root/CryptoAutoBot && .venv/bin/python -m pytest tests/test_snapshot.py -v`
Expected: FAIL — snapshot module missing.

- [ ] **Step 3: Write `cryptoauto/snapshot.py`**

```python
import json
from datetime import datetime, timezone

from . import config, data, indicators


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
            bars = data.fetch_bars(pair, tf_api)
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /root/CryptoAutoBot && .venv/bin/python -m pytest tests/test_snapshot.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
cd /root/CryptoAutoBot && git add cryptoauto/snapshot.py tests/test_snapshot.py && \
git commit -m "feat: hourly snapshot entrypoint with tests"
```

---

### Task 4: Daily digest aggregation (TDD)

**Files:**
- Create: `cryptoauto/digest.py`
- Test: `tests/test_digest.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_digest.py`:
```python
import json

from cryptoauto import digest


def _snap(captured, btc_close, ema_stack="up"):
    if ema_stack == "up":
        e8, e20, e55 = 110.0, 105.0, 100.0
    else:
        e8, e20, e55 = 100.0, 105.0, 110.0
    return {
        "captured_at": captured,
        "symbols": [{
            "symbol": "BTC", "pair": "BTC/USD", "status": "ok",
            "timeframes": {"1H": {
                "status": "ok", "last_close": btc_close,
                "ema8": e8, "ema20": e20, "ema55": e55,
                "rsi14": 60.0, "adx14": 30.0, "atr14": 2.0,
            }},
        }],
    }


def test_summarize_empty():
    out = digest.summarize([])
    assert out["snapshot_count"] == 0 and out["coins"] == []


def test_summarize_computes_change_and_trend():
    snaps = [_snap("2026-06-16T00:00:00Z", 100.0), _snap("2026-06-16T05:00:00Z", 110.0)]
    out = digest.summarize(snaps)
    assert out["snapshot_count"] == 2
    coin = out["coins"][0]
    assert coin["symbol"] == "BTC"
    assert coin["change_pct_day"] == 10.0
    assert coin["trend"] == "up"
    assert coin["atr_pct"] == round(2.0 / 110.0 * 100, 2)


def test_summarize_marks_error_status():
    snaps = [{"captured_at": "t", "symbols": [
        {"symbol": "UNI", "status": "partial",
         "timeframes": {"1H": {"status": "error", "error": "x"}}}]}]
    out = digest.summarize(snaps)
    assert out["coins"][0]["status"] == "error"


def test_load_day_reads_sorted(tmp_path, monkeypatch):
    monkeypatch.setattr(digest.config, "DATA_DIR", tmp_path)
    d = tmp_path / "2026-06-16"
    d.mkdir()
    (d / "05.json").write_text(json.dumps(_snap("2026-06-16T05:00:00Z", 110.0)))
    (d / "00.json").write_text(json.dumps(_snap("2026-06-16T00:00:00Z", 100.0)))
    snaps = digest.load_day("2026-06-16")
    assert len(snaps) == 2
    assert snaps[0]["captured_at"].endswith("00:00:00Z")  # sorted by filename
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /root/CryptoAutoBot && .venv/bin/python -m pytest tests/test_digest.py -v`
Expected: FAIL — digest module missing.

- [ ] **Step 3: Write `cryptoauto/digest.py`**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /root/CryptoAutoBot && .venv/bin/python -m pytest tests/test_digest.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Run the FULL suite + commit**

Run: `cd /root/CryptoAutoBot && .venv/bin/python -m pytest -q`
Expected: PASS (24 passed).

```bash
cd /root/CryptoAutoBot && git add cryptoauto/digest.py tests/test_digest.py && \
git commit -m "feat: deterministic daily digest aggregation with tests"
```

---

### Task 5: README + GitHub remote + first push

**Files:**
- Create: `/root/CryptoAutoBot/README.md`

- [ ] **Step 1: Create `README.md`**

```markdown
# CryptoAutoBot

Automated crypto analysis. Hourly it captures Alpaca OHLCV + indicator snapshots
(`data/snapshots/`); once daily a Claude routine analyzes the day, writes
`memory/`, and sends a Telegram digest. No trading in Phase A — analysis only.
Phase B (paper-trading bot) is designed separately once ~1 month of data exists.

## Universe
BTC ETH SOL XRP DOGE AVAX LINK DOT LTC UNI (Alpaca-tradable)

## Run manually
    .venv/bin/python -m cryptoauto.snapshot   # hourly capture
    .venv/bin/python -m cryptoauto.digest     # print today's summary JSON

## Tests
    .venv/bin/python -m pytest -q

## Schedule (UTC)
- `5 * * * *`  hourly snapshot
- `0 17 * * *` daily Claude digest (= 00:00 WIB) via claude-routines/run-routine.sh cryptoauto-daily
```

- [ ] **Step 2: Configure the GitHub remote reusing the existing PAT**

Run (extracts the PAT from an existing bot's HTTPS remote and reuses it):
```bash
cd /root/CryptoAutoBot
TOKEN=$(git -C /root/USTradeWisBot remote get-url origin | sed -E 's#https://[^:@]*:?([^@]*)@.*#\1#')
git remote remove origin 2>/dev/null || true
git remote add origin "https://${TOKEN}@github.com/redowls/CryptoAutoBot.git"
git remote get-url origin | sed -E 's#//[^@]*@#//***@#'
```
Expected: prints `https://***@github.com/redowls/CryptoAutoBot.git`.
(If `USTradeWisBot` isn't the right source, use any bot that pushes via HTTPS — see the `github-pat` memory.)

- [ ] **Step 3: Commit and push**

```bash
cd /root/CryptoAutoBot && git add README.md && git commit -m "docs: README" && \
git branch -M main && git push -u origin main
```
Expected: push succeeds to `redowls/CryptoAutoBot`. If the remote already has commits, run `git pull --rebase origin main` first, then push.

---

### Task 6: Claude daily routine assets

**Files:**
- Create: `/root/claude-routines/cryptoauto-daily.md`
- Create: `/root/claude-routines/cryptoauto-daily.conf`

- [ ] **Step 1: Create the routine prompt `cryptoauto-daily.md`**

```markdown
# CryptoAutoBot — Daily Analysis & Digest

You are the analyst for CryptoAutoBot. Working directory: /root/CryptoAutoBot.
Do the steps below, then output ONLY the Telegram digest as your final message.

1. Get the day's data:
   `cd /root/CryptoAutoBot && .venv/bin/python -m cryptoauto.digest`
   This prints a JSON summary of today's hourly snapshots for 10 coins
   (per-coin: close, day % change, EMA-stack trend, RSI, ADX, ATR%).

2. Analyze it. For each coin weigh: trend (EMA stack), momentum (RSI — flag
   >70 overbought / <30 oversold), trend strength (ADX — >25 trending), and
   volatility (ATR%). Identify the 2-3 strongest setups (long/short bias with a
   one-line reason) and any risk flags. Note coins with error/no_data status.

3. Write the full reasoning to `memory/analysis/<UTC-date>.md` (YYYY-MM-DD).
   Create the folder if needed. Include date, per-coin notes, top setups, risks.

4. Append ONE concise dated bullet of the most reusable learning to
   `memory/insights.md` (create with an `# Insights` header if missing).

5. Commit and push:
   `cd /root/CryptoAutoBot && git add -A && git commit -m "analysis: <UTC-date>" && git push`
   If push fails, continue — data is safe locally and the next run re-pushes.

6. Output ONLY the Telegram digest as your final message — no preamble, no
   markdown headers, under ~1500 chars. Format:

   📊 CryptoAutoBot — <date>
   Regime: <one line on overall market>
   Top setups:
   • <COIN>: <long/short/watch> — <one line>
   • ...
   Risk flags: <one line, or "none">
```

- [ ] **Step 2: Create the routine config `cryptoauto-daily.conf`**

```bash
# CryptoAutoBot daily digest: Claude reads the day's snapshots, writes memory/,
# pushes to GitHub, and delivers the digest via the dedicated CryptoAutoBot bot.
TIMEOUT_SECS=600
MODEL="claude-sonnet-4-6"
ALLOWED_TOOLS="Bash Read Write Edit Grep Glob"
DISALLOWED_TOOLS="Skill Agent"
TELEGRAM_TOKEN="8900684488:AAFAoSlOdBf1GaGCr-tZ02Il9pNDlua8bic"
TELEGRAM_CHAT_ID="7739672535"
```

- [ ] **Step 3: Commit the routine assets**

These live in the claude-routines repo/dir. Commit there if it is a git repo;
otherwise they are tracked by the VPS only. Verify both files exist:
```bash
ls -l /root/claude-routines/cryptoauto-daily.md /root/claude-routines/cryptoauto-daily.conf
```
Expected: both files listed.

---

### Task 7: Manual end-to-end validation (before cron)

No code — verification only. Do NOT install cron until these pass.

- [ ] **Step 1: One live snapshot**

Run: `cd /root/CryptoAutoBot && .venv/bin/python -m cryptoauto.snapshot`
Expected: prints `wrote .../data/snapshots/<date>/<HH>.json`.

- [ ] **Step 2: Inspect the snapshot**

Run: `cd /root/CryptoAutoBot && .venv/bin/python -m cryptoauto.digest`
Expected: JSON with `snapshot_count >= 1` and 10 coins, most `status: "ok"` with
numeric `rsi14`/`adx14`/`close`. (1D ADX may be null early — that is fine; 1H drives the digest.)

- [ ] **Step 3: Manual digest routine run (real Telegram + git push)**

Run: `/root/claude-routines/run-routine.sh cryptoauto-daily`
Expected: a Telegram message arrives at chat `7739672535` via the new bot; a new
commit `analysis: <date>` appears in `redowls/CryptoAutoBot`; `memory/analysis/<date>.md`
and `memory/insights.md` exist. Check the run log under `/root/claude-routines/logs/`.

- [ ] **Step 4: Capture the result**

If Telegram + push both worked, proceed to Task 8. If not, debug from the run log
before scheduling (common: git remote/PAT, or model name — must be an accessible model).

---

### Task 8: Install cron schedule (go-live)

No code — installs the two cron entries.

- [ ] **Step 1: Append the cron entries**

Run:
```bash
( crontab -l 2>/dev/null; \
  echo "# CryptoAutoBot hourly snapshot (deterministic, no Claude)"; \
  echo "5 * * * * cd /root/CryptoAutoBot && .venv/bin/python -m cryptoauto.snapshot >> /root/CryptoAutoBot/logs/snapshot.log 2>&1"; \
  echo "# CryptoAutoBot daily Claude digest -> memory + git + Telegram (00:00 WIB = 17:00 UTC)"; \
  echo "0 17 * * * /root/claude-routines/run-routine.sh cryptoauto-daily >> /root/claude-routines/logs/cron.log 2>&1" \
) | crontab -
```

- [ ] **Step 2: Verify the crontab**

Run: `crontab -l | grep -i cryptoauto`
Expected: both lines present (snapshot at `5 * * * *`, digest at `0 17 * * *`).

- [ ] **Step 3: Final confirmation**

The system is live: snapshots accumulate hourly; the first scheduled digest fires
at the next 17:00 UTC. Report to the user that Phase A is running and Phase B will
be designed after ~1 month of data.

---

## Notes for the executor

- **Run all commands from `/root/CryptoAutoBot`** with the venv Python (`.venv/bin/python`); the package imports as `cryptoauto`.
- **Secrets:** the Telegram token/chat-id live only in `cryptoauto-daily.conf` (not in the CryptoAutoBot repo). The GitHub PAT is embedded in the git remote URL, reused from an existing bot.
- **Phase B is out of scope.** Do not add trading, orders, or Alpaca account calls.
- **Memory:** after go-live, record a memory entry for CryptoAutoBot (location, schedule, universe, bot/chat-id, repo) and add it to MEMORY.md.
```

