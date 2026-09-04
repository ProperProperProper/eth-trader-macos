# §9 Empirical Validation — Reproduction Scripts

Backing scripts for `README.md` section 9 (Deflated Sharpe Ratio and
Probability of Backtest Overfitting). Reuses the production `eth_trader_bt.py`
signal/simulation code directly — nothing here re-implements or approximates the
entry signal or the grid exit; `fixed_tpsl.py` implements only the benchmark
single-TP/SL exit, which has no production equivalent to reuse.

Run in order, from this directory, with the project venv active:

```
python3 fetch_data.py                  # one-time: caches eth30_long.pkl (~10,000 bars, ~208 days)
python3 run_matrix.py --mode grid      # grid strategy: 20,000 candidates x 8 blocks -> matrix.npz
python3 run_matrix.py --mode fixed     # fixed-TP/SL benchmark: same protocol -> matrix_fixed.npz
python3 analyze.py --matrix matrix.npz         # DSR + PBO for the grid matrix -> results.npz
python3 analyze.py --matrix matrix_fixed.npz   # DSR + PBO for the benchmark -> results_fixed.npz
```

`eth30_long.pkl`, `matrix*.npz`, `candidates.json`, and `results*.npz` are data
artifacts, not checked into the repo (regenerate them via the scripts above) — market
data moves on, so a stale cached copy would silently misrepresent "current" results.

Every individual simulation call across these scripts evaluates exactly one 7-day
tradeable window plus its own warmup padding — the longer cached history exists only
to supply 8 independent such windows spread across a 9-week span, never to feed more
than 7 days into any single backtest evaluation (see `README.md` sec.9.1).
