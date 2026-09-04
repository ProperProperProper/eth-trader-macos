"""
Fetches a long, continuous ETHUSDT 30m price history and caches it as eth30_long.pkl
in this same directory, for run_matrix.py / run_matrix_fixed.py to slice into
independent 7-day out-of-sample blocks (README.md sec.9). Run this once
before either run_matrix script.

This is a one-time DATA FETCH, not a backtest -- it does not itself evaluate any
strategy over more than 7 days. Each individual _bt_combo_pair / bt_fixed_pair call
downstream still only ever sees one 7-day tradeable window (plus its own warmup
padding), exactly like every other backtest in this project.
"""
import os, pickle
import pandas as pd
from datetime import datetime, timezone

import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
import eth_trader_bt as bt
from pybit.unified_trading import HTTP

HERE = os.path.dirname(os.path.abspath(__file__))
SYMBOL, INTERVAL = "ETHUSDT", "30"
MAX_PAGES = 10  # 10 * 1000 bars ~= 208 days at 30m -- ample for 8*7=56 tradeable
                # days plus ~47 days of GC warmup per block


def main():
    sess = HTTP(demo=False)
    all_bars = {}
    end_ms = None
    for _ in range(MAX_PAGES):
        kw = dict(category=bt.CATEGORY, symbol=SYMBOL, interval=INTERVAL, limit=bt.FETCH_LIMIT)
        if end_ms:
            kw["end"] = end_ms
        r = bt._api(sess.get_kline, **kw)
        raw = r.get("result", {}).get("list", [])
        if not raw:
            break
        for b in raw:
            all_bars[int(b[0])] = b
        if len(raw) < bt.FETCH_LIMIT:
            break
        end_ms = min(int(b[0]) for b in raw) - 1

    bars = sorted(all_bars.values(), key=lambda x: int(x[0]))
    idx = pd.to_datetime([datetime.fromtimestamp(int(b[0]) / 1000, tz=timezone.utc) for b in bars])
    df = pd.DataFrame({
        "open": [float(b[1]) for b in bars], "high": [float(b[2]) for b in bars],
        "low": [float(b[3]) for b in bars], "close": [float(b[4]) for b in bars],
        "volume": [float(b[5]) for b in bars],
    }, index=idx)

    print(f"fetched {len(df)} bars, {df.index[0]} -> {df.index[-1]} "
          f"({(df.index[-1]-df.index[0]).total_seconds()/86400:.1f} days)")
    with open(os.path.join(HERE, "eth30_long.pkl"), "wb") as f:
        pickle.dump(df, f)
    print(f"saved {os.path.join(HERE, 'eth30_long.pkl')}")


if __name__ == "__main__":
    main()
