import os, argparse
import numpy as np
from scipy.stats import norm
from itertools import combinations

SCRATCH = os.path.dirname(os.path.abspath(__file__))

p = argparse.ArgumentParser(description="Compute DSR + PBO from a run_matrix.py output "
                                         "(README.md sec.9).")
p.add_argument("--matrix", default="matrix.npz",
               help="matrix.npz (grid, default) or matrix_fixed.npz (fixed-TP/SL benchmark)")
args = p.parse_args()
out_suffix = "_fixed" if "fixed" in args.matrix else ""

d = np.load(f"{SCRATCH}/{args.matrix}")
score_raw, sharpe_raw, trades_raw = d["score"], d["sharpe"], d["trades"]
M, S = score_raw.shape
print(f"M={M} candidates, S={S} independent 7-day blocks")

# Economically-motivated 0-fill: a candidate that fails MIN_TRADES/MIN_AVG_HOLD/
# MIN_RR_RATIO/total_ret_pct>0 in a given block simply produced no live-deployable
# signal that week -- treated as a 0-return "sat out" observation for that block, not
# a missing value, so CSCV/DSR are computed over the full candidate pool rather than
# the tiny subset that happened to qualify in every block simultaneously.
score = np.nan_to_num(score_raw, nan=0.0)
sharpe = np.nan_to_num(sharpe_raw, nan=0.0)

qual_rate = (~np.isnan(score_raw)).mean(axis=0)
print("\nPer-block qualification rate (fraction of M candidates clearing quality gates):")
for s in range(S):
    print(f"  block {s}: {qual_rate[s]*100:.2f}%")
print(f"  mean: {qual_rate.mean()*100:.2f}%")

# ---------------------------------------------------------------------------
# 1. Deflated Sharpe Ratio -- Bailey & Lopez de Prado (2014)
# ---------------------------------------------------------------------------
BLOCK = S - 1  # most recent block = the operationally relevant "live" cycle
sr_trials = sharpe[:, BLOCK]
best_idx = np.argmax(score[:, BLOCK])
SR_hat = sharpe[best_idx, BLOCK]
T = trades_raw[best_idx, BLOCK]
if np.isnan(T): T = 0
N = M

sigma_sr = sr_trials.std(ddof=1)
gamma = 0.5772156649
if sigma_sr > 0:
    z1 = norm.ppf(1 - 1.0/N)
    z2 = norm.ppf(1 - 1.0/(N*np.e))
    E_max_SR = sigma_sr * ((1-gamma)*z1 + gamma*z2)
else:
    E_max_SR = 0.0

# Gaussian-return simplification for sigma(SR_hat) (skew=0, kurtosis=3) -- an
# explicit, disclosed simplification: T is on the order of a handful of trades here,
# far too few to estimate 3rd/4th moments reliably, so the higher-moment terms of the
# full PSR/DSR variance formula are dropped rather than fit to noise.
if T > 1:
    se_sr = np.sqrt((1 + 0.5*SR_hat**2) / (T-1))
    DSR = norm.cdf((SR_hat - E_max_SR) / se_sr)
else:
    se_sr = float("nan"); DSR = float("nan")

print(f"\n--- Deflated Sharpe Ratio (block {BLOCK}, most recent 7-day window) ---")
print(f"N trials (candidates drawn)        : {N}")
print(f"cross-trial Sharpe std (sigma_SR)  : {sigma_sr:.4f}")
print(f"Expected max Sharpe under null     : {E_max_SR:.4f}")
print(f"Selected candidate's Sharpe (SR_hat): {SR_hat:.4f}")
print(f"Selected candidate's trade count(T): {T:.0f}")
print(f"SE(SR_hat), Gaussian-return approx : {se_sr:.4f}")
print(f"Deflated Sharpe Ratio (DSR)        : {DSR:.4f}")
print(f"  (DSR = P[true Sharpe > 0 | correcting for {N} trials]; "
      f"conventionally >0.95 is treated as significant)")

# ---------------------------------------------------------------------------
# 2. Probability of Backtest Overfitting -- CSCV (Bailey, Borwein, Lopez de Prado,
#    Zhu 2017), adapted to block-level aggregated Sharpe/score rather than pooled
#    raw per-observation returns (a standard, disclosed simplification when the
#    underlying strategies do not produce a shared, regularly-sampled return series --
#    here, an entry-signal-gated strategy that may trade zero times in a given block).
# ---------------------------------------------------------------------------
blocks = list(range(S))
half = S // 2
logits = []
flips = 0
total = 0
for train_idx in combinations(blocks, half):
    test_idx = tuple(b for b in blocks if b not in train_idx)
    is_perf = score[:, train_idx].mean(axis=1)
    oos_perf = score[:, test_idx].mean(axis=1)
    n_star = np.argmax(is_perf)
    # relative rank of the IS-winner's OOS performance among all M candidates
    rank = (oos_perf <= oos_perf[n_star]).sum()
    omega = rank / (M + 1)
    omega = min(max(omega, 1e-6), 1-1e-6)
    logit = np.log(omega/(1-omega))
    logits.append(logit)
    total += 1
    if logit <= 0:
        flips += 1

PBO = flips/total
print(f"\n--- Probability of Backtest Overfitting (CSCV, S={S} blocks, "
      f"C({S},{half})={total} train/test splits) ---")
print(f"PBO = {PBO*100:.1f}%  "
      f"(fraction of splits where the IS-best candidate ranked below the OOS median)")
print(f"mean logit lambda_c: {np.mean(logits):.3f}  "
      f"(negative mean logit also indicates net overfitting)")

np.savez(f"{SCRATCH}/results{out_suffix}.npz", DSR=DSR, PBO=PBO, N=N, sigma_sr=sigma_sr,
         E_max_SR=E_max_SR, SR_hat=SR_hat, T=T, mean_logit=np.mean(logits),
         qual_rate=qual_rate)
print(f"saved results{out_suffix}.npz")
