# A Regime-Filtered Stochastic Mean-Reversion Strategy with Adaptive Multi-Level Profit-Taking and Reversal-Contingent Position Flipping

### A Technical Thesis on the ATR_PARTIAL Grid Combo Strategy (Unified Combo Grid)

**Subject system:** `eth_trader_bt.py` / `eth_trader.py`, Unified Combo Grid (macOS fork)
**Asset class:** Bybit USDT linear perpetual futures (single-instrument deployment: ETHUSDT)
**Document status:** Living technical thesis — should be revised whenever the strategy's mechanics, parameter space, or selection rule change materially. Every formula and constant below is transcribed directly from the production source and should be re-verified against `eth_trader_bt.py` if this document and the code ever appear to disagree — the code is authoritative.

**Looking for build/run/config instructions instead?** They moved to [`docs/SETUP.md`](docs/SETUP.md) (this file previously held them).

---

## Abstract

This thesis formalizes a discretionary-turned-systematic trading strategy that combines a **regime-filtered stochastic oscillator crossover** for trade timing with an **adaptive, multi-level, ATR-scaled exit structure** for risk and profit management. The strategy departs from the conventional single take-profit / single stop-loss template in three material ways: (i) the exit is a *grid* of independently-parameterized partial take-profit levels rather than one fixed target; (ii) a *cross-down unwind* mechanism allows the position to give back profit-taking credit incrementally rather than only via a monolithic stop; and (iii) an entry-signal reversal can *flip* the position instantaneously rather than merely closing it. All of these mechanisms — including the number of grid levels, their spacing, and the size of the position closed at each — are treated as **free parameters discovered by random search over a walk-forward-validated in-sample/out-of-sample split**, not as hand-tuned constants. Beyond the system's formalization, this thesis poses and empirically tests one narrow research question (§1.3): after correcting for the selection bias inherent in a $2\times10^4$–$2\times10^5$-candidate search, is the strategy's out-of-sample edge statistically distinguishable from a zero-edge null (via the Deflated Sharpe Ratio; Bailey & López de Prado, 2014), and does its grid exit structure produce a measurable improvement over a conventional single take-profit/stop-loss exit sharing the identical entry signal? Using 8 independent, non-overlapping 7-day out-of-sample windows spanning a 9-week period of real ETHUSDT market data and a purpose-built benchmark control, we find the grid exit clears the system's own quality gates roughly 3.9× as often as the fixed-exit benchmark (a consistent effect across all 8 windows), and that the selected candidate's Deflated Sharpe Ratio nominally exceeds the conventional significance threshold — with substantial, explicitly quantified caveats regarding sample size and search-space sparsity that are reported alongside the results rather than omitted. We also present the complete mathematical formalization of the signal-generation and exit machinery, the optimization and validation methodology, and the evolution of the strategy's capital-allocation selection rule across seven documented revisions. This document is written to the standard expected of a thesis committee: every claim is either a verifiable statement about the source code, a reproducible empirical result with disclosed methodology, or is explicitly flagged as an assumption, a design choice, or an open threat to validity.

---

## 1. Introduction

### 1.1 Motivation

Systematic intraday strategies on liquid crypto perpetuals face a well-known tension: a stochastic oscillator crossover is a *high-frequency, low-information* signal — it fires often and is individually weak evidence of a genuine reversal — while transaction costs (taker fees, funding, slippage) are *fixed per trade*. A strategy that acts on every raw crossover will be fee-dominated; a strategy that filters too aggressively will starve for trade count and become statistically unverifiable. The system under study addresses this tension with a **three-stage entry filter** (oscillator state, trend-regime direction, and choppiness) and compensates for the resulting low trade frequency with an **exit structure that extracts value across the whole subsequent price path** rather than at a single point, so that the relatively rare entries which do fire are monetized as fully as possible.

### 1.2 Contribution of this document

This is not a description of an idealized strategy; it is a formal accounting of a *specific, versioned, running implementation*, including the messy real history of how its risk-selection rule has been revised (§8) and the concrete failure modes that have been observed in production (§10.3). Where the implementation embodies a value judgment rather than a mathematical necessity (e.g., the decision to abandon a hard drawdown gate in favor of unconstrained profit-ranking — §8.6), this is stated as such, not disguised as a theoretical result.

### 1.3 Research question and hypotheses

A system description, however rigorous, is not a testable claim. This thesis's empirical content (§9) is organized around one narrow, falsifiable research question, chosen specifically because it is the point at which this strategy's own design is most exposed to the standard critique of any large-scale parameter search:

> **RQ.** After correcting for the selection bias inherent in evaluating $\mathcal{O}(10^4)$–$10^5$ candidate parameterizations per optimization cycle, does (a) the strategy's best-selected out-of-sample Sharpe ratio remain statistically distinguishable from a zero-edge null, and (b) does its multi-level grid exit structure (§4) produce a measurably different — specifically, a more consistently *viable* — outcome than a conventional single take-profit/single stop-loss exit paired with the identical entry signal?

This decomposes into two testable hypotheses, addressed respectively in §9.3 and §9.4:

- **H1 (edge significance):** The Deflated Sharpe Ratio (Bailey & López de Prado, 2014) of the strategy's best-selected candidate, computed against the empirical distribution of Sharpe ratios actually produced by the search's own candidate pool, exceeds the conventional $0.95$ significance threshold.
- **H2 (exit-structure contribution):** The grid exit structure produces a higher rate of candidates clearing the system's own pre-existing quality gates (§5.4: $n_{trades}\ge3$, average holding period $\ge2$ bars, reward:risk $\ge0.8$, positive total return) than a single-TP/single-SL exit sharing the identical entry-signal composition (§2.4), holding the evaluation windows, instrument, and random-search protocol fixed.

Neither hypothesis is a claim that the strategy is profitable in any general sense; both are narrow, mechanically well-defined comparisons designed to be falsifiable within the constraints this system's own operator has placed on its historical data usage (§5.5, §9.1).

---

## 2. Theoretical Foundations

### 2.1 The stochastic oscillator as a mean-reversion timing signal

The raw stochastic %K over a lookback window of length $k$ is

$$
\%K_t = 100 \cdot \frac{C_t - LL_{t,k}}{HH_{t,k} - LL_{t,k}}, \qquad HH_{t,k} = \max_{t-k+1 \le \tau \le t} H_\tau,\;\; LL_{t,k} = \min_{t-k+1 \le \tau \le t} L_\tau
$$

with the degenerate case $HH_{t,k}=LL_{t,k}$ mapped to $\%K_t = 50$ (implemented via `_stoch_raw_k`). The strategy smooths this twice — a simple moving average of length $k_{smooth}$ produces the working $\%K$ series, and a further simple moving average of length $d_{smooth}$ produces $\%D$:

$$
K_t = \mathrm{SMA}_{k_{smooth}}(\%K)_t, \qquad D_t = \mathrm{SMA}_{d_{smooth}}(K)_t
$$

A **bullish cross** is defined at bar $t$ as $K_{t-1} \le D_{t-1} \wedge K_t > D_t$; a **bearish cross** is the mirror condition. This is a standard slow-stochastic crossover; on its own it is well documented to produce excessive false signals in trending regimes, which motivates §2.2–2.3.

### 2.2 Gaussian Channel midline as a trend-regime filter

Rather than gating on a moving-average slope, the system computes a **Gaussian Channel midline**, an IIR (infinite impulse response) low-pass filter applied to $HLC_3 = (H+L+C)/3$. The filter is a $2n$-pole Gaussian approximation implemented as a binomial-expansion recursion (Pine Script convention; see `_gc_filt9x`):

$$
\beta = \frac{1-\cos\left(\frac{2\pi}{P}\right)}{c^{2/N}-1}, \qquad \alpha = -\beta + \sqrt{\beta^2+2\beta}
$$

$$
f_t = \alpha^N \cdot x_t + \sum_{k=1}^{N}(-1)^{k+1}\binom{N}{k}(1-\alpha)^k f_{t-k}
$$

where $P$ is the filter period (`gc_period`), $N$ is the pole count (`gc_poles`), $x_t = HLC_{3,t}$, and $c$ is a constant that **differs between the strategy's two entry-signal variants** — this is the single point of divergence between them (§3.2). The channel's *direction* (rising vs. falling, $\Delta f_t = f_t - f_{t-1}$) is used as a coarse trend-regime proxy: a bullish entry additionally requires $\Delta f_t > 0$.

This recursion is a genuine IIR feedback loop, and at high pole counts ($N$ up to 27 in the wider of the two search spaces, §5) it is **numerically unstable for some $(P,N)$ pairs**: the implementation defensively detects divergence (output exceeding $10^6\times$ the input series' own scale, or a non-finite value) and marks the filter NaN from that point forward, which naturally suppresses the affected combination from producing entry signals rather than propagating a corrupted, spuriously-trending value. This is a numerical-stability engineering fix, not a modeling choice, and is disclosed here because it constrains which parts of the pole-count search space are *usable* versus merely *sampleable* (§10.2).

### 2.3 Choppiness Index as a regime-quality filter

The Choppiness Index (Tushar Chande) measures whether price action over a window is trending or range-bound:

$$
CI_{t,p} = 100 \cdot \frac{\log_{10}\!\left(\dfrac{\sum_{\tau=t-p+1}^{t} TR_\tau}{HH_{t,p}-LL_{t,p}}\right)}{\log_{10}(p)}
$$

where $TR_\tau$ is the true range at bar $\tau$. Low values indicate a trending market; high values indicate consolidation/chop. The strategy requires $CI_t < \theta_{chop}$ (a searched threshold) as a **precondition for any entry** — i.e., it explicitly refuses to trade into choppy, low-directional-conviction regimes, which is the correct complement to a mean-reversion oscillator: a stochastic crossover in a genuinely trending market is a much higher-quality signal than the same crossover in a chop regime, where it is closer to noise.

### 2.4 The composite entry condition

Collecting §2.1–2.3, the full long-entry condition at bar $t$ (short is the exact mirror, substituting the bearish cross, $K_t \ge \theta_{ob}$, and $\Delta f_t<0$) is:

$$
\text{BUY}_t \;=\; \underbrace{\big(K_{t-1}\le D_{t-1}\big)\wedge\big(K_t>D_t\big)}_{\text{bullish cross}} \;\wedge\; \underbrace{K_t \le \theta_{os}}_{\text{oversold}} \;\wedge\; \underbrace{\Delta f_t>0}_{\text{regime up}} \;\wedge\; \underbrace{CI_t<\theta_{chop}}_{\text{trending, not choppy}}
$$

This is a logical conjunction of four independent conditions across three distinct indicator families (momentum-oscillator, trend-filter, volatility-of-direction filter), which is the source of the signal's *selectivity* — and, as a direct consequence, its low absolute trade frequency (§10.1).

### 2.5 Related work

**Indicator provenance.** The slow-stochastic crossover traces to Lane's original stochastic oscillator (Lane, 1984). The Gaussian Channel filter is a discrete IIR approximation of a Gaussian low-pass response via repeated first-order pole cascading, in the tradition of Ehlers' digital-signal-processing approach to market filters (Ehlers, 2004; 2013), which explicitly popularized applying classical filter-design theory — including the pole-cascade construction used here (§2.2) — to price series in place of ad hoc moving averages. The Choppiness Index is Chande's (Chande & Kroll, 1994) log-ratio measure of range-to-path-length, designed specifically to distinguish trending from directionless regimes independent of trend *direction*, which is the complementary role it plays here (§2.3) alongside a directional filter.

**Walk-forward validation.** The IS/OOS discipline this system enforces (§5.4–5.5) follows the walk-forward optimization framework formalized by Pardo (Pardo, 2008): a strategy is never permitted to trade on parameters chosen using the same data used to choose them, and the OOS window's role is specifically to detect curve-fitting that an IS-only backtest cannot. Pardo's own treatment, however, evaluates *one* walk-forward run per strategy variant; it does not address the compounding effect of running the same walk-forward procedure across $\mathcal{O}(10^5)$ candidate variants per cycle, which is the specific concern the next paragraph's literature addresses and which motivates §9's empirical treatment.

**Backtest overfitting and multiple comparisons.** A large body of quantitative-finance methodology addresses exactly the failure mode a walk-forward split alone does not: that trying enough candidate strategies against the same evaluation data will, by chance alone, eventually produce one that looks good regardless of whether any of them have genuine predictive value. White's Reality Check (White, 2000) and Hansen's Superior Predictive Ability test (Hansen, 2005) provide hypothesis tests for whether the best of many candidate strategies beats a benchmark by more than multiple-comparisons luck would predict. Bailey and López de Prado's Deflated Sharpe Ratio (Bailey & López de Prado, 2014) directly corrects an observed Sharpe ratio for the number of trials conducted and the variance of the resulting trial distribution, converting "best Sharpe out of $N$ trials" into a proper significance statement. Bailey, Borwein, López de Prado, and Zhu's Probability of Backtest Overfitting (Bailey et al., 2017) and the associated Combinatorially Symmetric Cross-Validation (CSCV) procedure estimate, directly from repeated in-sample/out-of-sample resampling, the probability that a strategy-selection *process* — not any single strategy — is selecting for in-sample noise rather than genuine out-of-sample skill. §9 below applies the latter two methods, adapted as described therein, directly to this system's own search-and-select mechanism (§5, §8) rather than treating their applicability as a purely theoretical concern.

---

## 3. The Dual Entry-Source Design

### 3.1 Motivation

The strategy is deliberately implemented as **two independently-optimized entry-signal variants competing for capital allocation on identical exit mechanics** — "searched" and "pine." This is an unusual design choice worth justifying: rather than committing to a single canonical parameterization of §2.4, the system treats the *entry formula itself* as a hypothesis to be A/B-tested continuously against live market data, with the winner (by the criterion in §8) receiving live capital.

### 3.2 The sole formal difference

Both variants search the identical logical structure of §2.4 and (with one exception, §5.2) the identical exit mechanism (§4). They differ in exactly one constant: the value of $c$ in the Gaussian Channel's $\beta$ formula (§2.2).

- **"pine"** uses $c = 1.414$ — the literal four-significant-figure truncation of $\sqrt 2$ hardcoded in the reference "Stochastic Triple Filter [ATP]" Pine Script indicator this system was originally ported from. This variant exists for *faithful reproduction* of that reference implementation's exact arithmetic, not because $1.414$ is believed to be superior to $\sqrt2$ on any theoretical basis.
- **"searched"** uses $c = \sqrt2$ exactly (`math.sqrt(2)`).

The two values differ by a relative magnitude of order $10^{-4}$–$10^{-6}$ depending on $(P,N)$ — a genuinely tiny perturbation. Because the two variants otherwise search overlapping parameter spaces (§5.1–5.2) and their entry timing is consequently highly, though not perfectly, correlated, it is an **expected outcome, not a bug**, for both variants to occasionally converge on an identical winning parameter combination within a given optimization cycle when the entry signal's robustness to that $10^{-6}$-scale perturbation happens to be high for that particular combination (empirically observed and diagnosed in this system's operational history, see §10.5).

### 3.3 Asymmetric search-space width

"searched" is granted a **3× wider search range on all 9 entry-signal parameters** than "pine" (exit-side parameters are held identical between the two — see §5.2 for the exact bounds). This is an explicit, non-mathematically-motivated design decision: "pine" is scoped narrowly to explore local perturbations around the reference indicator's intended operating range, while "searched" is given latitude to discover entry-timing regimes the reference indicator's author never intended. Neither variant is *a priori* expected to dominate; the live selection rule (§8) is what actually adjudicates.

---

## 4. Exit Architecture: The Adaptive Profit-Taking Grid

### 4.1 Departure from single-target exits

A conventional trend-or-reversion strategy exits via one of: a fixed take-profit, a fixed stop-loss, or a trailing stop. This system instead constructs, **at the moment of entry**, a full ladder of $\ell \in \{2, \dots, 8\}$ (`grid_levels`) discrete take-profit levels, each with its own ATR-scaled distance and its own position fraction to close:

$$
P_i \;=\; P_0 + s\cdot A_0 \cdot \sum_{j=1}^{i} d_j, \qquad i = 1,\dots,\ell
$$

where $P_0$ is the entry price, $A_0$ is the ATR (Wilder's, period `atr_p`) sampled *at entry* and held fixed for the life of the trade, $s\in\{+1,-1\}$ is the trade side, and $d_j$ (`grid_dist_j`) is the *incremental* ATR-multiple distance of level $j$ from level $j-1$ — **not** an absolute distance. Because $d_j > 0$ for every $j$ by construction of its search range ($[0.3, 2.5]$), the cumulative sum guarantees $P_1 < P_2 < \dots < P_\ell$ (long case) unconditionally, which is a structural correctness property relied upon by the sequential fill-scanning implementation (`grid_level_prices`, shared byte-for-byte between the backtest's pure-Python path and the live trading engine — a design choice specifically intended to make live/backtest divergence on exit-price construction structurally impossible).

Each level $i$ closes a fraction $f_i$ (`grid_frac_i`, searched independently per level in $[0.1, 0.4]$) of the **original** entry quantity $q_0$, except the final level reached, which closes the entirety of whatever quantity remains — a design guarantee that the position always fully unwinds once every level has filled, regardless of whether $\sum_i f_i \gtrless 1$.

### 4.2 Stop-loss and the breakeven staircase

The stop-loss is placed at $P_0 - s\cdot(\kappa\cdot A_0)$ at entry, where $\kappa$ (`stop_mult`) is a searched multiplier in $[1.5, 6.0]$. After the **first** level fills, the stop is moved to breakeven ($P_0$); after each **subsequent** fill $i>1$, the stop moves to $P_{i-1}$ — i.e., the previously-filled level's own price. This produces a monotonically-tightening trailing stop that can never give back profit already banked at a lower level once a higher level fills, without requiring an explicit "breakeven-then-trail" state machine distinct from the fill-count itself.

### 4.3 Cross-down unwind: bidirectional grid interaction

A level that has filled is not permanently "spent." If price subsequently crosses back *strictly* below (long) / above (short) an already-filled-but-not-yet-unwound level, that level's own fraction $f_i$ of the **current remaining** quantity is closed at the crossing price — capturing a retracement's worth of additional profit-taking rather than riding the position all the way back to the (unchanged) stop. Each level can unwind at most once; a later, higher-index fill re-arms eligibility for its own future unwind but never re-arms an already-unwound lower level. This mechanism is deliberately and provably **independent of the stop-loss**: because the unwind check only executes after the stop-loss check has already returned false for the current bar, and the stop always trails exactly one level behind whichever level is next eligible to unwind, **at most one level can ever unwind before the stop takes over** — a property that was directly verified (not merely argued) against hand-constructed deterministic price paths during this feature's development.

### 4.4 Reversal-contingent flip (`flip_on_signal`)

If, prior to the stop-loss triggering, the *opposite-direction* entry condition of §2.4 becomes true while a position is open, the position is closed at the current price and an opposite-side position is immediately opened at that same price — reusing the exact signal arrays already computed for entry detection, at zero additional indicator cost. This is checked after the stop-loss test (a same-bar stop always takes priority) and before the grid-fill/unwind checks (a flip signal is treated as invalidating the current trade's thesis entirely, making further grid-fill evaluation moot). As of the current production configuration this behavior is **unconditionally enabled** (searched range collapsed to the constant $1$) rather than left as a free binary parameter, following a direct empirical finding that a genuine live position had experienced a qualifying opposite-signal reversal on 5 of its 6 historical trades while the parameter had, by chance of random sampling, been drawn off.

### 4.5 Trailing take-profit (`trail_tp_mult`)

Layered on top of, not in place of, §4.1–4.4: the position's most favorable price reached since entry (`peak_price`) is tracked continuously. If price retraces $\tau \cdot A_0$ (searched $\tau\in[0.3,3.0]$) from that peak — and only once the peak has moved favorably past entry at all — the **entire remaining position** is closed at the current price, independent of grid-fill state. This specifically targets the failure mode in which price moves favorably but not far enough to reach even the first grid level ($d_1 \cdot A_0$ away) before reversing cleanly to the stop: such a trade currently banks nothing at all (a "zero-fill reversal," §7.3) even though it was, briefly, in genuine unrealized profit. A worked example from this system's own trade history: a short position moved favorably to $-1.10\times$ ATR (short of the $1.66\times$ ATR needed to bank its first grid level), round-tripped through breakeven, and then reversed violently to a $-6.28\times$ ATR loss at the stop; a trailing take-profit of $\tau\approx0.5$ would have closed that trade near breakeven roughly three hours before the eventual stop-out. This example is presented as *motivating evidence for the mechanism's inclusion*, not as proof that any particular $\tau$ is optimal — that determination is left entirely to the search (§5).

---

## 5. Parameter Space and Search Methodology

### 5.1 The full free-parameter vector

| Parameter | Role | "pine" range | "searched" range |
|---|---|---|---|
| $k_{len}$ | Stochastic %K lookback | $[10,40]$ | $[30,120]$ |
| $k_{smooth}$ | %K smoothing | $[1,5]$ | $[3,15]$ |
| $d_{smooth}$ | %D smoothing | $[3,10]$ | $[9,30]$ |
| $\theta_{ob}$ | Overbought threshold | $[70,90]$ | $[50,100]$ |
| $\theta_{os}$ | Oversold threshold | $[10,30]$ | $[30,90]$ |
| $chop_{len}$ | Choppiness lookback | $[8,20]$ | $[24,60]$ |
| $\theta_{chop}$ | Choppiness ceiling | $[38,62]$ | $[14,86]$ |
| $P$ (gc\_period) | GC filter period | $[50,250]$ | $[150,750]$ |
| $N$ (gc\_poles) | GC filter pole count | $[1,9]$ | $[3,27]$ |
| $atr_p$ | ATR period | $[8,20]$ | identical |
| $\kappa$ (stop\_mult) | Stop distance, ×ATR | $[1.5,6.0]$ | identical |
| $\ell$ (grid\_levels) | Number of TP levels | $[2,8]$ | identical |
| $d_1,\dots,d_8$ | Per-level incremental distance, ×ATR | $[0.3,2.5]$ each | identical |
| $f_1,\dots,f_8$ | Per-level close fraction | $[0.1,0.4]$ each | identical |
| $\tau$ (trail\_tp\_mult) | Trailing-TP distance, ×ATR | $[0.3,3.0]$ | identical |
| flip\_on\_signal | Reversal flip | fixed at $1$ | fixed at $1$ |

The first 9 rows are the *entry-signal* parameters (§2.4); the remainder are *exit-mechanism* parameters. The "exit same as the entry source" invariant means rows 10–16 are never widened between the two variants — only the entry timing itself is searched differently.

### 5.2 Locked-entry fine-tuning: a hybrid exploitation/exploration regime

For any $(symbol, interval, source)$ triple explicitly marked in a persistent configuration file, the system supports **pinning the entry-signal vector to a previously-validated operating point while continuing to fully, independently search the entire exit-parameter vector** every cycle. Critically, this is not an exact freeze: the entry vector is *jittered* on each cycle within a fixed fractional radius ($15\%$ of that parameter's own declared range, clamped into range) of the locked base values, via the identical mechanism (`_sample_local`) already used to let the "pine" variant refine around "searched"'s current live winner (§5.3). This is a deliberate exploitation/exploration compromise: the entry signal's *character* is preserved (guarding against the search discovering a spurious, overfit entry timing on a single week of data and discarding a signal with a demonstrated track record), while both a small local neighborhood of entry variants *and* the entire exit space remain under continuous re-optimization against the freshest available data.

### 5.3 Anchored local refinement

Independently of §5.2, when "pine" has no locked entry of its own, its random search is *centered* around "searched"'s current best validated (i.e., empirically profitable, §7) result for the same $(symbol,interval)$ rather than drawn from scratch, at a reduced sample budget (one-tenth of the standard budget). This treats "searched" — the wider-ranging of the two variants — as an exploratory outer loop and "pine" as a local exploitation pass around whatever it discovers, a coarse-to-fine search pattern, while still permitting "pine" to fall back to an independent global search whenever "searched" has not yet found anything profitable.

### 5.4 Random search over walk-forward-validated windows

For each $(symbol, interval, source)$ combination, per optimization cycle:

1. An **in-sample (IS) window** of $\le 7$ calendar days (a hard, code-enforced ceiling — `IS_DAYS = min(7, \cdot)`, independent of any configuration value) is used to score a large random sample ($N=200{,}000$ combinations per source per interval, drawn uniformly over the bounds in §5.1, subject to a Gaussian-filter warm-up padding of $3\times$ the maximum searchable `gc_period` to avoid transient-startup bias) by a composite score
$$
\text{score} = \text{Sharpe} \cdot \sqrt{\frac{n_{trades}}{n_{min}}} \cdot \max\!\big(0,\, 1-\rho_{zf}\big)
$$
where $n_{min}=3$ is a minimum-evidence floor and $\rho_{zf}$ is the **zero-fill rate** — the fraction of trades that closed (by stop or forced end-of-window) without a single grid level ever filling (§4.5, §7.3). This last multiplicative term is a deliberate, direct penalty against a parameterization that produces frequent "instant reversal, zero protection banked" trades, independent of whatever the aggregate equity curve happens to show.

2. The top $50$ IS-ranked candidates per source (union of newly-drawn combinations, an all-time top-50-by-score elite carry-forward, and every combination that has *ever* been recorded as a genuine winner for this exact triple — an unbounded, unranked guarantee distinct from the ranked elite list, so that a demonstrated past winner cannot silently drop out of rotation purely because other, possibly-overfit combinations scored higher on their own IS windows) are then **re-evaluated out-of-sample (OOS)** on a disjoint $168$-hour (7-day) forward window.
3. The single OOS candidate with the best score (ties broken, historically, by a hierarchy of criteria that has itself evolved — §8) is written as that source's live-tradeable result.

### 5.5 Walk-forward integrity and the 7-day ceiling

The **in-sample window is hard-capped at 7 days at the code level**, independent of any operator-configurable value — a deliberate, non-negotiable constraint adopted specifically to bound how much historical regime information any single optimization cycle can exploit, at the direct cost of very small per-cycle trade counts (§10.1). This is the single most consequential methodological choice in the entire system and is discussed at length in §10.

---

## 6. Position Sizing and Capital Model

Position size is computed identically in backtest and live execution as

$$
q_0 = \frac{E \cdot L \cdot m}{P_0}
$$

where $E$ is current equity, $L=11$ is a **fixed leverage constant** (uniform across the single traded instrument; not derived from any volatility- or risk-based calculation), $m=0.98$ is a margin-headroom safety factor, and $P_0$ is the entry price. A taker fee of $5.5$ basis points is charged against notional on entry and on each partial/total exit.

**This is deliberately *not* a risk-based (e.g., fixed-fractional or volatility-targeted) sizing model.** An earlier iteration of this system implemented and empirically validated (via an out-of-band, longer-horizon stress test) a risk-based sizing scheme that scaled position size inversely with stop distance, specifically to bound the equity impact of any single stop-out. That scheme was subsequently and explicitly reverted at the system operator's direction, on the stated principle that "the best-performing backtested parameters should win, full stop," accepting as a known and named consequence that **no mechanism in the current system bounds the fraction of equity a single losing trade can consume** beyond whatever the searched stop-loss distance happens to imply at $11\times$ leverage. This is recorded here as a first-order, currently-accepted risk exposure, not a latent bug (§10.4).

Capital allocation across instruments (currently moot with a single configured symbol, ETHUSDT) uses a **slot** model: at most one symbol may hold capital at $97\%$ of available equity at any time; a second qualifying symbol's entry signal is simply skipped, not queued, while the slot is held.

---

## 7. Trade Classification and Performance Attribution

### 7.1 Fee- and magnitude-adjusted win definition

A closed trade's realized PnL counts as a **win** only if it clears *two* independent bars, not merely $\text{PnL}>0$:

$$
\text{PnL}_{net} > 2.0 \times \text{fees}_{trade} \quad \wedge \quad \left|\frac{\bar P_{exit}-P_0}{P_0}\right| \ge 0.33\%
$$

where $\bar P_{exit}$ is the quantity-weighted average exit price across every grid-fill/unwind leg of the trade, and the percentage move is measured on the **underlying, pre-leverage** price — i.e., roughly $1/11$th of the corresponding leveraged equity return. A trade that merely edges out its own transaction costs, or whose entire realized move was smaller than the stated minimum edge threshold, is still recorded (its PnL flows fully into the loss/breakeven aggregate) but does not count toward win-rate.

### 7.2 Metric suite

Every OOS-tested candidate produces: Sharpe ratio (per-trade returns, annualization via the interval's own bars-per-year), CAGR, total return %, maximum drawdown %, trade count, average holding period (in bars), win rate (§7.1), profit factor, cumulative gross profit and gross loss (`cum_profit`/`cum_loss` — dollar-denominated, not percentage), and the zero-fill rate (§5.4).

### 7.3 The zero-fill-reversal failure mode

A **zero-fill trade** is one that closes (by stop-loss or forced end-of-data) having never filled a single grid level. Direct replay of two live-deployed parameter sets against real historical data found zero-fill rates of approximately $20\%$ for both the "searched" and "pine" variants examined — i.e., roughly one trade in five currently extracts *no* partial profit whatsoever before reversing to the stop, a material fraction of total trade flow. This is the empirical finding that directly motivated both the zero-fill-rate scoring penalty (§5.4) and the trailing-take-profit mechanism (§4.5); its persistence at a nontrivial rate even after both mitigations is disclosed here as an open, only partially-addressed structural weakness of a grid-based (rather than immediate-trailing-stop) exit design, not a solved problem.

---

## 8. Evolution of the Live-Capital Selection Rule

Which of the (up to four, given two configured intervals × two entry sources) independently-optimized candidates for a given symbol actually receives live capital is governed by a selection rule that has been revised **seven times** over this system's documented operating history, each revision made in direct response to either an operator value judgment or a concrete empirical counterexample. A thesis-level treatment of this strategy is incomplete without this history, because it is itself a case study in the difficulty of specifying "good performance" for a strategy of this shape:

1. **100%-win-rate baseline only.** The earliest rule traded only a candidate with a perfect historical win rate. Rejected once it became clear this systematically favored candidates with too few trades to be statistically meaningful.
2. **100%-WR baseline, overridden by an 80%-WR/low-loss tier if more profitable.** A two-tier compromise: a candidate need not be perfect if it clears an 80% win-rate floor *and* a hard cumulative-loss ceiling *and* out-earns the perfect-WR candidate.
3. **Floor lowered to 60%.** The 80% floor was found in practice to reject candidates with materially higher total profit purely for occasionally taking a loss.
4. **Win-rate abandoned entirely, replaced by direct return/loss/drawdown targets** ($\text{ret}\ge15\%$, $\text{cum\_loss}<\$5$, $\text{drawdown}$ tighter than a fixed ceiling). This revision was triggered by a direct, documented counterexample: of three real candidates compared side-by-side, the one that best matched what the operator actually wanted (high return, small realized losses) had the *lowest* win rate of the three — direct empirical evidence that win rate is not merely an imperfect but an actively *misleading* proxy for this strategy's actual risk/reward shape, since the exit structure (§4) is explicitly designed to produce many small breakeven-or-worse trades offset by few larger banked-profit trades, which a win-rate floor structurally penalizes.
5. **The drawdown ceiling itself was subsequently loosened, then removed entirely**, on the stated principle that "the best-performing backtested parameters should win" without any secondary risk constraint — the most consequential and most philosophically contestable revision in this history. Its direct, disclosed consequence: a candidate with an $83\%$ win rate but a single $-41\%$-drawdown loss was, for a period, ranked ahead of a candidate with a $-10\%$ drawdown and a far smaller absolute loss, purely because its gross profit was larger.
6. **Net-profit ranking.** In direct response to the above, the ranking metric was changed from *gross* cumulative profit to **net profit** ($\text{cum\_profit}-\text{cum\_loss}$) — reintroducing a loss-side counterweight into the ranking *without* reinstating a hard reject gate, on the reasoning that a joint profit/loss ranking, rather than either a pure-profit ranking or an independent pass/fail loss ceiling, better matches the operator's stated preference for "highest profit, lowest loss" as a single trade-off rather than two separately-gated criteria.
7. **Current state.** The live rule is: for each symbol, across every $(interval, source)$ result currently on file and within its freshness window, select the single candidate with the highest $(\text{cum\_profit}-\text{cum\_loss})$; a symbol with no qualifying result at all receives no live capital.

This progression is presented not as a settled, theoretically-derived optimum but as an ongoing empirical negotiation between (a) what is easy to compute and rank, (b) what the strategy's own exit-structure statistically produces, and (c) the operator's evolving, explicitly-stated risk tolerance. A rigorous reader should treat the *current* rule (item 7) as one point in an unfinished search, not as a validated conclusion.

---

## 9. Empirical Validation: Deflated Sharpe Ratio and Probability of Backtest Overfitting

This chapter addresses RQ/H1/H2 (§1.3) directly, using real ETHUSDT 30-minute market data and the production system's own signal and simulation code (§2, §4) rather than synthetic data or a re-implemented approximation of the strategy.

### 9.1 Data and window construction

A single continuous series of $10{,}000$ 30-minute ETHUSDT bars (2026-02-07 through 2026-09-03, Bybit linear perpetual) was fetched once. From its tail, $S=8$ **non-overlapping, sequential 7-day blocks** were carved, each preceded by its own $2{,}250$-bar ($\approx$47-day) Gaussian-Channel warm-up padding drawn from the same continuous series — the identical warm-up convention the production in-sample sweep itself uses (§5.4). This is the specific mechanism by which the operator-mandated "no single backtest evaluation may exceed 7 days of data" constraint (§5.5, and this document's own operating rule) is satisfied *and* a genuine multi-week, multi-regime validation panel is constructed: **every individual simulation call in this chapter evaluates exactly one 7-day tradeable window**; the 8 blocks are independent single-window backtests run back-to-back over a ~9-week span, not one long backtest. The 8 resulting blocks span 2026-07-09 through 2026-09-03.

### 9.2 Candidate pool and quality gates

For the grid strategy, $M=20{,}000$ entry+exit parameter combinations were drawn independently and identically from `PARAM_SPACE_SEARCHED` (§5.1, "searched" entry source — this chapter's scope is deliberately restricted to one entry source; §3.2 argues the two sources' signals are near-identical, so this is not expected to materially change the qualitative conclusions, but is disclosed as an explicit scope restriction, not a completeness claim). Each candidate was backtested against all 8 blocks independently using the production `_bt_combo_pair` simulation, subject to the system's own pre-existing quality gates (§5.4): $n_{trades}\ge3$, average holding period $\ge2$ bars, reward:risk $\ge0.8$, positive total return. A candidate failing any gate in a given block produced no result for that block.

A candidate that fails these gates in a given week is, for live-deployment purposes, indistinguishable from a strategy that simply does not trade that week — it is not a missing observation, it is a $0\%$-return observation. All statistics below therefore treat a non-qualifying block as a **score of $0$**, not as excluded/missing data; treating it as missing would restrict the entire analysis to the small handful of candidates that happen to qualify in *every* one of 8 independent weeks (empirically, 1 of 20,000 in this dataset — itself a finding, see §9.5), discarding the great majority of the search's own actual behavior.

### 9.3 H1 — Deflated Sharpe Ratio of the selected candidate

Using the Bailey & López de Prado (2014) formulation, for the most recent block (block 8, 2026-08-27–2026-09-03, the block most representative of "what would be selected for live trading today"):

$$
\widehat{SR}_0 \;=\; \hat\sigma_{SR}\Big[(1-\gamma)\,Z^{-1}\!\big(1-\tfrac1N\big) + \gamma\,Z^{-1}\!\big(1-\tfrac{1}{Ne}\big)\Big], \qquad
DSR \;=\; \Phi\!\left(\frac{\widehat{SR} - \widehat{SR}_0}{\widehat\sigma(\widehat{SR})}\right)
$$

where $\hat\sigma_{SR}$ is the empirical standard deviation of Sharpe ratios across all $N=20{,}000$ trial candidates in that block (non-qualifying trials scored $0$, §9.2), $\gamma\approx0.5772$ is the Euler–Mascheroni constant, and $\widehat\sigma(\widehat{SR})$ is the standard error of the selected candidate's own Sharpe estimate. Because the selected candidate's trade count $T$ in any single 7-day block is necessarily tiny (§9.6 below), the full higher-moment (skew/kurtosis) term of $\widehat\sigma(\widehat{SR})$ cannot be estimated reliably from $T\le6$ observations without simply fitting noise — this analysis therefore uses the **Gaussian-return simplification** $\widehat\sigma(\widehat{SR})\approx\sqrt{(1+\tfrac12\widehat{SR}^2)/(T-1)}$ (skew$=0$, kurtosis$=3$), an explicit, disclosed approximation rather than a claim that trade returns are actually Gaussian at this sample size.

| Quantity | Grid exit | Fixed TP/SL benchmark |
|---|---|---|
| $N$ (candidates drawn) | 20,000 | 20,000 |
| $\hat\sigma_{SR}$ (cross-trial Sharpe std) | 1.2087 | 0.7084 |
| $\widehat{SR}_0$ (expected max under null) | 4.8672 | 2.8528 |
| $\widehat{SR}$ (selected candidate) | 14.3350 | 22.2753 |
| $T$ (selected candidate's trade count) | 6 | 3 |
| $\widehat\sigma(\widehat{SR})$ | 4.5551 | 11.1601 |
| **DSR** | **0.9812** | **0.9591** |

Both exceed the conventional $DSR>0.95$ significance threshold — **H1 is nominally supported for both exit structures**, not merely the grid. This should not be read as strong confirmation of a genuine edge: at $T=3$–$6$ trades, $\widehat\sigma(\widehat{SR})$ is enormous (11.16 for the benchmark), meaning the DSR calculation is highly sensitive to exactly which few trades occurred, and the raw $\widehat{SR}$ values themselves (14–22) are inflated by this system's own bars-per-year annualization convention applied to a handful of multi-day-hold trades — a known distortion of annualized Sharpe at very low trade counts, not a claim of a 14×-Sharpe strategy in any conventional sense. The honest reading of §9.3 is: *conditional on the Gaussian-return approximation, neither result can be dismissed as pure trial-count noise* — a considerably weaker and more defensible claim than "the strategy has a proven edge."

### 9.4 H2 — Grid exit vs. fixed TP/SL: qualification rate and PBO

A benchmark exit was implemented sharing the identical entry-signal composition (§2.4) and parameter-search protocol, replacing the grid (§4) with a single take-profit and single stop-loss, each an independently searched ATR multiple, subject to the identical quality gates. This isolates the exit structure as the only varying factor.

**Qualification rate** (fraction of the $20{,}000$-candidate pool clearing all quality gates in a given block) is the cleanest, least assumption-laden comparison available from this data:

| Block | Grid | Fixed TP/SL |
|---|---|---|
| 1 | 3.91% | 0.88% |
| 2 | 3.26% | 0.75% |
| 3 | 5.38% | 1.38% |
| 4 | 3.14% | 0.22% |
| 5 | 4.78% | 0.77% |
| 6 | 1.79% | 0.43% |
| 7 | 3.94% | 1.59% |
| 8 | 3.10% | 1.49% |
| **Mean** | **3.66%** | **0.94%** |

The grid exit produced a viable (quality-gate-clearing) candidate roughly **3.9× as often** as the fixed TP/SL benchmark, consistently across all 8 independent weekly windows (grid $>$ fixed in every single block, a $2^{-8}\approx0.4\%$-probability sign pattern under a null of no difference). **This is the strongest, most assumption-free empirical result in this chapter, and it directly supports H2**: given the identical entry signal, the multi-level grid structure is a materially more robust mechanism for producing a backtest-legitimate, deployable parameterization than a conventional single-target exit, independent of which specific parameterization is ultimately selected.

**PBO** (CSCV, $S=8$ blocks split into all $\binom{8}{4}=70$ train/test combinations, ranking each split's IS-training-average-score winner by its OOS-test-average-score percentile) was computed identically for both:

| | Grid | Fixed TP/SL |
|---|---|---|
| PBO | 24.3% | 0.0% |
| mean logit $\bar\lambda$ | 4.698 | 4.350 |

Both fall well below the $50\%$ "no-better-than-random-selection" reference point, which on its face favors both exit structures roughly equally and would suggest the *selection process itself* is not simply picking in-sample noise. **This comparison is disclosed as unreliable rather than reported at face value**: the fixed-TP/SL benchmark's much lower qualification rate (§9.4 table above) means its OOS score distribution within any CSCV split is overwhelmingly zero-valued: a candidate need only be *any* qualifying, nonzero-scoring parameterization to rank near the top of an OOS set that is $>99\%$ zeros, which mechanically deflates PBO toward $0$ regardless of genuine selection skill. The qualification-rate result (which requires no such artifact-prone ranking step) is judged the credible test of H2 in this chapter; the PBO comparison between the two exit structures is reported for completeness but should not be cited as evidence that either structure is *more* overfitting-resistant than the other — only the within-grid PBO of $24.3\%$ (an internally consistent number, not a cross-structure comparison) is treated as informative on its own terms.

### 9.5 A direct, quantified overfitting-risk finding

Independent of H1/H2, the raw qualification-rate data above yields a finding directly relevant to §10.2's (multiple-comparisons) concern, stated quantitatively for the first time in this document: **only $1$ of the $20{,}000$ randomly-drawn grid candidates cleared the quality gates in all 8 independent weekly windows simultaneously**; the median candidate cleared them in zero. The production search's $200{,}000$-combination-per-cycle budget (§5.4) is therefore not usefully understood as "200,000 independent shots at finding an edge" — the overwhelming majority of any such sweep is, by this data, parameter space that never produces a single deployable week, in any regime tested. The system's *effective* multiple-comparisons exposure for DSR purposes (§9.3) is better approximated by the $\sim700$–$1{,}100$ candidates that qualify in any *given* single block (§9.2's per-block counts) than by the raw $200{,}000$ draw count — a materially smaller, but still large, number of "live" comparisons.

### 9.6 Summary against RQ/H1/H2

| Hypothesis | Verdict | Strength of evidence |
|---|---|---|
| H1 (edge significance, grid) | Nominally supported (DSR $=0.981>0.95$) | Weak — $T=6$, high-annualization-inflation, Gaussian-SE approximation |
| H2 (grid exit outperforms fixed TP/SL) | Supported | Moderate-to-strong on qualification rate (consistent across all 8 blocks); the PBO comparison is inconclusive due to a disclosed sparsity artifact (§9.4) |

Both verdicts are reported as provisional, single-instrument, single-9-week-sample findings (§10.6), not as a general validation of the strategy — see §10 for the full accounting of what this chapter does and does not establish.

---

## 10. Limitations and Threats to Validity

### 10.1 Sample size

The 7-day hard IS-window ceiling (§5.5) — itself a deliberate, explicitly-reaffirmed operator constraint, not an oversight — combined with the entry filter's inherent selectivity (§2.4), routinely produces **single-digit trade counts per OOS evaluation window** (observed range: 3–10 trades per 7-day OOS window across this system's documented history). Every Sharpe ratio, win rate, and drawdown figure this system computes and ranks candidates by should be read with the understanding that it is estimated from a sample size at which classical asymptotic statistics (which underlie the Sharpe ratio's usual interpretation) provide essentially no guarantee. A single trade routinely represents 10–35% of the entire evaluation sample. This is the dominant source of estimation noise in the entire system and is not mitigated by any of the mechanisms in §4 or §5 — it is a direct, accepted consequence of the 7-day constraint.

### 10.2 Multiple-comparisons / search overfitting

Each optimization cycle draws on the order of $2\times10^5$ candidate parameterizations per source per interval and selects the single best performer on a 7-day IS window. This is a large multiple-comparisons problem: with a sufficiently large and flexible search space, some combination will fit the idiosyncratic noise of any particular 7-day window well by chance alone, independent of any genuine, persistent edge. The OOS retest (§5.4) is the system's primary defense against this, but the OOS window is itself only 7 days and is reused across every retry/refinement round within a cycle (§5.2–5.3), which is a second-order re-use of the same evaluation data across many candidates within one cycle — a genuine, only partially mitigated risk of **OOS overfitting through repeated reuse**, distinct from and in addition to ordinary IS overfitting. No held-out, never-reused-until-deployment third data window currently exists in this system.

### 10.3 The zero-fill and grid-shape trade-offs

As documented in §7.3, roughly one trade in five currently banks no partial profit at all before reversing to the stop. The trailing take-profit (§4.5) and zero-fill scoring penalty (§5.4) are direct, motivated responses to this, but neither is a structural fix — both operate by making the *search* prefer parameterizations less prone to the failure mode, which is a statistical nudge over a finite, small sample (§10.1), not a guarantee.

### 10.4 Absence of risk-based position sizing

As discussed in §6, position size is a fixed function of equity and leverage, entirely independent of the searched stop distance. A stop-loss discovered by the search to be, e.g., $5\times$ ATR wide is executed at the identical $11\times$-leverage notional as one discovered to be $1.5\times$ ATR wide, meaning the two can imply vastly different fractions of equity at risk on a single trade despite receiving identical position sizing. This was a known, previously-implemented-and-then-explicitly-reverted mitigation (§6) and represents the single largest unmitigated tail-risk exposure in the currently deployed system.

### 10.5 No exchange-side stop-loss

Consistent with §4.2's stop being a purely software-tracked level (never submitted to the exchange as a resting stop order), the strategy's only mechanism for closing a losing position is the trading engine's own signal evaluation on the next bar close of the relevant interval. This is an accepted-risk design choice (a restart-recovery reconciliation mechanism exists specifically to ensure a live-exchange position is never left orphaned from its local risk-management state across a process restart), but it means the strategy carries genuine execution risk — a process crash, an exchange API outage, or sufficiently fast intra-bar price movement — that a resting exchange-side stop order would not.

### 10.6 Single-instrument, single-exchange scope

All empirical claims in this document pertain to ETHUSDT perpetuals on Bybit specifically. No claim of cross-asset or cross-venue generalization is made or should be inferred; the correlation, liquidity, and backtest-quality analysis this system's own operating history describes as a prerequisite for adding a second instrument has not, as of this writing, been repeated for any symbol beyond ETHUSDT.

### 10.7 Numerical stability boundary of the trend filter

As noted in §2.2, the Gaussian Channel recursion is numerically unstable for some high-pole-count parameterizations, particularly within the wider "searched" pole-count range (up to 27). The current defense is a runtime divergence detector that suppresses affected candidates' entry signals rather than a formal stability proof over the full searched $(P,N)$ domain — i.e., the boundary between "stable" and "unstable" regions of the pole-count space is discovered empirically at evaluation time, not characterized analytically in advance.

---

## 11. Conclusion

The strategy formalized here is best characterized as a **narrowly-filtered, infrequent-trade mean-reversion signal paired with a maximally-adaptive, multi-stage exit structure**, where nearly every exit-side design decision — stop distance, number of profit-taking levels, their spacing and sizing, the trailing-profit distance, and now even the fine-tuned neighborhood of a "locked" entry signal — has been converted from a fixed rule into a free parameter subject to continuous, walk-forward-validated random search. This is a coherent and internally consistent design philosophy: rather than asserting *a priori* what the "right" exit shape is, the system lets a disjoint out-of-sample window adjudicate among tens of thousands of candidate shapes every cycle.

Returning to RQ (§1.3): across 8 independent 7-day out-of-sample windows spanning 9 weeks of real ETHUSDT data, H2 is supported by the single cleanest metric available (a 3.9× higher quality-gate qualification rate for the grid exit versus a fixed-TP/SL benchmark sharing the identical entry signal, consistent in direction across all 8 windows), and H1 is nominally, but only weakly, supported (Deflated Sharpe Ratio $0.981$, against a $0.95$ threshold, resting on as few as 6 trades and an explicit Gaussian-return approximation). Neither result should be mistaken for proof of a durable trading edge; both are exactly what they claim to be — a falsifiable test of two narrow claims, run once, on one instrument, over one 9-week historical sample, with every simplifying assumption disclosed in §9 rather than absorbed silently into the headline numbers.

The system's principal, clearly-identified vulnerabilities remain downstream of the same design choice that gives it its adaptability: very small per-window sample sizes (§10.1), meaningful multiple-comparisons exposure quantified directly in §9.5 (a 20,000-candidate sweep effectively contains on the order of $10^3$ "live" comparisons, not $2\times10^5$), and — following an explicit, values-based operator decision rather than a technical failure — the complete absence of any risk-based position-sizing counterweight to whatever stop distance the search happens to select (§10.4, §6). Any further development of this system should treat closing that last gap, extending §9's validation to a second, held-out historical period and to the "pine" entry source, and replacing the PBO comparison's disclosed sparsity artifact with a design that does not require the 0-fill convention, as the highest-priority open items ahead of any further exit-mechanism sophistication.

---

## References

Bailey, D. H., & López de Prado, M. (2014). The Deflated Sharpe Ratio: Correcting for Selection Bias, Backtest Overfitting, and Non-Normality. *The Journal of Portfolio Management*, 40(5), 94–107.

Bailey, D. H., Borwein, J. M., López de Prado, M., & Zhu, Q. J. (2017). The Probability of Backtest Overfitting. *Journal of Computational Finance*, 20(4), 39–69.

Chande, T. S., & Kroll, S. (1994). *The New Technical Trader: Boost Your Profit by Plugging into the Latest Indicators*. John Wiley & Sons. (Choppiness Index.)

Ehlers, J. F. (2004). *Cybernetic Analysis for Stocks and Futures: Cutting-Edge DSP Technology to Improve Your Trading*. John Wiley & Sons.

Ehlers, J. F. (2013). *Cycle Analytics for Traders: Advanced Technical Trading Concepts*. John Wiley & Sons. (Gaussian and related IIR filter constructions for price series.)

Hansen, P. R. (2005). A Test for Superior Predictive Ability. *Journal of Business & Economic Statistics*, 23(4), 365–380.

Lane, G. C. (1984). Lane's Stochastics. *Technical Analysis of Stocks & Commodities*, 2(3), 87–90.

López de Prado, M. (2018). *Advances in Financial Machine Learning*. John Wiley & Sons. (Combinatorial Purged Cross-Validation and related backtest-validation methodology.)

Pardo, R. (2008). *The Evaluation and Optimization of Trading Strategies* (2nd ed.). John Wiley & Sons. (Walk-forward optimization.)

White, H. (2000). A Reality Check for Data Snooping. *Econometrica*, 68(5), 1097–1126.

---

## Appendix A — Glossary of Implementation-Specific Terms

| Term | Meaning |
|---|---|
| IS / OOS | In-sample / out-of-sample: the walk-forward split used to validate a candidate before it is eligible to trade live. |
| Zero-fill trade | A trade that closed without any grid level ever filling. |
| Locked entry | An entry-signal vector pinned (with small permitted jitter) to a previously-validated operating point, while exit parameters remain fully searched. |
| Flip | Closing a position and immediately opening the opposite side on an opposing entry signal, without an intervening flat period. |
| Unwind | Closing part of an already-filled grid level's fraction on a retracement crossing back through that level's price. |
| Net profit (selection metric) | `cum_profit − cum_loss`, the current live-capital ranking criterion (§8, item 7). |
| Slot | The single unit of tradeable capital (97% of equity) shared across all configured symbols. |

## Appendix B — Source Correspondence

For verification against the running implementation: entry-signal composition — `eth_trader_bt.py::compute_partial_signals` region (buy/sell construction, §2.4); Gaussian Channel — `gaussian_channel_midline`/`_gc_filt9x` (§2.2); grid construction — `grid_level_prices`, shared verbatim between `_bt_combo_pair` and the live `ComboTrader.tick()` (§4.1); trailing take-profit and flip — `_sim_grid_jit`/`_bt_combo_pair`'s trail/flip branches and `eth_trader.py::_manage_exit`/`_flip` (§4.4–4.5); scoring — the `score = sharpe * sqrt(trades/MIN_TRADES) * max(0, 1-zero_fill_rate)` expression in `_bt_combo_pair` (§5.4); selection rule — `eth_trader.py::_load_all_worthy_crypto` (§8, item 7); win classification — the `WIN_FEE_MULT`/`MIN_WIN_PRICE_PCT` constants and their seven check-sites (§7.1).

## Appendix C — Reproducibility of the §9 Empirical Validation

The §9 analysis is a standalone research artifact, not part of the production module (`eth_trader_bt.py` itself is unmodified by it), reusing the production signal/simulation functions directly (`_bt_combo_pair`, `_sample`, `PARAM_SPACE_SEARCHED`, `_bars_per_day`, `GC_WARMUP_BARS`) rather than a re-implementation. Data: one continuous fetch of 10,000 ETHUSDT 30-minute bars (2026-02-07 through 2026-09-03) via the same `HTTP`/kline pagination pattern `fetch_ohlcv` uses, cached once. Grid-strategy trial matrix: 20,000 `PARAM_SPACE_SEARCHED`-sampled candidates × 8 blocks, evaluated via unmodified `_bt_combo_pair`. Fixed-TP/SL benchmark: a standalone `bt_fixed_pair` function (single take-profit/single stop-loss, identical entry-signal construction, identical win-classification and quality-gate constants imported directly from `eth_trader_bt`) — included here in full for auditability:

- Random seeds: `20260904` (grid trial pool), `20260905` (fixed-TP/SL trial pool).
- $S=8$ blocks, `WINDOW_BARS = 7 * bars_per_day("30") = 336`, `WARMUP = GC_WARMUP_BARS = 2250`.
- DSR: Bailey & López de Prado (2014) formula, Gaussian-return simplification (§9.3), evaluated at the most recent block.
- PBO: CSCV with $\binom{8}{4}=70$ train/test splits, block-level mean score as the IS/OOS statistic (§9.4's disclosed simplification versus pooled raw-return CSCV).

A reader wishing to reproduce or extend this chapter should regenerate the trial matrices rather than treat the specific numbers in §9's tables as immutable — they are one realization of the search's own randomness over one historical sample, exactly as §10.6 cautions.
