#!/usr/bin/env python3
"""
Unified Combo Grid Backtester
ATR_PARTIAL only — the ATR_STOP shadow strategy and the single fixed-TP/SL exit were
removed in the Grid fork. Exits are a grid of ATR-multiple take-profit levels
(grid_levels/grid_dist_1..8/grid_frac_1..8, each level's own distance/fraction
independently searched, like everything else — see grid_level_prices' docstring), with
the stop-loss trailed up to the previous filled level after each fill.
Two competing entry signal sources per (symbol, interval), each independently IS-ranked/
OOS-retested/saved (see PINE_GC_SQRT2's docstring): "searched" and "pine" — both search
every entry param (k_len/k_smooth/d_smooth/ob/os/chop_len/chop_thr/gc_period/gc_poles)
via the same backtester sweep; "pine" differs only in the Gaussian Channel formula,
using the "Stochastic Triple Filter [ATP]" Pine Script's hardcoded 1.414 constant
instead of the mathematically exact sqrt(2). Writes
eth_trader_results_{sym}_{iv}m_{searched,pine}.json.
Crypto only (stock/tokenized-equity support removed 2026-08-22).

MAINNET ONLY. This module takes a session from eth_trader.py as a parameter —
it never creates its own — but the same rule applies everywhere in this codebase:
demo=False/testnet=False, unconditionally, no fallback. This is a live app handling
real money.
"""
import os, sys, time, json, math, logging, random, multiprocessing, sqlite3
from datetime import datetime, timezone, timedelta
from logging.handlers import RotatingFileHandler
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from contextlib import nullcontext
from math import comb

import ctypes  # ctypes.wintypes is imported locally inside _win_kill_on_close, Windows-only

import numpy as np
import pandas as pd
from pybit.unified_trading import HTTP

# JIT-compiles the bar-by-bar trade simulation loops (added 2026-08-22) — this is the
# dominant cost of a backtest cycle (~4.8M simulations/cycle across 24 symbol/interval
# pairs x 200k combos each), previously pure Python. Falls back to the plain-Python
# `njit` no-op decorator if numba isn't available so this module still imports/runs
# (slower) rather than hard-crashing — but the shipped exe always bundles numba, so
# _NUMBA_OK should be True in production.
try:
    from numba import njit
    _NUMBA_OK = True
except Exception:
    _NUMBA_OK = False
    def njit(*_a, **_kw):
        def _wrap(f): return f
        return _wrap

# ── Paths ────────────────────────────────────────────────────────────────────
if getattr(sys, "frozen", False):
    _DIR = os.path.dirname(sys.executable)
else:
    _DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR    = os.path.join(_DIR, "data")
CONFIG_PATH = os.path.join(DATA_DIR, "eth_trader_config.json")
os.makedirs(DATA_DIR, exist_ok=True)
# Key storage/session creation lives in eth_trader.py (the one app that owns
# credentials) — this module is a pure library now and takes a session as a param.
# Every session in this app connects to Bybit mainnet only; nothing ever uses Bybit's
# demo trading environment.

# ── Config ────────────────────────────────────────────────────────────────────
_DEFAULT_CONFIG = {
    "symbols":            ["ETHUSDT"],
    "crypto_intervals":   ["15", "30"],  # 15m added back 2026-09-04, explicit user
                        # ask ("i want you to add 15 min candles as well. for pine
                        # and search") — both entry sources are already swept per
                        # interval automatically (optimize_symbol_interval loops
                        # CRYPTO_INTERVALS x ("pine","searched")), so no other code
                        # change was needed. History: ["5","15","30"] -> ["5"] ->
                        # ["5","15"] (2026-08-31) -> ["30"] only (2026-09-01, "30m
                        # candles only. trade only 30m candles") -> this.
    "n_random":           200000,
    "is_days":            7,
    "oos_hours_list":     [168],
    "min_trades":         3,
    "min_avg_hold":       2.0,
    "initial_equity":     117.0,
    "entry_hours_utc":    None,
}
if os.path.exists(CONFIG_PATH):
    try:
        with open(CONFIG_PATH, encoding="utf-8-sig") as _f:
            _cfg = {**_DEFAULT_CONFIG, **json.load(_f)}
    except Exception:
        _cfg = _DEFAULT_CONFIG.copy()
else:
    _cfg = _DEFAULT_CONFIG.copy()
    with open(CONFIG_PATH, "w") as _f:
        json.dump(_DEFAULT_CONFIG, _f, indent=2)

SYMBOLS = [s.upper() for s in _cfg["symbols"]]

# ── Constants ─────────────────────────────────────────────────────────────────
FETCH_LIMIT     = 1000
TAKER_FEE       = 0.00055
MARGIN_HEADROOM = 0.98
LEVERAGE        = 11   # hardcoded for every symbol — no per-symbol lookup
WIN_FEE_MULT    = 2.0  # a trade only counts as a win if part_pnl > this many times
                        # its own round-trip fees — explicit user ask, "tiny trades
                        # should never be counted as wins" (a trade that barely beat
                        # what it paid in fees isn't a real edge)
MIN_WIN_PRICE_PCT = 0.0033  # a trade only counts as a win if its whole-trade
                        # avg-exit-vs-entry raw price move (BEFORE leverage — i.e.
                        # unleveraged % move of the underlying, not the leveraged
                        # equity return) is at least this much — explicit user ask,
                        # "only params where each trade is no less than .33% before
                        # leverage profit makes it through". Checked ALONGSIDE
                        # WIN_FEE_MULT (a win needs both); only affects what counts
                        # as a win, never touches a losing trade's own gl/ml/eq.

CRYPTO_INTERVALS = [str(iv) for iv in _cfg.get("crypto_intervals", ["30"])]

INITIAL_EQUITY  = float(_cfg["initial_equity"])
CATEGORY        = "linear"

IS_DAYS        = max(1, min(7, int(_cfg.get("is_days", 7))))  # strict hard cap: backtest IS window
                                                                # may never exceed 7 days — raised from a
                                                                # 2-day cap 2026-09-01, explicit user ask
                                                                # ("i want it to backtest 7 days"), which
                                                                # itself replaced an even earlier explicit
                                                                # ask ("it must be strictly two days data
                                                                # for bt") — editing
                                                                # data/eth_trader_config.json's is_days
                                                                # beyond 7 cannot raise this further, the
                                                                # code itself enforces the ceiling; raising
                                                                # the ceiling itself (as done here) has
                                                                # always required being asked again, and
                                                                # now has been. The max(1, ...) floor is a
                                                                # separate safety guard: a non-positive
                                                                # config value must never zero/negate
                                                                # IS_DAYS either (is_n<=0 -> negative
                                                                # backtest_bars -> np.empty(nb) crash
                                                                # inside every combo evaluation for the
                                                                # whole sweep). _max_pages(interval)
                                                                # already scales its Bybit pagination
                                                                # dynamically off IS_DAYS/GC_WARMUP_BARS,
                                                                # so no separate fetch-window fix was
                                                                # needed for this to work correctly at 7
                                                                # days, as long as the exchange actually
                                                                # has enough history — this now needs
                                                                # roughly 60 calendar days of 30m candles
                                                                # (7d IS + up to 2.5d OOS + a
                                                                # ~47-calendar-day GC_WARMUP_BARS settling
                                                                # window at 30m) — see CLAUDE.md for
                                                                # whether this was confirmed against a
                                                                # real fetch.
OOS_HOURS_LIST = [int(h) for h in _cfg.get("oos_hours_list", [168])]  # widened
                        # 2026-09-03, explicit user ask. History same day: 48h dropped
                        # first ("remove 48h thing as it tests 7 days" — it kept
                        # failing MIN_TRADES on a locked, naturally-low-frequency combo
                        # as trades aged out of the sliding window), leaving just 60h —
                        # then even 60h started failing the SAME way within the hour
                        # (confirmed directly: the exact combo dropped from 3 trades to
                        # 2 on 60h too), proving the real fix was never which single
                        # window width to pick, it was giving the window enough
                        # calendar time at all. Widened to 168h (7 days, matching
                        # IS_DAYS) on explicit follow-up ("widen window width") after
                        # explicitly rejecting the alternative of lowering MIN_TRADES
                        # instead — same evidentiary bar (3 real trades), just enough
                        # elapsed time to actually observe them for a combo this
                        # infrequent, rather than accepting weaker evidence as
                        # sufficient. A 7-day OOS window is now as large as the IS
                        # window itself (a 50/50 split of whatever's fetched), not a
                        # small tail-end slice of it — verify GC_WARMUP_BARS/fetch
                        # pagination still comfortably covers IS+OOS+warmup if this is
                        # ever widened further.

N_RANDOM      = int(_cfg["n_random"])  # combos tested PER ENTRY SOURCE per
                        # optimize_symbol_interval call — reverted 2026-09-04, explicit
                        # user ask ("should be 200k each"), back to each source
                        # independently drawing the full configured value. History: this
                        # WAS the original behavior; changed 2026-09-01 to split N_RANDOM
                        # in half between the two sources ("200k combos per run. not
                        # 400k!!!!" — a "200000" config value was testing 400000 combos
                        # total, 200k pine + 200k searched, and the user wanted the config
                        # value to mean the total). That reasoning no longer fits how this
                        # sweep is actually used now that locked_combos.json exists: even a
                        # locked source still draws the full random budget (its entry is
                        # pinned, but every exit-side param is genuinely searched — see
                        # load_locked_entry's docstring and optimize_symbol_interval's
                        # locked_entry_by_src handling), so splitting the budget between
                        # "both sources" silently halved it for whichever source was
                        # actually still searching. Reverting to "each source gets the
                        # full N_RANDOM, independently" fixes that regardless of
                        # which/how-many sources have a locked entry at any moment.
N_RANDOM_PER_SOURCE = N_RANDOM
# Pine refinement budget (added 2026-09-01, explicit user ask: "pine should take params
# found in search and then refine it for better profit but reduce overfitting" — see
# _sample_local's docstring for the actual mechanism). A local jitter search over a
# narrow window needs far fewer draws to cover well than a from-scratch global sweep —
# this is deliberately much smaller than N_RANDOM_PER_SOURCE so the freed compute goes
# back to "searched"'s own (still fully global) sweep.
N_PINE_REFINE_COMBOS = max(1, N_RANDOM_PER_SOURCE // 10)
N_WORKERS     = max(1, int(multiprocessing.cpu_count() * 0.5))
BATCH_SIZE    = 50
MIN_TRADES    = int(_cfg.get("min_trades", 3))
MIN_AVG_HOLD  = float(_cfg.get("min_avg_hold", 2.0))
MIN_RR_RATIO  = 0.8

N_TOP_RETEST  = 50
LOOP_INTERVAL = 60 * 60  # backtest auto-repeat cadence, in the sense of "wait this
                     # long AFTER the previous cycle's own work (sweep + missed-trade
                     # check) finished before starting the next one" — NOT "start a new
                     # cycle every LOOP_INTERVAL measured from the previous cycle's own
                     # start". BacktestRunner._run() already computes
                     # `next_run_ts = time.time() + interval_s` at the moment the
                     # previous cycle's work is fully done, not at cycle start, so this
                     # was already correct semantics before this value even changed —
                     # confirmed by reading that code rather than assumed. History: 2h
                     # → 30min (2026-09-01, explicit user ask, "retest every 30
                     # minutes") → 60min the same day (explicit user ask, "200k combos
                     # per run. not 400k!!!! run again 60 minutes after last run
                     # finished" — the "30 minutes" reading was superseded by this
                     # explicit "after last run finished" framing).

# TARGET_* — REMOVED ENTIRELY 2026-09-03, explicit user ask ("remove all gates. best
# params pnl wins"), the same conversation/day as removing the DD-ratio gate just
# before it. Full history of this selection floor: win_rate-based tiers (2026-08-28 →
# 2026-08-31) → total_ret_pct>=15%/cum_loss<$5/flat DD<5% (2026-09-01) → DD swapped for
# a return-to-DD ratio floor (2026-09-01, same day) → DD requirement dropped entirely
# (2026-09-03, earlier this same conversation) → return/cum_loss requirement dropped
# too (this change) — every numeric selection-level floor this app has ever had is now
# gone. Root cause across every one of those iterations, restated by the user plainly
# this time: whichever backtested params produced the most profit should just win,
# full stop — no threshold, however reasoned, survived contact with how few trades
# (~3) a 7-day-IS/up-to-60h-OOS window actually produces. `_load_all_worthy_crypto`
# already ranks candidates by `cum_profit` (dollars) on its own; removing the gate
# means that ranking is now the ENTIRE selection rule, unconditionally.
# `MIN_RR_RATIO`/`MIN_TRADES`/`MIN_AVG_HOLD` are a separate layer — SIMULATION-level
# validity filters that decide whether a candidate is even a well-formed result worth
# keeping at all (reject degenerate/single-fluke-trade combos), not the
# SELECTION-level "is this good enough to trade" floor this section used to add on
# top. `MAX_DD_PCT` (the hard drawdown reject gate that USED to also live in this
# simulation-validity layer) was REMOVED entirely 2026-09-03, explicit user ask ("get
# rid of the drawdown gate!!!!!!!"), in the same conversation as everything above —
# see MAX_DD_PCT's own former definition site (now gone) for the full history. Real
# consequence, stated plainly rather than left implicit: with no selection floor AND
# no drawdown validity floor, a symbol with no genuinely profitable candidate can
# still get a leg, and a candidate whose backtested equity curve round-trips through
# an arbitrarily deep drawdown before recovering is just as eligible as one that
# never draws down at all — "best of what's there" wins on final PnL alone, path
# ignored entirely. Nothing enforces a minimum edge OR a maximum risk before real
# capital trades on a result any more; that enforcement is now entirely on whoever
# reviews the Backtest tab before starting Paper/Live.

def _clears_target(r):
    """No selection-level gates remain (see the comment above — all removed
    2026-09-03, explicit user ask). Kept as a function, not deleted, because every
    caller across this module and eth_trader.py still calls it as the "is
    this a real, usable result" checkpoint: `r` is explicitly allowed to be a non-dict
    (e.g. BacktestRunner passes `self.status.get(...)` straight through, which is a
    plain status STRING like "queued"/"sweep 100/200" until a real result lands) —
    anything without a `.get` method (not just None) returns False rather than
    raising, so callers never need to isinstance-check before calling this. Every
    actual result dict now unconditionally passes; leg selection's own `cum_profit`
    ranking in `_load_all_worthy_crypto` is the entire selection rule now."""
    return isinstance(r, dict)

def _is_winner(r):
    """True if `r` is worth permanently remembering in `winning_params` (see
    db_save_winner/db_load_winners) — added 2026-09-03, explicit user ask, immediately
    after `_clears_target` itself was drained of every threshold: with no bar left at
    all, `winning_params` had started accumulating literal net losers (-34% return,
    -40% max_dd, confirmed directly against this app's real winning_params table) and
    retesting them ahead of random sampling forever, since the table has no size cap
    or ranking the way db_load_top's elite carry-forward does — a bad pick from one
    unlucky cycle would otherwise never age out. This is deliberately the MINIMAL bar
    that still means anything: `total_ret_pct > 0`, i.e. the candidate made ANY
    profit, however small — no return floor, no DD ceiling, no cum_loss cap, no ratio.
    Not a new invented threshold: it's the exact same bar db_save already requires
    before writing a combo to param_runs in the first place — this just applies that
    same standard to what gets written to winning_params too, which previously only
    checked `_clears_target` (now a no-op) and nothing else. Does NOT affect
    `_load_all_worthy_crypto`'s selection rule or the BacktestRunner retry-qualifies
    check — both still call `_clears_target` directly and remain fully gate-free, per
    explicit user ask; this only scopes what's remembered as worth retesting later."""
    return isinstance(r, dict) and float(r.get("total_ret_pct", -1)) > 0

# The retry DECISION and LOOP live in eth_trader.py's BacktestRunner._run()
# (moved there 2026-09-01 from a per-interval loop inside optimize_symbol_interval
# itself, once the user clarified retrying should span ALL of a symbol's configured
# intervals together, not force each interval to individually qualify even after a
# sibling already had): after sweeping every bt.CRYPTO_INTERVALS entry for a symbol, if
# none of its (interval, source) results pass `_clears_target`, BacktestRunner sweeps
# ALL of that symbol's intervals again from scratch (fresh combos, same DB-backed
# `tried` dedup so nothing is ever retested) — no time limit; the only thing that stops
# a symbol's retry short of qualifying is optimize_symbol_interval reporting zero
# genuinely new combos found across every interval in a pass (`new_combo_count`, its
# return value), not a time-based give-up. Deliberately accepted risk (explicit user
# choice, unchanged by the win-rate-to-target switch): a symbol that can't currently
# clear these targets can occupy that symbol's retry loop indefinitely, since
# BacktestRunner processes symbols sequentially within a cycle. `optimize_symbol_
# interval` itself is single-pass again — it just reports how many new combos it tested
# (see its own docstring) so the outer loop can tell whether sweeping again is worth it.
#
# IMPORTANT NUANCE discovered verifying the retry mechanism (2026-09-01, still true
# after switching from win-rate to these targets — this is about the DEDUP machinery,
# not what's being gated on): the cross-CALL "already tried" persistence
# (`db_load_tried_set`) only remembers combos `db_save` actually wrote — and `db_save`
# only writes combos that cleared the backtest QUALITY gates (`total_ret_pct>0`/
# `max_dd_pct`/`MIN_RR_RATIO` — the pre-existing hard simulation-level filter, distinct
# from the TARGET_* selection-level ones above). A combo that gets sampled and FAILS
# those gates is deduped correctly WITHIN the one optimize_symbol_interval call that
# drew it (`_gen_combos`'s in-memory `tried` set adds every sampled combo
# unconditionally), but is "forgotten" the moment that call ends, since the next call's
# `tried` set rebuilds from the DB and only gate-passing combos are in there. Directly
# verified: a deliberately tiny 8-distinct-combo space whose combos never happened to
# clear quality gates reported the SAME `new_combo_count=8` on every one of 5 repeated
# calls — it never converged toward 0, because each call "rediscovered" the same 8
# combos as new. For the real, enormous continuous PARAM_SPACE this is harmless — the
# search essentially never runs out of genuinely fresh 2-decimal-precision float
# combinations to try regardless, so `new_combo_count` will realistically stay near
# `N_RANDOM` (i.e. `2*N_RANDOM_PER_SOURCE`, one draw per entry source) for a very long
# time either way — but it does mean the "genuinely
# exhausted" stop condition is a much rarer, weaker signal in practice than "every combo
# has ever been tried" — it is closer to "every combo that has ever PASSED quality gates
# has been tried", which is consistent with (if anything reinforces) the explicit "just
# keep retrying, no time cap" intent this feature was built for, but is worth knowing if
# `new_combo_count` is ever used to reason about true exhaustion rather than just
# "should I bother sweeping again".

# Grid exit bounds (Grid fork — replaces the old fixed TP/SL + stochastic partial-exit).
# MAX_GRID_LEVELS is a hard cap used to size fixed-length arrays inside the numba hot
# path — must stay >= PARAM_SPACE["grid_levels"][1] below.
MAX_GRID_LEVELS = 8


def _bars_per_day(interval): return 24 * 60 // int(interval)
def _is_bars(interval): return IS_DAYS * _bars_per_day(interval)
def _oos_bars(interval, oos_hours=None):
    h = oos_hours if oos_hours is not None else max(OOS_HOURS_LIST)
    return h * 60 // int(interval)
def _bars_per_year(interval): return 365.25 * _bars_per_day(interval)
def _max_pages(interval):
    # +400 is the pre-existing indicator warm-up margin; GC_WARMUP_BARS (defined below,
    # after PARAM_SPACE) additionally covers ATR_PARTIAL's searched Gaussian Channel
    # settling time — referenced lazily since this function isn't called until well
    # after module load, so the later definition is already resolved by call time.
    needed = _is_bars(interval) + _oos_bars(interval) + 400 + GC_WARMUP_BARS
    return math.ceil(needed / FETCH_LIMIT) + 1


# ── Parameter space ───────────────────────────────────────────────────────────
# Single strategy (ATR_PARTIAL, Grid fork) — searched gc_period/gc_poles generate the
# entry signal; grid_levels/grid_dist_1..8/grid_frac_1..8 (grid_dist/grid_frac made
# per-level 2026-08-28, replacing a single shared grid_atr_mult/grid_level_frac — see
# grid_level_prices' docstring) define the exit: grid_levels ATR-multiple take-profit
# levels at cumulative distances built from each level's own independently-searched
# ATR increment, each closing that level's own independently-searched fraction of the
# ORIGINAL entry qty (the last level always closes whatever remains, guaranteeing full
# exit), with the stop trailed up to the previous filled level after each fill
# (breakeven after the first). stop_mult still sets the initial (pre-fill) stop
# distance.
PARAM_SPACE = {
    "k_len":          (10,  40),
    "k_smooth":       (1,   5),
    "d_smooth":       (3,   10),
    "ob":             (70,  90),
    "os":             (10,  30),
    "chop_len":       (8,   20),
    "chop_thr":       (38.0, 62.0),
    "atr_p":          (8,   20),
    "stop_mult":      (1.5, 6.0),
    "grid_levels":    (2,   MAX_GRID_LEVELS),
    "gc_period":      (50,  250),
    "gc_poles":       (1,   9),
}
# Per-level grid distance/fraction, one independently-searched pair per possible level
# slot 1..MAX_GRID_LEVELS (added 2026-08-28, explicit user ask — see grid_level_prices'
# docstring; replaces the old single grid_atr_mult/grid_level_frac shared across every
# level). Ranges are the same per-level bounds the old shared scalars used — this is a
# direct per-level generalization, not a re-tuned range. Only the first `grid_levels`
# slots of any sampled combo are ever read; slots beyond that are sampled but unused.
PARAM_SPACE.update({f"grid_dist_{i}": (0.3, 2.5) for i in range(1, MAX_GRID_LEVELS + 1)})
PARAM_SPACE.update({f"grid_frac_{i}": (0.1, 0.4) for i in range(1, MAX_GRID_LEVELS + 1)})
# flip_on_signal — added 2026-08-31 as a searched 0/1 toggle (explicit user ask,
# "reverse-and-flip"). FORCED ALWAYS-ON 2026-09-04, explicit user ask ("flip needs to
# be enabled!!!! in bt paper and live"), after this same conversation traced through a
# real locked pine combo's actual trades and found a genuine opposite-direction
# reversal signal occurred during 5 of its 6 trades (2 of which even cleared the full
# gated entry-grade condition flip itself requires) while flip sat disabled the whole
# time purely because the random search happened to draw 0 for this param on that
# particular combo. Range changed from (0, 1) to (1, 1) — `_sample`'s
# `random.randint(1, 1)` always returns 1, so every NEWLY sampled combo (both sources,
# since this lives in the base PARAM_SPACE inherited by PARAM_SPACE_SEARCHED) has flip
# on from now on; no special-casing needed anywhere else, same generic _INT_PARAMS
# handling as before. Does NOT retroactively touch already-saved param_runs/
# winning_params rows or the currently-locked combo's own JSON — see the same-day
# commit that hand-sets flip_on_signal=1 on the actual locked ETHUSDT/30/pine combo
# for that half of the fix.
PARAM_SPACE["flip_on_signal"] = (1, 1)
# trail_tp_mult — added 2026-09-04, explicit user ask ("i want grid and trailing tp.
# can you test and tell me results" -> "i want this built in"). A searched float, same
# treatment as stop_mult/grid_dist_i — NOT a hand-picked constant, since this
# conversation's own real-data comparisons showed the "best" trailing distance varies
# a lot run to run on a tiny sample (exactly the same overfitting risk stop_mult/
# grid_dist_i already exist to search around, rather than hardcode). Tracks the best
# price reached since entry (peak_price) and closes the ENTIRE remaining position once
# price retraces trail_tp_mult*entry_ATR from that peak — layered ON TOP of the
# existing grid TP-on-cross-up + cross-down-unwind mechanism, not replacing it: a
# direct real-data comparison (grid+unwind+trailing-TP vs. removing the grid's own
# up-cross TP-taking in favor of trailing-only) found keeping the grid's existing
# up-cross profit-taking AND adding this on top beat every simpler alternative tested.
# An exit-behavior param like stop_mult/flip_on_signal, so it lives in the base
# PARAM_SPACE (inherited as-is by PARAM_SPACE_SEARCHED, never widened) — "exit same as
# the bot" applies here too. Range 0.3-3.0 matches the multiples actually tested in
# this conversation's own comparisons (0.5x-3.0x ATR). Legacy combos saved before this
# feature existed fall back to 0.0 via params.get(), which the trail-check's own
# `trail_mult > 0.0` guard treats as "disabled" — reconstructing their exact old
# behavior, same fallback pattern flip_on_signal/grid_dist_i already use.
PARAM_SPACE["trail_tp_mult"] = (0.3, 3.0)
_INT_PARAMS = {"k_len", "k_smooth", "d_smooth", "ob", "os", "chop_len", "atr_p",
               "grid_levels", "gc_period", "gc_poles", "flip_on_signal"}

# "searched" gets a 3x wider entry-param search range than "pine" (added 2026-08-28,
# explicit user ask: "widen the param values search range x3 for searched"). Only the
# 9 entry-signal params widen (k_len/k_smooth/d_smooth/ob/os/chop_len/chop_thr/
# gc_period/gc_poles) — exit/grid params (atr_p/stop_mult/grid_levels/grid_dist_1..8/
# grid_frac_1..8) are NOT overridden below, they stay whatever's in PARAM_SPACE (the
# dict(PARAM_SPACE) copy inherits them as-is), so both sources keep searching the exit
# mechanism, including the per-level grid shape, over the exact same range (the "exit
# same as the bot" invariant from the pine feature is unaffected by this). Method for each
# widened bound, per explicit user choice: both bounds x3 (e.g. k_len 10-40 -> 30-120)
# EXCEPT ob and chop_thr, which are 0-100-bounded oscillator/chop-index levels where
# x3-both-bounds collapses to a degenerate 100-100 after clipping — those two instead
# use a centered triple-width (e.g. ob 70-90, width 20 -> center 80, 3x width 60 ->
# 50-110, clipped to the 0-100 ceiling -> 50-100).
PARAM_SPACE_SEARCHED = dict(PARAM_SPACE)
PARAM_SPACE_SEARCHED.update({
    "k_len":     (30,   120),
    "k_smooth":  (3,    15),
    "d_smooth":  (9,    30),
    "ob":        (50,   100),
    "os":        (30,   90),
    "chop_len":  (24,   60),
    "chop_thr":  (14.0, 86.0),
    "gc_period": (150,  750),
    "gc_poles":  (3,    27),
})
# The 9 entry-signal params that differ between the two sources' search ranges (every
# other PARAM_SPACE key is an exit/grid param, always searched identically for both) —
# same list "searched"'s wider range above widens, kept explicit here rather than
# re-derived so _sample_local's own list can't silently drift out of sync with it.
_ENTRY_PARAM_NAMES = ("k_len", "k_smooth", "d_smooth", "ob", "os",
                      "chop_len", "chop_thr", "gc_period", "gc_poles")

DEFAULT_PARAMS = {
    "k_len": 21, "k_smooth": 3, "d_smooth": 5,
    "ob": 80, "os": 20, "chop_len": 14, "chop_thr": 50.0,
    "atr_p": 14, "stop_mult": 3.5,
    "grid_levels": 4,
    "gc_period": 144, "gc_poles": 4,
    "flip_on_signal": 0,
}
# 1.0/0.25 per slot reproduces the old uniform grid_atr_mult=1.0/grid_level_frac=0.25
# default exactly (cumsum of 1.0s = the old li+1 multiple; every level closing the same
# 0.25 fraction is the old uniform behavior).
DEFAULT_PARAMS.update({f"grid_dist_{i}": 1.0 for i in range(1, MAX_GRID_LEVELS + 1)})
DEFAULT_PARAMS.update({f"grid_frac_{i}": 0.25 for i in range(1, MAX_GRID_LEVELS + 1)})

# "pine" entry source (added 2026-08-28, explicit user ask: "add it to the bt. entry
# logic. exit same as the bot"; revised same day: "pine needs to search params too" —
# the first version fixed k_len/k_smooth/d_smooth/ob/os/chop_len/chop_thr/gc_period/
# gc_poles to the Pine Script's own default values; that's gone now) — a faithful port
# of the user's "Stochastic Triple Filter [ATP]" Pine Script indicator's Gaussian
# Channel MATH specifically (stochastic K/D crossover in an oversold/overbought zone,
# filtered by GC direction and a Choppiness Index trending gate — same formula shape as
# "searched" in every other respect). PINE_GC_SQRT2 is the Pine script's own hardcoded
# 1.414 literal (see gaussian_channel_midline's sqrt2 param) — not math.sqrt(2) — for a
# bit-for-bit faithful port of that one constant, not just an equivalent formula. Every
# other entry param (k_len/k_smooth/d_smooth/ob/os/chop_len/chop_thr/gc_period/
# gc_poles) is backtester-searched for both sources — "pine" from PARAM_SPACE, "searched"
# from the 3x-wider PARAM_SPACE_SEARCHED (added 2026-08-28, see that dict's own comment)
# — so as of the widening, the two sources differ in BOTH the entry-param search range
# AND the GC sqrt2 constant, not the constant alone. The exit side (stop_mult/
# grid_levels/grid_dist_1..8/grid_frac_1..8/atr_p) was never part of either difference —
# both sources always search/optimize the same grid-exit params over the same range.
PINE_GC_SQRT2 = 1.414

# ATR_PARTIAL's Gaussian Channel is a searched param (gc_period up to 250 for "pine",
# 750 for "searched" — see PARAM_SPACE/PARAM_SPACE_SEARCHED) recomputed fresh on
# whatever bar array _bt_combo_pair receives, so without extra bars before the IS/OOS
# window it cold-starts at the very first bar of the IS window, which can be shorter
# than the filter's own settling time for a large gc_period — biasing which params look
# like 100%-win-rate winners. 3x the max searchable gc_period across BOTH sources'
# spaces is a generous settling margin for this recursive filter.
GC_WARMUP_BARS = 3 * max(PARAM_SPACE["gc_period"][1], PARAM_SPACE_SEARCHED["gc_period"][1])

DB_PATH = os.path.join(DATA_DIR, "eth_trader_params.db")

# Locked combos — added 2026-09-03, explicit user ask ("make the pine use that one
# winning param please. i want that one to always be tested" -> "yeah lock it in"),
# redefined 2026-09-04 from a full-param lock to an ENTRY-ONLY lock (explicit user ask,
# "only the entry params for pine should be fixed. everything else in the strat is
# param tested" — see load_locked_entry's docstring for the incident that motivated
# it: the original full lock froze a 5.22x-ATR stop that was far wider than any of its
# own winning trades ever needed), then loosened again the same day (explicit user
# ask, "for entry params it is allowed to fine tune the base params in pine. fine tune
# only"): a locked (symbol, interval, src)'s entry signal is FINE-TUNED, not frozen —
# every combo's 9 entry params are jittered around the locked dict's values via the
# same radius-based mechanism _sample_local already uses for pine's refine-around-
# searched anchor (see optimize_symbol_interval's locked_entry_by_src handling), never
# resampled independently from the full PARAM_SPACE range. Every exit-side param
# (atr_p/stop_mult/grid_levels/grid_dist_i/grid_frac_i/flip_on_signal/trail_tp_mult)
# is genuinely searched every cycle, same random budget as an unlocked source. It
# still goes through the REAL IS sweep + OOS retest + result-file write every cycle
# same as any other combo, so its stats stay honestly fresh against current market
# data — the difference from normal operation is only that the entry signal can never
# drift far from this fixed base while locked. If nothing under this entry signal
# produces a valid IS/OOS result on a given cycle's data (e.g. a data hiccup), that
# cycle simply skips writing for that source — the previous file is left untouched,
# never silently replaced by an unrelated combo. Plain JSON file, not a DB table,
# since this is a rare, manually-set override rather app-generated state: edit or
# delete a key to unlock. Independent of `_protected_source` (open-position freeze,
# eth_trader.py) — a locked source that also happens to have an open
# position is simply both at once, no conflict (protection still skips the OOS
# retest/write step same as always, locking only changes what's fed into the sweep
# ahead of that check).
LOCKED_COMBOS_PATH = os.path.join(DATA_DIR, "locked_combos.json")

def load_locked_combo(symbol, interval, src):
    """Returns the locked params dict for this (symbol, interval, src), or None if
    nothing is locked for it (including if the file doesn't exist or is malformed —
    fails open to normal search behavior, never raises)."""
    try:
        with open(LOCKED_COMBOS_PATH) as f:
            d = json.load(f)
    except Exception:
        return None
    return d.get(f"{symbol}_{interval}_{src}")


def load_locked_entry(symbol, interval, src):
    """Returns just the ENTRY-signal params (_ENTRY_PARAM_NAMES — the same 9-param
    tuple _sample_local already jitters for pine's refine-around-searched anchor) from
    this (symbol, interval, src)'s locked_combos.json entry, or None if nothing is
    locked for it. Redefined 2026-09-04 (explicit user ask: "only the entry params for
    pine should be fixed. everything else in the strat is param tested") from an
    earlier, same-day version that pinned EVERY param verbatim (added after tracing the
    locked ETHUSDT/30/pine combo's one big loss — a short that moved favorably to
    -1.10x its own entry ATR, round-tripped back through breakeven, then reversed hard,
    never reaching grid_dist_1 to bank anything — and finding the combo's 5.22x-ATR
    stop was far wider than any of its own winning trades ever needed). Exit-side
    params (atr_p/stop_mult/grid_levels/grid_dist_i/grid_frac_i/flip_on_signal/
    trail_tp_mult) are genuinely searched like any unlocked combo, always paired with
    this entry signal. Same-day follow-up ("for entry params it is allowed to fine
    tune the base params in pine. fine tune only"): the entry signal itself is no
    longer frozen to one exact value either — see optimize_symbol_interval's
    locked_entry_by_src handling, which jitters it via _sample_local's existing
    radius-based mechanism rather than pinning it byte-for-byte."""
    full = load_locked_combo(symbol, interval, src)
    if full is None:
        return None
    return {k: full[k] for k in _ENTRY_PARAM_NAMES if k in full}


# ── Logging ───────────────────────────────────────────────────────────────────
_log = logging.getLogger("eth_trader_bt")
_log.setLevel(logging.DEBUG)
_log.propagate = False
_fh = RotatingFileHandler(os.path.join(DATA_DIR, "eth_trader_bt.log"),
                          maxBytes=5*1024*1024, backupCount=3, encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                                    datefmt="%Y-%m-%d %H:%M:%S"))
_log.addHandler(_fh)


# ── Indicators (module-level for worker pickling) ─────────────────────────────
def _sma(arr, p):
    return pd.Series(arr).rolling(p, min_periods=p).mean().values

def _atr_wilder(hi, lo, cl, p):
    tr = np.empty(len(cl))
    tr[0] = hi[0] - lo[0]
    tr[1:] = np.maximum(hi[1:]-lo[1:], np.maximum(abs(hi[1:]-cl[:-1]), abs(lo[1:]-cl[:-1])))
    return pd.Series(tr).ewm(alpha=1/p, adjust=False).mean().values

def _stoch_raw_k(hi, lo, cl, k_len):
    hh = pd.Series(hi).rolling(k_len, min_periods=k_len).max().values
    ll = pd.Series(lo).rolling(k_len, min_periods=k_len).min().values
    d = hh - ll
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.where(d == 0, 50.0, 100*(cl-ll)/d)
    r[np.isnan(hh)] = np.nan; return r

def _chop_index(hi, lo, cl, p):
    tr = np.empty(len(cl))
    tr[0] = hi[0]-lo[0]
    tr[1:] = np.maximum(hi[1:]-lo[1:], np.maximum(abs(hi[1:]-cl[:-1]), abs(lo[1:]-cl[:-1])))
    s  = pd.Series(tr).rolling(p, min_periods=p).sum().values
    hh = pd.Series(hi).rolling(p, min_periods=p).max().values
    ll = pd.Series(lo).rolling(p, min_periods=p).min().values
    rng = hh - ll
    with np.errstate(divide="ignore", invalid="ignore"):
        ci = np.where(rng == 0, 100.0,
             100*np.log10(np.maximum(s, 1e-10)/np.maximum(rng, 1e-10))/math.log10(p))
    ci[np.isnan(hh)] = np.nan; return ci

def _gc_filt9x(alpha, src, poles):
    """This directly implements the Pine Script Gaussian filter's binomial-expansion
    recursion — a high-order IIR filter whose feedback coefficients (comb(poles,k), up
    to comb(27,13)~2.7e7 since `PARAM_SPACE_SEARCHED` allows poles up to 27) can make the
    recursion numerically unstable for some (period, poles) combinations: observed
    directly on this codebase — RuntimeWarning: overflow/invalid value firing repeatedly,
    and a real saved result file whose cagr_pct came back at 2.68e24, traced to this
    filter blowing up for that candidate's gc_poles=18. Because this is a feedback loop,
    once a step goes far outside the input series' own scale, every later step is built
    from that corrupted value too — so instead of letting it run to inf/nan on its own
    (which can briefly pass through large-but-finite values first, capable of producing
    spurious trend-direction crossings before the blowup fully resolves to NaN), this
    detects the instability at the step it starts and marks every remaining step NaN
    immediately — comparisons against NaN are always False, so a candidate whose filter
    goes unstable cleanly produces zero entry signals from that point on (naturally
    excluded downstream by MIN_TRADES) rather than a handful of bogus ones."""
    n = len(src); x = 1-alpha; a_i = alpha**poles
    c = [(-1)**(k+1)*comb(poles,k)*(x**k) for k in range(1, poles+1)]
    f = np.empty(n)
    for i in range(min(poles, n)): f[i] = src[i]
    src_hi = float(np.max(np.abs(src))) if n else 0.0
    bound = max(src_hi, 1.0) * 1e6
    blown = False
    with np.errstate(over="ignore", invalid="ignore"):
        for t in range(poles, n):
            if blown:
                f[t] = np.nan
                continue
            v = a_i*src[t]
            for ki, co in enumerate(c, 1): v += co*f[t-ki]
            if not (v == v) or abs(v) > bound:  # `v == v` is False only for NaN
                blown = True
                f[t] = np.nan
            else:
                f[t] = v
    return f

def gaussian_channel_midline(hi, lo, cl, period, poles, sqrt2=None):
    """sqrt2 (added 2026-08-28, "pine" entry source): the "Stochastic Triple Filter
    [ATP]" Pine Script hardcodes the literal 1.414 (a 4-significant-figure truncation
    of √2) in the GC pole-width formula's denominator, rather than computing it
    exactly. Passing sqrt2=PINE_GC_SQRT2 (1.414) here reproduces that Pine script's
    formula; the default (None -> math.sqrt(2)) is the mathematically exact value used
    by the "searched" entry source. This is the ONLY difference between the two entry
    sources — every other param (k_len, gc_period, gc_poles, etc.) is searched by the
    backtester's random sweep for both sources alike."""
    hlc3 = (hi+lo+cl)/3
    base = sqrt2 if sqrt2 is not None else math.sqrt(2)
    beta = (1-math.cos(2*math.pi/period))/(base**(2/poles)-1)
    alpha = -beta + math.sqrt(beta**2 + 2*beta)
    fn = _gc_filt9x(alpha, hlc3, poles)
    return fn


def grid_level_prices(entry_price, atr, side, levels, grid_dists):
    """Shared by _bt_combo_pair's pure-Python grid loop and eth_trader.py's
    live tick()/position-seeding grid construction (added 2026-08-28, explicit user
    ask: "test number of grids is optimal in bt. and where they should be set") — a
    single implementation both call, instead of each hand-duplicating the cumulative-
    distance formula, so live and backtest can never silently diverge on how grid
    prices are built (the JIT hot path in _sim_grid_jit below still inlines its own
    copy of this same formula by hand — numba nopython mode can't call back into plain
    Python functions like this one — see that function's own header comment for the
    "keep both in sync by hand" note that still applies to JIT vs. this function).
    side: +1 for a long position (levels laid out above entry), -1 for short (below).
    grid_dists: per-level ATR-multiple INCREMENT from the previous level's cumulative
    distance, not an absolute distance — level li's price is entry_price +
    side*atr*sum(grid_dists[0..li])."""
    prices = []
    cum = 0.0
    for li in range(levels):
        cum += grid_dists[li]
        prices.append(entry_price + side * atr * cum)
    return prices


# ── JIT-compiled hot-path simulation loop ──────────────────────────────────────
# Numeric twin of the pure-Python grid loop in _bt_combo_pair below — same logic, same
# operation order, no Python objects inside the loop (numba nopython requirement). Only
# used on the record_entries=False hot path; the record_entries=True path (rare, small
# windows, used only by replay_recent_trades for the missed-trade report) keeps the
# original pure-Python loop since numba doesn't handle the (idx, "long"/"short")
# tuple-list append cleanly and that path doesn't need the speed.
#
# Grid exit (Grid fork, replaces the old single fixed TP + stochastic-triggered 50%
# partial): on entry, `levels` ATR-multiple take-profit prices are laid out at
# entry ± atr*cumsum(grid_dists[0..levels-1]) — each level's distance is the running
# total of independently-searched per-level ATR increments (added 2026-08-28, explicit
# user ask: "test number of grids is optimal in bt. and where they should be set" +
# independent-fraction follow-up), not a single uniform grid_mult*  (li+1) any more.
# Cumulative construction guarantees levels are monotonically farther from entry as li
# increases regardless of what each individual increment samples to — required for the
# sequential fill-scanning loop below (and _next_grid_hit's live-trader mirror) to stay
# correct, since both walk lvl_px assuming later indices are always farther out. Each
# level closes grid_fracs[li] of the ORIGINAL entry qty (qty0) except the last, which
# always closes whatever remains — guaranteeing full exit regardless of rounding. The
# stop trails to breakeven after the first fill, then to the previous filled level's
# price after each subsequent fill, so profit already banked at a lower level can never
# be given back once a higher one fills. Multiple levels can fill within one bar (a gap
# candle) — the inner while loop walks through them sequentially, same as a human
# reading the chart bar-by-bar would. The stop-loss check/formula above is completely
# UNCHANGED by the cross-down TP-capture below — added 2026-08-28, explicit user ask
# ("i want it to close on a cross down the grid" / "the stop loss stays the same as
# original. this is purely capturing TP"). Separately from the stop, if price crosses
# back STRICTLY BELOW an already-filled-but-not-yet-unwound level, that level's own
# grid_fracs[i] of CURRENT remaining qty closes — a fresh partial close (not "undoing"
# the earlier fill; that qty/profit is already banked). `unwound[i]` tracks which
# levels have used their one-time down-close; a new up-fill doesn't reset any of it,
# it just means the new top index becomes eligible too. Scanning from the current top
# filled index downward and stopping at the first level whose price cl_i is NOT below
# is correct without an explicit "stop below sl" special case: since lvl_px is
# monotonically increasing with index (see grid_level_prices), and this whole block
# only runs when cl_i > sl already (the sl branch above returns first), the scan
# self-terminates at whichever level sl's own formula happens to reuse — in practice
# this means at most one level unwinds before the (unchanged) stop-loss takes over,
# since sl always sits exactly at the next level down. Strict `<` (not `<=`) means the
# level that JUST filled this same bar (price sitting AT or ABOVE it) can never also
# immediately "unwind" in the same check.
@njit(cache=True)
def _sim_grid_jit(cl, atr_arr, bad, buy, sell, start_i, n, lev, equity_base,
                   stop_m, levels, grid_dists, grid_fracs, flip_on, trail_mult):
    # WIN_FEE_MULT (explicit user ask, "tiny trades should never be counted as
    # wins"): a trade only counts toward win_rate/gw/mw if part_pnl clears its own
    # round-trip fees by this multiple — a trade that merely edged out what it paid
    # in fees isn't a real edge, it's noise. fees (entry fee0 + every exit ef/ef_i
    # paid across all legs) is tracked separately from part_pnl, which already nets
    # fees out — so this is a check against a SEPARATE running total, not a second
    # subtraction. MIN_WIN_PRICE_PCT (explicit user ask, "only params where each
    # trade is no less than .33% before leverage profit makes it through"): a win
    # ALSO needs the whole trade's avg-exit-vs-entry raw price move to clear this
    # floor — exit_notional accumulates cl_i*qty_i across every exit/unwind leg of
    # the current trade so exit_notional/qty0 gives the qty-weighted average exit
    # price at close time, regardless of how many grid levels fired. `t`/`gl`/`ml`/
    # `th` (trade count, loss stats, hold time) are unaffected by either check: a
    # trade that fails either bar still counts as a trade, just not a win, and its
    # part_pnl (even if nominally positive) flows into gl/ml like any other non-win.
    # zf (explicit user ask, "how do we make this profitable instead" re: entries that
    # reverse straight to the stop without ever filling a single grid level — real
    # ETHUSDT data showed ~20% of entries do this): counts trades that closed (by any
    # reason — SL, forced end-of-data) with `filled` still 0, i.e. price never even
    # reached the first grid level before the position closed. Checked at every close
    # site alongside `t += 1`, not just the SL site — a trade forced closed at the end
    # of the data window with filled==0 never banked anything either, same failure mode.
    # zero_fill_rate = zf/trades then multiplies into `score` below (shared tail code),
    # so the search is pushed toward params that either place grid_dist_1 close enough
    # to entry to bank something before reversing, or have cleaner entries that reverse
    # less often — whichever the data actually rewards — instead of only ever being
    # judged on the overall equity curve's risk-adjusted return.
    # flip_on (params["flip_on_signal"], a new searched 0/1 param — explicit user ask,
    # "reverse-and-flip": if the entry signal flips against an open position before the
    # stop is hit, close it and immediately open the opposite side instead of just
    # waiting for the ATR-distance stop to eventually trigger — the same move that would
    # have stopped out the old position becomes the entry for the new one. Checked ONLY
    # after the stop-loss check already came back false each bar (SL always wins if both
    # would trigger the same bar) and BEFORE the grid-fill/unwind checks (a flip signal
    # means the entry thesis is already invalidated, so there's no point evaluating
    # whether this bar also crossed a grid level for the position about to close). Fires
    # on ANY open position regardless of fill state, reusing the exact same buy/sell
    # signal arrays the entry logic already computes — no new indicator. It's a searched
    # 0/1 param specifically so the sweep can decide per (symbol, interval, entry_source)
    # whether flipping actually helps, rather than assuming it always does.
    # trail_mult (params["trail_tp_mult"], added 2026-09-04, explicit user ask: "i want
    # grid and trailing tp" -- built on top of the grid+unwind mechanism rather than
    # replacing it, per this same conversation's own real-data comparison, which found
    # grid+unwind+trailing-TP beat every simpler alternative tested, including removing
    # the grid's own up-cross TP-taking in favor of trailing-only). Tracks peak_price
    # (the best price reached since entry, updated every bar) and closes the ENTIRE
    # remaining position once price retraces trail_mult*entry_atr from that peak --
    # entry_atr is the ATR at entry time (fixed for the trade's life, same convention
    # grid_px/sl already use), not the current bar's ATR. Checked AFTER the stop-loss
    # and flip checks (SL/flip always win if either also triggers the same bar) and
    # BEFORE the grid-fill/unwind checks -- a trailing-TP exit means the whole position
    # is closing regardless of whether this bar also crossed a grid level. Guarded by
    # `trail_mult > 0.0` so a legacy combo saved before this feature existed (which
    # falls back to 0.0 via params.get) reconstructs its exact old behavior --
    # trail_mult=0.0 can never satisfy `retraced >= 0.0*entry_atr` as a genuine gate
    # because the guard short-circuits first, same pattern as flip_on's 0/1 gate.
    # `peak_price > ep` (long) / `peak_price < ep` (short) guards against triggering on
    # a position that has never actually been in profit -- only a genuine retracement
    # FROM a favorable extreme counts, matching "trailing TAKE-PROFIT", not a second
    # stop-loss.
    eq = equity_base
    in_long = False; in_short = False
    ep = 0.0; sl = 0.0; qty0 = 0.0; qty_rem = 0.0; fee_rem = 0.0
    ei = 0; filled = 0; part_pnl = 0.0; fees = 0.0; exit_notional = 0.0
    entry_atr = 0.0; peak_price = 0.0
    t = 0; w = 0; gw = 0.0; gl = 0.0; th = 0.0
    mw = 0.0; ml = 0.0; zf = 0
    nb = n - start_i
    curve = np.empty(nb)
    lvl_px = np.empty(MAX_GRID_LEVELS)
    unwound = np.zeros(MAX_GRID_LEVELS, dtype=np.bool_)

    for idx in range(start_i, n):
        ii = idx - start_i
        if bad[idx]:
            curve[ii] = eq
            continue
        cl_i = cl[idx]

        if not (in_long or in_short):
            if buy[idx]:
                ntl = eq * lev * MARGIN_HEADROOM
                qty0 = ntl / cl_i; fee0 = ntl * TAKER_FEE; eq -= fee0
                ep = cl_i; sl = cl_i - atr_arr[idx] * stop_m
                entry_atr = atr_arr[idx]; peak_price = cl_i
                cum = 0.0
                for li in range(levels):
                    cum += grid_dists[li]
                    lvl_px[li] = cl_i + atr_arr[idx] * cum
                qty_rem = qty0; fee_rem = fee0; filled = 0; part_pnl = 0.0; fees = fee0
                exit_notional = 0.0
                for li in range(MAX_GRID_LEVELS): unwound[li] = False
                in_long = True; ei = idx
            elif sell[idx]:
                ntl = eq * lev * MARGIN_HEADROOM
                qty0 = ntl / cl_i; fee0 = ntl * TAKER_FEE; eq -= fee0
                ep = cl_i; sl = cl_i + atr_arr[idx] * stop_m
                entry_atr = atr_arr[idx]; peak_price = cl_i
                cum = 0.0
                for li in range(levels):
                    cum += grid_dists[li]
                    lvl_px[li] = cl_i - atr_arr[idx] * cum
                qty_rem = qty0; fee_rem = fee0; filled = 0; part_pnl = 0.0; fees = fee0
                exit_notional = 0.0
                for li in range(MAX_GRID_LEVELS): unwound[li] = False
                in_short = True; ei = idx
        elif in_long:
            if cl_i > peak_price: peak_price = cl_i
            if cl_i <= sl:
                ef = cl_i * qty_rem * TAKER_FEE; pnl_i = (cl_i - ep) * qty_rem - ef
                eq += pnl_i; part_pnl += pnl_i - fee_rem; fees += ef
                exit_notional += cl_i * qty_rem
                t += 1; th += idx - ei
                if filled == 0: zf += 1
                raw_pct = (exit_notional / qty0 - ep) / ep
                if part_pnl > WIN_FEE_MULT * fees and raw_pct >= MIN_WIN_PRICE_PCT:
                    w += 1; gw += part_pnl
                    if part_pnl > mw: mw = part_pnl
                else:
                    gl += abs(part_pnl)
                    if abs(part_pnl) > ml: ml = abs(part_pnl)
                in_long = False
            elif flip_on and sell[idx]:
                ef = cl_i * qty_rem * TAKER_FEE; pnl_i = (cl_i - ep) * qty_rem - ef
                eq += pnl_i; part_pnl += pnl_i - fee_rem; fees += ef
                exit_notional += cl_i * qty_rem
                t += 1; th += idx - ei
                if filled == 0: zf += 1
                raw_pct = (exit_notional / qty0 - ep) / ep
                if part_pnl > WIN_FEE_MULT * fees and raw_pct >= MIN_WIN_PRICE_PCT:
                    w += 1; gw += part_pnl
                    if part_pnl > mw: mw = part_pnl
                else:
                    gl += abs(part_pnl)
                    if abs(part_pnl) > ml: ml = abs(part_pnl)
                ntl = eq * lev * MARGIN_HEADROOM
                qty0 = ntl / cl_i; fee0 = ntl * TAKER_FEE; eq -= fee0
                ep = cl_i; sl = cl_i + atr_arr[idx] * stop_m
                entry_atr = atr_arr[idx]; peak_price = cl_i
                cum = 0.0
                for li in range(levels):
                    cum += grid_dists[li]
                    lvl_px[li] = cl_i - atr_arr[idx] * cum
                qty_rem = qty0; fee_rem = fee0; filled = 0; part_pnl = 0.0; fees = fee0
                exit_notional = 0.0
                for li in range(MAX_GRID_LEVELS): unwound[li] = False
                in_long = False; in_short = True; ei = idx
            elif trail_mult > 0.0 and peak_price > ep and (peak_price - cl_i) >= trail_mult * entry_atr:
                ef = cl_i * qty_rem * TAKER_FEE; pnl_i = (cl_i - ep) * qty_rem - ef
                eq += pnl_i; part_pnl += pnl_i - fee_rem; fees += ef
                exit_notional += cl_i * qty_rem
                t += 1; th += idx - ei
                if filled == 0: zf += 1
                raw_pct = (exit_notional / qty0 - ep) / ep
                if part_pnl > WIN_FEE_MULT * fees and raw_pct >= MIN_WIN_PRICE_PCT:
                    w += 1; gw += part_pnl
                    if part_pnl > mw: mw = part_pnl
                else:
                    gl += abs(part_pnl)
                    if abs(part_pnl) > ml: ml = abs(part_pnl)
                in_long = False
            else:
                while filled < levels and cl_i >= lvl_px[filled]:
                    qty_i = qty_rem if filled == levels - 1 else min(qty0 * grid_fracs[filled], qty_rem)
                    ef_i = cl_i * qty_i * TAKER_FEE; pnl_i = (cl_i - ep) * qty_i - ef_i
                    entry_fee_i = fee_rem * (qty_i / qty_rem)
                    eq += pnl_i; part_pnl += pnl_i - entry_fee_i; fees += ef_i
                    exit_notional += cl_i * qty_i
                    fee_rem -= entry_fee_i; qty_rem -= qty_i; filled += 1
                    sl = ep if filled == 1 else lvl_px[filled - 2]
                    if qty_rem <= 1e-12 or filled >= levels:
                        t += 1; th += idx - ei
                        if filled == 0: zf += 1
                        raw_pct = (exit_notional / qty0 - ep) / ep
                        if part_pnl > WIN_FEE_MULT * fees and raw_pct >= MIN_WIN_PRICE_PCT:
                            w += 1; gw += part_pnl
                            if part_pnl > mw: mw = part_pnl
                        else:
                            gl += abs(part_pnl)
                            if abs(part_pnl) > ml: ml = abs(part_pnl)
                        in_long = False
                        break
                if in_long:
                    ui = filled - 1
                    while ui >= 0 and cl_i < lvl_px[ui]:
                        if not unwound[ui]:
                            qty_i = min(qty0 * grid_fracs[ui], qty_rem)
                            ef_i = cl_i * qty_i * TAKER_FEE; pnl_i = (cl_i - ep) * qty_i - ef_i
                            entry_fee_i = fee_rem * (qty_i / qty_rem)
                            eq += pnl_i; part_pnl += pnl_i - entry_fee_i; fees += ef_i
                            exit_notional += cl_i * qty_i
                            fee_rem -= entry_fee_i; qty_rem -= qty_i
                            unwound[ui] = True
                            if qty_rem <= 1e-12:
                                t += 1; th += idx - ei
                                if filled == 0: zf += 1
                                raw_pct = (exit_notional / qty0 - ep) / ep
                                if part_pnl > WIN_FEE_MULT * fees and raw_pct >= MIN_WIN_PRICE_PCT:
                                    w += 1; gw += part_pnl
                                    if part_pnl > mw: mw = part_pnl
                                else:
                                    gl += abs(part_pnl)
                                    if abs(part_pnl) > ml: ml = abs(part_pnl)
                                in_long = False
                                break
                        ui -= 1
        elif in_short:
            if cl_i < peak_price: peak_price = cl_i
            if cl_i >= sl:
                ef = cl_i * qty_rem * TAKER_FEE; pnl_i = (ep - cl_i) * qty_rem - ef
                eq += pnl_i; part_pnl += pnl_i - fee_rem; fees += ef
                exit_notional += cl_i * qty_rem
                t += 1; th += idx - ei
                if filled == 0: zf += 1
                raw_pct = (ep - exit_notional / qty0) / ep
                if part_pnl > WIN_FEE_MULT * fees and raw_pct >= MIN_WIN_PRICE_PCT:
                    w += 1; gw += part_pnl
                    if part_pnl > mw: mw = part_pnl
                else:
                    gl += abs(part_pnl)
                    if abs(part_pnl) > ml: ml = abs(part_pnl)
                in_short = False
            elif flip_on and buy[idx]:
                ef = cl_i * qty_rem * TAKER_FEE; pnl_i = (ep - cl_i) * qty_rem - ef
                eq += pnl_i; part_pnl += pnl_i - fee_rem; fees += ef
                exit_notional += cl_i * qty_rem
                t += 1; th += idx - ei
                if filled == 0: zf += 1
                raw_pct = (ep - exit_notional / qty0) / ep
                if part_pnl > WIN_FEE_MULT * fees and raw_pct >= MIN_WIN_PRICE_PCT:
                    w += 1; gw += part_pnl
                    if part_pnl > mw: mw = part_pnl
                else:
                    gl += abs(part_pnl)
                    if abs(part_pnl) > ml: ml = abs(part_pnl)
                ntl = eq * lev * MARGIN_HEADROOM
                qty0 = ntl / cl_i; fee0 = ntl * TAKER_FEE; eq -= fee0
                ep = cl_i; sl = cl_i - atr_arr[idx] * stop_m
                entry_atr = atr_arr[idx]; peak_price = cl_i
                cum = 0.0
                for li in range(levels):
                    cum += grid_dists[li]
                    lvl_px[li] = cl_i + atr_arr[idx] * cum
                qty_rem = qty0; fee_rem = fee0; filled = 0; part_pnl = 0.0; fees = fee0
                exit_notional = 0.0
                for li in range(MAX_GRID_LEVELS): unwound[li] = False
                in_short = False; in_long = True; ei = idx
            elif trail_mult > 0.0 and peak_price < ep and (cl_i - peak_price) >= trail_mult * entry_atr:
                ef = cl_i * qty_rem * TAKER_FEE; pnl_i = (ep - cl_i) * qty_rem - ef
                eq += pnl_i; part_pnl += pnl_i - fee_rem; fees += ef
                exit_notional += cl_i * qty_rem
                t += 1; th += idx - ei
                if filled == 0: zf += 1
                raw_pct = (ep - exit_notional / qty0) / ep
                if part_pnl > WIN_FEE_MULT * fees and raw_pct >= MIN_WIN_PRICE_PCT:
                    w += 1; gw += part_pnl
                    if part_pnl > mw: mw = part_pnl
                else:
                    gl += abs(part_pnl)
                    if abs(part_pnl) > ml: ml = abs(part_pnl)
                in_short = False
            else:
                while filled < levels and cl_i <= lvl_px[filled]:
                    qty_i = qty_rem if filled == levels - 1 else min(qty0 * grid_fracs[filled], qty_rem)
                    ef_i = cl_i * qty_i * TAKER_FEE; pnl_i = (ep - cl_i) * qty_i - ef_i
                    entry_fee_i = fee_rem * (qty_i / qty_rem)
                    eq += pnl_i; part_pnl += pnl_i - entry_fee_i; fees += ef_i
                    exit_notional += cl_i * qty_i
                    fee_rem -= entry_fee_i; qty_rem -= qty_i; filled += 1
                    sl = ep if filled == 1 else lvl_px[filled - 2]
                    if qty_rem <= 1e-12 or filled >= levels:
                        t += 1; th += idx - ei
                        if filled == 0: zf += 1
                        raw_pct = (ep - exit_notional / qty0) / ep
                        if part_pnl > WIN_FEE_MULT * fees and raw_pct >= MIN_WIN_PRICE_PCT:
                            w += 1; gw += part_pnl
                            if part_pnl > mw: mw = part_pnl
                        else:
                            gl += abs(part_pnl)
                            if abs(part_pnl) > ml: ml = abs(part_pnl)
                        in_short = False
                        break
                if in_short:
                    ui = filled - 1
                    while ui >= 0 and cl_i > lvl_px[ui]:
                        if not unwound[ui]:
                            qty_i = min(qty0 * grid_fracs[ui], qty_rem)
                            ef_i = cl_i * qty_i * TAKER_FEE; pnl_i = (ep - cl_i) * qty_i - ef_i
                            entry_fee_i = fee_rem * (qty_i / qty_rem)
                            eq += pnl_i; part_pnl += pnl_i - entry_fee_i; fees += ef_i
                            exit_notional += cl_i * qty_i
                            fee_rem -= entry_fee_i; qty_rem -= qty_i
                            unwound[ui] = True
                            if qty_rem <= 1e-12:
                                t += 1; th += idx - ei
                                if filled == 0: zf += 1
                                raw_pct = (ep - exit_notional / qty0) / ep
                                if part_pnl > WIN_FEE_MULT * fees and raw_pct >= MIN_WIN_PRICE_PCT:
                                    w += 1; gw += part_pnl
                                    if part_pnl > mw: mw = part_pnl
                                else:
                                    gl += abs(part_pnl)
                                    if abs(part_pnl) > ml: ml = abs(part_pnl)
                                in_short = False
                                break
                        ui -= 1

        if in_long:
            curve[ii] = eq + (cl_i - ep) * qty_rem
        elif in_short:
            curve[ii] = eq + (ep - cl_i) * qty_rem
        else:
            curve[ii] = eq

    if in_long or in_short:
        cl_i = cl[n - 1]
        ef = cl_i * qty_rem * TAKER_FEE
        if in_long:
            pnl_i = (cl_i - ep) * qty_rem - ef
        else:
            pnl_i = (ep - cl_i) * qty_rem - ef
        eq += pnl_i; part_pnl += pnl_i - fee_rem; fees += ef
        exit_notional += cl_i * qty_rem
        t += 1; th += (n - 1) - ei
        if filled == 0: zf += 1
        raw_pct = (exit_notional / qty0 - ep) / ep if in_long else (ep - exit_notional / qty0) / ep
        if part_pnl > WIN_FEE_MULT * fees and raw_pct >= MIN_WIN_PRICE_PCT:
            w += 1; gw += part_pnl
            if part_pnl > mw: mw = part_pnl
        else:
            gl += abs(part_pnl)
            if abs(part_pnl) > ml: ml = abs(part_pnl)

    return eq, t, w, gw, gl, th, mw, ml, zf, curve


def warm_up_jit():
    """Compiles `_sim_grid_jit` once, single-threaded, in the calling process.

    `@njit(cache=True)` writes its compiled machine code to an on-disk cache file
    (in `__pycache__`) the first time it's actually called with a given argument-type
    signature. `ProcessPoolExecutor` workers each import this module fresh (spawn, not
    fork) and the first sweep after a cold start calls `_sim_grid_jit` from every worker
    at roughly the same moment — several processes racing to compile-and-write the same
    cache file concurrently, which can corrupt it and crash whichever worker loses the
    race (observed directly: a `BrokenProcessPool` during a cold-cache first cycle on
    this machine, self-healed by the existing pool-recreate-and-continue path, but never
    recurring afterward once the cache file already existed). Calling this once here,
    before any worker pool is created, means the cache is already written by the time
    workers spawn — they only ever read it, never race to write it. No-op if numba isn't
    available (falls back to the plain-Python `njit` no-op decorator, nothing to warm)."""
    if not _NUMBA_OK:
        return
    n = 40
    cl = np.linspace(100.0, 101.0, n)
    atr_arr = np.full(n, 1.0)
    bad = np.zeros(n, dtype=np.bool_)
    buy = np.zeros(n, dtype=np.bool_); buy[5] = True
    sell = np.zeros(n, dtype=np.bool_); sell[25] = True
    grid_dists = np.full(MAX_GRID_LEVELS, 1.0)
    grid_fracs = np.full(MAX_GRID_LEVELS, 1.0 / MAX_GRID_LEVELS)
    _sim_grid_jit(cl, atr_arr, bad, buy, sell, 0, n, 11, 100.0,
                  1.5, MAX_GRID_LEVELS, grid_dists, grid_fracs, 0, 1.0)


# ── Combo inner BT (top-level for pickling) ───────────────────────────────────
def _bt_combo_pair(params, hi, lo, cl,
                   backtest_bars, bpy, lev=LEVERAGE, record_entries=False,
                   initial_equity=None, entry_source="searched"):
    """Single-strategy ATR_PARTIAL + grid-exit simulation (Grid fork — the STOP shadow
    strategy is gone entirely). entry_source (added 2026-08-28, explicit user ask —
    see PINE_GC_SQRT2's docstring; revised same day — "pine needs to search params
    too") chooses which Gaussian Channel formula this combo's entry signal uses:
    - "searched" (default): math.sqrt(2), full precision.
    - "pine": the Pine Script's own hardcoded 1.414 literal (see
      gaussian_channel_midline's sqrt2 param), for bit-for-bit fidelity to the
      "Stochastic Triple Filter [ATP]" indicator's actual math.
    Every entry-signal param (k_len/k_smooth/d_smooth/ob/os/chop_len/chop_thr/
    gc_period/gc_poles) always comes from `params`, searched by the random sweep, for
    BOTH sources — as does the exit side (stop_mult/grid_levels/grid_dist_1..8/
    grid_frac_1..8/atr_p). entry_source now only ever changes that one GC constant.

    initial_equity: explicit override for the module-level INITIAL_EQUITY (added
    2026-08-23). Must be passed explicitly by any caller that runs inside a
    ProcessPoolExecutor worker — spawned worker processes re-import this module fresh
    and never see main-process mutations to the INITIAL_EQUITY global (e.g.
    eth_trader.py's _bt_make_session() scaling it to the real wallet balance),
    so relying on the global there silently sizes IS-phase results off the config
    placeholder instead of real capital. Defaults to the module global for in-process
    callers (OOS walk-forward, replay_recent_trades) where that mutation is visible.

    record_entries=True (added 2026-08-22, for the periodic missed-trade report —
    see replay_recent_trades) additionally captures every entry this parameter set would
    have taken, as (bar_idx, side) tuples, and changes the return value to a 2-tuple
    (result_or_None, entries) regardless of which gate the normal result hits — entries
    are still returned even when the aggregate stats fail MIN_TRADES/MIN_AVG_HOLD
    (MAX_DD_PCT was a third such gate here until it was removed entirely 2026-09-03),
    since a short 2-day replay window legitimately may not clear those bars
    but the individual entries it took are still exactly what "did paper miss this"
    needs to compare against. Default False path is completely unmodified — same return
    shape, same behavior, zero cost, as always."""
    k_len     = int(params["k_len"]); k_sm = int(params["k_smooth"]); d_sm = int(params["d_smooth"])
    ob        = float(params["ob"]); os_ = float(params["os"])
    chop_len  = int(params["chop_len"]); chop_thr = float(params["chop_thr"])
    gc_p      = int(params.get("gc_period", 144)); gc_pl = int(params.get("gc_poles", 4))
    gc_sqrt2  = PINE_GC_SQRT2 if entry_source == "pine" else None
    atr_p     = int(params["atr_p"])
    stop_m    = float(params["stop_mult"])
    levels    = int(params.get("grid_levels", 4))
    flip_on   = bool(int(params.get("flip_on_signal", 0)))
    trail_mult = float(params.get("trail_tp_mult", 0.0))
    # Per-level ATR-multiple increments and close-fractions (added 2026-08-28, explicit
    # user ask — see grid_level_prices' docstring). grid_dists[li] is the INCREMENT from
    # level li-1's cumulative distance, not the absolute distance itself — see
    # grid_level_prices. Legacy DB rows predating this change fall back to the old
    # scalar grid_atr_mult/grid_level_frac (replicated across every slot), which
    # reconstructs their exact old uniform-grid behavior — see PARAM_SPACE's comment.
    grid_dists = np.array([float(params.get(f"grid_dist_{i+1}", params.get("grid_atr_mult", 1.0)))
                            for i in range(MAX_GRID_LEVELS)])
    grid_fracs = np.array([float(params.get(f"grid_frac_{i+1}", params.get("grid_level_frac", 0.25)))
                            for i in range(MAX_GRID_LEVELS)])

    entries = []
    def _ret(result):
        return (result, entries) if record_entries else result

    # Reward/risk gate: the LAST grid level's cumulative distance (sum of the first
    # `levels` increments) vs. the stop distance (stop_m ATR) — same spirit as the old
    # tp_mult/stop_mult gate, just measured against the grid's outermost target instead
    # of a single TP.
    if (ob <= os_ or not (1 <= levels <= MAX_GRID_LEVELS)
            or grid_dists[:levels].sum() / max(stop_m, 1e-9) < MIN_RR_RATIO):
        return _ret(None)

    n = len(cl)

    # Shared indicators
    raw_k   = _stoch_raw_k(hi, lo, cl, k_len)
    k_arr   = _sma(raw_k, k_sm); d_arr = _sma(k_arr, d_sm)
    ci_arr  = _chop_index(hi, lo, cl, chop_len)
    atr_arr = _atr_wilder(hi, lo, cl, atr_p)

    k_prev = np.roll(k_arr, 1); d_prev = np.roll(d_arr, 1)
    valid = ~np.isnan(k_arr) & ~np.isnan(d_arr) & ~np.isnan(k_prev) & ~np.isnan(d_prev)
    cup = valid & (k_arr > d_arr)  & (k_prev <= d_prev)
    cdn = valid & (k_arr < d_arr)  & (k_prev >= d_prev)
    cup[0] = cdn[0] = False
    ci_ok = ci_arr < chop_thr

    gm    = gaussian_channel_midline(hi, lo, cl, gc_p, gc_pl, sqrt2=gc_sqrt2)
    gd    = np.diff(gm, prepend=gm[0])
    gc_rising, gc_falling = gd > 0, gd < 0

    buy  = cup & (k_arr <= os_) & gc_rising  & ci_ok
    sell = cdn & (k_arr >= ob)  & gc_falling & ci_ok

    start_i = max(0, n - backtest_bars)
    nb = n - start_i
    equity_base = initial_equity if initial_equity is not None else INITIAL_EQUITY

    if not record_entries and _NUMBA_OK:
        # Hot path: JIT-compiled loop (added 2026-08-22, grid exit added in the Grid
        # fork). No entry recording needed here — replay_recent_trades (the only
        # record_entries=True caller) is a small, infrequent 2-day-window replay and
        # takes the pure-Python branch below instead.
        bad = np.isnan(atr_arr) | np.isnan(k_arr) | np.isnan(ci_arr)
        eq, t, w, gw, gl, th, mw, ml, zf, curve = _sim_grid_jit(
            cl, atr_arr, bad, buy, sell, start_i, n, lev,
            equity_base, stop_m, levels, grid_dists, grid_fracs, flip_on, trail_mult)
    else:
        # ── Grid loop (pure Python — used when record_entries=True or numba isn't
        # available). Numeric twin of _sim_grid_jit above — keep both in sync by hand.
        eq = equity_base
        in_long = in_short = False
        ep = sl = qty0 = qty_rem = fee_rem = 0.0
        ei = 0; filled = 0; part_pnl = 0.0; fees = 0.0; exit_notional = 0.0
        entry_atr = peak_price = 0.0
        t = w = 0; gw = gl = th = 0.0
        mw = ml = 0.0; zf = 0
        lvl_px = [0.0] * levels
        unwound = [False] * MAX_GRID_LEVELS
        curve = np.empty(nb)

        for idx in range(start_i, n):
            ii = idx - start_i
            if np.isnan(atr_arr[idx]) or np.isnan(k_arr[idx]) or np.isnan(ci_arr[idx]):
                curve[ii] = eq; continue
            cl_i = cl[idx]

            if not (in_long or in_short):
                if buy[idx]:
                    ntl = eq * lev * MARGIN_HEADROOM
                    qty0 = ntl/cl_i; fee0 = ntl*TAKER_FEE; eq -= fee0
                    ep = cl_i; sl = cl_i - atr_arr[idx]*stop_m
                    entry_atr = atr_arr[idx]; peak_price = cl_i
                    lvl_px = grid_level_prices(cl_i, atr_arr[idx], 1, levels, grid_dists)
                    qty_rem = qty0; fee_rem = fee0; filled = 0; part_pnl = 0.0; fees = fee0
                    exit_notional = 0.0
                    unwound = [False] * MAX_GRID_LEVELS
                    in_long = True; ei = idx
                    if record_entries: entries.append((idx, "long"))
                elif sell[idx]:
                    ntl = eq * lev * MARGIN_HEADROOM
                    qty0 = ntl/cl_i; fee0 = ntl*TAKER_FEE; eq -= fee0
                    ep = cl_i; sl = cl_i + atr_arr[idx]*stop_m
                    entry_atr = atr_arr[idx]; peak_price = cl_i
                    lvl_px = grid_level_prices(cl_i, atr_arr[idx], -1, levels, grid_dists)
                    qty_rem = qty0; fee_rem = fee0; filled = 0; part_pnl = 0.0; fees = fee0
                    exit_notional = 0.0
                    unwound = [False] * MAX_GRID_LEVELS
                    in_short = True; ei = idx
                    if record_entries: entries.append((idx, "short"))
            elif in_long:
                if cl_i > peak_price: peak_price = cl_i
                if cl_i <= sl:
                    ef = cl_i*qty_rem*TAKER_FEE; pnl_i = (cl_i-ep)*qty_rem - ef
                    eq += pnl_i; part_pnl += pnl_i - fee_rem; fees += ef
                    exit_notional += cl_i*qty_rem
                    t += 1; th += idx-ei
                    if filled == 0: zf += 1
                    raw_pct = (exit_notional/qty0 - ep) / ep
                    if part_pnl > WIN_FEE_MULT*fees and raw_pct >= MIN_WIN_PRICE_PCT: w += 1; gw += part_pnl; mw = max(mw, part_pnl)
                    else:                                                            gl += abs(part_pnl); ml = max(ml, abs(part_pnl))
                    in_long = False
                elif flip_on and sell[idx]:
                    ef = cl_i*qty_rem*TAKER_FEE; pnl_i = (cl_i-ep)*qty_rem - ef
                    eq += pnl_i; part_pnl += pnl_i - fee_rem; fees += ef
                    exit_notional += cl_i*qty_rem
                    t += 1; th += idx-ei
                    if filled == 0: zf += 1
                    raw_pct = (exit_notional/qty0 - ep) / ep
                    if part_pnl > WIN_FEE_MULT*fees and raw_pct >= MIN_WIN_PRICE_PCT: w += 1; gw += part_pnl; mw = max(mw, part_pnl)
                    else:                                                            gl += abs(part_pnl); ml = max(ml, abs(part_pnl))
                    ntl = eq * lev * MARGIN_HEADROOM
                    qty0 = ntl/cl_i; fee0 = ntl*TAKER_FEE; eq -= fee0
                    ep = cl_i; sl = cl_i + atr_arr[idx]*stop_m
                    entry_atr = atr_arr[idx]; peak_price = cl_i
                    lvl_px = grid_level_prices(cl_i, atr_arr[idx], -1, levels, grid_dists)
                    qty_rem = qty0; fee_rem = fee0; filled = 0; part_pnl = 0.0; fees = fee0
                    exit_notional = 0.0
                    unwound = [False] * MAX_GRID_LEVELS
                    in_long = False; in_short = True; ei = idx
                    if record_entries: entries.append((idx, "short"))
                elif trail_mult > 0.0 and peak_price > ep and (peak_price - cl_i) >= trail_mult*entry_atr:
                    ef = cl_i*qty_rem*TAKER_FEE; pnl_i = (cl_i-ep)*qty_rem - ef
                    eq += pnl_i; part_pnl += pnl_i - fee_rem; fees += ef
                    exit_notional += cl_i*qty_rem
                    t += 1; th += idx-ei
                    if filled == 0: zf += 1
                    raw_pct = (exit_notional/qty0 - ep) / ep
                    if part_pnl > WIN_FEE_MULT*fees and raw_pct >= MIN_WIN_PRICE_PCT: w += 1; gw += part_pnl; mw = max(mw, part_pnl)
                    else:                                                            gl += abs(part_pnl); ml = max(ml, abs(part_pnl))
                    in_long = False
                else:
                    while filled < levels and cl_i >= lvl_px[filled]:
                        qty_i = qty_rem if filled == levels-1 else min(qty0*grid_fracs[filled], qty_rem)
                        ef_i = cl_i*qty_i*TAKER_FEE; pnl_i = (cl_i-ep)*qty_i - ef_i
                        entry_fee_i = fee_rem * (qty_i/qty_rem)
                        eq += pnl_i; part_pnl += pnl_i - entry_fee_i; fees += ef_i
                        exit_notional += cl_i*qty_i
                        fee_rem -= entry_fee_i; qty_rem -= qty_i; filled += 1
                        sl = ep if filled == 1 else lvl_px[filled-2]
                        if qty_rem <= 1e-12 or filled >= levels:
                            t += 1; th += idx-ei
                            if filled == 0: zf += 1
                            raw_pct = (exit_notional/qty0 - ep) / ep
                            if part_pnl > WIN_FEE_MULT*fees and raw_pct >= MIN_WIN_PRICE_PCT: w += 1; gw += part_pnl; mw = max(mw, part_pnl)
                            else:                                                            gl += abs(part_pnl); ml = max(ml, abs(part_pnl))
                            in_long = False
                            break
                    if in_long:
                        ui = filled - 1
                        while ui >= 0 and cl_i < lvl_px[ui]:
                            if not unwound[ui]:
                                qty_i = min(qty0*grid_fracs[ui], qty_rem)
                                ef_i = cl_i*qty_i*TAKER_FEE; pnl_i = (cl_i-ep)*qty_i - ef_i
                                entry_fee_i = fee_rem * (qty_i/qty_rem)
                                eq += pnl_i; part_pnl += pnl_i - entry_fee_i; fees += ef_i
                                exit_notional += cl_i*qty_i
                                fee_rem -= entry_fee_i; qty_rem -= qty_i
                                unwound[ui] = True
                                if qty_rem <= 1e-12:
                                    t += 1; th += idx-ei
                                    if filled == 0: zf += 1
                                    raw_pct = (exit_notional/qty0 - ep) / ep
                                    if part_pnl > WIN_FEE_MULT*fees and raw_pct >= MIN_WIN_PRICE_PCT: w += 1; gw += part_pnl; mw = max(mw, part_pnl)
                                    else:                                                            gl += abs(part_pnl); ml = max(ml, abs(part_pnl))
                                    in_long = False
                                    break
                            ui -= 1
            elif in_short:
                if cl_i < peak_price: peak_price = cl_i
                if cl_i >= sl:
                    ef = cl_i*qty_rem*TAKER_FEE; pnl_i = (ep-cl_i)*qty_rem - ef
                    eq += pnl_i; part_pnl += pnl_i - fee_rem; fees += ef
                    exit_notional += cl_i*qty_rem
                    t += 1; th += idx-ei
                    if filled == 0: zf += 1
                    raw_pct = (ep - exit_notional/qty0) / ep
                    if part_pnl > WIN_FEE_MULT*fees and raw_pct >= MIN_WIN_PRICE_PCT: w += 1; gw += part_pnl; mw = max(mw, part_pnl)
                    else:                                                            gl += abs(part_pnl); ml = max(ml, abs(part_pnl))
                    in_short = False
                elif flip_on and buy[idx]:
                    ef = cl_i*qty_rem*TAKER_FEE; pnl_i = (ep-cl_i)*qty_rem - ef
                    eq += pnl_i; part_pnl += pnl_i - fee_rem; fees += ef
                    exit_notional += cl_i*qty_rem
                    t += 1; th += idx-ei
                    if filled == 0: zf += 1
                    raw_pct = (ep - exit_notional/qty0) / ep
                    if part_pnl > WIN_FEE_MULT*fees and raw_pct >= MIN_WIN_PRICE_PCT: w += 1; gw += part_pnl; mw = max(mw, part_pnl)
                    else:                                                            gl += abs(part_pnl); ml = max(ml, abs(part_pnl))
                    ntl = eq * lev * MARGIN_HEADROOM
                    qty0 = ntl/cl_i; fee0 = ntl*TAKER_FEE; eq -= fee0
                    ep = cl_i; sl = cl_i - atr_arr[idx]*stop_m
                    entry_atr = atr_arr[idx]; peak_price = cl_i
                    lvl_px = grid_level_prices(cl_i, atr_arr[idx], 1, levels, grid_dists)
                    qty_rem = qty0; fee_rem = fee0; filled = 0; part_pnl = 0.0; fees = fee0
                    exit_notional = 0.0
                    unwound = [False] * MAX_GRID_LEVELS
                    in_short = False; in_long = True; ei = idx
                    if record_entries: entries.append((idx, "long"))
                elif trail_mult > 0.0 and peak_price < ep and (cl_i - peak_price) >= trail_mult*entry_atr:
                    ef = cl_i*qty_rem*TAKER_FEE; pnl_i = (ep-cl_i)*qty_rem - ef
                    eq += pnl_i; part_pnl += pnl_i - fee_rem; fees += ef
                    exit_notional += cl_i*qty_rem
                    t += 1; th += idx-ei
                    if filled == 0: zf += 1
                    raw_pct = (ep - exit_notional/qty0) / ep
                    if part_pnl > WIN_FEE_MULT*fees and raw_pct >= MIN_WIN_PRICE_PCT: w += 1; gw += part_pnl; mw = max(mw, part_pnl)
                    else:                                                            gl += abs(part_pnl); ml = max(ml, abs(part_pnl))
                    in_short = False
                else:
                    while filled < levels and cl_i <= lvl_px[filled]:
                        qty_i = qty_rem if filled == levels-1 else min(qty0*grid_fracs[filled], qty_rem)
                        ef_i = cl_i*qty_i*TAKER_FEE; pnl_i = (ep-cl_i)*qty_i - ef_i
                        entry_fee_i = fee_rem * (qty_i/qty_rem)
                        eq += pnl_i; part_pnl += pnl_i - entry_fee_i; fees += ef_i
                        exit_notional += cl_i*qty_i
                        fee_rem -= entry_fee_i; qty_rem -= qty_i; filled += 1
                        sl = ep if filled == 1 else lvl_px[filled-2]
                        if qty_rem <= 1e-12 or filled >= levels:
                            t += 1; th += idx-ei
                            if filled == 0: zf += 1
                            raw_pct = (ep - exit_notional/qty0) / ep
                            if part_pnl > WIN_FEE_MULT*fees and raw_pct >= MIN_WIN_PRICE_PCT: w += 1; gw += part_pnl; mw = max(mw, part_pnl)
                            else:                                                            gl += abs(part_pnl); ml = max(ml, abs(part_pnl))
                            in_short = False
                            break
                    if in_short:
                        ui = filled - 1
                        while ui >= 0 and cl_i > lvl_px[ui]:
                            if not unwound[ui]:
                                qty_i = min(qty0*grid_fracs[ui], qty_rem)
                                ef_i = cl_i*qty_i*TAKER_FEE; pnl_i = (ep-cl_i)*qty_i - ef_i
                                entry_fee_i = fee_rem * (qty_i/qty_rem)
                                eq += pnl_i; part_pnl += pnl_i - entry_fee_i; fees += ef_i
                                exit_notional += cl_i*qty_i
                                fee_rem -= entry_fee_i; qty_rem -= qty_i
                                unwound[ui] = True
                                if qty_rem <= 1e-12:
                                    t += 1; th += idx-ei
                                    if filled == 0: zf += 1
                                    raw_pct = (ep - exit_notional/qty0) / ep
                                    if part_pnl > WIN_FEE_MULT*fees and raw_pct >= MIN_WIN_PRICE_PCT: w += 1; gw += part_pnl; mw = max(mw, part_pnl)
                                    else:                                                            gl += abs(part_pnl); ml = max(ml, abs(part_pnl))
                                    in_short = False
                                    break
                            ui -= 1

            if in_long:   curve[ii] = eq + (cl_i-ep)*qty_rem
            elif in_short:curve[ii] = eq + (ep-cl_i)*qty_rem
            else:         curve[ii] = eq

        if in_long or in_short:
            cl_i = cl[-1]; ef = cl_i*qty_rem*TAKER_FEE
            pnl_i = ((cl_i-ep) if in_long else (ep-cl_i))*qty_rem - ef
            eq += pnl_i; part_pnl += pnl_i - fee_rem; fees += ef
            exit_notional += cl_i*qty_rem
            t += 1; th += n-1-ei
            if filled == 0: zf += 1
            raw_pct = (exit_notional/qty0 - ep)/ep if in_long else (ep - exit_notional/qty0)/ep
            if part_pnl > WIN_FEE_MULT*fees and raw_pct >= MIN_WIN_PRICE_PCT: w += 1; gw += part_pnl; mw = max(mw, part_pnl)
            else:                                                            gl += abs(part_pnl); ml = max(ml, abs(part_pnl))

    trades = t
    if trades < MIN_TRADES: return _ret(None)
    avg_hold = th / trades if trades > 0 else 0.0
    if avg_hold < MIN_AVG_HOLD: return _ret(None)

    # Hard drawdown reject gate REMOVED 2026-09-03, explicit user ask ("get rid of the
    # drawdown gate!!!!!!!"), same conversation as removing the DD-ratio and flat-DD
    # selection-level gates just before it. max_dd is still computed and reported
    # (max_dd_pct is informational, shown in the Backtest tab) — nothing about a
    # candidate's drawdown, however deep, disqualifies it from being saved/selected
    # any more.
    peak    = np.maximum.accumulate(curve)
    dd_arr  = (curve - peak) / np.where(peak == 0, 1e-9, peak)
    max_dd  = float(dd_arr.min())

    diffs = np.diff(curve); prev = curve[:-1]
    with np.errstate(invalid="ignore", divide="ignore"):
        ret = diffs / np.where(prev == 0, 1e-9, prev)
    ret = ret[np.isfinite(ret)]
    std = float(ret.std()) if len(ret) > 1 else 0.0
    sharpe = float(ret.mean()/std * math.sqrt(bpy)) if std > 0 else 0.0

    equity_final = eq
    years = max(nb / bpy, 1e-9)
    # A tiny `years` (a handful of bars) blows the `1/years` exponent up to the point
    # where even a modest ratio overflows float64 (observed directly: a real saved
    # candidate's cagr_pct came back as 2.68e24, then 4.27e29 after a first, incomplete
    # cap) — cagr_pct is informational only (never used in `_clears_target`/`score`/
    # anywhere selection happens), so cap it at a clearly-nonsensical-but-finite value.
    # `eq`/`equity_base` are plain Python floats here (not numpy scalars), so `**`
    # raises OverflowError outright on true overflow rather than emitting a
    # numpy-warning-suppressible inf — np.errstate alone does NOT catch this; observed
    # directly (`OverflowError: (34, 'Result too large')` reproduced from real values).
    # A worker process hitting this unhandled would crash mid-sweep — a real, previously
    # unguarded crash path, not just a cosmetic one.
    try:
        with np.errstate(over="ignore", invalid="ignore"):
            cagr = max(equity_final / equity_base, 1e-9)**(1/years) - 1
    except OverflowError:
        cagr = float("inf")
    # isfinite() alone isn't enough — a tiny `years` can produce a merely astronomical
    # (not actually inf/nan) but still meaningless float64, like the 4.27e29 a real
    # 3-trade/48h-window candidate produced here even after the OverflowError guard
    # above. Cap the magnitude outright, not just the special-case infinities.
    if not math.isfinite(cagr) or cagr > 1e6:
        cagr = 1e6
    tot_ret = (equity_final - equity_base) / equity_base
    gross_w = gw; gross_l = gl
    pf   = gross_w / gross_l if gross_l > 0 else float("inf")
    wr   = w / trades if trades > 0 else 0.0
    # zero_fill_rate (explicit user ask, "how do we make this profitable instead" — see
    # zf's docstring in _sim_grid_jit): the fraction of trades that closed without ever
    # filling a single grid level. Multiplied straight into `score` as a linear penalty
    # — a param set where every trade reverses clean to the stop scores 0 regardless of
    # its raw sharpe, a param set with none of that failure mode keeps its full sharpe
    # score, and the search naturally prefers whichever params structurally avoid it
    # (a closer grid_dist_1, a cleaner entry signal, or some combination) over params
    # that only look good on the aggregate equity curve despite a high clean-loss rate.
    zero_fill_rate = zf / trades if trades > 0 else 0.0
    score = sharpe * math.sqrt(trades / MIN_TRADES) * max(0.0, 1.0 - zero_fill_rate)

    return _ret({
        "entry_source":    entry_source,
        "score":           round(score, 3),
        "sharpe":          round(sharpe, 3),
        "cagr_pct":        round(cagr*100, 2),
        "total_ret_pct":   round(tot_ret*100, 2),
        "max_dd_pct":      round(max_dd*100, 2),
        "trades":          int(trades),
        "avg_hold":        round(avg_hold, 1),
        "win_rate":        round(wr, 4),
        "profit_factor":   round(min(pf, 99.9), 3),
        "final_equity":    round(equity_final, 2),
        "cum_profit":      round(gross_w, 2),
        "cum_loss":        round(gross_l, 2),
        "max_tp":          round(mw, 2),
        "max_loss":        round(ml, 2),
        "zero_fill_rate":  round(zero_fill_rate, 4),
    })


def _combo_worker(args):
    # Each batch is tagged with a single fixed src (changed 2026-08-28 when "searched"
    # got its own wider PARAM_SPACE_SEARCHED — a combo sampled from one source's range
    # is only ever meaningful for that source; testing it under the other source too,
    # as the old code did when both shared one PARAM_SPACE, would silently mix a
    # searched-only param combo into pine's results). See optimize_symbol_interval's
    # combo-generation section for where the two per-source combo lists are built.
    combos, hi, lo, cl, backtest_bars, bpy, lev, initial_equity, src = args
    out = []
    for p in combos:
        m = _bt_combo_pair(p, hi, lo, cl, backtest_bars, bpy, lev,
                           initial_equity=initial_equity, entry_source=src)
        if m is not None:
            out.append({**p, **m})
    return out


# ── DB ────────────────────────────────────────────────────────────────────────
# Per-level grid columns (added 2026-08-28, see grid_level_prices' docstring) — built
# programmatically so the CREATE/ALTER/INSERT/SELECT/GROUP BY column lists below can
# never drift out of sync with each other or with PARAM_SPACE's grid_dist_i/grid_frac_i
# keys. The old grid_atr_mult/grid_level_frac columns stay in the schema (never
# dropped) purely so pre-2026-08-28 cached rows stay readable — new rows leave them
# NULL, new code never reads them except as a COALESCE fallback for those old rows.
_GRID_COLS = [f"grid_dist_{i}" for i in range(1, MAX_GRID_LEVELS + 1)] + \
             [f"grid_frac_{i}" for i in range(1, MAX_GRID_LEVELS + 1)]
_GRID_DIST_COALESCE = [f"COALESCE(grid_dist_{i}, grid_atr_mult, 1.0)"
                        for i in range(1, MAX_GRID_LEVELS + 1)]
_GRID_FRAC_COALESCE = [f"COALESCE(grid_frac_{i}, grid_level_frac, 0.25)"
                        for i in range(1, MAX_GRID_LEVELS + 1)]
_GRID_COALESCE = _GRID_DIST_COALESCE + _GRID_FRAC_COALESCE

def db_init():
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS param_runs (
                run_ts TEXT, symbol TEXT, interval TEXT,
                k_len INT, k_smooth INT, d_smooth INT, ob REAL, os REAL,
                chop_len INT, chop_thr REAL, atr_p INT,
                stop_mult REAL, grid_levels INT, grid_atr_mult REAL, grid_level_frac REAL,
                gc_period INT, gc_poles INT,
                score REAL, sharpe REAL, cagr_pct REAL, total_ret_pct REAL, max_dd_pct REAL,
                trades INT, avg_hold REAL, win_rate REAL, profit_factor REAL, final_equity REAL
            )""")
        # Migration: per-level grid_dist_i/grid_frac_i columns — CREATE TABLE IF NOT
        # EXISTS above is a no-op against a DB that already has this table from before
        # this change, so these need an explicit ALTER TABLE. Nullable — existing rows
        # keep NULL here and stay fully usable via _GRID_COALESCE's fallback to their
        # old grid_atr_mult/grid_level_frac scalar, everywhere this module reads
        # param_runs.
        cols = {r[1] for r in con.execute("PRAGMA table_info(param_runs)")}
        for col in _GRID_COLS:
            if col not in cols:
                con.execute(f"ALTER TABLE param_runs ADD COLUMN {col} REAL")
        # Migration: flip_on_signal (added 2026-08-31, explicit user ask, "reverse-and-
        # flip" — see PARAM_SPACE's comment). Nullable — existing rows keep NULL here and
        # are treated as flip-off (0) via _grid_select_cols'/db_load_top's COALESCE, same
        # pattern as the grid columns above: a pre-existing cached combo predates the
        # concept of flipping entirely, so "it never flipped" is the only honest default.
        if "flip_on_signal" not in cols:
            con.execute("ALTER TABLE param_runs ADD COLUMN flip_on_signal INT")
        # Migration: trail_tp_mult (added 2026-09-04, explicit user ask, "i want grid
        # and trailing tp" -> "i want this built in" — see PARAM_SPACE's comment).
        # Nullable — existing rows keep NULL here and are treated as trailing-TP-off
        # (0.0) via _grid_select_cols'/db_load_top's COALESCE, same pattern as
        # flip_on_signal above: a pre-existing cached combo predates this feature
        # entirely, so "it never had a trailing TP" is the only honest default.
        if "trail_tp_mult" not in cols:
            con.execute("ALTER TABLE param_runs ADD COLUMN trail_tp_mult REAL")
        # winning_params: a permanent, never-pruned record of every distinct param
        # combo that has EVER cleared `_clears_target` and been saved as a (symbol,
        # interval, entry_source)'s live result file — added 2026-09-01, explicit user
        # ask ("shouldnt it be recorded with all previously tested winning params and
        # always tested again before random tests??"). This is deliberately separate
        # from param_runs/db_load_top: that table's top-N-by-IS-score carry-forward is
        # a RANKING-based guarantee (a combo can be outscored by others on their own,
        # possibly-overfit IS windows and silently drop out of rotation), not an
        # absolute one — a combo that genuinely won a real OOS retest in the past could
        # vanish from every future sweep's candidate pool even though it once proved
        # itself. Recording every historical winner here and always including ALL of
        # them (see optimize_symbol_interval) closes that gap for good, independent of
        # score-ranking dynamics in param_runs.
        # grid_atr_mult/grid_level_frac columns are never actually populated here (every
        # winner recorded post-per-level-grid always has real grid_dist_i/grid_frac_i
        # values) but must still exist, NULL, because `_grid_select_cols()` is shared
        # with param_runs and its COALESCE(grid_dist_i, grid_atr_mult, 1.0) expressions
        # reference them by name — omitting them makes every query against this table
        # raise sqlite3.OperationalError (silently caught and swallowed as "no winners
        # found" by db_load_winners/db_load_winner_keys, which is exactly what happened
        # during verification: the table appeared query-broken, not merely empty).
        con.execute("""
            CREATE TABLE IF NOT EXISTS winning_params (
                run_ts TEXT, symbol TEXT, interval TEXT, entry_source TEXT,
                k_len INT, k_smooth INT, d_smooth INT, ob REAL, os REAL,
                chop_len INT, chop_thr REAL, atr_p INT,
                stop_mult REAL, grid_levels INT, grid_atr_mult REAL, grid_level_frac REAL,
                """ + ",".join(f"{c} REAL" for c in _GRID_COLS) + """,
                gc_period INT, gc_poles INT, flip_on_signal INT,
                score REAL, sharpe REAL, total_ret_pct REAL, max_dd_pct REAL, win_rate REAL
            )""")
        # Migration: trail_tp_mult on winning_params too — this table already existed
        # (created 2026-09-01) before this feature, so CREATE TABLE IF NOT EXISTS above
        # is a no-op for it, same reasoning as param_runs' migration above.
        wp_cols = {r[1] for r in con.execute("PRAGMA table_info(winning_params)")}
        if "trail_tp_mult" not in wp_cols:
            con.execute("ALTER TABLE winning_params ADD COLUMN trail_tp_mult REAL")
        # Indexes — added 2026-09-04 after a real crash: adding 15m back to
        # CRYPTO_INTERVALS doubled how often db_load_tried_set/db_load_top run per
        # cycle, and against param_runs' ~126k accumulated rows with NO index at all,
        # their WHERE symbol=?/interval=? filter was a full table scan every single
        # call. A macOS diagnostic report ("Event: disk writes", 34.36 GB dirtied over
        # 2076s) traced the app's main thread to sqlite3_step -> vdbeSorterListToPMA ->
        # pwrite — SQLite spilling a GROUP BY sort to disk — at the exact moment a
        # worker pool crash ("Process pool broken") was followed by the whole app
        # vanishing with no crash log. Several earlier same-session diagnostic reports
        # of the identical shape confirm this was already happening intermittently
        # before 15m was ever added; 15m simply made it fire twice as often.
        con.execute("CREATE INDEX IF NOT EXISTS idx_param_runs_sym_iv "
                    "ON param_runs(symbol, interval)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_winning_params_sym_iv_src "
                    "ON winning_params(symbol, interval, entry_source)")
        con.commit()

def db_save(symbol, interval, candidates):
    ts = datetime.now(timezone.utc).isoformat()
    def _grid_dist_sum(c, levels):
        return sum(float(c.get(f"grid_dist_{i}", c.get("grid_atr_mult", 1.0)))
                   for i in range(1, levels + 1))
    def _row(c):
        row = [ts, symbol, interval,
               int(c["k_len"]), int(c["k_smooth"]), int(c["d_smooth"]),
               float(c["ob"]), float(c["os"]), int(c["chop_len"]), float(c["chop_thr"]),
               int(c["atr_p"]), float(c["stop_mult"]), int(c.get("grid_levels", 4))]
        row += [float(c.get(f"grid_dist_{i}", c.get("grid_atr_mult", 1.0)))
                for i in range(1, MAX_GRID_LEVELS + 1)]
        row += [float(c.get(f"grid_frac_{i}", c.get("grid_level_frac", 0.25)))
                for i in range(1, MAX_GRID_LEVELS + 1)]
        row += [int(c.get("gc_period", 144)), int(c.get("gc_poles", 4)),
                float(c["score"]), float(c["sharpe"]), float(c["cagr_pct"]),
                float(c["total_ret_pct"]), float(c["max_dd_pct"]),
                int(c["trades"]), float(c["avg_hold"]), float(c["win_rate"]),
                float(c["profit_factor"]), float(c["final_equity"]),
                int(c.get("flip_on_signal", 0)), float(c.get("trail_tp_mult", 0.0))]
        return tuple(row)
    rows = [_row(c) for c in candidates if float(c.get("total_ret_pct", -1)) > 0
            and _grid_dist_sum(c, int(c.get("grid_levels", 4)))
                / max(float(c.get("stop_mult", 3.5)), 1e-9) >= MIN_RR_RATIO]
    if not rows: return
    cols_sql = ("run_ts,symbol,interval,k_len,k_smooth,d_smooth,ob,os,chop_len,chop_thr,"
                "atr_p,stop_mult,grid_levels," + ",".join(_GRID_COLS) +
                ",gc_period,gc_poles,score,sharpe,cagr_pct,total_ret_pct,max_dd_pct,"
                "trades,avg_hold,win_rate,profit_factor,final_equity,flip_on_signal,"
                "trail_tp_mult")
    placeholders = ",".join("?" * len(rows[0]))
    with sqlite3.connect(DB_PATH) as con:
        con.executemany(f"INSERT INTO param_runs ({cols_sql}) VALUES ({placeholders})", rows)
    _log.info(f"DB: +{len(rows)} rows for {symbol} {interval}m")
    _prune_param_runs(symbol, interval)

# PARAM_RUNS_RETENTION_DAYS/PARAM_RUNS_KEEP_N — added 2026-09-04, explicit user ask
# ("limit params saved to top 50 params over past seven days") directly following the
# db_load_tried_set/db_load_top disk-thrashing fix above: without this, param_runs
# grows by every newly-tried combo forever (confirmed: ~126k rows accumulated already,
# the root cause of that crash) — an index and a leaner dedup query make each query
# cheaper, but the table itself was still unbounded. Pruning after every db_save keeps
# it permanently capped at N_TOP_RETEST rows per (symbol, interval), always the
# best-scoring ones from the last 7 days — matches db_load_top's own existing
# "top N_TOP_RETEST by score" carry-forward exactly, just now physically enforced on
# the table instead of only applied at read time. Trade-off, stated plainly: the
# in-memory `tried` dedup set (db_load_tried_set) now only remembers the surviving
# top N_TOP_RETEST combos, not every combo ever tried — a pruned-away mediocre combo
# can be resampled and re-tested in a later cycle. Given N_RANDOM_PER_SOURCE draws
# 200k fresh floats per source per cycle, an occasional repeat is a rounding error
# against that budget, not a meaningful compute cost — an explicitly accepted
# trade-off for keeping the table small, mirroring the same trade-off already made
# for `winning_params`' own accepted growth pattern discussion.
PARAM_RUNS_RETENTION_DAYS = 7
PARAM_RUNS_KEEP_N = N_TOP_RETEST

def _prune_param_runs(symbol, interval, keep_days=PARAM_RUNS_RETENTION_DAYS,
                       keep_n=PARAM_RUNS_KEEP_N):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
            DELETE FROM param_runs
            WHERE symbol=? AND interval=?
              AND (run_ts < ?
                   OR rowid NOT IN (
                       SELECT rowid FROM param_runs
                       WHERE symbol=? AND interval=? AND run_ts>=?
                       ORDER BY score DESC LIMIT ?))
        """, (symbol, interval, cutoff, symbol, interval, cutoff, keep_n))
        con.commit()

def _param_key(p):
    key = [int(p["k_len"]), int(p["k_smooth"]), int(p["d_smooth"]),
           float(p["ob"]), float(p["os"]), int(p["chop_len"]),
           float(p["chop_thr"]), int(p["atr_p"]),
           float(p["stop_mult"]), int(p.get("grid_levels", 4))]
    key += [float(p.get(f"grid_dist_{i}", p.get("grid_atr_mult", 1.0)))
            for i in range(1, MAX_GRID_LEVELS + 1)]
    key += [float(p.get(f"grid_frac_{i}", p.get("grid_level_frac", 0.25)))
            for i in range(1, MAX_GRID_LEVELS + 1)]
    key += [int(p.get("gc_period", 144)), int(p.get("gc_poles", 4)),
            int(p.get("flip_on_signal", 0)), float(p.get("trail_tp_mult", 0.0))]
    return tuple(key)

def _grid_select_cols():
    return ("k_len,k_smooth,d_smooth,ob,os,chop_len,chop_thr,atr_p,stop_mult,"
            "COALESCE(grid_levels,4)," + ",".join(_GRID_COALESCE) +
            ",COALESCE(gc_period,144),COALESCE(gc_poles,4),"
            "COALESCE(flip_on_signal,0),COALESCE(trail_tp_mult,0.0)")

def db_load_tried_set(symbol, interval):
    """No SQL-side GROUP BY (removed 2026-09-04, see db_init's index comment) — the
    dedup this needs is already free, since the result is immediately wrapped in a
    Python set() below. GROUP BY over all 24 _grid_select_cols() columns forced SQLite
    to sort every matching row on disk once param_runs grew past a few hundred
    thousand rows (confirmed the direct cause of a real crash — see db_init); a plain
    filtered SELECT needs no sort at all and returns the exact same set."""
    try:
        cols = _grid_select_cols()
        with sqlite3.connect(DB_PATH) as con:
            rows = con.execute(f"""
                SELECT {cols}
                FROM   param_runs WHERE symbol=? AND interval=?""",
                (symbol, interval)).fetchall()
        return set(rows)
    except sqlite3.Error:
        return set()

def _decode_grid_row(r):
    """Shared by db_load_top and db_load_winners — both SELECT `_grid_select_cols()`
    in the same column order, so both decode identically into a params dict."""
    d = {"k_len":int(r[0]),"k_smooth":int(r[1]),"d_smooth":int(r[2]),
         "ob":float(r[3]),"os":float(r[4]),"chop_len":int(r[5]),
         "chop_thr":float(r[6]),"atr_p":int(r[7]),
         "stop_mult":float(r[8]),"grid_levels":int(r[9])}
    base = 10
    for i in range(MAX_GRID_LEVELS):
        d[f"grid_dist_{i+1}"] = float(r[base + i])
    base += MAX_GRID_LEVELS
    for i in range(MAX_GRID_LEVELS):
        d[f"grid_frac_{i+1}"] = float(r[base + i])
    base += MAX_GRID_LEVELS
    d["gc_period"] = int(r[base]); d["gc_poles"] = int(r[base + 1])
    d["flip_on_signal"] = int(r[base + 2])
    d["trail_tp_mult"] = float(r[base + 3])
    return d

def db_load_top(symbol, interval, n=N_TOP_RETEST):
    try:
        cols = _grid_select_cols()
        with sqlite3.connect(DB_PATH) as con:
            rows = con.execute(f"""
                SELECT {cols}
                FROM   param_runs
                WHERE  symbol=? AND interval=? AND total_ret_pct>0
                GROUP  BY {cols}
                ORDER  BY MAX(score) DESC LIMIT ?""", (symbol, interval, n)).fetchall()
    except sqlite3.Error:
        return []
    return [_decode_grid_row(r) for r in rows]

def db_save_winner(symbol, interval, src, c):
    """Permanently records one param combo as a historical winner for (symbol,
    interval, src) — called only when `_clears_target(c)` is true (see the call site
    in optimize_symbol_interval). Deduplicated by `_param_key` against every winner
    ever recorded for this exact (symbol, interval, src): a combo that has already won
    once is never re-inserted, so this table grows only when a genuinely NEW winning
    combo appears, not once per cycle a stable winner keeps re-qualifying."""
    key = _param_key(c)
    if key in db_load_winner_keys(symbol, interval, src):
        return
    ts = datetime.now(timezone.utc).isoformat()
    row = [ts, symbol, interval, src,
           int(c["k_len"]), int(c["k_smooth"]), int(c["d_smooth"]),
           float(c["ob"]), float(c["os"]), int(c["chop_len"]), float(c["chop_thr"]),
           int(c["atr_p"]), float(c["stop_mult"]), int(c.get("grid_levels", 4))]
    row += [float(c.get(f"grid_dist_{i}", c.get("grid_atr_mult", 1.0)))
            for i in range(1, MAX_GRID_LEVELS + 1)]
    row += [float(c.get(f"grid_frac_{i}", c.get("grid_level_frac", 0.25)))
            for i in range(1, MAX_GRID_LEVELS + 1)]
    row += [int(c.get("gc_period", 144)), int(c.get("gc_poles", 4)),
            int(c.get("flip_on_signal", 0)), float(c.get("trail_tp_mult", 0.0)),
            float(c.get("score", 0.0)), float(c.get("sharpe", 0.0)),
            float(c.get("total_ret_pct", 0.0)), float(c.get("max_dd_pct", 0.0)),
            float(c.get("win_rate", 0.0))]
    cols_sql = ("run_ts,symbol,interval,entry_source,k_len,k_smooth,d_smooth,ob,os,"
                "chop_len,chop_thr,atr_p,stop_mult,grid_levels," + ",".join(_GRID_COLS) +
                ",gc_period,gc_poles,flip_on_signal,trail_tp_mult,score,sharpe,"
                "total_ret_pct,max_dd_pct,win_rate")
    placeholders = ",".join("?" * len(row))
    with sqlite3.connect(DB_PATH) as con:
        con.execute(f"INSERT INTO winning_params ({cols_sql}) VALUES ({placeholders})", row)
        con.commit()

def db_load_winner_keys(symbol, interval, src):
    try:
        cols = _grid_select_cols()
        with sqlite3.connect(DB_PATH) as con:
            rows = con.execute(f"""
                SELECT {cols} FROM winning_params
                WHERE  symbol=? AND interval=? AND entry_source=?""",
                (symbol, interval, src)).fetchall()
        return set(rows)
    except sqlite3.Error:
        return set()

def db_load_winners(symbol, interval, src):
    """Every distinct (by `_param_key`) param combo ever recorded as a winner for this
    exact (symbol, interval, src) — see `db_save_winner`. Unlike `db_load_top`, this is
    NOT ranked/limited: every historical winner is always included, since the whole
    point is an absolute (not ranking-based) guarantee that a combo which once proved
    itself keeps getting retested against fresh data forever. No SQL-side GROUP BY
    (removed 2026-09-04, same reasoning as db_load_tried_set) — db_save_winner already
    guarantees no duplicate row for the same (symbol, interval, src, params) is ever
    inserted (it checks db_load_winner_keys, itself a plain ungrouped SELECT, before
    every INSERT), so this table structurally cannot contain a group with more than
    one row; sorting for a GROUP BY that can never collapse anything was pure cost."""
    try:
        cols = _grid_select_cols()
        with sqlite3.connect(DB_PATH) as con:
            rows = con.execute(f"""
                SELECT {cols} FROM winning_params
                WHERE  symbol=? AND interval=? AND entry_source=?""",
                (symbol, interval, src)).fetchall()
    except sqlite3.Error:
        return []
    return [_decode_grid_row(r) for r in rows]


# ── API + fetch ───────────────────────────────────────────────────────────────
_NO_RETRY = {110007, 110006, 110012, 110013, 110017, 110025}
_RATE_LIMIT_CODES = {10006, 10018}


def _api(fn, *args, **kwargs):
    """Raises on the final attempt if the API never returned a clean success — an
    unresolved failure must never look like a valid (if empty) response to the caller.
    fetch_ohlcv's pagination loop in particular reads an empty result list as "no more
    history" to stop paginating; before this fix a rate-limited or errored response on
    the last retry fell through to a bare `return r` here, which fetch_ohlcv could not
    distinguish from a legitimate end-of-history page, silently truncating the OHLCV
    window fed into parameter optimization."""
    r = None
    for attempt in range(3):
        try: r = fn(*args, **kwargs)
        except Exception:
            if attempt == 2: raise
            time.sleep(0.5*(attempt+1)); continue
        if isinstance(r, dict):
            rc = r.get("retCode",0)
            if rc in _RATE_LIMIT_CODES:
                if attempt == 2:
                    raise RuntimeError(f"API rate-limited after 3 attempts: {r.get('retMsg','')}")
                time.sleep(1+attempt); continue
            if rc in _NO_RETRY: return r
            if rc != 0:
                if attempt == 2:
                    raise RuntimeError(f"API error after 3 attempts (retCode={rc}): {r.get('retMsg','')}")
                time.sleep(0.5*(attempt+1)); continue
        return r
    return r

def fetch_ohlcv(session, symbol, interval):
    max_pages = _max_pages(interval)
    pub_session = None; all_bars = {}; end_ms = None
    for page in range(max_pages):
        kw = dict(category=CATEGORY, symbol=symbol, interval=interval, limit=FETCH_LIMIT)
        if end_ms: kw["end"] = end_ms
        r = _api(session.get_kline, **kw)
        raw = r.get("result",{}).get("list",[])
        if not raw:
            if page == 0:
                if pub_session is None: pub_session = HTTP(demo=False)
                r = pub_session.get_kline(**kw); raw = r.get("result",{}).get("list",[])
            if not raw: break
        for b in raw: all_bars[int(b[0])] = b
        if len(raw) < FETCH_LIMIT: break
        end_ms = min(int(b[0]) for b in raw) - 1
    if not all_bars: raise RuntimeError(f"No kline data for {symbol} {interval}m")
    bars = sorted(all_bars.values(), key=lambda x: int(x[0]))
    idx  = pd.to_datetime([datetime.fromtimestamp(int(b[0])/1000, tz=timezone.utc) for b in bars])
    return pd.DataFrame({"open":[float(b[1]) for b in bars], "high":[float(b[2]) for b in bars],
                         "low":[float(b[3]) for b in bars], "close":[float(b[4]) for b in bars],
                         "volume":[float(b[5]) for b in bars]}, index=idx)


# ── Random param sampling ─────────────────────────────────────────────────────
def _sample(space=PARAM_SPACE):
    p = {}
    for k, (lo, hi) in space.items():
        if k in _INT_PARAMS: p[k] = random.randint(int(lo), int(hi))
        else: p[k] = round(random.uniform(lo, hi), 2)
    return p

# Pine local refinement — "exploit harder" (added 2026-09-01, explicit user ask, see
# N_PINE_REFINE_COMBOS' docstring): jitters ONLY the 9 entry-signal params around an
# anchor (searched's current OOS-validated winner) instead of drawing them independently
# from scratch — a tighter window has far fewer degrees of freedom for the sweep to
# accidentally fit noise to than a fresh global draw does, which is the actual overfitting
# lever here. Exit/grid params (atr_p/stop_mult/grid_levels/grid_dist_i/grid_frac_i/
# flip_on_signal) are deliberately NOT jittered toward the anchor — sampled fully
# independently exactly like `_sample` — since "exit same as the bot" already means both
# sources search those identically, and jittering them too would just correlate pine's
# whole result with searched's instead of refining pine's own read on entry timing under
# ITS formula (PINE_GC_SQRT2, see that constant's docstring). The jitter window's width is
# a fraction of each param's own PARAM_SPACE (narrow, not the wide _SEARCHED) range, not a
# fraction of the anchor's own value — a value-relative window would degenerate toward
# zero width for anchors sitting near zero (e.g. gc_poles=1).
_PINE_REFINE_RADIUS = 0.15

def _sample_local(anchor, space=PARAM_SPACE, radius=_PINE_REFINE_RADIUS):
    p = {}
    for k, (lo, hi) in space.items():
        if k in _ENTRY_PARAM_NAMES and k in anchor:
            # The anchor is searched's winner, drawn from the WIDER PARAM_SPACE_SEARCHED
            # range — it can sit outside pine's own narrower `space` bounds entirely
            # (e.g. a searched gc_poles=18 anchor vs pine's declared 1-9 range). Clamp it
            # into `space` FIRST so the jitter window — and every value it can ever
            # produce — always stays within pine's own declared bounds, never silently
            # teleports pine outside them.
            base = min(max(anchor[k], lo), hi)
            width = (hi - lo) * radius
            lo_j = max(lo, base - width)
            hi_j = min(hi, base + width)
            if k in _INT_PARAMS:
                lo_i, hi_i = int(round(lo_j)), int(round(hi_j))
                p[k] = random.randint(lo_i, hi_i) if hi_i > lo_i else int(round(base))
            else:
                p[k] = round(random.uniform(lo_j, hi_j), 2) if hi_j > lo_j else round(base, 2)
        elif k in _INT_PARAMS:
            p[k] = random.randint(int(lo), int(hi))
        else:
            p[k] = round(random.uniform(lo, hi), 2)
    return p


# ── Optimisation loop ─────────────────────────────────────────────────────────
def optimize_symbol_interval(sess, symbol, interval, status_dict, executor=None,
                              protected_source=None):
    """Returns `new_combo_count` (int) — how many genuinely new (never-before-tried,
    per the DB-backed `tried` dedup set) param combos this single call actually tested,
    excluding the `top_params` elite carry-forward. Added 2026-09-01 for
    BacktestRunner._run()'s per-symbol, all-intervals retry loop (see `_clears_target`'s
    docstring): 0 means this call found nothing new anywhere (data-insufficient, or the
    param space for this exact (symbol, interval) is exhausted), which is what tells
    that outer loop whether sweeping the symbol's intervals again could possibly help.
    This function itself is single-pass again — it no longer retries internally; the
    retry-until-target decision now lives one level up, spanning ALL of a symbol's configured
    intervals together rather than forcing this one interval to individually qualify.

    executor: an already-running ProcessPoolExecutor to reuse (added 2026-08-22 —
    BacktestRunner passes one shared pool across every (symbol, interval) pair in a
    cycle instead of this function spawning/tearing down its own 24 times per cycle).
    If None, falls back to creating and tearing down a local pool, same as before —
    keeps this function usable standalone.

    protected_source (added back 2026-08-28 when the "pine" entry source made this a
    2-way choice again — see PINE_GC_SQRT2's docstring): "searched" or "pine" if an
    open position currently exists for this exact (symbol, interval) on that source, or
    the sentinel "ALL" if a real live position exists but which source it's on
    couldn't be pinned down (protect both rather than risk leaving the actual one
    unprotected — see eth_trader.py's _protected_entry_source). None protects
    nothing. For whichever source(s) are protected, the OOS retest and file save are
    skipped entirely this cycle, so the on-disk result backing a live position can
    never change out from under it. The IS sweep still runs for it (cheap to keep the
    DB's param_runs cache warm, and it's the OOS/save step that actually mutates the
    live file), it just never reaches OOS or gets saved. Any unprotected source updates
    normally."""
    lev = LEVERAGE
    # Both entry sources share one status_dict key during the shared fetch/sweep phase
    # (added 2026-08-28 — see _combo_worker's docstring: both sources are evaluated
    # together per combo in one sweep), then get their own key once results split by
    # source below.
    def _set_both(msg):
        status_dict[f"{symbol}_{interval}_searched"] = msg
        status_dict[f"{symbol}_{interval}_pine"] = msg
    _set = _set_both
    try:
        _set("fetching...")
        df = fetch_ohlcv(sess, symbol, interval)
        hi = df["high"].values; lo = df["low"].values; cl = df["close"].values
        n  = len(cl)
        bpy = _bars_per_year(interval)
        is_n = _is_bars(interval)

        # IS window (last is_n bars before OOS)
        max_oos = _oos_bars(interval, max(OOS_HOURS_LIST))
        # Must guarantee GC_WARMUP_BARS of real margin before BOTH the IS and OOS
        # windows, not just a flat 200 — a symbol that barely clears a smaller minimum
        # would silently get a thin, biased GC warmup (see the padding below) instead of
        # being honestly rejected as not-enough-data-yet. n - max_oos >= is_n +
        # GC_WARMUP_BARS guarantees is_start >= GC_WARMUP_BARS (full IS warmup) and,
        # since max_oos is the largest oos_n any OOS_HOURS_LIST entry can use, also
        # guarantees n - oos_n >= GC_WARMUP_BARS for every OOS window.
        if n < is_n + max_oos + GC_WARMUP_BARS:
            _set("not enough data")
            return 0

        is_start = n - is_n - max_oos
        is_end   = n - max_oos
        # Extend the slice backward by GC_WARMUP_BARS so the searched GC filter gets a
        # real settling window instead of cold-starting at the first bar of the IS
        # window. The data-sufficiency gate above guarantees is_start >= GC_WARMUP_BARS,
        # so min() here is a defensive floor, not the thing actually relied on for
        # correctness. backtest_bars stays is_n unchanged below, so _bt_combo_pair's own
        # start_i = len(array)-backtest_bars still trades only the real IS window — the
        # padding is warm-up-only, never traded.
        is_warmup = min(is_start, GC_WARMUP_BARS)
        is_pad_start = is_start - is_warmup
        hi_is = hi[is_pad_start:is_end]; lo_is = lo[is_pad_start:is_end]; cl_is = cl[is_pad_start:is_end]

        _set("loading top params...")
        top_params = db_load_top(symbol, interval)
        if not top_params:
            top_params = [DEFAULT_PARAMS.copy()]
        # Force flip_on_signal=1 on every carried-forward combo too, not just freshly
        # sampled ones (added 2026-09-04, same change as PARAM_SPACE["flip_on_signal"]
        # = (1, 1) below — explicit user ask, "flip needs to be enabled!!!! in bt paper
        # and live"). PARAM_SPACE alone only guarantees NEW draws get flip on; a combo
        # that was originally sampled before this change and saved into param_runs/
        # winning_params keeps whatever flip_on_signal value it was sampled with
        # forever, since both carry-forward lists load historical rows verbatim — a
        # real gap caught directly: a searched combo first tested at the OLD default
        # (flip off) kept winning the OOS comparison and getting saved as the live
        # result cycle after cycle, well after this fix shipped, purely because it was
        # elite-ranked/a past winner, never freshly re-sampled. Overriding it here,
        # once, right after load, closes that gap for every downstream consumer
        # (IS sweep, OOS retest, save) without needing to touch the DB rows themselves.
        for p in top_params:
            p["flip_on_signal"] = 1

        # Every historical winner, per source (added 2026-09-01, explicit user ask,
        # "make sure previous wining params are also tested" — raised after a real
        # screenshot showed a source's displayed win_rate/return sitting unrefreshed
        # next to a "no OOS winners" status; extended same day per the follow-up ask
        # "shouldnt it be recorded with all previously tested winning params and always
        # tested again before random tests??" — the first pass only re-added the
        # SINGLE currently-saved on-disk result, which a later worse cycle could
        # overwrite and lose forever). `top_params` above already carries the top
        # N_TOP_RETEST all-time best-BY-IS-SCORE combos from `param_runs` forward into
        # every future sweep, which USUALLY keeps a good combo in rotation — but that's
        # a ranking-based guarantee, not an absolute one: a combo that won a past
        # cycle's real OOS retest and got saved as the live result file is not
        # guaranteed to still be in the top `N_TOP_RETEST` of ALL-TIME `param_runs`
        # IS-scores (many other combos may score higher on their OWN, possibly
        # overfit, IS windows without ever panning out OOS), so it could silently drop
        # out of every future sweep's candidate pool even though it once proved itself
        # for real. `db_load_winners` (backed by the permanent `winning_params` table —
        # see db_save_winner's call site below and its own docstring) closes this gap
        # absolutely: EVERY combo that has ever cleared `_clears_target` and been saved
        # for this exact (symbol, interval, src), not just the current one, is always
        # re-tested against fresh IS+OOS data every single cycle, ahead of any new
        # random sampling, regardless of what `param_runs`' score-based ranking or a
        # later cycle's overwrite is doing.
        #
        # Backfill: a currently-saved on-disk result can predate the winning_params
        # table (e.g. an already-deployed instance's existing result file) or simply
        # not yet be recorded if this is its first winning cycle — read it directly
        # from disk and record it now if it clears target, so it's captured before
        # `db_load_winners` is queried below. Read directly from disk rather than
        # through `_load_result_for_symbol` (that helper lives in
        # eth_trader.py and additionally re-checks bt.SYMBOLS/
        # bt.CRYPTO_INTERVALS/freshness — irrelevant here, this just wants "whatever
        # params this exact file on disk currently holds").
        def _load_current_saved_params(src):
            path = os.path.join(DATA_DIR, f"eth_trader_results_{symbol}_{interval}m_{src}.json")
            if not os.path.exists(path):
                return None
            try:
                with open(path) as f:
                    return json.load(f)
            except Exception:
                return None
        saved_searched = None
        for src in ("pine", "searched"):
            saved = _load_current_saved_params(src)
            if src == "searched":
                saved_searched = saved
            if saved and _is_winner(saved):
                db_save_winner(symbol, interval, src, saved)
        winners_by_src = {src: db_load_winners(symbol, interval, src) for src in ("pine", "searched")}
        # Same flip_on_signal=1 override as top_params above, same reason: a historical
        # winner saved before this fix shipped would otherwise keep trading with flip
        # off forever, since winning_params loads its rows verbatim.
        for combos in winners_by_src.values():
            for p in combos:
                p["flip_on_signal"] = 1
        # Refine pine around searched's current winner once one exists and is actually
        # profitable (see _sample_local's docstring; switched from `_clears_target` to
        # `_is_winner` 2026-09-03 alongside winning_params' own fix — `_clears_target`
        # stopped meaning anything once every selection threshold was removed, which
        # would have let pine refine around a losing searched result) — not around any
        # losing best-available candidate, since refining around an already-losing
        # point isn't "exploiting a genuine edge harder." Falls back to pine's original
        # full independent global sweep otherwise (first run, or searched hasn't found
        # a profitable result yet for this pair).
        pine_refine_anchor = saved_searched if saved_searched and _is_winner(saved_searched) else None

        # Random sweep — never duplicate previously tried params. Each source samples
        # from its own PARAM_SPACE (pine) / PARAM_SPACE_SEARCHED (searched, 3x wider
        # entry range — added 2026-08-28); a combo drawn for one source is only ever
        # tested under that source (see _combo_worker's docstring), so this now builds
        # two independent combo lists sharing only the "tried" dedup set and the elite
        # top_params seed list.
        tried = db_load_tried_set(symbol, interval)
        def _gen_combos(space, n=N_RANDOM_PER_SOURCE, sampler=_sample):
            combos = []; attempts = 0
            while len(combos) < n and attempts < n * 5:
                p = sampler(space); attempts += 1
                key = _param_key(p)
                if key not in tried:
                    tried.add(key); combos.append(p)
            return combos
        # Locked sources (see load_locked_entry's docstring) FINE-TUNE ONLY the 9
        # entry-signal params — jittered around the locked base via the same
        # radius-based mechanism _sample_local uses for pine's refine-around-searched
        # anchor, never frozen to one exact value and never resampled independently
        # from the full PARAM_SPACE range either. Every exit param (atr_p/stop_mult/
        # grid_levels/grid_dist_i/grid_frac_i/flip_on_signal/trail_tp_mult) is still
        # genuinely searched, same random budget as an unlocked source, so the OOS
        # retest can keep finding a better-suited exit shape for it (e.g. a tighter
        # stop_mult) without the entry signal ever drifting far from this fixed base.
        locked_entry_by_src = {src: load_locked_entry(symbol, interval, src) for src in ("pine", "searched")}

        def _jitter_entry(p, entry_anchor, space):
            """Copy of `p` with its 9 entry-signal params replaced by a small jitter
            around entry_anchor (same clamp/radius rule as _sample_local) — everything
            else in `p` (its own exit shape) is left untouched. Used to carry a
            historical combo (top_params/winners_by_src) forward under a locked
            source's entry without freezing it to one exact value."""
            out = dict(p)
            for k in _ENTRY_PARAM_NAMES:
                if k not in space or k not in entry_anchor:
                    continue
                lo, hi = space[k]
                base = min(max(entry_anchor[k], lo), hi)
                width = (hi - lo) * _PINE_REFINE_RADIUS
                lo_j, hi_j = max(lo, base - width), min(hi, base + width)
                if k in _INT_PARAMS:
                    lo_i, hi_i = int(round(lo_j)), int(round(hi_j))
                    out[k] = random.randint(lo_i, hi_i) if hi_i > lo_i else int(round(base))
                else:
                    out[k] = round(random.uniform(lo_j, hi_j), 2) if hi_j > lo_j else round(base, 2)
            return out

        pine_entry = locked_entry_by_src["pine"]
        if pine_entry is not None:
            pine_top = [_jitter_entry(p, pine_entry, PARAM_SPACE) for p in top_params]
            pine_winners = [_jitter_entry(p, pine_entry, PARAM_SPACE) for p in winners_by_src["pine"]]
            new_pine = _gen_combos(PARAM_SPACE, sampler=lambda space: _sample_local(pine_entry, space))
        elif pine_refine_anchor is not None:
            pine_top, pine_winners = top_params, winners_by_src["pine"]
            new_pine = _gen_combos(
                PARAM_SPACE, n=N_PINE_REFINE_COMBOS,
                sampler=lambda space: _sample_local(pine_refine_anchor, space))
        else:
            pine_top, pine_winners = top_params, winners_by_src["pine"]
            new_pine = _gen_combos(PARAM_SPACE)

        searched_entry = locked_entry_by_src["searched"]
        if searched_entry is not None:
            searched_top = [_jitter_entry(p, searched_entry, PARAM_SPACE_SEARCHED) for p in top_params]
            searched_winners = [_jitter_entry(p, searched_entry, PARAM_SPACE_SEARCHED) for p in winners_by_src["searched"]]
            new_searched = _gen_combos(PARAM_SPACE_SEARCHED,
                                        sampler=lambda space: _sample_local(searched_entry, space))
        else:
            searched_top, searched_winners = top_params, winners_by_src["searched"]
            new_searched = _gen_combos(PARAM_SPACE_SEARCHED)

        combos_by_src = {
            "pine":     pine_top + pine_winners + new_pine,
            "searched": searched_top + searched_winners + new_searched,
        }
        # Count of genuinely NEW (never-before-tried) combos this call actually tested,
        # excluding the `top_params` elite-carry-forward and the `winners_by_src`
        # historical-winner carry-forward — this is what
        # BacktestRunner._run()'s outer per-symbol retry loop (see below) uses to detect
        # "the param space is exhausted, no point sweeping this symbol again": if this
        # comes back 0 across every interval in a pass, nothing new was found anywhere,
        # so further passes would just repeat the same elites for no benefit.
        new_combo_count = len(new_pine) + len(new_searched)
        total_combos = sum(len(v) for v in combos_by_src.values())
        results = []
        processed = 0

        def _batches(lst, sz):
            for i in range(0, len(lst), sz): yield lst[i:i+sz]

        _set(f"sweep 0/{total_combos}...")
        pool_cm = nullcontext(executor) if executor is not None else ProcessPoolExecutor(max_workers=N_WORKERS)
        with pool_cm as ex:
            futs = {ex.submit(_combo_worker,
                              (batch, hi_is, lo_is, cl_is, is_n, bpy, lev,
                               INITIAL_EQUITY, src)): True
                    for src, combos in combos_by_src.items()
                    for batch in _batches(combos, BATCH_SIZE)}
            for fut in as_completed(futs):
                try: results.extend(fut.result())
                except BrokenProcessPool: raise
                except Exception as e: _log.debug(f"Worker error: {e}")
                processed += BATCH_SIZE
                _set(f"sweep {min(processed,total_combos)}/{total_combos}")

        if not results:
            _set("no IS winners")
            return new_combo_count

        db_save(symbol, interval, results)

        # Each entry source is IS-ranked, OOS-retested, and saved independently — two
        # fully separate candidate pipelines sharing only the IS sweep results/OOS
        # window data above, each producing its own top_n, its own best_result, and its
        # own result file. Which one actually trades is decided by
        # eth_trader.py scanning ALL result files (the existing selection
        # rule is entry-source-agnostic — it just compares whichever candidates exist).
        top_n_by_src = {}
        for src in ("searched", "pine"):
            src_results = [r for r in results if r.get("entry_source", "searched") == src]
            if src_results:
                src_results.sort(key=lambda x: x["score"], reverse=True)
                top_n_by_src[src] = src_results[:N_TOP_RETEST]
            else:
                top_n_by_src[src] = []
                status_dict[f"{symbol}_{interval}_{src}"] = "no IS winners"

        protect_srcs = ("searched", "pine") if protected_source == "ALL" else \
                       (protected_source,) if protected_source in ("searched", "pine") else ()
        for src in protect_srcs:
            top_n_by_src[src] = []
            status_dict[f"{symbol}_{interval}_{src}"] = "protected (position open)"

        if not any(top_n_by_src.values()):
            return new_combo_count

        # Precomputed once — every OOS window's own (hi, lo, cl, n) slice, shared by
        # the initial retest round AND every later retry round below (the underlying
        # fetched data never changes within one optimize_symbol_interval call, only
        # which candidates get tested against it).
        oos_windows = {}
        for oos_h in OOS_HOURS_LIST:
            oos_n = _oos_bars(interval, oos_h)
            # Same GC warm-up padding as the IS window above. The data-sufficiency gate
            # above (n < is_n + max_oos + GC_WARMUP_BARS) guarantees n - oos_n >=
            # GC_WARMUP_BARS for every oos_h in OOS_HOURS_LIST, since max_oos is the
            # largest oos_n any of them can produce — so this max(0, ...) is a defensive
            # floor, not the thing actually relied on for correctness.
            oos_pad_start = max(0, n - oos_n - GC_WARMUP_BARS)
            oos_windows[oos_h] = (hi[oos_pad_start:], lo[oos_pad_start:], cl[oos_pad_start:], oos_n)

        def _better(a, b):
            """True if candidate `a` should replace `b` as the running best (explicit
            user ask, "it should keep backtesting each run until min 60 wr" — the
            floor itself later switched from win_rate to `_clears_target`'s three
            explicit targets, 2026-09-01, see that function's docstring): a candidate
            that clears the targets always beats one that doesn't, regardless of
            sharpe — otherwise a later retry round finding a genuine target-clearing
            candidate could lose to an earlier higher-sharpe-but-non-clearing one,
            which would defeat the point of retrying at all. Within the same
            target-clearing status (both clear it, or neither does), sharpe still
            breaks the tie, same comparison this used before the retry feature
            existed."""
            if a is None: return False
            if b is None: return True
            a_ok = _clears_target(a); b_ok = _clears_target(b)
            if a_ok != b_ok: return a_ok
            return a["sharpe"] > b["sharpe"]

        def _oos_retest_src(src, top_n):
            """Runs the OOS retest for one source's IS-ranked top_n candidates across
            every OOS_HOURS_LIST window, saving each window's own oos{h}h.json file
            (unchanged from before the retry feature), and returns whichever single
            candidate is `_better` across all windows (or None if nothing survived)."""
            local_best = None
            for oos_h, (hi_oos, lo_oos, cl_oos, oos_n) in oos_windows.items():
                oos_results = []
                for p in top_n:
                    m = _bt_combo_pair(p, hi_oos, lo_oos, cl_oos, oos_n, bpy, lev,
                                       initial_equity=INITIAL_EQUITY, entry_source=src)
                    if m:
                        oos_results.append({**p, **m, "oos_hours": oos_h})

                if oos_results:
                    oos_results.sort(key=lambda x: x["score"], reverse=True)
                    best_oos = oos_results[0]
                    _log.info(f"OOS {oos_h}h {symbol} {interval}m [{src}]: sharpe={best_oos['sharpe']:.3f} "
                              f"ret={best_oos['total_ret_pct']:.1f}% trades={best_oos['trades']} "
                              f"wr={best_oos['win_rate']:.1%}")
                    out_path = os.path.join(DATA_DIR,
                        f"eth_trader_results_{symbol}_{interval}m_{src}_oos{oos_h}h.json")
                    with open(out_path, "w") as f:
                        json.dump({**best_oos, "symbol": symbol, "interval": interval, "leverage": lev,
                                   "run_ts": datetime.now(timezone.utc).isoformat()}, f, indent=2)
                    if _better(best_oos, local_best):
                        local_best = best_oos
            return local_best

        # Single pass per call (retrying across ALL of a symbol's intervals together,
        # not per-interval in isolation, moved up to BacktestRunner._run() on
        # 2026-08-31 — explicit user ask, "sweep through all 5m 15m backtests. then
        # assess if non 60wr or above and then run all again": a symbol only needs ONE
        # of its intervals to clear 60% WR to become tradeable via
        # _load_all_worthy_crypto's per-symbol best-interval pick, so retrying THIS
        # interval alone until IT individually clears 60% — even after a SIBLING
        # interval already qualified — was wasted work under the previous per-interval
        # version of this retry loop. See BacktestRunner._run() for the actual retry
        # decision now; this function just reports `new_combo_count` (see above) so
        # that outer loop can tell whether another pass is worth attempting.
        best_result = {"searched": None, "pine": None}
        for src in ("searched", "pine"):
            top_n = top_n_by_src[src]
            if not top_n:
                continue
            best_result[src] = _oos_retest_src(src, top_n)

        for src in ("searched", "pine"):
            if best_result[src]:
                out_path = os.path.join(DATA_DIR, f"eth_trader_results_{symbol}_{interval}m_{src}.json")
                with open(out_path, "w") as f:
                    json.dump({**best_result[src], "symbol": symbol, "interval": interval, "leverage": lev,
                               "run_ts": datetime.now(timezone.utc).isoformat()}, f, indent=2)
                status_dict[f"{symbol}_{interval}_{src}"] = best_result[src]
                # Only a genuinely profitable result is a "winner" worth permanently
                # retesting forever (see _is_winner's docstring — `_clears_target`
                # itself stopped meaning anything here once every selection threshold
                # was removed 2026-09-03). `best_result[src]` can also be the
                # best-available-but-losing candidate when nothing this cycle made
                # money at all (see `_better`'s fallback), and that one should keep
                # competing on its own merits via random search, not get baked into
                # the permanent priority-retest list.
                if _is_winner(best_result[src]):
                    db_save_winner(symbol, interval, src, best_result[src])
            elif top_n_by_src[src]:
                status_dict[f"{symbol}_{interval}_{src}"] = "no OOS winners"

        return new_combo_count

    except BrokenProcessPool:
        # A caller-supplied executor is reused across every (symbol, interval) pair for
        # a whole cycle (see BacktestRunner._run) — once broken, every subsequent
        # submit()/result() against it raises the same error, so this must propagate
        # rather than being swallowed as a generic per-pair error: the caller needs to
        # know to recreate the pool, or every remaining pair this cycle silently fails
        # the same way.
        _set("ERROR: worker process pool broken")
        raise
    except Exception as e:
        _set(f"ERROR: {e}")
        _log.exception(f"optimize_symbol_interval {symbol} {interval}m: {e}")
        return 0


def replay_recent_trades(sess, symbol, interval, params, lev, days=2, entry_source="searched"):
    """Walk-forward replay of the last `days` of fresh bars for one symbol/interval,
    using its own already-selected winning params — no new parameter search, just the
    same _bt_combo_pair signal logic paper/live actually run, simulating what paper
    trading would have done over that window. For the periodic missed-trade report (see
    eth_trader.py's _report_missed_trades). entry_source (added 2026-08-28 —
    see PINE_GC_SQRT2's docstring): must match whichever source actually won this
    symbol's backtest and is what paper/live is really trading on, or this replay
    predicts entries off a signal paper never used. Returns a list of {"side",
    "entry_ts"} dicts, one per entry these params would have taken — exits aren't
    tracked here, only entries, since a "did paper miss this" check only needs to know
    what should have opened, not how it would have closed."""
    df = fetch_ohlcv(sess, symbol, interval)
    hi = df["high"].values; lo = df["low"].values; cl = df["close"].values

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    mask = df.index >= cutoff
    if not mask.any():
        return []
    start_i = int(np.argmax(mask))  # first bar within the window

    backtest_bars = len(cl) - start_i
    _, entries = _bt_combo_pair(params, hi, lo, cl, backtest_bars, _bars_per_year(interval), lev,
                                record_entries=True, entry_source=entry_source)

    return [{"side": side, "entry_ts": df.index[idx]} for idx, side in entries]


# ── Windows: kill worker processes when this console window closes ────────────
def _win_kill_on_close():
    """Best-effort safety net, NOT the primary cleanup path — callers must still shut
    down their own ProcessPoolExecutor pools deterministically before exit. Returns True
    if the Job Object was actually created and this process assigned to it, False
    otherwise (logged as a warning either way it fails) — never raises, so a failure here
    never blocks startup, but it's no longer silent."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes.wintypes as wt
        class _BLI(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                        ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wt.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wt.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wt.DWORD),
                        ("SchedulingClass", wt.DWORD)]
        class _IOC(ctypes.Structure):
            _fields_ = [(f, ctypes.c_ulonglong) for f in
                        ("ReadOps","WriteOps","OtherOps","ReadBytes","WriteBytes","OtherBytes")]
        class _ELI(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", _BLI), ("IoInfo", _IOC),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wt.HANDLE
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        k32.SetInformationJobObject.restype = wt.BOOL
        k32.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
        k32.AssignProcessToJobObject.restype = wt.BOOL
        k32.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
        k32.GetCurrentProcess.restype = wt.HANDLE

        job = k32.CreateJobObjectW(None, None)
        if not job:
            _log.warning(f"_win_kill_on_close: CreateJobObjectW failed "
                         f"(error {ctypes.get_last_error()}) — orphaned backtest worker "
                         f"processes are NOT protected against on this run")
            return False
        eli = _ELI(); eli.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(job, 9, ctypes.byref(eli), ctypes.sizeof(eli)):
            _log.warning(f"_win_kill_on_close: SetInformationJobObject failed "
                         f"(error {ctypes.get_last_error()}) — orphaned backtest worker "
                         f"processes are NOT protected against on this run")
            return False
        if not k32.AssignProcessToJobObject(job, k32.GetCurrentProcess()):
            _log.warning(f"_win_kill_on_close: AssignProcessToJobObject failed "
                         f"(error {ctypes.get_last_error()}) — orphaned backtest worker "
                         f"processes are NOT protected against on this run (this process "
                         f"may already belong to another job without breakaway rights)")
            return False
        return True
    except Exception as e:
        _log.warning(f"_win_kill_on_close: unexpected error setting up the kill-on-close "
                     f"Job Object ({e}) — orphaned backtest worker processes are NOT "
                     f"protected against on this run")
        return False
