---
name: unified-combo
description: Use when working in unified_combo_gui_grid (this repo) — the Grid fork of the Bybit ATR_PARTIAL combo backtester + paper/live trader GUI, crypto only. MAINNET ONLY, never demo/testnet, not even by asking — this is a live app handling real money. Single strategy/bot (AtrPartialPaperBot, no ATR_STOP shadow) but TWO competing entry-signal sources per (symbol, interval) — "searched" (all entry params backtester-optimized, drawn from a 3x-widened space, bt.PARAM_SPACE_SEARCHED, added 2026-08-28) and "pine" (searches the same 9 entry params, via an independent random sweep, over the original narrower space, bt.PARAM_SPACE — the two sources briefly shared the exact same range the same day "pine" was added, before the same-day widening split them; the only difference in the entry SIGNAL FORMULA itself, as opposed to the range each source draws from, is the Gaussian Channel formula's constant, bt.PINE_GC_SQRT2=1.414, the user's "Stochastic Triple Filter [ATP]" Pine Script's hardcoded literal, in place of math.sqrt(2)) — each independently IS-ranked/OOS-retested/saved to its own result file (`..._searched.json`/`..._pine.json`), whichever wins becomes the entry signal paper/live trades; only the entry differs, the exit (the searched GRID of ATR-multiple take-profit levels: grid_levels, plus each active level's OWN independently-searched grid_dist_i/grid_frac_i, per-level since 2026-08-28 — see grid_level_prices) is always searched/optimized for both sources — each level closes that level's own fraction of the ORIGINAL entry qty, the stop trails to breakeven then to the previous filled level. Also covers the capital-slot system (at most CAPITAL_TIERS symbols hold funds at once, currently just 1, claimed first-signal-wins), per-symbol live position mirroring (LiveExecutor.partial_exit(bot_id, frac) — one call per grid level), the position-freeze rule (_protected_entry_source, returns None/"searched"/"pine"/"ALL"), the periodic missed-trade report, DPAPI key storage, hardcoded leverage, the unified_combo_trader_grid.exe naming (deliberately distinct from the source repo's exe so a kill-by-image-name build routine can never cross-kill the other repo's live process), build/exclude setup. Do not apply this to unified_combo_gui (the separate, still-live source repo) or the user's other trading-bot repos — it is specific to this codebase only.
---

# Unified Combo Grid GUI

This skill is scoped to exactly one project — originally
`C:\Users\User\Documents\Trading\unified_combo_gui_grid` on Windows, a fork of
`unified_combo_gui` (a separate, still-live mainnet repo at
`C:\Users\User\Documents\Trading\unified_combo_gui` — do not touch it from here, and do
not assume anything fixed/changed in one applies to the other). **This checkout is the
macOS port of that Grid fork** (ported 2026-09-01/02) — everything below describing
strategy/backtest/trading LOGIC applies identically on both platforms (that's all pure
Python, unchanged by the port); everything describing the EXE name, key storage,
packaging, or kill/build mechanics is Windows-era history except where a "macOS port"
note says otherwise — see CLAUDE.md's own macOS-port note at its top, and the
`uc-build` skill (mac-native) for the actual build/deploy procedure on this platform.
The user has ~40 other trading-bot repos, all deleted — nothing in this skill applies
to those either.

## What this is

A PyQt6 desktop app, one executable — `unified_combo_trader_grid.exe` on Windows, a real
`Unified Combo Grid.app` bundle on this macOS port, built from the same
`unified_combo_trader.py` entry point either way (deliberately NOT
`unified_combo_trader`/`unified_combo_trader.exe`, see "Hard invariants") — that
backtests, paper trades, and
optionally live-mirrors a single combo strategy on Bybit USDT linear perpetuals —
**crypto only**. `unified_combo_bt.py` is a pure library — no `main()`, no standalone
TUI/exe. The trader imports it directly (`import unified_combo_bt as bt`) and drives it
in-process from the Backtest tab.

Four tabs: **Home** (API keys, Start/Stop Paper, per-leg status), **Backtest** (starts
automatically on launch — see below), **Paper** (per-leg portfolio + the one bot's
state), **Live** (mirrors paper signals to a real account, per leg, when a live key is
saved).

**The Backtest sweep auto-starts on launch — the one deliberate exception to "nothing
auto-starts."** `MainWindow.__init__` calls `BacktestTab.auto_start()` immediately, which
checks the auto-repeat box and starts a `BacktestRunner` with zero clicks, repeating every
`bt.LOOP_INTERVAL` (60min as of 2026-09-01, was 2h then briefly 30min — see "Timing
tightened" below) for the process's life. Paper and Live are unaffected —
`_paper_running` still starts `False`, Start Paper is still a user click, and the
live-trading confirmation dialog still fires there.

**Every symbol is tested at every configured interval (`bt.CRYPTO_INTERVALS`) every
cycle** — this dropped from `["5","15","30"]` to `["5"]` only for part of 2026-08-31
(alongside the 1-minute-feed removal below, since exits/entries now both live on one
timeframe), then `"15"` was added back the same day (explicit user ask, "add 15m to
it") → `["5", "15"]`, then replaced with 30m-only on 2026-09-01 (explicit user ask,
"30m candles only. trade only 30m candles") → `["30"]`, the current default — and
whichever interval currently scores best for a symbol is the one used for its leg (one
winning interval per symbol, not one leg per qualifying interval). `crypto_intervals`
in `data/unified_combo_config.json` can still list more than one; the selection-rule
code itself has no hardcoded assumption about the count.

**Legs and capital are decoupled.** `_load_all_worthy_crypto()` returns every currently
qualifying symbol and each gets its own single bot (`AtrPartialPaperBot`); backtesting
continues for all configured symbols regardless — but **capital is a separate, scarce
resource**: at most `len(CAPITAL_TIERS)` symbols may actually hold funds and trade at any
moment (currently `CAPITAL_TIERS = [0.97]`, i.e. only **one** symbol, at 97% of funds).
Whichever qualifying symbol signals an entry first claims the slot
(`TradingEngine.claim_slot`); any other symbol's signal while the slot is held **skips its
order entirely, paper and live**, until the occupant goes fully flat and the slot is
reclaimed by whichever symbol signals next. Current config is narrowed to
`symbols: ["ETHUSDT"]` (see Configuration reference), so multi-symbol contention isn't
currently in play, but the mechanism supports it if config is widened again. Zero legs
running is a normal, expected state if nothing currently qualifies.

**Current selection rule (2026-09-01 onward): win_rate gates nothing.** A symbol gets a
leg iff at least one of its (interval, source) result files clears
`bt._clears_target` — `total_ret_pct>=15` AND `cum_loss<5` AND `max_dd_pct>-5`
(`bt.TARGET_MIN_RET_PCT`/`TARGET_MAX_CUM_LOSS`/`TARGET_MAX_DD_PCT`) — the best-by-
`cum_profit` such candidate across every symbol/interval/source wins; no 100%-WR
baseline tier any more. See "Win-rate abandoned as a selection criterion" in
CLAUDE.md for the full data-driven reasoning and verification. Everything in the
**"Selection rule"** paragraph immediately below this one describes the SUPERSEDED
win-rate-based system (2026-08-28 → 2026-08-31) — kept as historical record of how the
current rule was arrived at, not the current mechanism.

**Selection rule — replaced 2026-08-28, same day as the pine-entry-source work but a
separate change (explicit user ask: "if cuml less than $5 in bt and WR 80% and above
but pnl higher than 100wr params then trade 80% params. remove all other methods of
selection").** This fully replaced the earlier three-tier system (a since-removed
$1-cum_loss "low-risk" tier, the 100%-WR tier, and an 80%-WR/20%-total_ret_pct
fallback). **The win-rate floor moved from 80% to 60% on 2026-08-31** (explicit user
ask, "60 replaces 80" — this followed a brief detour where a genuinely new third tier
was built first, then collapsed back into the existing second tier once the user
clarified — see "Retry-until-60%-WR sweep" below); the tier's shape and override logic
are otherwise unchanged from the 2026-08-28 rule. Exactly two candidates are looked up
per symbol in `_load_all_worthy_crypto()` (`unified_combo_trader.py:~1172`):
- `hundred[sym]` — the best-by-sharpe 100%-win-rate candidate, if any (still the
  baseline/default trade).
- `sixty_plus[sym]` (named `eighty_plus` before 2026-08-31) — the best-by-`cum_profit`
  candidate clearing BOTH `win_rate >= _MIN_WR_60PLUS` (0.60, inclusive — was
  `_MIN_WR_80PLUS`/0.80) AND `cum_loss < _MAX_CUML_60PLUS` ($5, strict — a candidate
  whose total backtested losses equal exactly $5 does NOT qualify; constant renamed
  from `_MAX_CUML_80PLUS`, same $5 value), if any.

A symbol trades `sixty_plus` instead of `hundred` only when `sixty_plus` exists AND
(`hundred` doesn't exist, OR `sixty_plus`'s `cum_profit` is strictly higher than
`hundred`'s) — otherwise it trades `hundred` if that exists. A symbol with neither a
100%-WR nor a qualifying 60%+ candidate gets no leg at all; there is no other fallback
at the initial-selection level (an already-running leg's separate pause-when-nothing-
clears-60%-WR behavior, added the same day, lives in `_param_reload_loop` instead — see
"Retry-until-60%-WR sweep" below). This selection is entirely independent of
`entry_source` — `_iter_result_files()` scans every `_searched.json`/`_pine.json` file
for every symbol/interval, so a symbol's winning `hundred`/`sixty_plus` candidate can
come from either entry source, whichever one actually produced the best-scoring
backtest. Verified with an isolated 6-scenario test (override case, non-override case,
the `cum_loss==$5` exclusion boundary, the no-100%-WR default-win case, the
no-qualifying-candidate exclusion case, the `WR==80%` inclusive boundary) before being
considered done, since it changes which params real capital actually trades on;
re-verified with a 5-scenario test after the 60%-floor change (override in both
directions, the `cum_loss==$5` exclusion, the `WR==0.60` inclusive boundary, the
sixty_plus-only case).

**Retry-until-60%-WR sweep + already-running-leg pause — added 2026-08-31, explicit
user ask** ("it should keep backtesting each run until min 60 wr", "if there is no
params above 60wr then paper pauses trading until there is"). Two independent halves —
**the "search side" half below was SUPERSEDED 2026-09-01, see "Per-symbol,
all-intervals retry" further down; kept here as the historical record of the first
version**:
- **Search side** (`unified_combo_bt.py`, `optimize_symbol_interval`): after the normal
  IS sweep + OOS retest, for each non-protected entry source whose best OOS-retested
  candidate hasn't cleared `RETRY_MIN_WR` (0.60) yet, keep sampling fresh COMPLETE
  random batches (each a full `N_RANDOM` sweep — same `PARAM_SPACE`/
  `PARAM_SPACE_SEARCHED`, same `tried` dedup set, no combo ever retested) and
  OOS-retesting them, until the floor clears. **No time limit** — a first version
  capped retries at `RETRY_TIME_BUDGET_S` (5 min/source); removed the same day,
  explicit user ask ("it needs to complete all sweeps [then] if none above 60wr go
  through them again", confirmed via AskUserQuestion: drop the cap entirely). The only
  remaining stop condition is `_gen_combos` returning empty — the param space is
  genuinely exhausted (every combo within `N_RANDOM*5` sampling attempts already in
  `tried`) — a deliberately accepted risk: a symbol where 60% WR isn't reachable can
  now occupy this retry loop indefinitely, since `BacktestRunner` processes
  (symbol, interval) pairs sequentially. A new `_better(a, b)` comparator — a candidate
  clearing `RETRY_MIN_WR` always beats one that doesn't regardless of sharpe, sharpe
  breaks ties within the same floor-clearing status — replaces the old raw-sharpe
  comparison everywhere a "which OOS candidate wins" decision is made, including
  across the 48h/60h OOS windows themselves. If the space is exhausted without
  clearing the floor, whichever candidate scored best across every round (initial +
  retries) still gets saved — a low-WR result isn't withheld, it just won't clear
  either selection tier above. A protected source is never retried.
  `optimize_symbol_interval`'s OOS-retest logic was refactored into a reusable
  `_oos_retest_src(src, top_n)` helper shared by the initial round and every retry
  round.
- **Already-running leg pause** (`unified_combo_trader.py`, `_param_reload_loop` /
  `_load_result_for_symbol`): `_load_result_for_symbol` gained a `min_win_rate=None`
  parameter, threaded into its existing `_parse_result_file(min_win_rate=...)` call.
  `_param_reload_loop` now passes `min_win_rate=_MIN_WR_60PLUS` alongside its existing
  `require_fresh=True` — reusing the exact pre-existing "no fresh result → pause new
  entries, leave the open position and current params untouched" path
  (`combo.partial.entries_paused = True`) that staleness already triggered. A symbol
  whose best current result is fresh but under 60% WR now pauses through that identical
  path, and unpauses the moment a fresh AND floor-clearing result reappears. The
  open-position rescue scan in `TradingEngine._run()` deliberately keeps
  `min_win_rate=None` (the default) — same reasoning as its existing
  `require_fresh=False` default: it must still hand back params to manage an
  already-open position even when the symbol no longer qualifies.

Verified (originally, while the time cap still existed — the loop BODY mechanics this
proved are unchanged by its later removal, only the termination condition changed): a
real end-to-end `optimize_symbol_interval` run (fetch mocked to synthetic OHLCV,
`N_RANDOM`/the since-removed time budget shrunk for test speed) completed without
error and produced valid saved results; with `RETRY_MIN_WR` temporarily forced to 0.99
(so retries were guaranteed to activate and never satisfy the floor), the retry loop
ran exactly 3 extra rounds per source within the then-still-present time budget,
logged its give-up message on deadline, and correctly kept the best candidate found
across ALL rounds rather than being overwritten by a worse later round.
**Re-verified after the time-cap removal**: a direct unit test of `_gen_combos`'s
exhaustion mechanism, replicated verbatim against a genuinely tiny fully-enumerable
8-distinct-combo param space (`atr_p`×`grid_levels`×`flip_on_signal`, 2 choices each),
confirmed all 8 combos were found on the first call and every call after that
correctly returned empty — confirming the loop's only remaining stop condition
(`if not retry_combos: break`) actually fires. A separate 2-scenario test of the pause gate
confirmed a 45%-WR-only result is blocked by `_load_result_for_symbol(...,
min_win_rate=0.60)` while still visible to a rescue-scan-style call with no
`min_win_rate`, and that adding a qualifying 70%-WR result immediately clears the gate.

**Per-symbol, all-intervals retry — added 2026-09-01, explicit user ask** ("sweep
through all 5m 15m backtests. then assess if non 60wr or above and then run all
again"). Supersedes the per-interval search-side retry described above: a symbol only
needs ONE of its configured intervals to clear `RETRY_MIN_WR`, so forcing every
interval to individually hit 60% — even after a sibling already qualified — wasted
compute. The retry decision and loop moved from `optimize_symbol_interval` into
`unified_combo_trader.py`'s `BacktestRunner._run()`, which already owned the
`for sym in bt.SYMBOLS: for iv in bt.CRYPTO_INTERVALS:` sweep:
- `optimize_symbol_interval` is single-pass again (no internal `while` retry, no time
  budget, no per-source retry status messages) — it now returns `new_combo_count`
  (int): how many genuinely new param combos it tested this call, excluding the
  `top_params` elite carry-forward. 0 means nothing new was found (data-insufficient,
  or this exact (symbol, interval)'s space is exhausted).
- Per symbol, `BacktestRunner._run()` sweeps every configured interval (one full
  pass), sums the returned `new_combo_count` across the pass, then checks whether ANY
  `(interval, source)` result for that symbol clears `RETRY_MIN_WR` — a protected
  result counts as already-qualifying too, since it's already live-managed. If nothing
  qualifies and the pass found at least one new combo somewhere, sweep all the
  symbol's intervals again from scratch (no time limit). If nothing qualifies AND the
  pass found zero new combos anywhere, stop — the space is exhausted for now.

**A real nuance surfaced verifying this**: cross-call "already tried" persistence only
remembers combos `db_save` actually wrote, and `db_save` only writes combos that
cleared the backtest quality gates. A combo that's sampled and FAILS those gates
dedupes correctly within the one call that drew it, but is forgotten once that call
ends — the next call's `tried` set rebuilds from the DB, which never saw it. Verified
directly: a tiny 8-combo space whose combos never cleared quality gates reported the
same `new_combo_count=8` on 5 repeated calls, never converging to 0. Harmless for the
real, enormous `PARAM_SPACE` (fresh float combinations essentially never run out
regardless), but "genuinely exhausted" is a weaker signal than "every combo has ever
been tried" — closer to "every combo that ever PASSED quality gates has been tried."
Verified: a direct test of the new return value (positive on a fresh symbol, 0 on the
"not enough data" path, non-converging on the tiny-space case above); a
control-flow-only simulation of `BacktestRunner._run()`'s new per-symbol loop (the
exact `qualifies` expression, stubbed `optimize_symbol_interval`) confirmed 3
scenarios: both intervals swept every pass even once one is already failing (never
short-circuited mid-pass); the loop stops the instant any interval/source qualifies
(15m qualifying on pass 2 while 5m never does); it stops via exhaustion after 3 passes
rather than spinning forever when nothing ever qualifies; a protected interval/source
is treated as already-qualifying.

**Win-rate abandoned as a selection criterion, replaced by direct return/loss/DD
targets — added 2026-09-01, explicit user ask**, arrived at by reviewing a real
Backtest tab screenshot together: "which is smart one to trade" (answer: none of the 4
rows shown cleared the then-current 60% WR floor) → "what should the clear % be?
analyse the data. i want to profit return 15% or more and loss under 5 usdt cuml. dd
is a factor too" → confirmed via AskUserQuestion: `total_ret_pct>=15% AND cum_loss<$5
AND DD tighter than 8%`, DD ceiling 5%. The data: of the 3 real candidates shown, only
ETHUSDT 15m searched (20.1% ret, $3.28 cum_loss, -3.4% DD) cleared the new targets —
and it had the LOWEST win_rate of the three (40%). Proof win_rate doesn't track what
matters here; this grid+breakeven-trail strategy's shape (fewer, larger wins offsetting
many small/breakeven losses) is exactly what a win-rate floor fights.
- New shared predicate (`unified_combo_bt.py`): `TARGET_MIN_RET_PCT=15.0`,
  `TARGET_MAX_CUM_LOSS=5.0`, `TARGET_MAX_DD_PCT=5.0`, `_clears_target(r)` — true iff
  `total_ret_pct>=15` AND `cum_loss<5` AND `max_dd_pct>-5`. Explicitly tolerates
  non-dict input (returns False, doesn't raise) — `BacktestRunner` calls it directly
  against `self.status.get(...)`, a plain status STRING until a real result lands;
  this was a real bug caught while verifying, not hypothetical. Independent of, and
  tighter than, the pre-existing hard `MAX_DD_PCT=0.08` simulation-level reject gate.
- Replaces `RETRY_MIN_WR`/win_rate everywhere: `_better()`'s OOS comparator, `_run()`'s
  `qualifies` check, `_load_all_worthy_crypto` (collapses the `hundred`/`sixty_plus`
  two-tier system to ONE rule — best-by-`cum_profit` candidate clearing
  `_clears_target`, no 100%-WR baseline tier any more), and `_param_reload_loop`'s
  pause gate (`min_win_rate=...` → `require_target=True`, same pause mechanism, new
  trigger). `_parse_result_file` drops `require_perfect_wr`/`min_win_rate`/
  `min_ret_pct` for one `require_target` param. `win_rate` stays in every result file
  and the Backtest tab table — informational only now, gates nothing.
- GUI text updated (Home status, Paper/Live empty-states, several docstrings) to stop
  implying win-rate is part of the bar.
- Verified: `_clears_target` unit tests (None/string rejection, boundary semantics —
  `15.0` inclusive, `$5`/`5%` exclusive); the exact real-screenshot numbers classify
  correctly (2 fail, ETHUSDT 15m searched passes); an end-to-end
  `_load_all_worthy_crypto` test reproducing that screenshot's 3 result files selects
  only ETHUSDT 15m searched; `_load_result_for_symbol` confirmed `require_target=False`
  sees every candidate, `require_target=True` sees only the target-clearing one.

**Timing tightened, then `LOOP_INTERVAL` corrected — added 2026-09-01, explicit user
ask** ("also retest every 30 minutes. reload every 40 minutes if no open positions. if
open position wait till close then load new params"), corrected same day ("200k
combos per run. not 400k!!!! run again 60 minutes after last run finished"):
`PARAM_RELOAD_S` 3.5h→40min (`RESULT_MAX_AGE_S` auto-derives, no separate edit). The
"wait till close" part needed no code change — `_param_reload_loop` already had
exactly that wait-for-flat loop before applying any reload, confirmed by re-reading
rather than assumed. `bt.LOOP_INTERVAL` went 2h→30min→**60min** — but `BacktestRunner.
_run()` already computed `next_run_ts` from when the PREVIOUS cycle's work finished,
never from its start, so "run N minutes after the last run finished" was already the
real behavior even at 30min; only the number needed correcting to match what was
actually wanted. Also same day: `N_RANDOM` now means TOTAL combos per run, not per
entry source (`_gen_combos` used to draw the full `N_RANDOM` once per source — 400k
total for a "200k" config — new `N_RANDOM_PER_SOURCE = N_RANDOM // 2` fixes this; the
config value 200000 is unchanged, only its interpretation). Note `LOOP_INTERVAL` can
still be shorter than a single hard-to-qualify symbol's retry-until-target loop (no
time cap) — it controls how soon a new cycle is queued once the current one finishes,
not a ceiling on any one cycle's duration.

**There is exactly ONE strategy/bot — `AtrPartialPaperBot` — but TWO competing
entry-signal sources per (symbol, interval), added 2026-08-28 (explicit user ask: "add
it to the bt. entry logic. exit same as the bot. it must show results for it. paper
should use it if it wins in bt").** The Grid fork still removed the whole ATR_STOP
shadow *strategy* (a second bot/exit mechanism) — that stays gone — but the old
stop-vs-partial dual-candidate *shape* came back in a new form, now competing on ENTRY
signal only:
- **`"searched"`** — all entry-signal params (`k_len`/`k_smooth`/`d_smooth`/`ob`/`os`/
  `chop_len`/`chop_thr`/`gc_period`/`gc_poles`) are backtester-optimized, same as
  always, but drawn from a **3x-widened** space, `bt.PARAM_SPACE_SEARCHED` (added
  2026-08-28, same day as the initial "pine" work but a later, separate ask: "widen the
  param values search range x3 for searched" — see "Bugs already fixed here" #13).
- **`"pine"`** — searches the same 9 entry-signal params, via its own independent
  random sweep, over the **original, narrower** space, `bt.PARAM_SPACE` (unchanged).
  Earlier the same day "pine" was added, both sources briefly searched the exact same
  range (corrected 2026-08-28 after an even earlier pass wrongly locked these to fixed
  values via a since-deleted `PINE_ENTRY_PARAMS` dict — see "Bugs already fixed here"
  #12) — that range-equality lasted only until the later same-day widening in #13. What
  remains identical in the entry SIGNAL FORMULA itself, as opposed to the range each
  source's params are drawn from, is the Gaussian Channel's pole-width constant:
  `"pine"` uses the Pine script's own hardcoded `1.414` literal (`bt.PINE_GC_SQRT2`,
  passed as `gaussian_channel_midline`'s `sqrt2` param) instead of `math.sqrt(2)` — for
  bit-for-bit fidelity to the user's own "Stochastic Triple Filter [ATP]" Pine Script
  indicator's GC formula, not just an equivalent formula.

**Only the entry signal differs — the exit mechanism (the ATR grid: `stop_mult`/
`grid_levels`/`grid_dist_1..8`/`grid_frac_1..8`) is always searched/optimized for BOTH
sources equally**, per the "exit same as the bot" ask. Every symbol is backtested at 3
intervals **x 2 entry sources**, each its own independently IS-ranked/OOS-retested/saved
candidate, writing two independent result files per (symbol, interval) —
`unified_combo_results_{symbol}_{interval}m_searched.json` and `..._pine.json` (plus
their own `_oosXXh.json` variants) — not one. `compute_partial_signals` (in
`unified_combo_trader.py`) and `_bt_combo_pair` (in `unified_combo_bt.py`) both take an
`entry_source` param (`"searched"`/`"pine"`) that branches exactly this way. The
Backtest tab has an "Entry" column back and two rows per `(symbol, interval)` pair
(`self._rows` keyed `(symbol, interval, entry_source)`). **Leg selection
(`_load_all_worthy_crypto()`) needed zero code changes for this** — it already scans
every result file by glob pattern via `_iter_result_files()`, so "pine" and "searched"
candidates just become more entries in the same selection pool automatically; whichever
source actually wins for a symbol is what paper/live trades, via
`AtrPartialPaperBot.entry_source` (persisted, see below).

**A result file backing an open position is frozen — the backtest sweep will never
overwrite it while that position is open.** `BacktestRunner._run()` calls
`_protected_entry_source(symbol, interval)` (`unified_combo_trader.py:1270`, renamed
back from the Grid fork's briefly-existing plain-bool `_position_is_open` once the
"pine" entry source made this a 2-way choice again) before every
`bt.optimize_symbol_interval(..., protected_source=...)` call. It returns
`None`/`"searched"`/`"pine"`/the `"ALL"` sentinel (ambiguous provenance — protect both
sources) by reading `paper_position.entry_source` for that exact (symbol, interval).
`optimize_symbol_interval` still runs the IS sweep for a protected source (keeps
`param_runs`' DB cache warm) but skips the OOS retest and file write for it — status
shows `"protected (position open)"` for that source only; the *other* source for the
same (symbol, interval) still updates normally.

**After every backtest cycle, a periodic missed-trade report runs** — replays the last 2
days for each currently-qualifying (or currently-open-position) symbol using its own
already-selected params (no new parameter search) and logs a warning for any signal it
finds with no matching real trade in paper's history. Log-only, no UI panel. See "Periodic
missed-trade report" below.

## Strategy — exact entry/exit rules

**Entry** (`compute_partial_signals` in `unified_combo_trader.py:1453`, `_bt_combo_pair` in
`unified_combo_bt.py:423` — both call the *same* shared indicator functions, see the
"indicator math" note below, not hand-duplicated copies; both take an `entry_source`
param, `"searched"` or `"pine"` — see "There is exactly ONE strategy..." above for what
that switches). Steps 1-4 below describe the signal shape; every param driving them is
searched for both sources, but independently and, as of 2026-08-28, over different
ranges (see "Parameter reference" below) — `entry_source` changes (a) which space the
random sweep draws each of those params from (`bt.PARAM_SPACE_SEARCHED` for
`"searched"`, `bt.PARAM_SPACE` for `"pine"`) and (b) the Gaussian Channel's pole-width
constant in step 3 (`math.sqrt(2)` for `"searched"`, `bt.PINE_GC_SQRT2=1.414` for
`"pine"`):
1. **Stochastic %K/%D cross** — `%K` computed from a `k_len`-bar high/low range, smoothed
   by `k_smooth` (SMA) into `%K`, then `%D` = `k_smooth`'d `%K` smoothed again by
   `d_smooth`. A **bullish cross** is `%K` crossing above `%D`; **bearish** is the
   reverse (checked via previous-bar values, `k_prev`/`d_prev`).
2. **Overbought/oversold gate** — a bullish cross only counts as a long signal if
   `%K <= os` (oversold zone); a bearish cross only counts as a short signal if
   `%K >= ob` (overbought zone). `ob` must be `> os` (enforced in `_bt_combo_pair`) —
   combos violating this are rejected outright during the parameter search.
3. **Trend filter — Gaussian Channel direction** — a 9-pole-style recursive Gaussian
   filter (`bt.gaussian_channel_midline`/`bt._gc_filt9x`) over `hlc3 = (high+low+close)/3`,
   using `gc_period`/`gc_poles` (searched — `"pine"` draws these from `bt.PARAM_SPACE`,
   50-250 / 1-9; `"searched"` draws them from the 3x-wider `bt.PARAM_SPACE_SEARCHED`,
   150-750 / 3-27, added 2026-08-28, see "Bugs already fixed here" #13 — `"pine"` still
   searches its own full range, it does not fix these to constants). Long signals
   require the midline to be **rising**
   (today's value > prior bar's); short signals require it **falling**.
   `gaussian_channel_midline`'s pole-width constant is `math.sqrt(2)` for `"searched"`
   but the Pine script's own hardcoded `1.414` literal (`bt.PINE_GC_SQRT2`, passed as
   the `sqrt2` param) for `"pine"` — a bit-for-bit port, not just an equivalent formula.
   Verified 2026-08-28: `1.414` vs `math.sqrt(2)` produces a tiny (~4e-6 relative) but
   genuinely nonzero difference in the midline.
4. **Choppiness Index regime filter** — `ci < chop_thr` must hold (a market classified as
   "choppy"/ranging by the Choppiness Index above the threshold blocks entry entirely,
   long or short).
5. **Stop-loss set from ATR at entry**: `sl = entry ∓ stop_mult*ATR` (sign depends on
   side). `atr_p` controls the Wilder ATR lookback. The parameter search enforces
   `sum(grid_dist_1..grid_levels) / stop_mult >= MIN_RR_RATIO (0.8)` — the grid's outermost
   target's CUMULATIVE distance (sum of every level's own increment up to and including
   the last one, added 2026-08-28, replaces the old `grid_levels * grid_atr_mult`
   uniform-spacing formula) vs. the stop distance must clear the same 0.8:1 reward:risk
   floor the old single-TP design used, just measured against the grid's last level
   instead of one fixed TP.
6. **Cooldown**: `COOLDOWN_S = 1800` (30min) after any close before a new entry is allowed
   on the live/paper side (not modeled in the backtester's bar-index loop the same way —
   `bt.py` has no wall-clock cooldown, only "flat" state).
7. **Entry-hours window** (`_entry_allowed()`, live/paper only, no arguments): if
   `entry_hours_utc = [start, end]` is set in config, new entries are blocked outside that
   UTC hour range (wraps midnight if `start > end`). `null` = unrestricted (the default).

**Exit — a GRID of ATR-multiple take-profit levels, computed once at entry, not a single
fixed TP + stochastic-triggered partial.** At entry, `grid_levels` price levels are laid
out via `grid_level_prices(entry, atr, side, levels, grid_dists)` (added 2026-08-28,
explicit user ask: "test number of grids is optimal in bt. and where they should be
set" + independent-distance/independent-fraction follow-up — replaces a single uniform
`grid_atr_mult*(i+1)` spacing). Each level's distance is the CUMULATIVE SUM of that
level's own independently-searched ATR increment (`grid_dist_i`) plus every increment
before it — not evenly spaced any more, since each `grid_dist_i` is its own free
parameter:
```
cum = 0
for i in 0..levels-1:
    cum += grid_dists[i]
    grid_px[i] = entry + side*atr*cum   (side = +1 long, -1 short)
```
This cumulative construction guarantees `grid_px` is always monotonically farther from
entry as `i` increases, regardless of what each individual increment samples to —
REQUIRED for the sequential fill-scanning loop below (and `_next_grid_hit`'s live
mirror) to stay correct, since both assume `lvl_px[filled]` is always farther out than
`lvl_px[filled-1]`. `grid_level_prices` (`unified_combo_bt.py`, placed just before
`_sim_grid_jit`) is called from THREE places: `_bt_combo_pair`'s pure-Python grid loop,
`AtrPartialPaperBot.tick()`'s entry block (`unified_combo_trader.py:~1787`), and the
live-position-seeding block (`unified_combo_trader.py:~1946`) — ONE shared function
instead of three hand-duplicated copies, so live and backtest can never silently
diverge on how grid prices are built. `_sim_grid_jit`'s JIT hot path still hand-inlines
its own copy of the same cumulative-sum logic (numba nopython mode can't call back into
a plain Python function like `grid_level_prices`) — kept in sync by hand with the
shared function, same as the pre-existing JIT-vs-pure-Python twin pattern this file
already used.
- Checked only on the entry-timeframe bar close (`tick()` → `_manage_exit()`) — the
  supplementary 1-minute WS feed (`ComboTrader._ws_loop_1m`/`_on_kline_1m` →
  `check_exit()`) was **removed 2026-08-31, explicit user ask** ("get rid of 1 minute
  candle shit. same for entries as exit"): `check_exit()` is gone from `_PaperBotBase`,
  `_ws_loop_1m`/`_on_kline_1m`/`_force_reconnect_1m`/`_last_kline_ts_1m` are gone from
  `ComboTrader`, and `bt.CRYPTO_INTERVALS` dropped to `["5"]` only (was `["5","15","30"]`)
  for part of that day before `"15"` was added back (see "Every symbol is tested at
  every configured interval" above) — entries and exits now both check on the same
  single entry-timeframe candle, so worst-case exit-check latency is bounded by
  whichever interval a leg is on, never faster than 1 minute again. `_manage_exit()`
  (`unified_combo_trader.py:~1618`) handles SL first, then walks a `while` loop over any
  grid levels the price has crossed — multiple levels can fill in one move (a gap), same
  as the backtest's bar-by-bar simulation.
- Each level closes **its own** independently-searched `grid_frac_i` of the
  **ORIGINAL** entry qty (`orig_qty`, fixed at entry — added 2026-08-28, replaces a
  single `grid_level_frac` shared by every level) — **except the last level, which
  always closes whatever remains** regardless of rounding drift, guaranteeing the
  position always fully exits once every level has filled (or the stop hits first).
  `_manage_exit()`'s check (`p["grid_filled"] >= len(p["grid_px"]) - 1`) routes the
  last level straight to `_close()` instead of `_partial_grid()`. Per-level fractions
  are stored as `p["grid_fracs"]`, a list indexed by fill count (`_partial_grid` reads
  `p["grid_fracs"][p["grid_filled"]]` — the level about to fill — before incrementing
  the counter), replacing the old single `p["grid_level_frac"]` float.
- **The stop trails**: to breakeven (`sl = entry`) after the **first** fill, then to the
  **previous filled level's price** after each subsequent fill
  (`p["sl"] = p["entry"] if filled==1 else p["grid_px"][filled-2]`) — profit already
  banked at a lower level can never be given back once a higher one fills. Identical logic
  in `_sim_grid_jit`/the pure-Python twin in `_bt_combo_pair`. **This stop-trail formula
  is completely unchanged by the cross-down TP-capture mechanism below** — not one line
  of it was touched when that was added.
- **Cross-down TP-capture — added 2026-08-28, a SEPARATE mechanism from the stop-loss
  above, purely for locking in profit, explicit user ask** ("i want it to close on a
  cross down the grid" ... "the stop loss stays the same as original. this is purely
  capturing TP" — see "Bugs already fixed here" for the full clarification history and
  design reasoning). If price crosses STRICTLY below (long) / above (short) an
  already-filled-but-not-yet-unwound level, that level's own `grid_frac_i` of CURRENT
  remaining qty closes (a fresh partial close, not "undoing" the earlier fill —
  `_next_grid_unwind_idx()`/`_partial_unwind()`, `unified_combo_trader.py:~1543/~1866`,
  mirrored in `_sim_grid_jit`/`_bt_combo_pair`'s pure-Python twin via a per-level
  `unwound` bool array/list). Each level unwinds at most once; a NEW fill makes that
  new top level freshly eligible regardless of older levels' unwind history. In
  practice only the single most-recently-filled level ever gets a chance to unwind
  before the (unchanged) stop-loss takes over, since `sl`'s formula always sits
  exactly one level behind. `paper_position.grid_unwound` (JSON bool list, `ALTER
  TABLE` migration in `get_db()`) persists this per-level state.
- Realized per-level P&L and its share of the entry fee accumulate into `partial_pnl`,
  which carries forward into the final trade's all-in P&L when the position eventually
  fully closes (`_close()`) — dropping this double-counts or under-counts fees/P&L.
- `grid_levels` (int, 2-8), and EACH active level's own `grid_dist_i` (float, 0.3-2.5)
  and `grid_frac_i` (float, 0.1-0.4) for `i` in 1..8 — 16 independently-searched params
  total, replacing the old single shared `grid_atr_mult`/`grid_level_frac` — are
  **searched params in `bt.PARAM_SPACE`**, same random-sweep mechanism as
  `stop_mult`/`k_len`/etc — **the backtester optimizes both the grid's shape AND how
  much closes at each level**, not just the entry-signal params. Only the first
  `grid_levels` slots of a given combo are ever read; slots beyond that are sampled but
  unused. A candidate saved before 2026-08-28 (only has the old scalar keys) falls back
  via `params.get(f"grid_dist_{i}", params.get("grid_atr_mult", 1.0))` (same pattern for
  `grid_frac_i`/`grid_level_frac`) — replicating its old scalar across every slot,
  which reconstructs its EXACT old uniform-grid behavior (verified byte-identical).
  `MAX_GRID_LEVELS = 8` (`unified_combo_bt.py:127`) is a hard cap sizing fixed-length
  arrays inside the numba JIT hot path (`_sim_grid_jit`) — must stay
  `>= PARAM_SPACE["grid_levels"][1]`. `tp_mult` and `partial_lvl` (the old single-TP
  distance and stochastic partial-exit-level params) no longer exist anywhere in this
  codebase.

**Position sizing:** `notional = equity * LEVERAGE(=11) * MARGIN_HEADROOM(0.98)`,
`qty = notional / price`, entry fee `= notional * TAKER_FEE(0.00055)` deducted from
equity immediately. Live sizing uses the live account balance (`LIVE_MARGIN_HEADROOM`,
same 0.98) independently of paper's equity — the two tracks are never mixed.

**Capital-slot system** (`TradingEngine.claim_slot`/`release_slot`,
`_PaperBotBase._try_claim_capital`). `CAPITAL_TIERS` (currently `[0.97]`) defines how many
symbols can hold capital concurrently and at what fraction each — today, only one symbol
at a time, at 97% of funds. A symbol claims a slot the moment its bot signals a fresh
entry (idempotent per symbol). **`AtrPartialPaperBot` gets the FULL claimed slot fraction**
— there's no other bot per symbol to split it with (Grid fork; the source repo's `/2`
split between PARTIAL and STOP doesn't exist here at all). `self.equity` (paper) and
`LiveExecutor.equity_fraction` (live) are only re-baselined on a **fresh** claim
(`bot._slot_frac != frac`), never on a re-entry into a slot the symbol already holds, so
real compounding equity history is never wiped by re-entering. A slot frees when its
symbol's bot is flat (`ComboTrader._maybe_release_slot()`, called after every
tick/reconcile and from the manual Close Position button) and is reclaimed by
whichever symbol signals next, at that same slot's rate. Live sizing
(`LiveExecutor._enter_locked`) mirrors this: `bal * equity_fraction * LIVE_MARGIN_HEADROOM`,
`equity_fraction` set to the claimed slot fraction dynamically — `LiveExecutor` is
constructed with `equity_fraction=0.0` and only gets a real value the moment its symbol
actually claims a slot.

**Parameter reference** (search ranges apply to the random sweep in
`unified_combo_bt.py`; `PARAM_SPACE` at `:155`, `PARAM_SPACE_SEARCHED` at `:186` — a
copy of `PARAM_SPACE` with only the 9 entry-signal rows below widened, added
2026-08-28, "Bugs already fixed here" #13). **The 9 entry-signal params (`k_len`
through `gc_poles`) are searched over a different range per source; the exit/grid
params (`atr_p`/`stop_mult`/`grid_levels`/`grid_dist_1..8`/`grid_frac_1..8`, 21 params
total) are searched over the identical range for both**, preserving the "exit same as
the bot" invariant:

| Param | Range — `"pine"` (`PARAM_SPACE`) | Range — `"searched"` (`PARAM_SPACE_SEARCHED`) | Default | Meaning |
|---|---|---|---|---|
| `k_len` | 10-40 (int) | 30-120 (int) | 21 | Stochastic %K lookback (bars) |
| `k_smooth` | 1-5 (int) | 3-15 (int) | 3 | SMA smoothing applied to raw %K |
| `d_smooth` | 3-10 (int) | 9-30 (int) | 5 | SMA smoothing applied to %K to get %D |
| `ob` | 70-90 | 50-100 [1] | 80 | Overbought threshold (short signal gate) |
| `os` | 10-30 | 30-90 | 20 | Oversold threshold (long signal gate) |
| `chop_len` | 8-20 (int) | 24-60 (int) | 14 | Choppiness Index lookback (bars) |
| `chop_thr` | 38-62 | 14-86 [1] | 50.0 | Max Choppiness Index value to allow entry |
| `gc_period` | 50-250 (int) | 150-750 (int) | 144 | Gaussian Channel period |
| `gc_poles` | 1-9 (int) | 3-27 (int) | 4 | Gaussian Channel pole count |
| `atr_p` | 8-20 (int) | *same as pine* | 14 | Wilder ATR lookback (bars) — exit param, shared |
| `stop_mult` | 1.5-6.0 | *same as pine* | 3.5 | Initial (pre-fill) stop distance, in ATR multiples — exit param, shared |
| `grid_levels` | 2-8 (int, `MAX_GRID_LEVELS`) | *same as pine* | 4 | Number of ATR-multiple TP levels — exit param, shared |
| `grid_dist_1` .. `grid_dist_8` | 0.3-2.5 each | *same as pine* | 1.0 each | Level `i`'s OWN ATR-multiple INCREMENT (added 2026-08-28, replaces one shared `grid_atr_mult`) — level `i`'s actual distance from entry is `cumsum(grid_dist_1..i)`, not `grid_dist_i` alone; see `grid_level_prices` — exit param, shared, independently searched per level, only the first `grid_levels` slots are used |
| `grid_frac_1` .. `grid_frac_8` | 0.1-0.4 each | *same as pine* | 0.25 each | Level `i`'s OWN fraction of ORIGINAL qty it closes (added 2026-08-28, replaces one shared `grid_level_frac`; last FILLED level always closes all remaining regardless) — exit param, shared, independently searched per level, only the first `grid_levels` slots are used |

[1] `ob` and `chop_thr` are 0-100-bounded oscillator/index levels where a naive
both-bounds-x3 widening collapses to a degenerate `(100,100)` after clipping to that
ceiling (e.g. `ob` 70-90 x3 both bounds -> 210-270 -> clipped to 100-100). Both instead
use a **centered triple-width**: keep the original range's center, widen to 3x its
original width, then clip to `0-100` (e.g. `ob` 70-90, width 20, center 80 -> 3x width
60 -> 50-110 -> clipped to 50-100). Every other widened param uses plain
both-bounds-x3.

A candidate saved before the `grid_dist_i`/`grid_frac_i` per-level design (2026-08-28)
only has the old `grid_atr_mult`/`grid_level_frac` scalar keys — every read site
(`_bt_combo_pair`, `db_save`, `_param_key`, `db_load_tried_set`/`db_load_top`'s SQL
`COALESCE`, and both of `unified_combo_trader.py`'s live grid-construction sites) falls
back to that old scalar, replicated across every slot, which reconstructs the
candidate's EXACT old uniform-grid behavior — verified byte-identical in
`test_per_level_grid.py`.

Random search draws uniformly within these ranges (`_sample(space=PARAM_SPACE)` —
takes an optional `space` arg now, defaulting to `PARAM_SPACE`;
`optimize_symbol_interval` passes `PARAM_SPACE_SEARCHED` when building `"searched"`'s
combo list, `PARAM_SPACE` when building `"pine"`'s); int params via `random.randint`,
float params via `random.uniform` rounded to 2dp. `db_load_top` seeds each new sweep
with the best previously-found candidates (ranking-based: top `N_TOP_RETEST` by
all-time IS score); `db_load_tried_set` prevents re-testing exact duplicate combos
already in `unified_combo_params.db`. `db_load_winners(symbol, interval, src)`
(added 2026-09-01) additionally seeds EVERY combo that has ever actually cleared
`_clears_target` and been saved as that (symbol, interval, src)'s live result file —
an absolute guarantee, not a ranking-based one, closing the gap where a genuine past
OOS winner could be outscored on raw IS score and silently drop out of `db_load_top`'s
rotation. Each source's combo list is `top_params + db_load_winners(...) +
new_random_combos`, in that order — historical winners are always included ahead of
random sampling, every cycle.

**The exit-side rows (`atr_p`/`stop_mult`/`grid_levels`/`grid_dist_1..8`/`grid_frac_1..8`)
are searched identically for both `entry_source="searched"` and `entry_source="pine"`,
over the same `PARAM_SPACE`
range** — `optimize_symbol_interval` still tests these under one shared range for both
sources, preserving the "exit same as the bot" invariant. **The entry-signal rows
(`k_len` through `gc_poles`) are searched independently per source** (range-split added
2026-08-28, explicit user ask "widen the param values search range x3 for searched" —
see "Bugs already fixed here" #13): `"searched"` draws from the 3x-wider
`PARAM_SPACE_SEARCHED`, `"pine"` still draws from the original `PARAM_SPACE`.
`optimize_symbol_interval` now builds two independent combo lists
(`combos_by_src["pine"]`/`combos_by_src["searched"]`) instead of one shared list — a
combo sampled for one source is tested ONLY under that source, never both. `"pine"`
still does **not** fix or ignore any of `params` (an earlier pass wrongly did this via a
since-deleted `bt.PINE_ENTRY_PARAMS` dict — corrected same day, see "Bugs already fixed
here" #12) — it searches its own full range, just a narrower one than `"searched"`'s.
Besides the range each source draws from, the **only** other thing `entry_source`
changes anywhere is which constant gets passed as `gaussian_channel_midline`'s `sqrt2`
param: `math.sqrt(2)` for `"searched"`, `bt.PINE_GC_SQRT2=1.414` (the Pine script's own
hardcoded literal) for `"pine"`.

## Architecture map

### `unified_combo_trader.py` — classes

- **`TradingEngine`** (`:3450`) — owns however many concurrent legs currently qualify
  (`self.legs: list[TradingLeg]`) and the capital-slot bookkeeping
  (`self._active_slots: dict[symbol, fraction]`, `claim_slot`/`release_slot`). `_run()`
  (`:3489`): keys check → wait-for-any-result gate (first run only, unfiltered
  `_load_combo()`) → `_load_all_worthy_crypto()` → connect paper session, fetch shared
  balance once, refuse to start with zero balance → a rescue scan
  (`:3546-3576`) force-adds any symbol with an already-open position (paper DB or, if a
  live key exists, checked directly against the exchange) that isn't otherwise worthy, so
  an existing position is never orphaned by a restart → for each leg: optionally connect a
  live session + `LiveExecutor(..., equity_fraction=0.0)` (placeholder, real value assigned
  only when the symbol claims a slot) + `.setup()` + wait up to 50s for a positive live
  balance, build `AtrPartialPaperBot(params, 0.0, db, lev=lev, live=live_exec,
  bot_id=f"partial_{sym}", entry_source=entry_source)` (equity=0.0 placeholder —
  `_load_state` overwrites it with the real restored equity if a position already
  exists, and overrides `entry_source` too if a saved one exists — see
  `_load_state`/`AtrPartialPaperBot.entry_source` above), `entries_paused=True` if this is a
  rescued (stale-but-open) leg, `reconcile_on_start` if live, **pre-claim a slot** if the
  bot was restored with an open position, build+start `ComboTrader`, append a
  `TradingLeg`, spawn that leg's `_reconcile_loop`/`_param_reload_loop(combo, stop_ev,
  locked_symbol)` → once all legs exist, spawn one shared `_live_poll` (if any leg is
  live) and one shared `_bal_loop` → mark `ready=True`. Created fresh by
  `MainWindow._on_start_paper()` every time — never reused across a stop/start cycle.
- **`TradingLeg`** (`:3443`) — a small `@dataclass` (`combo`, `live_exec`) bundling one
  leg's running state; every leg is identified purely by its symbol
  (`leg.combo.symbol`). `TradingEngine.legs` holds zero, one, or many of these.
- **`ComboTrader`** (`:1859`) — owns the shared entry-timeframe kline/WebSocket feed
  (`_ws_loop`, reconnects on `WS_STALE_S=120` staleness) and drives the one paper bot's bar
  processing off that feed. The second, stateless 1-minute supplementary feed
  (`_ws_loop_1m`/`_on_kline_1m` → `check_exit(price)`) was **removed 2026-08-31**
  (explicit user ask, "get rid of 1 minute candle shit. same for entries as exit") —
  exits now only check on the same entry-timeframe bar close as entries, via `tick()`'s
  own `_manage_exit()` call. `__init__` still
  takes a single `partial_bot` argument (no `stop_bot`). `entry_source` (`:2087`) is a
  **read-only `@property`** — `return self.partial.entry_source` — not a separately
  tracked field, so it can never drift from the bot's own persisted value.
- **`_PaperBotBase`** (`:1543`) / **`AtrPartialPaperBot`** (`:1635`) — the one remaining
  paper strategy, split base/subclass in case a second variant is ever reintroduced.
  `AtrPartialPaperBot.entry_source` (`"searched"`/`"pine"`, default `"searched"`) is set
  in `__init__` but then **restored from `paper_position.entry_source` on `_load_state()`
  if a saved value exists** (`:~1735-1741`), overriding whatever the constructor was
  called with — this is deliberate: an already-open position must never be
  retroactively relabeled onto a different entry source just because the caller (e.g. a
  param reload picking a new winner) constructed the bot with a different one. See
  "Complete method reference" below for the full method list.
- **`LiveExecutor`** (`:324`) — everything live-order-related: order placement (`_order`),
  entry/partial/close flows, and all the reconciliation safety nets. One instance per leg,
  constructed with `equity_fraction=0.0` and only assigned a real value the moment its
  symbol actually claims a capital slot. `self.live_pos` stays a `dict` keyed by `bot_id`
  (kept for the `_unattributed`/reconcile machinery) even though there's only ever one
  live-capable bot per symbol in the Grid fork.
- **`BacktestRunner`** (`:2543`) / **`BacktestTab`** (`:2744`) — Backtest tab's engine and
  UI. Auto-started by `MainWindow.__init__` via `BacktestTab.auto_start()`. Sweeps every
  `(symbol, interval)` pair in `bt.SYMBOLS × bt.CRYPTO_INTERVALS` each cycle
  (`bt.optimize_symbol_interval(..., protected_source=_protected_entry_source(sym, iv))`
  — which internally runs both entry sources), then calls `_report_missed_trades(sess,
  _load_worthy_plus_open_positions())` once per cycle before waiting for the next one.
- **`MainWindow`** (`:3729`) — top-level window: tab wiring, backtest auto-start,
  Start/Stop Paper (`_on_start_paper`/`_on_stop_paper`), API key save/delete callbacks,
  exit/close handling (`closeEvent` → `os._exit(0)`).
- **`HomeTab`** (`:3183`) / **`PaperTab`** (`:2892`) / **`LiveTab`** (`:3020`) — the other
  3 tabs' UI. All three build widget blocks **dynamically, one per currently running leg**,
  laid out **kanban-style** (`KANBAN_COLS = 2` — a wrapping `QGridLayout`, not a single
  vertical stack), rebuilt whenever the leg identity set changes.
- **`BotPanel`** / **`_StatusBar`** / **`_SG`** / **`_TradesTable`** — small reusable
  widgets shared across tabs. `PaperTab` now builds exactly one `BotPanel("ATR GRID")` per
  leg (not a two-`BotPanel` splitter — there's only one strategy).

### `unified_combo_trader.py` — module-level functions (not in any class)

- **Key storage**: `has_keys`/`save_keys`/`delete_keys`/`load_keys_secure` branch on
  `sys.platform`. Windows/source-repo behavior: `_dpapi_protect`/`_dpapi_unprotect` (raw
  ctypes DPAPI calls) encrypting to `keys/demo.dat`/`keys/live.dat`, plus `_keys_path`.
  **This macOS port's actual behavior**: the `darwin` branch shells out to the `security`
  CLI (service `unified-combo-grid`, accounts `demo`/`live`) — no key files are ever
  written, `_keys_path`/`KEYS_DIR` are never touched. Both branches share the exact same
  call signatures, so every other caller in this file is platform-agnostic.
- **Sessions**: `make_session` (paper, returns `(session, error)`), `make_live_session`
  (live, returns `session` or `None`), `_bt_make_session` (Backtest tab's session), `_api`
  (bounded-retry wrapper; `_retry_exc=False` for non-idempotent order calls),
  `fetch_balance`.
- **Combo loading**: `_load_combo()` (:1128, no-argument — picks the single best-by-sharpe
  result across every symbol/interval/entry_source; only used for the first-run wait
  gate), `_load_all_worthy_crypto()` (:1163, every symbol whose best-scoring
  (interval, entry_source) clears `bt._clears_target` — as of 2026-09-01 this is the
  ONLY selection rule; win_rate gates nothing — see "Win-rate abandoned as a selection
  criterion" above), `_load_result_for_symbol(symbol, require_fresh=False,
  require_target=False)` (:1220, one symbol's own best-scoring current result, checking
  both `_searched`/`_pine` suffixed files per interval; `require_target` replaced the
  since-removed `min_win_rate` param 2026-09-01 — see "Win-rate abandoned as a
  selection criterion" above), `_protected_entry_source(symbol, interval)` (:1270, the freeze
  check — returns `None`/`"searched"`/`"pine"`/`"ALL"`, see "What this is"),
  `_load_worthy_plus_open_positions()` (:1305, `_load_all_worthy_crypto()` extended with
  any symbol that still has an open paper position but no longer qualifies — feeds the
  missed-trade report so it never silently stops checking a symbol the moment it drops out
  of "worthy"), `_iter_result_files()`/`_parse_result_file()` (:1040/:1053, shared
  file-discovery + gate logic — `_parse_result_file` returns an 8-tuple `(symbol, interval,
  params, gc_period, gc_poles, leverage, sharpe, entry_source)`; a `"pine"`-tagged
  result reports its own searched `gc_period`/`gc_poles` straight from that result
  file's `params` dict, exactly like `"searched"` does — there is no fixed-default
  special-casing here, that was removed along with `PINE_ENTRY_PARAMS`), `get_db` (:1330, opens
  `unified_combo_paper.db`, migrates `paper_position.entry_source` in via `ALTER TABLE`
  if missing — see Data layer).
- **Indicators**: **no local copies anymore** — `compute_partial_signals(hi, lo, cl,
  params, entry_source="searched")` (:1453) calls `bt._stoch_raw_k`/`bt._sma`/
  `bt._chop_index`/`bt._atr_wilder`/`bt.gaussian_channel_midline` directly, branching on
  `entry_source` exactly like `_bt_combo_pair` does. See the dedicated note below.
  `seed_bars` fetches the initial `SEED_BARS=600` bars a `ComboTrader` starts from.
- **Background loops**: `_reconcile_loop` (periodic `combo.reconcile()` every
  `RECONCILE_S=180`), `_param_reload_loop(combo, stop_ev, locked_symbol)` (:3830, reloads
  that leg's own symbol's params every `PARAM_RELOAD_S=40min` (was 3.5h, tightened
  2026-09-01 — see "Timing tightened" below; `RESULT_MAX_AGE_S` auto-derives from this
  same constant), only when the leg's bot is flat and no live position is open — waits
  indefinitely in that flat-check loop if a position is open, never force-reloads mid
  -position).
- **Missed-trade report**: `_report_missed_trades(sess, crypto_results)` (:1334) — see its
  own section below.
- **UI helpers**: `_apply_dark_theme`, `_titem`, `_pc`/`_dc`/`_wc` (color-by-value
  helpers), `_bot_stats`, `_entry_allowed`, `_set_kanban_card_accent`.
- **Entry point**: `main()`, `_wait_for_results` (polls for the first backtest result up
  to 1h if none exist yet).

### `unified_combo_bt.py` — module-level functions (pure library, no classes)

- **Indicators**: `_sma`, `_atr_wilder`, `_stoch_raw_k`, `_chop_index`, `_gc_filt9x`,
  `gaussian_channel_midline` — the one and only copy; `unified_combo_trader.py` imports
  these directly (`bt.*`), it does not maintain its own.
- **Grid simulation core**: `_sim_grid_jit` (`:299`, `@njit(cache=True)`) — the JIT-compiled
  hot-path grid loop, replacing the source repo's `_sim_partial_jit`/`_sim_stop_jit`. Its
  pure-Python numeric twin lives inline in `_bt_combo_pair` (`:526-`) — used when
  `record_entries=True` or numba isn't available. **Both loops must be kept in sync by
  hand** — see the code comments at `unified_combo_bt.py:281` and `:527`.
- **Backtest core**: `_bt_combo_pair(params, hi, lo, cl, backtest_bars, bpy, lev=LEVERAGE,
  record_entries=False, initial_equity=None, entry_source="searched")` (`:423`) —
  single-strategy grid-exit simulation for one parameter set over a price array;
  `entry_source="pine"` only swaps the GC pole-width constant for `PINE_GC_SQRT2`
  (`1.414`) in place of `math.sqrt(2)` — every param, entry-signal and exit alike
  (`k_len`/`k_smooth`/`d_smooth`/`ob`/`os`/`chop_len`/`chop_thr`/`gc_period`/`gc_poles`/
  `stop_mult`/`grid_levels`/`grid_dist_1..8`/`grid_frac_1..8`/`atr_p`), always comes from
  `params` and is always searched regardless of `entry_source` — **which space each
  param in `params` was originally sampled from (`PARAM_SPACE` for `"pine"`,
  `PARAM_SPACE_SEARCHED` for `"searched"`, since 2026-08-28) is decided earlier, by
  `_sample`/`optimize_symbol_interval` below, not by this function** — by the time
  `_bt_combo_pair` sees `params` it's just a plain dict, source-agnostic except for the
  `entry_source` string itself. Returns performance stats (14 fields: `score`, `sharpe`,
  `cagr_pct`, `total_ret_pct`, `max_dd_pct`, `trades`, `avg_hold`, `win_rate`,
  `profit_factor`, `final_equity`, `cum_profit`, `cum_loss`, `max_tp`, `max_loss`) or
  `None` if it fails `MIN_TRADES`/`MIN_AVG_HOLD`/`MAX_DD_PCT`/`MIN_RR_RATIO`;
  `record_entries=True` additionally captures every entry as `(bar_idx, side)` tuples
  for the missed-trade report. **`WIN_FEE_MULT = 2.0`** (added 2026-08-31, explicit user
  ask, "tiny trades should never be counted as wins"): a closed trade only counts toward
  `win_rate`/`gw`/`mw` if its `part_pnl` (already net of fees) exceeds `WIN_FEE_MULT`
  times that trade's own separately-tracked total round-trip `fees` — `part_pnl > 0`
  used to be the only bar. A trade that merely edged out what it paid in fees no longer
  counts as a win; it still counts as a trade (`t`/`th` unaffected) and its `part_pnl`
  flows into `gl`/`ml` like any other non-win. `fees` accumulates the entry fee plus
  every exit/unwind leg's fee across the trade's life and is a running total separate
  from `part_pnl` (not a second subtraction against it). Applied identically in both
  `_sim_grid_jit` (the JIT hot path) and `_bt_combo_pair`'s pure-Python twin, at every
  win-check site (SL close, grid-level-fill full-close, cross-down-unwind full-close) —
  three sites per long/short branch, plus one shared forced end-of-data close site, seven
  total in each function.

  **`MIN_WIN_PRICE_PCT = 0.0033`** (added 2026-08-31, same day, explicit user ask, "only
  params where each trade is no less than .33% before leverage profit makes it through"):
  a win ALSO needs the whole trade's qty-weighted average exit price to have moved at
  least 0.33% from entry — raw underlying price move, BEFORE leverage (not the leveraged
  equity return, which at `LEVERAGE=11` is roughly 11x larger for the same price move).
  Checked ALONGSIDE `WIN_FEE_MULT` (a win needs both to clear); only affects what counts
  as a win, never touches a losing trade's own `gl`/`ml`/`eq`. New `exit_notional`
  accumulator (reset to 0.0 at entry, alongside `part_pnl`/`fees`) sums
  `close_price * qty_closed` across every exit/unwind leg of the trade — SL close, each
  grid-level fill, each cross-down unwind, forced end-of-data close — so
  `exit_notional / qty0` is the qty-weighted average exit price at the moment the trade
  fully closes, however many grid levels fired; `raw_pct = (avg_exit-entry)/entry` for a
  long, sign-flipped for a short. Applied at the same 7 win-check sites as `WIN_FEE_MULT`,
  in both functions.

  **A real bug was found and fixed while adding this**: `_sim_grid_jit`'s non-win branch
  was `gl += -part_pnl` (only correct when `part_pnl` is negative, i.e. a true loss) —
  but once `WIN_FEE_MULT`/`MIN_WIN_PRICE_PCT` can route a small POSITIVE-`part_pnl` trade
  into that same branch, `-part_pnl` goes negative and corrupts `gl` (a hand-crafted
  $3.02-profit trade that failed the new floor produced `gl=-3.0154` instead of
  `+3.0154`). `_bt_combo_pair`'s pure-Python twin already used `abs(part_pnl)` correctly
  — meaning the two branches had silently disagreed on `gl`/`ml`/`profit_factor` ever
  since the `WIN_FEE_MULT` commit, for any marginal-but-positive trade. Fixed all 7 sites
  in `_sim_grid_jit` to `abs(part_pnl)`, matching the pure-Python twin. Verified: the
  0.25%-move/0.40%-move hand-crafted cases behave as expected (`w=0`/`w=1` respectively,
  `gl` correctly positive); a 60-trial cross-check of real `_bt_combo_pair` (both entry
  sources, both `PARAM_SPACE`/`PARAM_SPACE_SEARCHED`) over a 3000-bar random walk found
  the JIT and pure-Python branches agree exactly on all 14 result fields in 60/60 trials.

  **`zf`/`zero_fill_rate` — added 2026-08-31, same day, explicit user ask** ("how do we
  make this profitable instead", following a real-data check that found ~20% of entries
  on both current param sets reverse straight to the stop without ever filling a single
  grid level): `score` previously only looked at the aggregate equity curve
  (`sharpe * sqrt(trades/MIN_TRADES)`), blind to whether a param set's trades banked
  something at a grid level first or lost cleanly. New `zf` counter increments whenever
  a trade closes (any reason — SL, forced end-of-data) with `filled` still 0; checked
  at the same 7 close sites `WIN_FEE_MULT`/`MIN_WIN_PRICE_PCT` use. `zero_fill_rate =
  zf/trades` is a new 15th result field, and `score` becomes
  `sharpe * sqrt(trades/MIN_TRADES) * max(0.0, 1 - zero_fill_rate)` — a param set where
  every trade reverses clean to the stop scores 0 regardless of sharpe, pushing the
  search toward params that either place `grid_dist_1` close enough to bank something
  before reversing, or have cleaner entries that reverse less often.
  `_sim_grid_jit`'s return tuple grew a 9th element (`zf`, inserted before `curve`) —
  its one callsite in `_bt_combo_pair` updated to match. The real-data check itself: a
  live replay of both saved ETHUSDT 5m param sets against 5,000 real fetched bars
  (2026-08-14 to 2026-08-31) found searched clean-losing on 5/24 entries (20.8%, median
  37 bars/~3h — a slow grind) and pine on 13/65 (20.0%, median 6 bars/30min, 4/13
  within 15 minutes — genuinely fast reversals). Verified with hand-crafted
  `_sim_grid_jit` cases: reverses-before-any-fill produces `zf=1`; an otherwise
  identical trade that fills level 1 first, then reverses to the resulting breakeven
  stop, produces `zf=0`. A 60-trial JIT-vs-pure-Python cross-check found exact
  agreement on all 15 fields including `zero_fill_rate`, and confirmed `score`'s value
  matches its formula exactly against the returned `sharpe`/`trades`/`zero_fill_rate`
  in every trial.
  `_combo_worker` (`:678`, per-process batch wrapper for
  `ProcessPoolExecutor`) **takes a fixed `src` as part of its args tuple now** and tests
  every combo in its batch under exactly that one source — it used to loop `for src in
  ("searched", "pine")` internally and test each combo under both per call; that loop
  moved up into `optimize_symbol_interval` on 2026-08-28 (see "Bugs already fixed here"
  #13), since a combo sampled from one source's range is only meaningful for that
  source. `_sample(space=PARAM_SPACE)` (`:848`) — random parameter draw, **now takes an
  optional `space` arg** instead of always reading the module-level `PARAM_SPACE`
  directly; nothing inside `_sample` itself branches on `entry_source`, it just draws
  from whichever `space` dict it's given.
- **Optimization loop**: `optimize_symbol_interval(sess, symbol, interval, status_dict,
  executor=None, protected_source=None)` (`:857`) — the whole per-(symbol, interval)
  pipeline: fetch OHLCV → build two independent combo lists, `combos_by_src["pine"]`
  sampled from `PARAM_SPACE` and `combos_by_src["searched"]` sampled from
  `PARAM_SPACE_SEARCHED` (`:940-943`, added 2026-08-28 — see "Bugs already fixed here"
  #13), sharing only the "tried" params dedup set and `db_load_top`'s elite seed list
  between them (previously ONE shared combo list was generated and every combo tested
  under BOTH sources at identical param values) → in-sample random sweep, one
  `ProcessPoolExecutor` batch submitted per source per combo-chunk (`N_RANDOM=200000`
  combos per source) → save to DB → **for whichever source(s) `protected_source` names
  (`"searched"`, `"pine"`, or both via the `"ALL"` sentinel), stop here** (`:988-992`,
  status `"protected (position open)"` for that source) → walk-forward OOS validation
  across `OOS_HOURS_LIST=[48,60]` → write that source's own result JSON(s). **Writes
  straight to the real `DATA_DIR`, no scratch mode.** `executor=` lets a caller pass an
  already-running `ProcessPoolExecutor` to reuse across multiple calls (`BacktestRunner`
  passes one shared pool for the entire run).
- **Missed-trade replay**: `replay_recent_trades(sess, symbol, interval, params, lev,
  days=2, entry_source="searched")` (`:1029`) — must be called with whichever
  `entry_source` actually won that symbol's current result, so the replay matches what
  paper/live is really trading. Fetches fresh OHLCV, slices to the last `days` days, calls
  `_bt_combo_pair(..., record_entries=True, entry_source=entry_source)` with the symbol's
  own already-selected params, returns a list of `{"side", "entry_ts", "strategy"}` dicts.
- **DB layer** (`unified_combo_params.db`): `db_init`, `db_save`, `db_load_tried_set`,
  `db_load_top`, `_param_key` — `param_runs` table now has `grid_levels`/
  `grid_dist_1..8`/`grid_frac_1..8` columns instead of `tp_mult`/`partial_lvl`. The old
  `grid_atr_mult`/`grid_level_frac` columns (added earlier in the Grid fork, superseded
  2026-08-28) are kept in the schema forever, never dropped, so pre-existing cached
  rows stay readable via `_GRID_COALESCE`'s `COALESCE(grid_dist_i, grid_atr_mult, 1.0)`
  fallback everywhere this module reads `param_runs` — new rows leave the two old
  columns `NULL`. `db_init()` ALTER-TABLEs in the 16 new columns if missing (checks
  `PRAGMA table_info` first), safe against a DB that already has this table.
- **Data fetch**: `fetch_ohlcv` (paginated kline fetch, public-session fallback), `_api`
  (separate bounded-retry wrapper, not shared with trader.py's).
  `_bars_per_day`/`_is_bars`/`_oos_bars`/`_bars_per_year`/`_max_pages`.
- **`_win_kill_on_close`** — Windows Job Object setup, called from `main()`.

### Indicator math — no longer duplicated (changed from the source repo)

The source repo (`unified_combo_gui`) hand-duplicated the same indicator math in both
files under different function names, as a deliberate speed tradeoff. **This repo does
not** — `unified_combo_trader.py`'s `compute_partial_signals` calls `bt._stoch_raw_k`,
`bt._sma`, `bt._chop_index`, `bt._atr_wilder`, `bt.gaussian_channel_midline` directly (no
local `_sma`/`_atr`/`_stoch_k`/`_chop`/`_gc_filt`/`gc_midline` copies exist in
`unified_combo_trader.py` — confirmed by grep). This was already fixed in the source repo
before the fork (source repo's "Bugs already fixed here" #12) and carried forward as-is.
**Don't reintroduce a second copy of these functions** — if you ever need trader.py's
signal computation to diverge from bt.py's backtest computation, that's a real design
decision, not something to solve by copy-pasting.

The **grid simulation loop itself is still duplicated by necessity** (numba's nopython
mode can't share code with plain Python cleanly) — `_sim_grid_jit` and its pure-Python
twin inline in `_bt_combo_pair` — see the note in the Architecture map above.

## Periodic missed-trade report

Runs once per backtest cycle, right after that cycle's sweep completes (inside
`BacktestRunner._run()`, before the repeat-wait), via `_report_missed_trades(sess,
_load_worthy_plus_open_positions())`.

- For each currently-qualifying-or-open-position symbol: `bt.replay_recent_trades(sess,
  sym, iv, params, lev, days=2)` replays the last 2 days using that symbol's own
  already-selected winning params — **no new parameter search**.
- Compares against `paper_trades` (a fresh short-lived `get_db()` connection, closed after
  the check). `paper_trades` only stores each trade's **close** timestamp + `bars_held` (no
  separate entry-time column), so a real trade's entry time is *reconstructed*:
  `close_ts - bars_held * interval_min`.
- A simulated entry counts as matched if any real trade of the same side has a
  reconstructed entry within **1.5 bar-durations** of it. Anything unmatched logs one
  `_log.warning("MISSED TRADE?: ...")` line.
- **This is a heuristic early-warning, not a precise audit.** Log-only — no new UI panel,
  no popup. Exits are never compared, only entries.

## Complete method reference — the three most load-bearing classes

### `LiveExecutor` (`unified_combo_trader.py:324`)

- `__init__(session, symbol, equity_fraction=0.5, db=None)` — sets defaults (`lot_step`,
  `min_qty`, etc.) overwritten by real values in `setup()`. `_load_live_trades()`/
  `_load_live_positions()` restore state across restarts. Starts one persistent worker
  thread (`_worker_loop`) draining a `queue.Queue` (`self._work_q`) — `enter()`/
  `partial_exit()`/`mark_closed()` all just `self._work_q.put((fn, args))` and return
  immediately; the worker executes them strictly serially (entry/partial/close are
  mutually-exclusive states for one shared position anyway — this makes that already-
  intended invariant explicit rather than relying on internal lock contention).
- `log(msg, level)` — pushes to `self.log_msgs` (`deque(maxlen=20)`) + module logger.
- `setup()` — fetches `get_instruments_info` for lot/qty/notional limits; calls
  `set_leverage` best-effort (retCode `110043` "leverage not modified" silenced). No
  `switch_margin_mode` call (removed upstream before the fork).
- `reconcile_on_start(partial_bot)` — single-argument now (was `(partial_bot, stop_bot)`
  in the source repo). See Live-trading safety mechanisms below.
- `fetch_balance()` — USDT `equity` (falls back to `walletBalance`).
- `_round_qty`/`_qty_str`/`_order` — unchanged mechanics from the source repo.
- `enter(bot_id, side, price)` → queues `_do_enter` → `_enter_locked` — sizes from
  `bal * equity_fraction * LIVE_MARGIN_HEADROOM` (no `/2` — this is the only bot per
  symbol). Stores `orig_qty`/`grid_filled` (not `partial_done`) on the new `live_pos`
  entry.
- **`partial_exit(bot_id, frac)`** (`:726`) — takes an explicit `frac` argument now (was a
  hardcoded 50% single call in the old fixed-TP design). Queues `_do_partial(bot_id,
  frac)`, which closes `min(orig_qty * frac, qty)` — one call per grid level fill, not
  once ever. On an unknown-outcome transport failure, stashes `_pending_frac` on the
  position so `_retry_pending_partial()` can resend with the *same* fraction later.
- `_live_qty(side)` — queries `get_positions`, returns the size for the expected side.
- `mark_closed`/`_do_close` — closes the *entire* current `live_pos["qty"]` as reported by
  a fresh `_live_qty` call.
- `_reconcile_unconfirmed_entry()` / `_retry_pending_partial()` / `poll_positions()` /
  `record_manual_close()` — see Live-trading safety mechanisms below.
- `mtm(bot_id, price)` / `mtm_total(price)` — unrealized P&L, display-only.

### `_PaperBotBase` (`:1543`) / `AtrPartialPaperBot` (`:1635`)

`_PaperBotBase` holds `log()`/`_try_claim_capital()`/`_manage_exit()`/
`reconcile()`/`cum_pnl`/`cum_loss`/`mtm()` — everything that doesn't depend on the
grid-specific position shape. `AtrPartialPaperBot` (the only subclass; kept as a
base/subclass split in case a second strategy variant is ever reintroduced) owns
`__init__`/`_state_sig`/`_save_state`/`_load_state`/`_save_trade`/`tick`/`_partial_grid`/
`_close`.

- `_try_claim_capital(sym)` (`:1555`) — claims this symbol's capital slot via
  `self.engine.claim_slot(sym)` before a new entry; returns `False` (blocking the entry
  entirely) if every `CAPITAL_TIERS` slot is already held by another symbol. Re-baselines
  `self.equity`/`self.live.equity_fraction` only on a *fresh* claim.
- `check_exit(price)` — **removed 2026-08-31** (explicit user ask, "get rid of 1 minute
  candle shit. same for entries as exit") along with the 1-minute feed that called it;
  `_manage_exit()` is now only reached from `tick()`.
- `_manage_exit(price, buy_now=False, sell_now=False, atr=0.0, stop_m=0.0, sym=None,
  iv=None)` (`:1632`) — shared SL-then-grid logic, called from `tick()` on every
  entry-timeframe bar close: `_hit_sl()` first, then (added 2026-08-31, see
  "reverse-and-flip" below) a flip check, then a `while _next_grid_hit(p, price):`
  loop — the last level routes straight to `_close()` instead of `_partial_grid()`,
  guaranteeing full exit. The `buy_now`/`sell_now`/`atr`/`stop_m`/`sym`/`iv` params are
  this bar's already-computed entry signal, only consulted when
  `params["flip_on_signal"]` is on.
- **`_flip(price, atr, stop_m, sym, iv)`** (`:1671`, added 2026-08-31, explicit user
  ask, "reverse-and-flip" — chosen from three offered options after "so there is no
  mechanism possible that detects when it is worth changing the trade to take
  advantage of potential profit?") — if the entry signal flips against an open
  position before the stop is hit (same `buy`/`sell` arrays the entry logic already
  computes, no new indicator), close it via `_close(price, "FLIP")` and immediately
  reopen the opposite side at the same price using the identical qty/fee/`grid_px`/
  `sl` construction `tick()`'s own flat-branch entry uses (same `bt.grid_level_prices`
  call live and backtest already share). Only reached from `_manage_exit` after the SL
  check already came back False this bar (SL always wins) and gated behind
  `params["flip_on_signal"]` — a new searched 0/1 `PARAM_SPACE` entry, not always-on,
  so the sweep decides per (symbol, interval, entry_source) whether flipping actually
  helps. `atr<=0` (a live-only edge case with no backtest equivalent, since the
  backtest's `bad[idx]` mask already guarantees a valid ATR at any bar a signal can
  fire) aborts the reopen, leaving the bot flat. Mirrors `_sim_grid_jit`'s/
  `_bt_combo_pair`'s flip branch exactly, including doing no grid/unwind check on the
  freshly-flipped position the same call (a position that just opened can't have
  crossed a grid level yet). Verified against real ETHUSDT 5m data: 1 of 602 tried
  param combos genuinely triggered the flip branch, and JIT vs pure-Python agreed
  exactly on all 15 result fields for that run — see CLAUDE.md for the full
  verification writeup (hand-crafted `_sim_grid_jit` case, 60+400-trial synthetic
  cross-checks, the real-data confirmation, and a DB round-trip test for the new
  `flip_on_signal` `param_runs` column / `_param_key` dedup fix).
- `__init__(params, equity, db, lev=None, live=None, bot_id=None,
  entry_source="searched")` (`:1645`) — `self.BOT_ID = bot_id or self.__class__.BOT_ID`
  (class default `"partial"`), `self.entry_source = entry_source`, both set before
  `_load_state()` (which overrides `entry_source` from the DB if a saved value exists —
  see the `_PaperBotBase`/`AtrPartialPaperBot` note above). `self.entries_paused`
  (default `False`) blocks new entries only — the `if self.position:` branch of `tick()`
  is completely unaffected either way. `self.engine`/`self._slot_frac` are the
  capital-slot bookkeeping fields, set post-construction by `TradingEngine._run()`.
- `_state_sig()` (`:1678`) — snapshot tuple including `grid_filled`/`partial_pnl` (not
  `partial_done`) for the reconcile no-op-write check.
- `_save_state()`/`_load_state()` (`:1684`/`:1709`) — persist/restore `grid_px` (as JSON),
  `grid_fracs` (as JSON, added 2026-08-28, replaces a single `grid_level_frac` REAL —
  see Data layer), `grid_filled`, `orig_qty`, `partial_pnl`, **and `entry_source`** to/
  from `paper_position` (see Data layer for the full schema). `_load_state()` restores
  `self.entry_source` from the DB row when a saved value is present, overriding whatever
  the constructor was called with — this is what stops an already-open position from
  being retroactively relabeled onto a different entry source. If `grid_fracs` is
  `NULL` (a position opened before that column existed), it's reconstructed as
  `[grid_level_frac] * len(grid_px)` — replicating the old uniform fraction across
  however many levels that already-open position has.
- `tick(hi, lo, cl, sym, iv, entry_signal)` (`:1766`) — `entry_signal` is the
  `(buy, sell, atr_arr, stop_m)` tuple `ComboTrader._on_kline` precomputes via
  `compute_partial_signals(..., entry_source=self.partial.entry_source)` and passes in
  (recomputed fresh every bar over the whole buffer, using whichever source this bot
  is currently persisted as). On a flat bot, checks
  `entries_paused`/cooldown/`_entry_allowed()`, then on a buy/sell signal: claims capital
  (`_try_claim_capital`), sizes the position, lays out `grid_px` via the formula above,
  sets `self.position`, saves state, mirrors to `self.live.enter(self.BOT_ID, side,
  price)` if a `LiveExecutor` is attached.
- `_partial_grid(price)` (`:1812`) — closes `grid_fracs[grid_filled]` (that level's own
  fraction, read before incrementing the counter — added 2026-08-28, replaces a single
  shared `grid_level_frac`) of `orig_qty` (capped to whatever remains), trails the
  stop, increments `grid_filled`, saves state, calls
  `self.live.partial_exit(self.BOT_ID, frac)`. **Never called for the last level.**
- `_close(price, reason)` (`:1840`) — fully closes, records the trade (`had_partial =
  grid_filled > 0`), saves state, calls `self.live.mark_closed(self.BOT_ID, reason,
  price)`. Shared close path for SL, the final grid level, and manual stop
  (`"stopped_by_user"`).

### `ComboTrader` (`:1859`)

- `__init__(session, symbol, interval, partial_bot)` — single-bot signature now.
  `_peak_combined` starts at the bot's already-loaded equity post-`_load_state()`.
  `entry_source` is a read-only `@property` (`:2087`) reading `self.partial.entry_source`
  — not stored separately on `ComboTrader` itself.
- `start()` (`:1878`) — seeds `SEED_BARS=600` bars synchronously, then calls
  `_force_reconcile_paper_from_live()` (see below) **before** spawning the WS thread,
  then spawns `_ws_loop` (entry-timeframe) as a daemon thread. (`_ws_loop_1m` was
  removed 2026-08-31 — see below.)
- **`_force_reconcile_paper_from_live()`** (`:1888`) — restart safety net: if the exchange
  has a real open slice for this bot but its own paper position is missing (never
  restored, or left under the `_unattributed` pseudo-bot-id), force-reconstructs a paper
  position from the live slice's real entry/side/qty and its own `open_ts`, computing a
  fresh grid from the just-seeded bars using the bot's current params (same formula
  `tick()` uses for a new entry), starting from `grid_filled=0` (the real fill count can't
  be recovered from the exchange). Falls back to a conservative fixed `±5%`/`±10%`
  stop/grid band if ATR can't be computed yet. This exists specifically because **live has
  no exchange-side stop-loss** — a live slice with no paper position behind it has NO path
  to ever closing itself.
- `_ws_loop()` — see Threading model. `sub_symbol`/`sub_interval` captured at subscribe
  time; `_on_kline` discards any bar whose subscription has since been superseded by a
  symbol switch. (`_ws_loop_1m`/`_on_kline_1m` — the 1-minute supplementary feed —
  removed 2026-08-31, explicit user ask, "get rid of 1 minute candle shit. same for
  entries as exit"; `_force_reconnect_1m`/`_last_kline_ts_1m` removed with it.)
- `_on_kline(msg, sub_symbol, sub_interval)` (`:1999`) — appends the new bar under
  `self._lock`, trims to `SEED_BARS`, computes `entry_signal = compute_partial_signals(hi,
  lo, cl, self.partial.params, entry_source=self.partial.entry_source)`, calls
  `self.partial.tick(...)` (which itself calls `_manage_exit()` — entries and exits both
  check on this same bar close now), then `self._maybe_release_slot()`.
- `_maybe_release_slot()` (`:2068`) — frees this symbol's capital slot the instant the bot
  goes flat. Idempotent, called unconditionally after every tick/reconcile.
  Must be called with `self._lock` already held.
- `reconcile()` — computes WS staleness, delegates to the bot's `reconcile()`.
- `ws_ok` / `combined_equity` / `drawdown_pct` / `cum_pnl` / `cum_loss` (properties) —
  `combined_equity`/`cum_pnl`/`cum_loss` are now just `self.partial.*` directly (no
  summing across two bots).
- `last_price()` — returns `self._display_price`, updated by `_on_kline` on every
  confirmed bar. **Display-only** — entry/exit signal logic still reads exclusively from
  `self.bars`.

## GUI classes — remaining methods

### `TradingEngine`

- `claim_slot(symbol)` / `release_slot(symbol)` (`:3462`/`:3482`) — the capital-slot
  primitives, see the Strategy section.
- `_bal_loop()` — refreshes the shared paper balance plus each leg's own live balance,
  every 60s.
- `_live_poll()` — loops every leg's `LiveExecutor.poll_positions()`/`record_manual_close`,
  every 30s, one thread total.
- `shutdown()` — sets `stop_ev`, stops each leg's `ComboTrader` (`combo._stopped.set()`)
  and force-saves the bot's state. Does **not** close any open position — that's
  `MainWindow`'s job via the Stop Paper confirmation flow.

### `MainWindow`

- `__init__(engine)` (`:3730`) — window title "Unified Combo Grid" (was "Unified Combo").
  Builds all four tabs, wires `HomeTab`'s callbacks, calls `self._backtest.auto_start()`,
  starts the 1s `QTimer`.
- `_refresh()` — the `QTimer` tick: always refreshes the Backtest tab; everything else
  only if `_paper_running` and `eng.ready`.
- **`_on_stop_paper()`** (`:3780`) — collects every leg with an open live position into
  `open_live_legs`; if non-empty, shows a close-or-not confirmation. **The "Close
  Position(s) & Stop" path closes through the paper bot's own `_close()`, never
  `LiveExecutor.mark_closed()` directly** (ported into this fork on the same day it was
  fixed in the source repo — see "Bugs already fixed here" #1): `with leg.combo._lock: if
  leg.combo.partial.position is not None: leg.combo.partial._close(price,
  "stopped_by_user")`, which clears the paper side AND mirrors to the exchange via its own
  existing `if self.live: self.live.mark_closed(...)` call inside `_close()` — the same
  path every other close (SL/GRID/manual) already uses. Snapshots `bot.position` freshly
  under the lock rather than trusting a pre-dialog snapshot, since `_live_poll` or the
  bot's own SL/grid tick can close the position in the background while the confirmation
  dialog is still open.
- `_on_start_paper()` — refuses with a warning if no paper key is saved; shows the
  live-trading confirmation if a live key is saved; constructs a **fresh** `TradingEngine`
  and starts it.
- `_do_exit()` — plain yes/no confirm, then `self.close()`.
- `closeEvent(event)` — stops the timer, calls `eng.shutdown()`, calls the Backtest
  runner's `.kill_now()` if one exists, then `event.accept()`.

### `HomeTab`

- `_build_leg_block(title)` (`:3353`) — factory for one leg's box: Symbol/Price/WS cards +
  a single `_StatusBar` (titled "ATR GRID", not a PARTIAL/STOP pair).
- `_sync_leg_widgets(legs)` — rebuilds `self._leg_widgets` (keyed by symbol), kanban-style
  wrapping layout, only when the leg identity set changes.
- Everything else (key management, `set_run_state`/`set_loading`/`set_stopped`/`refresh`)
  is unchanged in spirit from the source repo.

### `BacktestTab` / `BacktestRunner`

- `_BT_COLS` (`:2740`) — 16 columns, **"Entry" column back**: `Symbol, Interval, Status,
  Entry, Sharpe, Ret%, DD%, CAGR%, Trades, WR%, PF, AvgHold, cumP, cumL, MaxTP, MaxLoss`.
- `_run()` (`:2619`) — each cycle loops every symbol × every `bt.CRYPTO_INTERVALS` entry
  (and, inside `optimize_symbol_interval`, every entry source), calling
  `_protected_entry_source(sym, iv)` then `bt.optimize_symbol_interval(sess, sym, iv,
  self.status, executor=pool, protected_source=protected)` for each pair; after the
  sweep, calls `_report_missed_trades(sess, _load_worthy_plus_open_positions())`.
- Everything else (`start`/`stop`/`run_now`/`kill_now`/`auto_start`/`_on_start`/`_on_stop`/
  `refresh`, the `BrokenProcessPool` recovery) is unchanged in spirit from the source repo.

### `PaperTab` / `LiveTab`

Both use the kanban-style dynamic pattern (`_build_leg_block`/`_sync_leg_widgets`,
`KANBAN_COLS = 2`, wrapping `QGridLayout`) rather than a single-column `QScrollArea` stack.

- `PaperTab._build_leg_block` (`:2925`) — one `QGroupBox` per leg: "Paper Portfolio" `_SG`
  (12 fields: Symbol/Price/Balance/Comb.Eq/Peak/DD/cumPnL/cumLoss/Trades/WR/PF/Expect) +
  one `BotPanel("ATR GRID")` (not a two-panel splitter). The params summary label now
  includes `gridLv`/`gridAtrX`/`gridFrac` instead of `tpX`/`partLvl`, and leads with
  `entry={combo.entry_source.upper()}` (`"SEARCHED"`/`"PINE"`, `:3010`) so it's visible
  at a glance which entry source this leg is currently trading.
- `BotPanel.refresh(bot, price, iv, combo)` (`:2484`) shows, when a position is open:
  Entry/Qty/Notional/SL/SL dst/MTM, **`NextLvl`/`NextLvl dst`** (the next unfilled grid
  level's price and distance, `"—"` once all levels have filled), Age, and **`Grid`**
  (`f"{filled}/{levels}"`) — replacing the old TP/partial-done display.
- `LiveTab.refresh(legs)` (`:3122`) — Current Position `_SG` now shows a **`Grid`** field
  (`str(grid_filled)`, cyan once `>0`) instead of a partial-done indicator.

## Every remaining module-level function

See the "Architecture map" section above for the authoritative list — this section is
intentionally not re-duplicated here (unlike the source repo's skill doc) to avoid drift;
grep the actual file for anything not already covered above, and update both sections
together if you add something new.

## `_api()` retry semantics

Unchanged from the source repo. Both files define their own `_api(fn, *args, **kwargs)`
wrapper (not shared): retry on a transport exception (up to 3 attempts, unless
`_retry_exc=False`); retry with backoff on a `_RATE_LIMIT_CODES` retCode
(`{10006, 10018}`); return immediately for anything in `_NO_RETRY`
(`{110007, 110006, 110012, 110013, 110017, 110025}`) **and for literally any other retCode
too** — `_NO_RETRY` is documentation, not a behavioral gate; only `_RATE_LIMIT_CODES` is
checked-and-different. The final attempt raises `RuntimeError` on an unresolved
rate-limit/error retCode instead of falling through to a bare `return r`.

## UI theme, colors, and reusable widgets

- **Color constants** (`_G`/`_R`/`_Y`/`_C`/`_M`/`_W`/`_D`) and the dark `QPalette` +
  stylesheet are set once in `_apply_dark_theme(app)`.
- **Color-by-value helpers**: `_pc(v)` (P&L), `_dc(v)` (drawdown %), `_wc(v)` (win rate).
- **`_titem`** — read-only, optionally-colored table cell factory.
- **`_StatusBar`** — one bot's current state: `_flat`/`_cd`/`_long`/`_short`/`_init`.
- **`_SG`** — generic 3-column key/value grid.
- **`_TradesTable`** — fixed-size (5 rows) recent-trades table.
- **`BotPanel`** — one strategy's full panel: `_StatusBar` + Position grid + Statistics
  `_SG` + `_TradesTable` + log. Titled "ATR GRID" now (was "ATR PARTIAL"/"ATR STOP").
- **`_set_kanban_card_accent`** — colors a leg's `QGroupBox` border by position side.

## Configuration reference (`data/unified_combo_config.json`)

| Key | Meaning |
|---|---|
| `symbols` | Bybit crypto symbols to backtest/trade (currently `["ETHUSDT"]`) |
| `crypto_intervals` | List of candle intervals (minutes) tested per symbol every cycle — `["30"]` by default as of 2026-09-01 (history: `["5","15","30"]` → `["5"]` → `["5","15"]` → `["30"]`) |
| `n_random` | TOTAL random parameter combos sampled per (symbol, interval) per backtest pass, split evenly across the two entry sources as `N_RANDOM_PER_SOURCE = max(1, n_random // 2)` (fixed 2026-09-01 — before this fix each of the two sources independently drew the full `n_random`, so a "200k combos" config silently tested 400k) |
| `is_days` | In-sample window length, days (clamped `1..7` as of 2026-09-01, was `1..2` before that) |
| `oos_hours_list` | Out-of-sample walk-forward windows tested, hours |
| `min_trades` / `min_avg_hold` | Minimum trade count / average hold (bars) to keep a candidate |
| `initial_equity` | Backtest starting equity — overridden by real wallet balance when a key is available in `_bt_make_session` |
| `entry_hours_utc` | Optional `[start, end]` UTC hour window restricting new entries; `null` = unrestricted |

**`gc_period`/`gc_poles` are no longer config keys** — in the Grid fork there's no fixed
GC signal source to configure; `gc_period`/`gc_poles` only exist as searched per-symbol
params inside each result file's `params`. Not config-driven (deliberately hardcoded):
leverage, whether demo/testnet is ever used (never), whether Backtest auto-starts
(always).

Verify a symbol actually exists on Bybit before adding it — a plausible-looking symbol can
simply not exist. Check with a read-only `get_tickers(category="linear", symbol=...)` call
first.

## End-to-end data flow — one bar's journey

1. Bybit pushes a kline message over the public WebSocket
   (`WebSocket(testnet=False, demo=False, channel_type="linear")`, subscribed in
   `ComboTrader._ws_loop`). Only `confirm=True` (fully closed) bars are processed further.
2. `ComboTrader._on_kline` runs **synchronously on the WebSocket callback thread** —
   appends the new bar under `self._lock`, trims the buffer, computes `entry_signal` via
   `compute_partial_signals`, then calls `self.partial.tick(...)`.
3. `tick()` checks SL/grid-level crossings against the latest close (if a position is
   open) or the entry signal (if flat), updating `self.position`/`self.equity` in place.
   Still synchronous, still on the WS callback thread, still fast (no I/O).
4. If a signal fired, the bot calls `self.live.enter(...)` / `.partial_exit(...)` /
   (via `_close`) `.mark_closed(...)` on the attached `LiveExecutor` — these push onto
   `_work_q` and return immediately; the executor's own persistent worker thread runs them
   serially, so the WS callback is never blocked on a Bybit order-placement round-trip.
5. `_save_state()` persists the bot's position/trade to `unified_combo_paper.db`
   synchronously, still within the callback.
6. Independently, `MainWindow`'s 1-second `QTimer` polls `TradingEngine.legs` and repaints
   Home/Paper/Live — the UI never reacts directly to a kline event.

There is no longer a second, faster feed running in parallel: the 1-minute supplementary
WS feed (`_ws_loop_1m`/`_on_kline_1m` → `check_exit()`) was **removed 2026-08-31**
(explicit user ask, "get rid of 1 minute candle shit. same for entries as exit") — SL/grid
checks now happen only inside step 3 above, on the same entry-timeframe bar as the entry
signal. `bt.CRYPTO_INTERVALS` defaults to `["30"]` only as of 2026-09-01 (see above for
the full history), so worst-case exit-check latency is bounded by whichever
interval a leg is currently on.

## Threading model

- **Main thread** — Qt event loop. Owns all widget mutation.
- **`ws-{symbol}`** (`ComboTrader._ws_loop`) — one per active leg, entry-timeframe feed;
  now the only per-leg WS feed (the `ws1m-{symbol}` 1-minute SL/grid fast-exit feed was
  removed 2026-08-31 — see above).
- **`engine`** (`TradingEngine._run`) — one-shot: connects, decides legs, spawns loops,
  exits.
- **`reconcile-{symbol}`** (`_reconcile_loop`, one per leg) — sleeps `RECONCILE_S=180`.
- **`param-reload-{symbol}`** (`_param_reload_loop`, one per leg) — sleeps
  `PARAM_RELOAD_S=3.5h`.
- **`live-poll`** (`TradingEngine._live_poll`, only if at least one leg has a live key) —
  sleeps 30s, one thread total.
- **`balance`** (`TradingEngine._bal_loop`) — sleeps 60s.
- **`live-worker-{symbol}`** (`LiveExecutor._worker_loop`, one per `LiveExecutor`) —
  persistent thread draining `self._work_q`; runs every queued `enter`/`partial_exit`/
  `mark_closed` action strictly serially. Replaces the source repo's earlier
  one-thread-per-call design.
- **`backtest`** (`BacktestRunner._run`) — runs continuously by default (auto-started,
  auto-repeat on); internally hands batches to a `ProcessPoolExecutor` (separate OS
  processes, protected from orphaning by `bt._win_kill_on_close()`'s Job Object). Every
  `(symbol, interval)` pair is optimized sequentially, one at a time.
- All of the above except `engine` (one-shot) and the `ProcessPoolExecutor` workers are
  `daemon=True`, but `os._exit(0)` is still used because pybit's WS library can leave
  non-daemon threads of its own running.

## UI map — exact contents of each tab

Home/Paper/Live build widget blocks **dynamically, one per currently running leg**,
laid out **kanban-style** (`KANBAN_COLS = 2`, a wrapping `QGridLayout`), rebuilt whenever
the leg identity set changes. A "nothing qualifies" placeholder covers the zero-legs case.

- **Home** (`HomeTab`): title → API Keys box → mode label → 2 engine-wide status cards
  (Balance, Uptime) → one `QGroupBox` per running leg (titled by symbol): 3 cards
  (Symbol, Price, WS) + a single "ATR GRID" `_StatusBar` → Start/Stop Paper buttons →
  Exit App button.
- **Backtest** (`BacktestTab`): Run/Stop buttons + auto-repeat checkbox → results table
  (two rows per `(symbol, interval)` pair — one per `entry_source` — 16 columns,
  **"Entry" column back**) → log panel.
- **Paper** (`PaperTab`): kanban grid, one `QGroupBox` per leg: "Paper Portfolio" (12-field
  `_SG`) → a single `BotPanel("ATR GRID")` (not a splitter of two panels).
- **Live** (`LiveTab`): header → engine-wide "no keys saved" placeholder when no leg has a
  live key — otherwise kanban grid, one `QGroupBox` per leg that has a `LiveExecutor`:
  "Account" `_SG`, "Current Position" `_SG` (includes a `Grid` field), "Live Trade
  History" `_TradesTable`, "Log" panel.

## Bybit API endpoints used

| Endpoint (pybit method) | Called from | Purpose |
|---|---|---|
| `get_wallet_balance` | `make_session`, `make_live_session`, `_bt_make_session`, `fetch_balance`, `LiveExecutor.fetch_balance` | Auth check + balance for position sizing |
| `get_kline` | `unified_combo_bt.fetch_ohlcv` (backtesting/replay) and `unified_combo_trader.seed_bars` (paper/live's initial seed) | Historical OHLCV |
| `kline_stream` (WebSocket) | `ComboTrader._ws_loop` (entry-timeframe; the `_ws_loop_1m` 1-minute fast-exit feed was removed 2026-08-31) | Live/paper's real-time bar feed |
| `get_instruments_info` | `LiveExecutor.setup` | Lot size, min qty, min notional, max market order qty |
| `get_positions` | `LiveExecutor._live_qty`, `poll_positions`, `reconcile_on_start` | Actual live position size/side |
| `place_order` | `LiveExecutor._order` | The only place any order is ever placed — market orders only, `reduceOnly` set appropriately for exits |
| `set_leverage` | `LiveExecutor.setup` | Sets `LEVERAGE=11` on the live symbol |

`switch_margin_mode` is not called anywhere (removed upstream before the fork). No other
Bybit endpoints are called anywhere in this codebase.

## Live-trading safety mechanisms (`LiveExecutor`)

- **`_entry_unconfirmed`** — set when an entry's outcome is unknown *and* the
  reconciliation query also fails. Blocks all new entries until
  `_reconcile_unconfirmed_entry()` resolves it.
- **`partial_retry_pending` + `_pending_frac`** — set when a grid partial's outcome is
  unknown and the position didn't actually shrink; stores the fraction so
  `_retry_pending_partial()` resends the *same* level's fraction (Grid fork: a position
  can have several partials over its life, not just one, so the fraction must be
  remembered per-attempt, not assumed to always be 50%).
- **`reconcile_on_start(partial_bot)`** — single-argument now. If a position already
  exists on the exchange, adopts it and attributes it to the one paper bot if its
  restored position matches the same side.
- **`poll_positions()` manual-close detection** — `POSITION_MISS_STRIKES=2` consecutive
  misses, ignores the first `POSITION_SETTLE_S=60` seconds after an entry.
- **`pending_reason`** — stashes the intended close reason so a later reconciliation
  doesn't mislabel it.
- **`_force_reconcile_paper_from_live()`** (`ComboTrader`, see above) — the Grid fork's
  additional net: if the exchange has a real slice with no paper position managing it at
  all (not just unattributed — genuinely missing), reconstructs one from scratch so it's
  never left invisible to every exit mechanism.

## Bugs already fixed here — don't reintroduce them

Historical fixes from the source repo (`unified_combo_gui`) that remain relevant to this
fork's current architecture, plus what's new here:

1. **"Close Position(s) & Stop" must go through the paper bot's own `_close()`, never
   `LiveExecutor.mark_closed()` directly — ported from `unified_combo_gui`'s 2026-08-28
   fix on the same day this fork was created.** Real incident in the source repo: closing
   only the live exchange slice left the paper bot's own DB row untouched, so a restart
   reloaded a stale "still open" position and a later manual cleanup recorded a fabricated
   paper loss at whatever price the market had moved to since. `MainWindow._on_stop_paper`'s
   "Close Position(s) & Stop" path (`:3808-3833`) calls `leg.combo.partial._close(price,
   "stopped_by_user")` under `combo._lock`, which clears the paper side AND mirrors to the
   real exchange via `_close()`'s own existing `if self.live: self.live.mark_closed(...)`
   call — the same path every other close (SL/GRID/manual) already uses. Never reintroduce
   a path that calls `LiveExecutor.mark_closed()`/`.partial_exit()` without going through
   the paper bot's own state first.
2. **`LiveExecutor.enter()`/`.partial_exit()`/`.mark_closed()` must always be called with
   `self.BOT_ID`, never a literal string.** Source-repo incident: a hardcoded literal
   instead of the symbol-scoped `bot_id` meant `LiveExecutor.live_pos["opener"]` never
   matched the real `BOT_ID`, so the close-authorization guard silently dropped every
   close signal — a real live position sat open and unmanaged for hours (live has no
   exchange-side stop-loss, so this was the *only* exit path). Confirmed in this fork's
   current code: every call site (`tick()`'s two `self.live.enter(self.BOT_ID, ...)`
   calls, `_partial_grid()`'s `self.live.partial_exit(self.BOT_ID, frac)`, `_close()`'s
   `self.live.mark_closed(self.BOT_ID, ...)`) already passes `self.BOT_ID` correctly —
   verify this stays true before touching any of those call sites.
3. **DB writes from concurrent legs sharing one sqlite connection can race.** A single
   process-wide `_DB_LOCK = threading.Lock()` wraps every `self.db.execute()`/`.commit()`
   call in `AtrPartialPaperBot`'s `_save_state`/`_load_state`/`_save_trade`
   (`unified_combo_trader.py:1684-1766`). If you ever add a new method that touches
   `self.db` directly, wrap it in `with _DB_LOCK:` too — not enforced by the type system.
4. **Duplicate live entries on double transport failure** — `_entry_unconfirmed` (see
   Live-trading safety mechanisms).
5. **Stale leverage after a failed symbol-switch refresh** — `LiveExecutor.setup()` resets
   `effective_leverage` before querying the exchange, not inside the try block.
6. **Silently-stuck partial exits** — `partial_retry_pending`/`_pending_frac` (see above;
   updated for the Grid fork's per-level fraction, not a fixed 50%).
7. **Mislabeled reconciled closes** — `pending_reason` (see above).
8. **Wall-clock used for a monotonic settle guard** — `open_mono` uses
   `time.monotonic()`, not `time.time()`.
9. **A symbol with an open position could be permanently orphaned across a restart if it
   stopped qualifying as worthy.** `TradingEngine._run()`'s rescue scan
   (`:3546-3576`, both `paper_position` and — if live — a direct exchange check across
   `bt.SYMBOLS`) force-adds a leg for any such symbol using
   `_load_result_for_symbol(sym)`, with `entries_paused=True` so it only manages the
   existing position rather than taking new entries on stale params.
10. **Leg selection has a staleness check.** `RESULT_MAX_AGE_S = PARAM_RELOAD_S = 3.5h` —
    `_parse_result_file()` rejects any result whose `run_ts` is older than that, applied
    unconditionally to `_load_all_worthy_crypto()` and via `require_fresh=True` to
    `_param_reload_loop`'s ongoing reload (the rescue scan deliberately passes
    `require_fresh=False` — its whole point is to keep managing a position on a symbol
    that's already gone stale).
11. **Grid-exit verification, performed the day this fork was created (2026-08-28) —
    the specific thing to re-run if `_sim_grid_jit`, the pure-Python twin in
    `_bt_combo_pair`, or `AtrPartialPaperBot`'s grid mechanics are ever touched again.**
    Three checks, all passing:
    - A **standalone hand-computation test** against `_sim_grid_jit` directly: entry,
      multiple level fills, the breakeven-then-staircase stop trail, a forced
      end-of-data close, both long and short sides — matched hand-computed expected
      values exactly.
    - A **3000-bar cross-check** feeding a random-walk price series (with real indicator
      computation, not synthetic signals) through both the pure-Python and JIT branches
      of `_bt_combo_pair` — the two branches agreed **exactly** on all 14 result fields
      (`score`/`sharpe`/`cagr_pct`/`total_ret_pct`/`max_dd_pct`/`trades`/`avg_hold`/
      `win_rate`/`profit_factor`/`final_equity`/`cum_profit`/`cum_loss`/`max_tp`/
      `max_loss`) across 48 real trades generated by the run.
    - A **full `AtrPartialPaperBot` lifecycle test**: entry → two grid fills delivered via
      the 1-minute fast path (`check_exit`, since removed 2026-08-31 — fills now arrive
      via `tick()`/`_manage_exit()` on the entry-timeframe bar instead, but the exit
      arithmetic this test exercised is unchanged) → a DB save/reload round-trip
      (simulating a restart mid-position) → final close on the last grid level. The
      resulting PnL was
      **bit-identical** to the standalone backtest simulation (`_sim_grid_jit`) run over
      the exact same price path — confirming live and backtest agree exactly, the
      property this whole app is built around (a backtested result must describe what
      live/paper will actually do).
    None of this has been re-verified against real market data or a real live order —
    see "Safe testing without demo/testnet" below for why that's structurally impossible
    here, and re-run the same three checks (not just a compile check) if the grid logic
    changes again.
12. **"pine" entry source was briefly a fixed-parameter preset — corrected 2026-08-28,
    same day it was added, before it shipped.** My first pass wrongly locked `"pine"`'s
    entry-signal params (`k_len`/`k_smooth`/`d_smooth`/`ob`/`os`/`chop_len`/`chop_thr`/
    `gc_period`/`gc_poles`) to the Pine script's literal default values via a
    `PINE_ENTRY_PARAMS` dict. The user corrected this ("pine needs to search params
    too" / "stop. focus on making pine strat in bt tests best params"): `"pine"` must
    search every entry param, exactly like `"searched"` does, via its own random sweep
    — at this point in the same day, still over the exact same range as `"searched"`
    (that range-equality didn't last the rest of the day either — see #13 below, the
    3x-widening that gave `"searched"` its own wider space). **`PINE_ENTRY_PARAMS` has
    been deleted from the codebase entirely** — it no longer exists anywhere in
    `unified_combo_bt.py`. Only `PINE_GC_SQRT2 = 1.414` remains, and as of this fix (but
    not after #13 below) it was the **only** thing `entry_source` controlled: which
    constant gets passed as `gaussian_channel_midline`'s `sqrt2` param. The specific
    thing to re-run
    if `PINE_GC_SQRT2`, `gaussian_channel_midline`'s `sqrt2` param, or the
    `entry_source` branching in `_bt_combo_pair`/`compute_partial_signals` are ever
    touched again — checks performed, all passing:
    - Confirmed `gaussian_channel_midline`'s `1.414` literal (`PINE_GC_SQRT2`) vs.
      `math.sqrt(2)` produces a tiny (~4e-6 relative) but genuinely nonzero difference in
      the midline — the port is faithful to the Pine script's own constant, not silently
      equivalent to the textbook formula.
    - Confirmed `compute_partial_signals` with `entry_source="pine"` responds to changes
      in `params` (e.g. `k_len`, `gc_period`) — proving it searches its params rather
      than ignoring them.
    - Confirmed that with `bt.PINE_GC_SQRT2` temporarily patched to the exact
      `math.sqrt(2)`, `"searched"` and `"pine"` produce byte-identical signals given
      identical params — proving the `sqrt2` constant is the ONLY difference between the
      two sources.
    - Confirmed `_parse_result_file` reports a pine-tagged result's own searched
      `gc_period`/`gc_poles` straight from that result file's `params` dict, exactly
      like `"searched"` does — no fixed-default special-casing remains.
    - Confirmed `_load_result_for_symbol` picks the best candidate across both the
      `_searched` and `_pine` suffixed result files for a symbol, not just one.
    - Confirmed the `paper_position.entry_source` `ALTER TABLE` migration in `get_db()`
      leaves every existing row completely untouched — tested against a real copy of the
      production DB, which already has real trade history (a real live ETHUSDT trade
      closed via SL before this feature existed).
    Like #11, none of this has been re-verified against a real live order — see "Safe
    testing without demo/testnet" below.
13. **"searched" gets a 3x wider entry-param search range than "pine" — added
    2026-08-28, same day as #12 but a later, separate change, explicit user ask:
    "widen the param values search range x3 for searched."** Until this change both
    sources searched the identical 9 entry-signal params over the identical range
    (`PARAM_SPACE`) — this widening is what actually broke that equality; #12 fixed
    `"pine"` searching its params at all, this widened where `"searched"` searches
    them. `bt.PARAM_SPACE_SEARCHED` (`unified_combo_bt.py:186`) is a copy of
    `bt.PARAM_SPACE` with only the 9 entry-signal rows widened — `k_len` 10-40→30-120,
    `k_smooth` 1-5→3-15, `d_smooth` 3-10→9-30, `ob` 70-90→50-100, `os` 10-30→30-90,
    `chop_len` 8-20→24-60, `chop_thr` 38-62→14-86, `gc_period` 50-250→150-750,
    `gc_poles` 1-9→3-27. Every bound uses plain both-bounds-x3 **except** `ob` and
    `chop_thr`, which are 0-100-bounded oscillator/index levels where both-bounds-x3
    collapses to a degenerate `(100,100)` after clipping — those two instead use a
    centered triple-width (keep the original center, widen to 3x the original width,
    clip to `0-100`). The exit/grid params (`atr_p`/`stop_mult`/`grid_levels`/
    `grid_dist_1..8`/`grid_frac_1..8`) are deliberately **not** copied wider — both
    sources still search those over the identical `PARAM_SPACE` range, preserving the
    pre-existing "exit same as the bot" invariant. `_sample()` (`:848`) now takes an
    optional `space=PARAM_SPACE` arg instead of always reading the module-level
    `PARAM_SPACE` directly. `optimize_symbol_interval()` (`:857`) used to generate ONE
    shared combo list and test every combo under BOTH entry sources at identical param
    values; it now builds TWO independent combo lists, `combos_by_src["pine"]` sampled
    from `PARAM_SPACE` and `combos_by_src["searched"]` sampled from
    `PARAM_SPACE_SEARCHED` (`:940-943`), sharing only the "tried" params dedup set and
    the elite `top_params` seed list (from `db_load_top`) between them — a combo
    sampled for one source is now tested ONLY under that source, never both.
    `_combo_worker()` (`:678`) used to loop `for src in ("searched", "pine")`
    internally, testing one combo under both sources per call; it now takes a fixed
    `src` as part of its args tuple and only tests that one source per call — the loop
    over both sources moved up into `optimize_symbol_interval`, which submits separate
    `ProcessPoolExecutor` batches per source. `GC_WARMUP_BARS` (`:233`, the settling-time
    margin for the recursive Gaussian Channel filter, used to pad the IS/OOS backtest
    windows) changed from `3 * PARAM_SPACE["gc_period"][1]` (`3 * 250 = 750`) to `3 *
    max(PARAM_SPACE["gc_period"][1], PARAM_SPACE_SEARCHED["gc_period"][1])` (`3 * 750 =
    2250`) — it must cover whichever source's `gc_period` ceiling is larger now, or a
    large-`gc_period` `"searched"` combo would get an undersized warm-up window and a
    biased/cold-started GC filter value. Compiled/linted clean
    (`python -m py_compile`/`pyflakes` on both files), committed as `7c642f1`.
14. **Grid exit made per-level — added 2026-08-28, explicit user ask: "can this be
    more dynamic. test number of grids is optimal in bt. and where they should be
    set" + confirmed follow-up: independent distance per level AND independent
    fraction per level.** Until this change every level shared ONE `grid_atr_mult`
    (uniform ATR spacing, `entry ± grid_atr_mult*atr*(i+1)`) and ONE `grid_level_frac`
    (same close-fraction at every level). Both are now per-level: `grid_dist_1..8`
    (each level's OWN ATR-multiple increment, `PARAM_SPACE` range 0.3-2.5 each, same
    bounds the old shared scalar used) and `grid_frac_1..8` (each level's OWN
    close-fraction, range 0.1-0.4 each) — 16 independently-searched params replacing
    the old shared `grid_atr_mult`/`grid_level_frac` scalars. New shared function
    `grid_level_prices(entry_price, atr, side, levels,
    grid_dists)` (`unified_combo_bt.py`, placed just before `_sim_grid_jit`) builds
    level `i`'s price as `entry_price + side*atr*cumsum(grid_dists[0..i])` — a
    CUMULATIVE sum of increments, not `grid_dists[i]` alone, guaranteeing levels stay
    monotonically farther from entry as `i` increases regardless of what each
    increment samples to (required for the sequential fill-scanning loop's
    `lvl_px[filled]` assumption to hold). This one function is called from THREE
    places — `_bt_combo_pair`'s pure-Python grid loop, `AtrPartialPaperBot.tick()`'s
    entry block, and the live-position-seeding block — instead of three
    hand-duplicated copies, so live and backtest can never silently diverge on how
    grid prices are built. `_sim_grid_jit`'s JIT hot path still hand-inlines its own
    copy (numba nopython can't call back into `grid_level_prices`) — kept in sync by
    hand, same as the pre-existing JIT/pure-Python twin pattern. **Legacy fallback,
    everywhere these are read**: `params.get(f"grid_dist_{i}", params.get("grid_atr_
    mult", 1.0))` (same pattern for `grid_frac_i`/`grid_level_frac`) — a params dict
    saved before this change transparently replicates its old scalar across every
    slot, reconstructing its EXACT old uniform-grid behavior; verified byte-identical
    in `test_per_level_grid.py`. Both DBs needed real migrations: `param_runs` gained
    16 nullable columns via `db_init()`'s `ALTER TABLE` (old `grid_atr_mult`/
    `grid_level_frac` columns kept forever for the COALESCE fallback,
    `_GRID_COALESCE`); `paper_position` gained one nullable `grid_fracs` TEXT (JSON
    list) column via `get_db()`'s `ALTER TABLE`, verified safe against a real copy of
    the production DB (existing rows/the one real live trade untouched). `db_save`'s
    reward/risk filter and `_bt_combo_pair`'s own RR gate both changed from
    `grid_levels * grid_atr_mult` to `sum(grid_dist_1..grid_levels)` (the cumulative
    last-level distance). `_param_key` extended to include all 16 per-level values so
    dedup still works correctly. `AtrPartialPaperBot._partial_grid` changed from
    `p.get("grid_level_frac", 0.25)` to `p["grid_fracs"][p["grid_filled"]]`. The Paper
    tab's params label changed from single `gridAtrX=`/`gridFrac=` fields to
    `gridDist=[...]`/`gridFrac=[...]` lists. Verified: JIT vs pure-Python agreement
    with diverse per-level values; a legacy-scalar-only params dict produces
    byte-identical results to the equivalent explicit-per-level dict; `_param_key`
    dedupes/distinguishes correctly; `db_save`/`db_load_tried_set`/`db_load_top`
    round-trip per-level values through a scratch DB including the `ALTER TABLE`
    migration path; live `tick()`'s `grid_px` matches calling `grid_level_prices`
    directly; `paper_position` migration safe against a real production DB copy.
    Compiled/linted clean, committed.
15. **Cross-down TP-capture — added 2026-08-28, same day as #14, explicit user ask,
    clarified over several rounds of Q&A.** Started as "i want it to close on a cross
    down the grid" — clarified through several exchanges: which level(s) trigger it
    ("any previously filled level, not just the most recent"), whether it needs a
    genuine two-point cross or a plain threshold ("plain threshold is fine"), whether
    the stop should move to a filled level's own price instead of breakeven after the
    1st fill ("yes" — but this got walked back), and crucially: **"mate. the stop loss
    stays the same as original. this is purely capturing TP."** — meaning the entire
    stop-loss formula/behavior from #11 stays byte-for-byte unchanged; this is a
    brand-new, separate, ADDITIONAL mechanism, not a modification of the stop. Then:
    should a cross-down fully close the position (like the stop) or only that level's
    own fraction — user picked **"partial close of just that level's fraction
    (mirrored unwind)."** Final design: if price crosses STRICTLY below (long) /
    above (short) an already-filled-but-not-unwound level, close that level's own
    `grid_frac_i` of CURRENT remaining qty (not the original amount — this is a fresh
    partial close using today's remaining position size, not "undoing" the earlier
    fill, whose profit is already banked). Each level unwinds at most once ever; a
    NEW fill (climbing to a fresh high after a partial retrace) makes that new top
    level freshly eligible for its own future unwind, independent of older levels'
    history. `_sim_grid_jit` gained a `np.zeros(MAX_GRID_LEVELS, dtype=np.bool_)`
    `unwound` array (reset to all-`False` on every new entry, alongside `filled=0`);
    after the existing up-fill `while` loop, a new `while ui>=0 and cl_i < lvl_px[ui]`
    loop (long; `>` for short) scans from `ui=filled-1` downward, closing
    `grid_fracs[ui]` at each not-yet-unwound level it crosses, stopping at the first
    level whose price isn't crossed (correct without an explicit "stop at sl" check
    because `lvl_px` is monotonically increasing with index — proven in testing that
    this self-limits to AT MOST ONE level unwinding before the stop-loss, since `sl`'s
    own unchanged formula, `entry` after 1 fill / `grid_px[filled-2]` after N fills,
    always sits exactly one level behind whichever level would be next to unwind).
    Strict `<`/`>` (not `<=`/`>=`) is deliberate: the level that JUST filled this same
    bar (price sitting AT or ABOVE it) can never also immediately "unwind" in the same
    check. `_bt_combo_pair`'s pure-Python twin got the identical logic by hand (list
    instead of array). `unified_combo_trader.py` gained `_next_grid_unwind_idx(pos,
    price)` (`unified_combo_trader.py:~1543`, mirrors `_next_grid_hit`'s pattern —
    returns the index to unwind or -1) and `_partial_unwind(price, ui)`
    (`:~1866`, mirrors `_partial_grid` but reads `grid_fracs[ui]` for the specific
    unwound index rather than `grid_filled`, explicitly never touches `p["sl"]`, and
    returns `True`/routes to `_close()` if qty hits ~0 — there's no "last one closes
    all" special case on the unwind side the way there is for up-fills, since the
    UNCHANGED stop-loss is still what guarantees eventual full exit if the whole grid
    round-trips). `_manage_exit()` now has a third `while` loop after the existing
    SL-check and up-fill loop, calling `_next_grid_unwind_idx`/`_partial_unwind` in
    sequence. `paper_position` gained a third same-day `ALTER TABLE` migration (after
    `entry_source` and `grid_fracs`): `grid_unwound` TEXT (JSON bool list per level),
    `NULL` for an already-open pre-2026-08-28 position defaulting to "nothing unwound
    yet" on load — the same conservative default used for an adopted live position
    with no local history. Verified: a hand-crafted deterministic price path
    (rally through level 1 → retrace strictly below it → fall to breakeven, which
    equals the unchanged stop-loss after 1 fill → full stop-out) produces exactly 1
    trade, and its final equity was cross-checked against an INDEPENDENTLY-WRITTEN
    from-scratch reference implementation of the OLD (pre-this-feature) behavior for
    the identical price path — new equity (1074.33) is strictly BETTER than old
    (1009.69), proving the mechanism is genuinely active and both reference/actual hit
    the stop at the identical price, proving the stop itself is unchanged; an
    oscillating multi-retrace-and-rally path still produces exactly 1 trade (no
    double-close bug); JIT vs pure-Python agree exactly on random data with the
    mechanism firing; a live `AtrPartialPaperBot` run through real `tick()`/
    `check_exit()` calls reproduces the exact fill→unwind→stop-at-breakeven sequence
    with `p["sl"]` provably unaffected by the unwind event; `paper_position.
    grid_unwound` migration verified safe against a copy of the real production DB.
    Compiled/linted clean, committed.

## Data layer

- **`data/unified_combo_paper.db`** (SQLite, WAL mode):
  - **`paper_trades`** — closed trade log: `id, timestamp, symbol, interval, strategy,
    side, entry, exit_price, qty, pnl, reason, partial, bars_held`. `strategy` is the
    leg's symbol-scoped `bot_id` (e.g. `"partial_ETHUSDT"`). No separate entry-timestamp
    column — the missed-trade report reconstructs it from `bars_held`.
  - **`paper_position`** — one row per `bot_id`: `bot_id, symbol, interval, side, entry,
    sl, qty, orig_qty, fee, equity, peak_equity, open_ts, grid_px, grid_level_frac,
    grid_filled, partial_pnl, entry_source, grid_fracs, grid_unwound`. `grid_px` is a
    JSON list of level prices; `grid_fracs` (added 2026-08-28, per-level grid work) is
    a JSON list of level fractions, replacing the single `grid_level_frac` REAL (kept
    in the schema, never dropped, purely as a fallback for an already-open
    pre-2026-08-28 position — see `_load_state`); `grid_unwound` (added 2026-08-28,
    same day, the cross-down TP-capture work — see "Bugs already fixed here" #15) is a
    JSON list of per-level booleans tracking which levels have used their one-time
    unwind, `NULL`/defaults to all-`False` for a position predating this column.
    **`entry_source`, `grid_fracs`, and `grid_unwound` (all added 2026-08-28) are the
    three columns added via actual `ALTER TABLE` migrations in `get_db()`**, not just
    baked into a fresh `CREATE TABLE IF NOT EXISTS` — this repo's DB already has real
    trade history (a real live ETHUSDT trade closed via SL before either feature
    existed), so a from-scratch schema wasn't an option here the way it was for the
    earlier grid-shaped columns. `get_db()` checks `PRAGMA table_info(paper_position)`
    and only runs `ALTER TABLE ... ADD COLUMN` for whichever of the two is missing.
    Verified 2026-08-28 against a real copy of the production DB that both migrations
    leave every existing row completely untouched (`entry_source`/`grid_fracs` just
    come back `NULL` for pre-existing rows — `entry_source=NULL` means "no saved value,
    keep the constructor's default"; `grid_fracs=NULL` means "reconstruct from the old
    `grid_level_frac` scalar, replicated across `len(grid_px)`"). Still **no `tp`,
    `partial_done`, or the old `signal_source` columns** — those were the source repo's
    pre-fork shape and were never part of this DB's schema.
  - **`live_position`** — one row per `bot_id`: `bot_id, symbol, side, entry, qty,
    orig_qty, grid_filled, open_ts`. No `interval`/`signal_source` columns (mirrors the
    source repo's `LiveExecutor` being symbol-scoped only).
  - **`live_trades`** — closed real-trade log: `id, symbol, timestamp, side, entry,
    exit_price, pnl, reason, bot_id, qty`.
  - **`bot_state`** — key/value, written only when the bot is flat (`equity_{bot_id}`/
    `peak_{bot_id}`). When flat, equity/peak load from here; when a position is open, they
    load from `paper_position` instead.
- **`data/unified_combo_params.db`** — `param_runs`: every backtested parameter
  combination tried per (symbol, interval), with `grid_levels`/`grid_dist_1..8`/
  `grid_frac_1..8` columns (the old `grid_atr_mult`/`grid_level_frac` columns are kept
  for legacy-row fallback, see "Bugs already fixed here") instead of `tp_mult`/
  `partial_lvl`. Used by `db_load_tried_set`/`db_load_top`. Also `winning_params`
  (added 2026-09-01, explicit user ask, "shouldnt it be recorded with all previously
  tested winning params and always tested again before random tests??") — a separate,
  permanent, never-pruned table recording every distinct (`_param_key`-deduped) combo
  that has EVER cleared `_clears_target` and been saved as a (symbol, interval,
  entry_source)'s live result file, one row per (symbol, interval, entry_source,
  distinct combo). Same grid/gc/flip columns as `param_runs` (including the unused
  legacy `grid_atr_mult`/`grid_level_frac` columns, needed only because
  `_grid_select_cols()` — shared with `param_runs` — references them by name).
  Written by `db_save_winner`, read by `db_load_winners` (unranked, unlimited — unlike
  `db_load_top`'s top-`N_TOP_RETEST`, every historical winner is always returned).
- **`data/unified_combo_results_{symbol}_{interval}m_searched.json` /
  `..._pine.json`** (+ `_oos{48,60}h` variants) — two independent files per
  (symbol, interval) again, added back 2026-08-28 by the pine entry source work, one
  per entry_source, each its own fully independent IS-ranked/OOS-retested/saved
  candidate. Read by
  `_load_combo()`/`_load_all_worthy_crypto()`/`_load_result_for_symbol()`, all of which
  scan by glob pattern via `_iter_result_files()` and treat both suffixes uniformly. A
  pine-tagged file's parsed gc_period/gc_poles are its own searched values, straight
  from that file's own params dict, exactly like a searched-tagged file — there is no
  fixed-default special-casing (the since-deleted PINE_ENTRY_PARAMS dict used to do
  this, before the user's correction). A result file backing
  an open position on its own entry_source is never overwritten while open (see the
  position-freeze rule); the other source's file for the same symbol/interval updates
  normally regardless.
- **`data/unified_combo_paper.log`** / **`unified_combo_bt.log`** — `RotatingFileHandler`,
  5MB max, 3 backups, UTF-8 encoded.
- **`data/crash_unified_combo_paper.log`** — unhandled *thread* exceptions only.

## Hard invariants — verify before changing any of these

- **MAINNET ONLY. No demo/testnet, no exceptions, not even by asking.** Every session must
  connect to Bybit mainnet — `HTTP(..., demo=False)` and `WebSocket(..., testnet=False,
  demo=False)`, everywhere, unconditionally. Before touching *any* session/API-client
  construction, `grep -n "demo=" unified_combo_trader.py unified_combo_bt.py` and confirm
  every match is a bare `demo=False`.
- **Backtest auto-starts; paper/live never do.** Deliberate, scoped to the Backtest tab
  only.
- **No plaintext key files, anywhere.** Windows/source-repo: DPAPI-encrypted
  `keys/demo.dat`/`keys/live.dat`. **This macOS port**: macOS Keychain (`security` CLI,
  service `unified-combo-grid`) — no key files exist on disk at all; `keys/` is
  vestigial here.
- **Leverage is hardcoded**: `LEVERAGE = 11` in both files, same for every symbol.
- **Crypto only.**
- **Every symbol tested at every configured `bt.CRYPTO_INTERVALS` entry (30m only as
  of 2026-09-01), one winning interval per symbol.**
- **Capital is a fixed set of slots (`CAPITAL_TIERS`), claimed first-signal-wins.** A
  qualifying symbol with no free slot simply doesn't trade. Never hardcode a fraction.
- **Live positions have no exchange-side stop-loss/take-profit.** Deliberately accepted
  risk — `ComboTrader._force_reconcile_paper_from_live()` exists specifically so a restart
  can never leave a real live slice with no paper position managing it.
- **The app is identified as `unified_combo_trader_grid` (Windows: `.exe`; this macOS
  port: the bundle name inside `Unified Combo Grid.app`), never renamed back to
  `unified_combo_trader`** without re-checking the cross-kill risk against the source
  repo (see the spec file's own comment — `unified_combo_trader.spec` on Windows,
  `unified_combo_trader_mac.spec` on macOS) — both repos may run live mainnet processes
  on the same machine simultaneously, and a shared process image name would let either
  repo's kill-by-image-name build routine accidentally kill the OTHER repo's live
  trading process.
- **A deploy-staging copy of the build, if one is ever made for distribution, must stay
  permanently keyless.** Windows: no `data`/`keys` junctions, ever — some zip tools
  dereference Windows junctions, so a junctioned `keys/` would bundle real
  DPAPI-encrypted key files into a public zip/GitHub Release. macOS: there's no key
  *file* to accidentally bundle this way (Keychain items aren't files), but the real
  `data/` (trade history, params DB) still shouldn't ship in a distributable build.
- **Packaging is onedir, not onefile.** Windows: `dist/unified_combo_trader_grid/
  unified_combo_trader_grid.exe`, with `data`/`keys` junctions that must be recreated
  after any `rm -rf dist`. **This macOS port**: `dist/Unified Combo Grid.app` (a real
  `.app` bundle via `BUNDLE()`, wrapping the same onedir `COLLECT()` at
  `dist/unified_combo_trader_grid/`) — `data`/`keys` inside it are REAL directories, not
  junctions, so a fresh build's `data/` starts empty and the real accumulated one must
  be copied in by hand (see `uc-build`); nothing needs recreating for `keys/`, which is
  vestigial on macOS.

## Local environment quirks (macOS — this port)

- The installed app is `/Applications/Unified Combo Grid.app` (confirm with `ps aux |
  grep -i unified_combo` + `lsof -p <pid> | grep data/` before assuming this path — it's
  wherever it was actually deployed to). `Contents/MacOS/data` inside it is a real
  directory holding the live trade DBs/params DB/logs/result JSONs — NOT a junction to
  this dev checkout's own `data/` (which is typically a separate, stale copy from
  whenever it was last built). Don't confuse the two when checking "what's actually
  running" state — always resolve the RUNNING process's own open file paths via `lsof`,
  never assume it's this repo's `data/`.
- **Building**: `pyinstaller unified_combo_trader_mac.spec --noconfirm` (from the
  project venv). Excludes `torch`/`torchvision`/`scipy`/`lxml`/`sklearn`/`jax`/etc, skips
  `collect_all('PyQt6')`. `collect_all('numba')`/`collect_all('llvmlite')` are the single
  biggest size contributor (~149MB uncompressed) — accepted tradeoff for the JIT
  speedup. No UPX step — the mac spec sets `upx=False` deliberately (UPX is a
  Windows/Linux-oriented compressor, not required here).
- **Testing triggers real backtest activity.** Since the Backtest sweep auto-starts on
  launch, even a quick offscreen launch immediately spawns a live mainnet session and
  `ProcessPoolExecutor` worker processes (visible as separate `--multiprocessing-fork`
  entries in `ps aux`). Cleanup must sweep **every** worker PID, not just the main one.
- **Killing the app is a normal POSIX `kill`/`kill -9` or `pkill -f "Unified Combo
  Grid"` on macOS** — no git-bash/Windows unreliability to work around, no
  `taskkill`/`tasklist`/PowerShell. Still verify explicitly afterward: `ps aux | grep -i
  unified_combo | grep -v grep` should come back empty.
- **After any build, launch-verify before trusting it.** Launch the `.app`'s executable
  directly (not via Finder, so stderr is visible), confirm the process survives, check
  for `data/crash_unified_combo_paper.log` inside the bundle, then kill it (and every
  worker PID). The full step-by-step procedure — including how to preserve the real
  `data/` across the rebuild, since a fresh `.app`'s `data/` starts empty on macOS — is
  `/uc-build`.
- **Never touch the user's real saved keys during testing.** Use in-memory monkeypatches
  (e.g. `tr.has_keys = lambda live=False: True`) rather than calling the real
  Keychain-backed `save_keys`/`delete_keys`.
- **In-process testing attaches to the REAL `data/unified_combo_paper.log`** unless
  explicitly detached — `tr._log.removeHandler(tr._fh)` right after import. Note this is
  whichever `data/` the import resolves relative to (this dev checkout's own, by
  default) — not necessarily the deployed app's `data/` in `/Applications`.

## Safe testing without demo/testnet

Mainnet-only means there is no sandbox to place a test order against.

- **Mock the session object, not the environment.** Give `LiveExecutor` a fake session
  whose `place_order`/`get_positions`/`get_wallet_balance`/`get_instruments_info` methods
  return canned dicts. Exercises retry/reconciliation/state-transition logic with zero
  risk.
- **Read-only real calls are fine** — `get_wallet_balance`, `get_instruments_info`,
  `get_positions`, `get_kline`/`get_tickers` against mainnet.
- **Never call anything that can place, modify, or cancel an order** as part of testing,
  against any session, real key or not.
- For the paper side, the bot never calls `place_order` itself (only `LiveExecutor` does)
  — feed it synthetic `hi`/`lo`/`cl` numpy arrays directly, no session needed.
- `bt._bt_combo_pair(..., record_entries=True)` and `bt.replay_recent_trades(...)` are
  both read-only (fetch + simulate, no orders) and safe to call directly against a real
  session for verification. This is also how the grid-exit verification in "Bugs already
  fixed here" #11 was performed — standalone in-process calls, never a real order.

## Debugging / triage workflow

1. **Check the logs first** — `data/unified_combo_paper.log`, `data/unified_combo_bt.log`,
   `data/crash_unified_combo_paper.log` (unhandled thread exceptions only).
2. **Match the symptom to a layer**: wrong entry timing → `compute_partial_signals`/
   `_bt_combo_pair`'s shared indicator calls; wrong grid-level prices/fill sizing →
   `_partial_grid`/`_manage_exit`/`_sim_grid_jit` (check both the JIT and pure-Python
   branches stay in sync); wrong position size → `LEVERAGE`/`equity_fraction` vs. the
   symbol's currently-claimed `CAPITAL_TIERS` slot; grid-level detected slower than
   expected → this is now bounded by the entry-timeframe interval itself
   (`bt.CRYPTO_INTERVALS`, `["30"]` only as of 2026-09-01 — the `ws1m-{symbol}`
   1-minute fast-exit feed was removed 2026-08-31), so confirm `ws-{symbol}` is
   connected instead; live/paper
   mismatch → re-run the checks in "Bugs already fixed here" #11; wrong entry-source
   behavior (searched vs. pine) → re-run the checks in "Bugs already fixed here" #12;
   stuck/duplicate live orders → Live-trading safety mechanisms section; wrong
   symbol/interval/entry_source selected → `_load_all_worthy_crypto()`'s single
   `bt._clears_target`-based selection rule (win-rate gates nothing as of 2026-09-01 —
   see "Win-rate abandoned as a selection criterion"); a leg's DB state showing
   wrong numbers → check its `BOT_ID` isn't colliding with another leg's; app won't start
   or exits immediately → check `has_keys()`/session errors in the log.
3. **Reproduce narrowly.** Import the module directly
   (`python3 -c "import unified_combo_trader as tr; ..."`) rather than launching the full
   app. Detach the real log handler first if testing anything that logs.
4. **Fix, then verify at the right level**: pure logic changes get a targeted in-process
   test; anything touching Qt widgets gets an offscreen launch
   (`QT_QPA_PLATFORM=offscreen`) before a real build — remember this spawns a real
   backtest sweep, clean up every worker PID; anything touching the build/bundle gets a
   full `/uc-build` pass.
5. **Update this skill's "Bugs already fixed here"/relevant sections** when you fix
   something non-trivial — see Git workflow below for why this file is the only record.

## Git workflow

Independent repo from `unified_combo_gui` (its own private GitHub repo — don't assume
actions in one apply to the other: git history, releases, collaborators are all
independent). If this repo adopts the source repo's squash-and-force-push publish
pattern, confirm before running it rather than assuming silently, even if the user treats
it as routine — it's still a destructive force-push. If so, `git log`/`git blame`/
`git bisect` won't be useful here either, and this skill file plus the persistent memory
become the only record of what changed and why — keep both current whenever you fix
something non-trivial.
