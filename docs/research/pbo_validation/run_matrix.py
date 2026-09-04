import sys, os, argparse, pickle, json, random
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
import numpy as np
import eth_trader_bt as bt
from fixed_tpsl import sample_fixed_params, bt_fixed_pair

SCRATCH = os.path.dirname(os.path.abspath(__file__))
# Run fetch_data.py first to produce eth30_long.pkl in this same directory.

p = argparse.ArgumentParser(description="Build the M-candidate x S-block trial matrix "
                                         "for README.md sec.9 (grid strategy or "
                                         "the fixed-TP/SL benchmark, same protocol either way).")
p.add_argument("--mode", choices=["grid", "fixed"], default="grid",
               help="grid = production _bt_combo_pair (default); "
                    "fixed = single-TP/SL benchmark from fixed_tpsl.py")
args = p.parse_args()

with open(f"{SCRATCH}/eth30_long.pkl", "rb") as f:
    df = pickle.load(f)
hi = df["high"].to_numpy(dtype=np.float64)
lo = df["low"].to_numpy(dtype=np.float64)
cl = df["close"].to_numpy(dtype=np.float64)
n = len(cl)

INTERVAL = "30"
WINDOW_BARS = 7 * bt._bars_per_day(INTERVAL)   # 336 — one 7-day tradeable block, matches IS_DAYS convention exactly
WARMUP = bt.GC_WARMUP_BARS                      # 2250 — same warmup the production IS sweep itself uses
S = 8
M = 20000   # value actually used for the numbers reported in README.md sec.9
SRC = "searched"
bpy = bt._bars_per_year(INTERVAL)

tail_start = n - S * WINDOW_BARS
assert tail_start - WARMUP >= 0, f"not enough history: need {WARMUP + S*WINDOW_BARS}, have {n}"

windows = []
for s in range(S):
    trade_start = tail_start + s * WINDOW_BARS
    trade_end = trade_start + WINDOW_BARS
    pad_start = trade_start - WARMUP
    windows.append((pad_start, trade_end))

print(f"Mode: {args.mode}")
print("Window spans (each independently backtested with its own warmup slice, "
      "each evaluating exactly 7 tradeable days — same convention as production IS sweeps):")
for i, (pad, e) in enumerate(windows):
    print(f"  block {i}: trade {df.index[e-WINDOW_BARS]} -> {df.index[e-1]}  "
          f"(warmup from {df.index[pad]})")

if args.mode == "grid":
    random.seed(20260904)
    candidates = [bt._sample(bt.PARAM_SPACE_SEARCHED) for _ in range(M)]
    sim = lambda combo, hi_w, lo_w, cl_w: bt._bt_combo_pair(
        combo, hi_w, lo_w, cl_w, WINDOW_BARS, bpy, bt.LEVERAGE,
        initial_equity=117.0, entry_source=SRC)
    out_suffix = ""
else:
    np.random.seed(20260905)
    random.seed(20260905)
    candidates = [sample_fixed_params(bt.PARAM_SPACE_SEARCHED) for _ in range(M)]
    sim = lambda combo, hi_w, lo_w, cl_w: bt_fixed_pair(
        combo, hi_w, lo_w, cl_w, WINDOW_BARS, bpy, bt.LEVERAGE,
        initial_equity=117.0, entry_source=SRC)
    out_suffix = "_fixed"

# score_matrix[m, s] = NaN if the combo produced no valid result in that block
# (MIN_TRADES/MIN_AVG_HOLD not cleared — expected and common at only 7 days/block)
score_matrix = np.full((M, S), np.nan)
sharpe_matrix = np.full((M, S), np.nan)
trades_matrix = np.full((M, S), np.nan)

for si, (pad_start, trade_end) in enumerate(windows):
    hi_w = hi[pad_start:trade_end]
    lo_w = lo[pad_start:trade_end]
    cl_w = cl[pad_start:trade_end]
    for mi, combo in enumerate(candidates):
        m = sim(combo, hi_w, lo_w, cl_w)
        if m is not None:
            score_matrix[mi, si] = m["score"]
            sharpe_matrix[mi, si] = m["sharpe"]
            trades_matrix[mi, si] = m["trades"]
    valid = np.sum(~np.isnan(score_matrix[:, si]))
    print(f"block {si}: {valid}/{M} candidates produced a valid result")

np.savez(f"{SCRATCH}/matrix{out_suffix}.npz", score=score_matrix, sharpe=sharpe_matrix,
         trades=trades_matrix)
if args.mode == "grid":
    with open(f"{SCRATCH}/candidates.json", "w") as f:
        json.dump(candidates, f)
print(f"saved matrix{out_suffix}.npz" + (" + candidates.json" if args.mode == "grid" else ""))
