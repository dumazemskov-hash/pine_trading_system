#!/usr/bin/env python3
"""VAL-FADE limit bot. Default DRY. Live only VAL_LIVE=1 + keys in env.

Cancel rules (code not here):
- timeout 12 bars 15m after ARMED
- high >= stop -> cancel
- close back above entry after touch -> cancel
- FIRED and no fill -> cancel resting limit
- one symbol = one resting order
"""
from __future__ import annotations
import json, os, time
from pathlib import Path

try:
    import ccxt
except ImportError:
    ccxt = None

ROOT = Path(__file__).resolve().parents[1]
ARMED = ROOT / "signals_val" / "armed.jsonl"
STATE = ROOT / "signals_val" / "limit_state.json"
SLEEP = 5.0

def load_state():
    if not STATE.exists():
        return {"done": [], "open": {}}
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"done": [], "open": {}}

def save_state(st):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st), encoding="utf-8")

def iter_armed():
    if not ARMED.exists():
        return []
    out = []
    for line in ARMED.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("kind") and rec.get("kind") != "armed":
            continue
        if "symbol" not in rec or "entry" not in rec:
            continue
        out.append(rec)
    return out

def key_of(rec):
    return f"{rec.get('symbol')}|{rec.get('pump_bar')}"

def live_ok():
    return (
        os.environ.get("VAL_LIVE") == "1"
        and os.environ.get("BYBIT_API_KEY")
        and os.environ.get("BYBIT_API_SECRET")
    )

def min_qty(ex, symbol):
    env = os.environ.get("VAL_QTY")
    if env:
        return float(env)
    try:
        m = ex.market(symbol)
        return float((m.get("limits") or {}).get("amount", {}).get("min") or 1)
    except Exception:
        return 1.0

def place_live(ex, rec):
    symbol = rec["symbol"]
    qty = min_qty(ex, symbol)
    return ex.create_order(symbol, "limit", "sell", qty, float(rec["entry"]))

def main():
    live = live_ok()
    if os.environ.get("VAL_LIVE") == "1" and not live:
        print("VAL_LIVE=1 but no BYBIT keys — DRY")
    print("VAL LIMIT BOT  " + ("LIVE" if live else "DRY"))
    print(f"armed {ARMED}")
    ex = None
    if live:
        if ccxt is None:
            raise SystemExit("need ccxt")
        ex = ccxt.bybit({
            "apiKey": os.environ["BYBIT_API_KEY"],
            "secret": os.environ["BYBIT_API_SECRET"],
            "enableRateLimit": True,
            "options": {"defaultType": "swap", "fetchMarkets": ["linear"]},
        })
    st = load_state()
    done = set(st.get("done") or [])
    open_sym = dict(st.get("open") or {})
    while True:
        for rec in iter_armed():
            k = key_of(rec)
            if k in done:
                continue
            sym = rec["symbol"]
            if sym in open_sym:
                print(f"SKIP {sym} already open {open_sym[sym]}")
                done.add(k)
                continue
            line = (
                f"PLACE SELL LIMIT {sym} @{rec['entry']} "
                f"sl={rec['stop']} tp={rec['tp04']}"
            )
            print(line)
            if live:
                try:
                    od = place_live(ex, rec)
                    oid = od.get("id") if isinstance(od, dict) else str(od)
                    print(f"LIVE id={oid}")
                    open_sym[sym] = oid
                except Exception as e:
                    print(f"LIVE FAIL {sym}: {e}")
                    continue
            else:
                open_sym[sym] = "dry"
            done.add(k)
            save_state({"done": list(done)[-400:], "open": open_sym})
        time.sleep(SLEEP)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("stop")
