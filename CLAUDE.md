# Unified Combo Grid GUI

Bybit ATR_PARTIAL combo backtester + paper/live trader — **Grid fork** of
`unified_combo_gui`, forked 2026-08-28. One exe does everything:
`unified_combo_trader_grid.exe`. `eth_trader_bt.py` is a pure library (no `main()`,
no standalone TUI/exe) imported and driven in-process by the trader's Backtest tab.
Full reference: `.claude/skills/unified-combo/SKILL.md` — read it before making any
non-trivial change here.

**User-facing app name changed to "ETH Trader" (2026-09-04, explicit user ask,
"Rename this bot to ETH TRADER").** This is the macOS `.app` bundle's Finder/Dock name
and the GUI window title only — the internal exe/module names
(`unified_combo_trader_grid`, `eth_trader.py`, `eth_trader_bt.py`) are
unchanged, deliberately, per the process-name-collision risk described under "Hard
invariants" below. The project/codebase itself keeps being called "Unified Combo Grid"
in this document and elsewhere; only the shipped product name changed.

**This checkout is the macOS port of the Grid fork** (ported 2026-09-01/02, commit
"Port secure key storage to macOS Keychain, fix Windows-only imports"). Most of the
history below predates the port and was written against Windows — read it for the
*feature/behavior* history (all of that carried over unchanged), but for anything
platform-specific, this note and the "macOS-specific facts" bullets under "Working
here"/"Hard invariants" below are authoritative, not the older Windows-era text:
- **Key storage**: macOS Keychain (`security` CLI, service `unified-combo-grid`,
  accounts `demo`/`live`) via `subprocess`, not Windows DPAPI. No `keys/*.dat` files are
  ever written on this platform — `save_keys`/`load_keys_secure`/`has_keys`/
  `delete_keys` in `eth_trader.py` branch on `sys.platform`; the `darwin`
  branch never touches `KEYS_DIR`. The `keys/` directory in this repo (and inside the
  built `.app`) is a vestigial empty folder, kept only because `unified_combo_gui`'s
  original code referenced `KEYS_DIR` — nothing reads or writes files in it on macOS.
- **Packaging**: a real `.app` bundle (`ETH Trader.app`, via
  `eth_trader_mac.spec`'s `BUNDLE()`), not a Windows onedir folder + `.exe`.
  Built with PyInstaller same as Windows, but no UPX (`upx=False` in the mac spec — UPX
  is a Windows/Linux-oriented compressor, skipping it just means a larger, uncompressed
  `.app`, not a build failure).
- **Data persistence**: the installed `/Applications/ETH Trader.app/Contents/
  MacOS/data` (and `/keys`, vestigial) are REAL directories physically inside the
  bundle — not Windows-style directory junctions pointing back at a project-root
  `data/`/`keys/`. A rebuild produces a fresh `.app` with its own empty `data/`; the
  real accumulated `data/` (trade DBs, params DB, logs, result JSONs) must be copied
  into the new bundle by hand after building, or it's lost. See the (mac-native)
  `uc-build` skill for the actual copy-preserve procedure.
- **Process management**: this is a normal POSIX process — plain `kill`/`kill -9` (or
  `pkill -f`) reliably terminates it; there is no git-bash/Windows-emulation
  unreliability to work around, and no `tasklist`/`taskkill`/PowerShell involved. Use
  `ps aux | grep -i unified_combo` to find PIDs (the app plus its
  `ProcessPoolExecutor` backtest workers, which show up as separate
  `--multiprocessing-fork` processes and must be swept too).

## What's different from `unified_combo_gui` (the source repo)

- **The ATR_STOP shadow strategy is gone entirely.** `AtrStopPaperBot`,
  `compute_stop_signals`, `GC_PERIOD_STOP`/`GC_POLES_STOP` (the fixed-GC signal
  source), `PARTIAL_TP_FRACTION`/`STOP_TP_FRACTION` are all removed. There is exactly
  one strategy/bot (`AtrPartialPaperBot`) and one exit mechanism (the ATR grid, below)
  — but see the next bullet: the OLD stop-vs-partial dual-candidate system's shape came
  back in a new form, competing on ENTRY signal only, not on strategy/exit.
- **"pine" entry source — added 2026-08-28, explicit user ask** ("add it to the bt.
  entry logic. exit same as the bot. it must show results for it. paper should use it
  if it wins in bt"), **corrected same day** ("pine needs to search params too" /
  "stop. focus on making pine strat in bt tests best params"): a second entry-signal
  candidate competes against the existing "searched" one, per (symbol, interval).
  **Pine is NOT a fixed-parameter preset** — my first pass wrongly locked it to the
  Pine Script's literal default values (`k_len=21` etc.) via a since-deleted
  `PINE_ENTRY_PARAMS` dict; the user corrected this. Both "searched" and "pine" search
  the same 9 entry params (k_len/k_smooth/d_smooth/ob/os/chop_len/chop_thr/
  gc_period/gc_poles), via the same kind of random sweep — as of this same-day
  correction, over the exact same range (`PARAM_SPACE`) for both, so the **only**
  difference at that point was the Gaussian Channel formula's constant: pine uses the
  user's "Stochastic Triple Filter [ATP]" Pine Script's hardcoded `1.414` GC constant
  (`bt.PINE_GC_SQRT2`) instead of the mathematically exact `math.sqrt(2)` — see
  `gaussian_channel_midline`'s `sqrt2` param. **That range-equality didn't last the
  same day** — see the very next bullet, "searched" gets a wider search range shortly
  after this. `entry_source`
  ("searched" or "pine") is threaded through `_bt_combo_pair`/`compute_partial_signals`/
  `_parse_result_file`/`AtrPartialPaperBot.entry_source`/`ComboTrader.entry_source`
  (a property reading the bot) purely to select which `sqrt2` value to pass into
  `gaussian_channel_midline` — every other param always comes from `params` for both
  sources. **The exit mechanism (ATR grid: `stop_mult`/`grid_levels`/`grid_dist_1..8`/
  `grid_frac_1..8`) is likewise always searched/optimized for BOTH sources**, per the
  "exit same as the bot" ask. Two independent result files per (symbol, interval)
  again —
  `eth_trader_results_{symbol}_{interval}m_{searched,pine}.json` — each its own
  IS-ranked/OOS-retested/saved candidate (mirrors the pre-existing stop/partial
  six-row mechanism this repo already had once, just renamed/recontextualized). The
  Backtest tab has an "Entry" column and two rows per (symbol, interval) again. Leg
  selection (`_load_all_worthy_crypto`'s hundred/eighty_plus rule) needed **no code
  change at all** — it already scans every result file by glob pattern, so it
  naturally treats "pine" and "searched" candidates as just more entries in the same
  pool and picks whichever wins on its own merits; paper/live automatically starts on
  (or switches to, via `_param_reload_loop`) whichever source is currently winning for
  a symbol.
- **"searched" gets a 3x wider entry-param search range than "pine" — added
  2026-08-28, explicit user ask** ("widen the param values search range x3 for
  searched"): `bt.PARAM_SPACE_SEARCHED` is a copy of `bt.PARAM_SPACE` with the 9
  entry-signal params widened (k_len 10-40→30-120, k_smooth 1-5→3-15, d_smooth
  3-10→9-30, os 10-30→30-90, chop_len 8-20→24-60, gc_period 50-250→150-750, gc_poles
  1-9→3-27 — all via both-bounds-x3; ob 70-90→50-100 and chop_thr 38-62→14-86 via a
  centered-triple-width instead, since both-bounds-x3 collapses their 0-100-bounded
  range to a degenerate 100-100 after clipping). Exit/grid params
  (`atr_p`/`stop_mult`/`grid_levels`/`grid_dist_1..8`/`grid_frac_1..8`) are **not**
  copied wider — both sources still search those over the identical `PARAM_SPACE`
  range, preserving the "exit same as the bot" invariant. `_sample()` now takes a
  `space=PARAM_SPACE` arg; `optimize_symbol_interval` builds two independent combo
  lists (`combos_by_src["pine"]` from `PARAM_SPACE`, `combos_by_src["searched"]` from
  `PARAM_SPACE_SEARCHED`) instead of one shared list — a combo sampled for one source
  is now tested ONLY under that source. `_combo_worker` correspondingly takes a fixed
  `src` per call instead of looping both sources per combo (each combo batch is
  submitted once per source it belongs to, not tested under both). `GC_WARMUP_BARS`
  changed from `3 * PARAM_SPACE["gc_period"][1]` (750) to `3 *
  max(PARAM_SPACE["gc_period"][1], PARAM_SPACE_SEARCHED["gc_period"][1])` (2250) — it
  must cover the wider searched ceiling (750) too, or a large-`gc_period` searched
  combo would get an undersized GC filter warm-up window. Verified: `PARAM_SPACE`
  itself unchanged; `PARAM_SPACE_SEARCHED`'s 9 entry params widened exactly as above
  with the 5 exit params byte-identical to `PARAM_SPACE`; `_sample(PARAM_SPACE_SEARCHED)`
  draws values outside pine's old bounds for every widened param across 500 draws;
  `GC_WARMUP_BARS` == 2250; `_combo_worker(..., src=X)` tags every output row
  `entry_source == X`, never the other source.
- **Exits are a grid of ATR-multiple take-profit levels, not a single fixed TP +
  stochastic-triggered 50% partial.** At entry, `grid_levels` price levels are laid
  out via `bt.grid_level_prices(entry, atr, side, levels, grid_dists)` — level *i*'s
  distance is the CUMULATIVE SUM of that level's own independently-searched ATR
  increment (`grid_dist_i`) plus every increment before it, not a single uniform
  `grid_atr_mult*(i+1)` any more (**made per-level 2026-08-28, explicit user ask**:
  "test number of grids is optimal in bt. and where they should be set" +
  "independent distance per level"/"independent fraction per level" — see
  `grid_level_prices`' docstring). Each level closes **its own** independently-
  searched fraction (`grid_frac_i`) of the ORIGINAL entry qty except the last, which
  always closes whatever remains — guaranteeing the position always fully exits once
  every level has filled (or the stop hits first). The stop trails to breakeven after
  the first fill, then to the previous filled level's price after each subsequent
  fill — profit already banked at a lower level can never be given back once a higher
  one fills. `grid_levels` (int, 2–8), `grid_dist_1..8` (float, 0.3–2.5 each) and
  `grid_frac_1..8` (float, 0.1–0.4 each) are searched params in `bt.PARAM_SPACE`, same
  mechanism as `stop_mult`/`k_len`/etc. — **the backtester's random sweep optimizes
  both the grid's shape AND how much closes at each level**, not just the
  entry-signal params. The cumulative-sum construction guarantees levels are always
  monotonically farther from entry as `li` increases, regardless of what each
  increment samples to — required for the sequential fill-scanning loop (both
  `_sim_grid_jit` and `_next_grid_hit`'s live mirror) to stay correct. A candidate
  saved before this per-level design falls back to its old `grid_atr_mult`/
  `grid_level_frac` scalar (replicated across every slot via `params.get(f"grid_dist_
  {i}", params.get("grid_atr_mult", 1.0))`), which reconstructs its exact old
  uniform-grid behavior — verified byte-identical in `test_per_level_grid.py`.
  `eth_trader.py`'s live `tick()` and its live-position-seeding path both
  call the SAME `bt.grid_level_prices` function the backtester's pure-Python twin
  uses, so live and backtest can never silently diverge on how grid prices are
  built (the JIT hot path still hand-inlines its own copy — numba nopython mode
  can't call back into it — "keep both in sync by hand," same as the pre-existing
  JIT/pure-Python twin pattern). `tp_mult` and `partial_lvl` (the old stochastic
  partial-exit-level param) are gone from `PARAM_SPACE` entirely — there is no more
  single TP distance or stochastic exit trigger to search for.
- **Cross-down TP-capture — added 2026-08-28, explicit user ask** ("i want it to close
  on a cross down the grid", clarified over several rounds: "any previously filled
  level, not just the most recent" triggers it, using a plain threshold (not a
  two-point cross-detection), landing straight on a level's own price after it fills
  rather than lagging one level behind, closing that level's own fraction (a "mirrored
  unwind" of *current* remaining qty, not undoing the earlier fill) — then explicitly
  confirmed **"the stop loss stays the same as original. this is purely capturing
  TP."**). **The stop-loss (`sl`, its trailing formula, its full-close-on-hit
  semantics) is completely untouched by this feature — not one line of that logic
  changed.** Separately and independently: if price crosses STRICTLY below (long) /
  above (short) an already-filled-but-not-yet-unwound level, that level's own
  `grid_frac_i` of CURRENT remaining qty closes (`_sim_grid_jit`'s `unwound` bool
  array / `_bt_combo_pair`'s pure-Python twin's `unwound` list / live's
  `pos["grid_unwound"]`, one bool per level). Each level can unwind at most once —
  no re-arming — but a NEW fill (a level filling for the first time) makes that new
  top level freshly eligible for its own future unwind, independent of older levels'
  unwind history. Scans from the most-recently-filled level downward, skipping
  already-unwound ones, stopping at the first level whose price hasn't been crossed —
  correct without any explicit "stop at sl" special case, since `grid_px` is
  monotonically increasing with index and this code path only runs once `_hit_sl`
  has already returned false for the current price: in practice this means **at most
  one level ever unwinds before the (unchanged) stop-loss takes over**, because sl's
  own unchanged formula (`entry` after the 1st fill, `grid_px[filled-2]` after each
  later one) always sits exactly one level behind whichever level would be next to
  unwind. Strict `<`/`>` (not `<=`/`>=`) means the level that just filled THIS bar
  can never also immediately unwind in the same check. `eth_trader.py`
  adds `_next_grid_unwind_idx()` (mirrors `_next_grid_hit`'s pattern) and
  `_partial_unwind()` (mirrors `_partial_grid`, but reads `grid_fracs[ui]` for
  whichever level index unwound rather than `grid_filled`, and returns `True`/closes
  the whole position via `_close()` if qty hits ~0 — there's no "last one closes all"
  rule on the unwind side the way there is for up-fills, since the unchanged
  stop-loss is still what guarantees eventual full exit). `paper_position` gained a
  `grid_unwound` TEXT (JSON bool list) column via another `ALTER TABLE` migration in
  `get_db()` (third such migration this same day, after `entry_source` and
  `grid_fracs`) — `NULL` for an already-open pre-2026-08-28 position defaults to "
  nothing unwound yet" on load, the same conservative default used for a freshly
  -adopted live position with no local history. Verified: a hand-crafted deterministic
  price path (fill → unwind → stop-out, all one trade) cross-checked against an
  independently-written from-scratch reference of the OLD no-unwind behavior for the
  identical path, proving the new behavior is strictly better (exits part of the
  position at the retrace price instead of riding it all the way to the stop) and
  that the stop's own trigger price/full-close semantics are byte-identical to
  before; an oscillating multi-retrace path still produces exactly one trade (no
  double-close bug); JIT vs pure-Python agree exactly on random data with the
  mechanism active; a live `AtrPartialPaperBot` run through `tick()`/`check_exit()`
  produces the exact expected fill→unwind→stop-at-breakeven trajectory with `sl`
  unaffected by the unwind event; `paper_position.grid_unwound` migration safe
  against a copy of the real production DB.
- **1-minute exit feed removed entirely — added 2026-08-31, explicit user ask** ("get
  rid of 1 minute candle shit. same for entries as exit"): `ComboTrader` used to run a
  second, stateless WS feed (`_ws_loop_1m`/`_on_kline_1m`) subscribed to 1-minute klines
  purely so an open position's SL/grid-level crossing could be caught up to ~29 minutes
  sooner than waiting for the next entry-timeframe (15m/30m) bar close — that feed,
  `_force_reconnect_1m`/`_last_kline_ts_1m`, and `_PaperBotBase.check_exit()` (its entry
  point, which just called `_manage_exit()`) are all gone. Exits now check only inside
  `tick()`'s own `_manage_exit()` call, on the same entry-timeframe bar close as entry
  signals — entries and exits are on one cadence again. `start()` now spawns only the
  one `ws-{symbol}` thread (no `ws1m-{symbol}`). Alongside this, `bt.CRYPTO_INTERVALS`
  (and `_DEFAULT_CONFIG["crypto_intervals"]`/`data/eth_trader_config.json`) dropped
  from `["5","15","30"]` to `["5"]` only that same day, so worst-case exit-check latency
  was bounded at 5 minutes rather than the up-to-30-minutes a slower entry-timeframe
  alone would otherwise allow — **`"15"` was added back the same day** ("add 15m to
  it"), making it `["5","15"]`; latency is now bounded by whichever interval a given
  leg's own symbol is currently trading (still never faster than 1 minute, since the
  1-minute feed itself is gone for good).
- **Tiny-win filter (`WIN_FEE_MULT`) — added 2026-08-31, explicit user ask** ("tiny
  trades should never be counted as wins"): a closed trade only counts toward
  `win_rate`/`gw`/`mw` if its `part_pnl` (already fee-netted) exceeds
  `WIN_FEE_MULT = 2.0` times that trade's own separately-tracked total round-trip
  `fees` — the old bar was simply `part_pnl > 0`. A trade that barely edged out what it
  paid in fees is no longer a "win"; it still counts as a trade and its `part_pnl` flows
  into `gl`/`ml` like any other non-win. Applied identically, at every win-check site (SL
  close, each grid-level fill, each cross-down unwind, forced end-of-data close), in both
  `_sim_grid_jit` (JIT hot path) and `_bt_combo_pair`'s pure-Python twin — the two must
  stay in sync by hand, same as every other JIT/pure-Python pair in this file.
- **Minimum raw price-move floor (`MIN_WIN_PRICE_PCT`) — added 2026-08-31, explicit
  user ask** ("only params where each trade is no less than .33% before leverage profit
  makes it through"): a trade only counts as a win if, ON TOP OF clearing `WIN_FEE_MULT`,
  its whole-trade qty-weighted average exit price also moved at least `0.33%` from entry
  — raw underlying price move, BEFORE leverage (i.e. NOT the leveraged equity return,
  which at `LEVERAGE=11` would be roughly 11x larger for the same price move). Only
  applies to what counts as a win — a losing trade's own `gl`/`ml`/`eq` are untouched.
  `exit_notional` (new running total, reset to 0 at entry alongside `part_pnl`/`fees`)
  accumulates `close_price * qty_closed` across every exit/unwind leg of the current
  trade, so `exit_notional / qty0` gives the qty-weighted average exit price at the
  moment the trade fully closes, regardless of how many grid levels fired —
  `raw_pct = (avg_exit - entry)/entry` for a long, sign-flipped for a short. Applied at
  the same 7 win-check sites as `WIN_FEE_MULT`, in both `_sim_grid_jit` and
  `_bt_combo_pair`. **Found and fixed a real bug while adding this**: `_sim_grid_jit`'s
  non-win branch was `gl += -part_pnl` (correct only when `part_pnl` is negative — a
  true loss); once `WIN_FEE_MULT`/`MIN_WIN_PRICE_PCT` route small POSITIVE-`part_pnl`
  trades into that same branch, `-part_pnl` goes negative and corrupts `gl` (verified:
  a synthetic $3.02-profit trade that failed the new floor produced `gl=-3.0154`
  instead of `+3.0154`) — `_bt_combo_pair`'s pure-Python twin already used
  `abs(part_pnl)` correctly, so the two branches had silently disagreed on `gl`/`ml`/
  `profit_factor` since the `WIN_FEE_MULT` commit, whenever a marginal-but-positive
  trade occurred. Fixed all 7 sites in `_sim_grid_jit` to `abs(part_pnl)`, matching the
  pure-Python twin. Verified: a hand-crafted 0.25%-move trade (clears `WIN_FEE_MULT`,
  fails the new 0.33% floor) correctly produces `w=0, gl=+3.0154`; a 0.40%-move trade
  clears both and produces `w=1`; a 60-trial cross-check running real `_bt_combo_pair`
  (both entry sources, both `PARAM_SPACE`/`PARAM_SPACE_SEARCHED`) over a 3000-bar random
  walk found the JIT and pure-Python branches agree exactly on all 14 result fields
  (including `win_rate`/`profit_factor`/`cum_loss`/`max_loss`) in 60/60 trials.
- **Zero-fill-reversal penalty in `score` — added 2026-08-31, explicit user ask**
  ("how do we make this profitable instead", after a real-data check — see below —
  confirmed ~20% of entries on both current param sets reverse straight to the stop
  without ever filling a single grid level). `score` (`sharpe * sqrt(trades/
  MIN_TRADES)`) previously had no idea whether a param set's trades banked something
  at a grid level or lost cleanly — a high clean-loss rate could still win the sweep if
  other trades compensated on the aggregate equity curve. New `zf` counter (alongside
  `t`/`w`/`gw`/`gl`) increments whenever a trade closes — by any reason: SL, forced
  end-of-data — with `filled` still 0; checked at all 7 close sites in both
  `_sim_grid_jit` (whose return tuple grew an 9th element, `zf`, before `curve`) and
  `_bt_combo_pair`'s pure-Python twin, same pattern as `WIN_FEE_MULT`/
  `MIN_WIN_PRICE_PCT`. `zero_fill_rate = zf/trades` is now a new 15th result field and
  multiplies straight into `score` as a linear penalty:
  `score = sharpe * sqrt(trades/MIN_TRADES) * max(0.0, 1 - zero_fill_rate)` — a param
  set where every trade reverses clean to the stop scores 0 regardless of raw sharpe; a
  param set with none of that failure mode is unaffected. This pushes the search toward
  params that either place `grid_dist_1` close enough to bank something before
  reversing, or have cleaner entries that reverse less often, rather than only ever
  being judged on the aggregate equity curve. **The real-data check that motivated
  this**: a live replay of both currently-saved ETHUSDT 5m param sets (searched,
  pine) against real fetched price history (5,000 bars, 2026-08-14 to 2026-08-31)
  found searched reversed clean-to-stop on 5/24 entries (20.8%, median 37 bars/~3h to
  the stop — a slow grind, not a snap reversal) and pine on 13/65 (20.0%, median 6
  bars/30min, with 4/13 hitting the stop within 15 minutes — genuinely fast). Verified
  the new mechanism itself with hand-crafted `_sim_grid_jit` cases: a trade that
  reverses to the stop before any level fills produces `zf=1`; an otherwise-identical
  trade that fills level 1 first, THEN reverses to the (now-breakeven) stop, produces
  `zf=0` — confirming the counter only fires on the genuine zero-protection case, not
  every eventual loss. A 60-trial JIT-vs-pure-Python cross-check (both entry sources)
  found exact agreement on all 15 fields including `zero_fill_rate`, and confirmed
  `score`'s formula matches its expected value exactly against the returned
  `sharpe`/`trades`/`zero_fill_rate` in every trial.
- **Reverse-and-flip (`flip_on_signal`) — added 2026-08-31, explicit user ask**
  ("so there is no mechanism possible that detects when it is worth changing the trade
  to take advantage of potential profit?" → chose "reverse-and-flip" from three
  offered options, clarified via AskUserQuestion: fires on ANY open position
  regardless of fill state, and is a new SEARCHED 0/1 param rather than always-on).
  If the entry signal flips against an open position before the stop is hit — reusing
  the exact same `buy`/`sell` arrays the entry logic already computes, no new
  indicator — close it and immediately open the opposite side at the same price,
  instead of waiting for the ATR-distance stop to eventually trigger. The same move
  that would have stopped out the old position becomes the entry for the new one.
  Checked in `_sim_grid_jit`/`_bt_combo_pair` ONLY after the stop-loss check already
  came back False this bar (SL always wins if both would trigger the same bar) and
  BEFORE the grid-fill/unwind checks (a flip signal means the entry thesis is already
  invalidated — no point evaluating whether this bar also crossed a grid level for a
  position about to close). `flip_on_signal` is a new `PARAM_SPACE` entry (0/1,
  sampled like any other `_INT_PARAMS` member — no special-casing needed in
  `_sample()`), living in the base `PARAM_SPACE` (inherited as-is by
  `PARAM_SPACE_SEARCHED`, never widened) since it's an exit-behavior param like
  `stop_mult`/`grid_levels` — "exit same as the bot" applies here too. Required a real
  DB schema change (unlike `WIN_FEE_MULT`/`MIN_WIN_PRICE_PCT`/`zero_fill_rate`, which
  only touch in-memory result fields): `param_runs` gained a nullable
  `flip_on_signal INT` column (`db_init`'s third `ALTER TABLE` this same day, after
  `_GRID_COLS`), and `_param_key()` — the dedup key `_gen_combos` checks against the
  DB's "already tried" set — had to include it too, or two combos differing ONLY in
  `flip_on_signal` would hash identically and the sweep would silently only ever test
  one of the two values for that combo. `db_save`/`db_load_top`/`_grid_select_cols`
  all updated to read/write the new column, with `COALESCE(flip_on_signal,0)` treating
  any pre-existing cached combo as flip-off (the only honest default for something
  that predates the concept of flipping).

  **Live side** (`eth_trader.py`): `_manage_exit()` gained
  `buy_now`/`sell_now`/`atr`/`stop_m`/`sym`/`iv` params (the current bar's
  already-computed signal, threaded from `tick()`) and a flip check between the SL
  check and the grid/unwind loops, exactly mirroring the backtest's ordering. New
  `_flip()` method closes via the existing `_close(price, "FLIP")` (proper trade
  recording, live-exchange mirroring via `self.live.mark_closed`) then reopens using
  the identical qty/fee/`grid_px`/`sl` construction `tick()`'s own flat-branch entry
  logic uses (same `bt.grid_level_prices` call live and backtest already shared) — so
  live and backtest can't silently diverge on how a flip is built, same invariant as
  every other exit mechanism here. Guards `atr<=0` (a live-only edge case — the
  backtest's `bad[idx]` mask already guarantees `atr_arr[idx]` is valid at any bar a
  signal can be true, so this has no backtest equivalent) by leaving the bot flat
  rather than flipping into a degenerate position.

  **Verified**: hand-crafted `_sim_grid_jit` scenario (long entry, price drifts down
  without hitting SL, a sell signal fires, THEN price crashes further) confirmed
  `flip_on=False` rides the original long into its SL loss (`t=1`) while
  `flip_on=True` flips to short at the signal bar and profits from the subsequent
  crash (`t=2`, final equity far higher) — and that flip-off behavior is byte-for-byte
  unchanged from before this feature (regression-safe default). A 60-trial + 400-trial
  JIT-vs-pure-Python cross-check with `flip_on_signal` forced on found exact agreement
  on all 15 fields in every trial that ran, though neither synthetic dataset happened
  to trigger the flip branch itself (a full oversold-to-overbought stochastic swing
  while still in position is a demanding, comparatively rare condition — the
  "immediate reversal" failure mode `zero_fill_rate` targets tends to keep the
  stochastic pinned near its original oversold/overbought zone rather than swinging
  all the way through). **Confirmed against real ETHUSDT 5m market data**: out of 602
  real param combos tried (both currently-saved best results plus 600 freshly sampled
  combos), exactly 1 genuinely triggered the flip branch — and JIT vs pure-Python
  agreed exactly on all 15 fields for that run. A separate DB round-trip test
  confirmed `_param_key` correctly distinguishes `flip_on_signal=1` from `=0` on
  otherwise-identical params (dedup would otherwise silently collapse them), and that
  `db_load_top` correctly recovers both flip values through a save/reload cycle. Given
  how rarely the trigger condition arises even across hundreds of real param combos,
  don't expect this to visibly change search outcomes often — it's a real, correctly
  wired mechanism, just a naturally infrequent one given how it's defined.
- **Retry-until-60%-WR sweep, 60% replaces 80% as the second selection tier's floor,
  and an already-running leg now pauses on new entries once its symbol has nothing
  clearing 60% WR — all added 2026-08-31, explicit user ask**, refined over several
  rounds: "it should keep backtesting each run until min 60 wr" (clarified via
  AskUserQuestion into: retry as an inner loop within the same cycle rather than just
  relying on the existing 2h auto-repeat — "if not reached test again"; capped by
  wall-clock time — "5 minutes per source"; initially built as a brand-new third
  selection tier below the existing 80% one) → then corrected twice more in quick
  succession: **"60 replaces 80"** (the just-added third tier was collapsed back down —
  the *existing* second tier's floor simply moved from 80% to 60%, restoring the
  original two-tier shape rather than adding a third), and **"if there is no params
  above 60wr then paper pauses trading until there is"** (a second, independent
  mechanism — see below). Three parts:
  - **Search side** (`eth_trader_bt.py`, `optimize_symbol_interval`) — **this
    per-interval version was SUPERSEDED 2026-09-01, see the "Per-symbol, all-intervals
    retry" bullet below**; kept here as the historical record of how the mechanism
    first shipped, unaffected by the "60 replaces 80" correction — this half was always
    about the search, not the selection tiers: after the normal IS sweep + OOS retest, for each non-protected
    entry source whose best OOS-retested candidate hasn't cleared `RETRY_MIN_WR` (0.60)
    yet, keep sampling fresh COMPLETE random batches (each one a full `N_RANDOM` sweep,
    same `PARAM_SPACE`/`PARAM_SPACE_SEARCHED`, same `tried` dedup set — no combo is
    ever retested) and OOS-retesting them, until the floor clears. **No time limit** —
    a first version capped retries at `RETRY_TIME_BUDGET_S` (5 min per source, explicit
    user ask at the time); removed the same day on a further explicit ask ("it needs to
    complete all sweeps [then] if none above 60wr go through them again" → confirmed via
    AskUserQuestion: drop the cap, keep retrying full sweeps indefinitely). The only
    thing that still stops the loop short of clearing the floor is `_gen_combos` coming
    back empty — the param space is genuinely exhausted (every combo within
    `N_RANDOM*5` sampling attempts was already in `tried`), verified directly: a
    deliberately tiny 8-distinct-combo param space was fully discovered by
    `_gen_combos` in its first call, and every call after that correctly returned an
    empty list. This is a deliberately accepted risk (explicit user choice): a symbol
    where 60% WR isn't currently reachable can now occupy this one (symbol, interval,
    source)'s retry loop indefinitely, since `BacktestRunner` processes
    (symbol, interval) pairs sequentially within a cycle. A new `_better(a, b)`
    comparator (a candidate that clears `RETRY_MIN_WR` always beats one that doesn't,
    regardless of sharpe; sharpe still breaks ties within the same floor-clearing
    status) replaces the old raw-sharpe-only comparison everywhere a "which OOS
    candidate wins" decision is made — including across the 48h/60h OOS windows
    themselves — so a later retry round's genuine 60%+-WR find can never lose to an
    earlier higher-sharpe-but-sub-60%-WR one. If the param space is exhausted without
    ever clearing the floor, whichever candidate scored best across every round
    (initial + retries) is what gets saved, same "always save the best found" behavior
    as before this feature existed — a low-WR result file isn't withheld, it just won't
    clear either selection tier downstream. A protected source (open position) is never
    retried. `optimize_symbol_interval`'s OOS-retest logic was refactored into a
    reusable `_oos_retest_src(src, top_n)` helper so the initial round and every retry
    round share identical logic instead of two hand-copies drifting apart.
  - **Initial leg selection** (`eth_trader.py`, `_load_all_worthy_crypto`):
    back to exactly two tiers, same shape as the pre-2026-08-31 rule — `hundred`
    (best-by-sharpe 100%-WR) and `sixty_plus` (best-by-`cum_profit` candidate with
    `win_rate>=_MIN_WR_60PLUS` [0.60, was `_MIN_WR_80PLUS`/0.80] AND
    `cum_loss<_MAX_CUML_60PLUS` [same $5 cap, renamed from `_MAX_CUML_80PLUS`]) — the
    ONLY change from the 2026-08-28 rule is the win-rate floor itself; `sixty_plus`
    still overrides `hundred` under the exact same condition `eighty_plus` used to
    (exists AND its `cum_profit` is strictly higher), it is NOT a fallback-only tier.
  - **Already-running leg pause** (`eth_trader.py`, `_param_reload_loop` /
    `_load_result_for_symbol`): a completely separate mechanism from initial selection
    — `_load_result_for_symbol` gained a `min_win_rate=None` parameter (threaded into
    its existing `_parse_result_file` call, which already supported the arg), and
    `_param_reload_loop` now passes `min_win_rate=_MIN_WR_60PLUS` alongside its existing
    `require_fresh=True`. This reuses the exact same pre-existing "no fresh result →
    pause new entries, leave the open position and current params untouched" path that
    staleness already triggered (`combo.partial.entries_paused = True`) — a symbol whose
    best current result is fresh but under 60% WR now pauses through that identical
    path, and unpauses the moment a fresh AND floor-clearing result reappears, same as
    it already did for staleness. The open-position rescue scan in `TradingEngine._run()`
    deliberately keeps `min_win_rate=None` (the default) — same reasoning as its existing
    `require_fresh=False` default: it must still hand back params to manage an already-
    open position even when the symbol no longer qualifies, or a rescue would have
    nothing to rescue with.
  Verified (originally, while the time cap still existed — since superseded by the
  no-cap version above, but the loop BODY mechanics this proved are unchanged by that
  removal, only its termination condition changed): a real end-to-end
  `optimize_symbol_interval` run (fetch mocked to synthetic OHLCV, `N_RANDOM`/the
  since-removed time budget shrunk for test speed) completed without error and
  produced valid saved results; with `RETRY_MIN_WR` temporarily forced to 0.99 (so
  retries were guaranteed to activate and never satisfy the floor), the retry loop ran
  exactly 3 extra rounds per source within the (then-still-present) time budget,
  logged its give-up message on deadline, and correctly kept the best candidate found
  across ALL rounds (not just the last one) rather than being overwritten by a worse
  later round — confirming `_better`'s floor-priority logic actually holds under real
  (not just hand-traced) execution. **Re-verified after the time-cap removal**: a
  direct unit test of `_gen_combos`'s exhaustion mechanism (replicated verbatim against
  a genuinely tiny, fully-enumerable 8-distinct-combo param space — `atr_p`(2 choices)
  × `grid_levels`(2) × `flip_on_signal`(2)) confirmed all 8 combos were discovered on
  the first call and every call after that correctly returned an empty list —
  confirming the ONLY remaining stop condition (`if not retry_combos: break`) actually
  fires rather than the loop spinning forever once nothing new is left to try. A
  5-scenario test of `_load_all_worthy_crypto` (rewritten
  after the "60 replaces 80" correction) confirmed: a `sixty_plus`-only candidate gets a
  leg; `sixty_plus` correctly OVERRIDES `hundred` when its own `cum_profit` is higher
  (999 vs 20); `hundred` still wins when ITS `cum_profit` is higher (80 vs 10); a
  `sixty_plus` candidate with `cum_loss>=$5` is excluded; `win_rate==0.60` (the exact
  boundary) is inclusive. A separate 2-scenario test of the pause gate confirmed a
  45%-WR-only result is blocked by `_load_result_for_symbol(..., min_win_rate=0.60)`
  while still visible to a rescue-scan-style call with no `min_win_rate`, and that
  adding a qualifying 70%-WR result immediately clears the gate.
- **Per-symbol, all-intervals retry — added 2026-09-01, explicit user ask** ("sweep
  through all 5m 15m backtests. then assess if non 60wr or above and then run all
  again"): supersedes the per-interval retry loop described above. The problem with
  the old shape: a symbol only needs ONE of its configured intervals (`bt.
  CRYPTO_INTERVALS`) to clear `RETRY_MIN_WR` to become tradeable, via
  `_load_all_worthy_crypto`'s per-symbol best-interval pick — so forcing 15m to keep
  retrying until IT individually hit 60%, even after 5m had already qualified for the
  same symbol, was pure wasted compute. The retry DECISION and LOOP moved out of
  `optimize_symbol_interval` entirely and into `eth_trader.py`'s
  `BacktestRunner._run()`, which already owned the `for sym in bt.SYMBOLS: for iv in
  bt.CRYPTO_INTERVALS:` sweep. New shape, per symbol: sweep every configured interval
  (a full pass), then check whether ANY (interval, source) result for that symbol —
  or a protected one, treated as already-qualifying since it's already live-managed —
  clears `RETRY_MIN_WR`; if not, sweep every interval again from scratch (fresh
  combos, same DB-backed `tried` dedup, no time limit — same "keep going" philosophy
  as the superseded version) until either something qualifies or a full pass finds
  zero genuinely new combos anywhere (`optimize_symbol_interval`'s new
  `new_combo_count` return value, summed across the pass). `optimize_symbol_interval`
  itself is single-pass again — its old internal `while` retry loop, `_gen_combos`
  time budget, and per-source retry status messages are gone; it just reports how
  many new combos it tested via its return value so the outer loop can tell whether
  another pass across the symbol's intervals is worth attempting.

  **A real nuance surfaced while verifying this**: the cross-CALL "already tried"
  persistence only remembers combos `db_save` actually wrote, and `db_save` only
  writes combos that cleared the backtest quality gates
  (`total_ret_pct>0`/`max_dd_pct`/`MIN_RR_RATIO`). A combo that gets sampled and FAILS
  those gates dedupes correctly WITHIN the one call that drew it, but is "forgotten"
  the moment that call ends — the next call's `tried` set rebuilds from the DB, which
  never saw it. Directly verified: a deliberately tiny 8-distinct-combo space whose
  combos never happened to clear quality gates reported the SAME `new_combo_count=8`
  on every one of 5 repeated calls, never converging toward 0. Harmless for the real,
  enormous continuous `PARAM_SPACE` (the search essentially never runs out of
  genuinely fresh float combinations regardless), but it means "genuinely exhausted"
  is a much rarer, weaker signal than "every combo has ever been tried" — closer to
  "every combo that has ever PASSED quality gates has been tried" — which if anything
  reinforces the explicit "just keep retrying, no time cap" intent this was built for,
  but is worth knowing rather than assuming a cleaner exhaustion guarantee exists.
  Verified: a direct unit test of `optimize_symbol_interval`'s new return value
  confirmed a positive `new_combo_count` on a fresh symbol, `0` on the pre-existing
  "not enough data" early-return path, and the tiny-space non-convergence behavior
  above. A separate control-flow-only simulation of `BacktestRunner._run()`'s new
  per-symbol loop (the exact `qualifies` expression and loop structure, against a
  stubbed `optimize_symbol_interval`) confirmed 3 scenarios: both intervals get swept
  on EVERY pass even once one interval is already failing (not short-circuited
  mid-pass), the loop correctly stops the moment ANY interval/source qualifies (here,
  15m qualifying on pass 2 while 5m never does), it correctly stops via exhaustion
  (`new_combo_count==0`) after 3 passes rather than spinning forever when nothing ever
  qualifies, and a protected interval/source is correctly treated as already-
  qualifying so it doesn't force endless resampling around an already-live position.
- **Win-rate abandoned as a selection criterion entirely, replaced by direct
  return/loss/drawdown targets — added 2026-09-01, explicit user ask**, arrived at by
  reviewing a real Backtest tab screenshot together: "which is smart one to trade" →
  (the answer: none of the 4 shown rows cleared the then-current 60% WR floor) →
  "what should the clear % be? analyse the data. i want to profit return 15% or more
  and loss under 5 usdt cuml. dd is a factor too" → confirmed via AskUserQuestion:
  `total_ret_pct>=15% AND cum_loss<$5 AND DD tighter than 8%`, DD ceiling set at 5%.
  **The data that motivated this**: of the 3 real OOS candidates in that screenshot,
  only ONE cleared the new targets (ETHUSDT 15m searched: 20.1% ret, $3.28 cum_loss,
  -3.4% DD) — and it had the LOWEST win_rate of the three (40%, vs 38%/50% for the two
  that failed). Proof that win_rate doesn't track what the user actually wants here:
  this grid+breakeven-trail strategy's whole shape (fewer, larger wins offsetting many
  small/breakeven losses) is exactly what a win-rate floor fights, not measures.
  - **New shared predicate** (`eth_trader_bt.py`): `TARGET_MIN_RET_PCT = 15.0`,
    `TARGET_MAX_CUM_LOSS = 5.0`, `TARGET_MAX_DD_PCT = 5.0`, and `_clears_target(r)` —
    true iff `total_ret_pct >= 15` AND `cum_loss < 5` AND `max_dd_pct > -5` (dd is
    stored negative). Works on both an in-memory result dict and a loaded result JSON
    (same field names either way), and explicitly tolerates non-dict input (returns
    False rather than raising) since `BacktestRunner` calls it directly against
    `self.status.get(...)`, which is a plain status STRING like `"queued"`/`"sweep
    100/200"` until a real result lands — this was a real bug caught while verifying,
    not a hypothetical. `TARGET_MAX_DD_PCT` is independent of, and tighter than, the
    pre-existing hard `MAX_DD_PCT=0.08` (8%) backtest-SIMULATION-level reject gate — a
    candidate can pass that 8% cutoff during the sweep and still fail this 5%
    selection-level one.
  - **Replaces `RETRY_MIN_WR`/win_rate everywhere it was checked**: `_better()`'s OOS-
    window comparator (`eth_trader_bt.py`) now prioritizes `_clears_target`-clearing
    candidates over sharpe, exactly as it prioritized `win_rate>=RETRY_MIN_WR` before.
    `BacktestRunner._run()`'s per-symbol retry `qualifies` check
    (`eth_trader.py`) now calls `bt._clears_target(...)` instead of comparing
    `win_rate`. `_load_all_worthy_crypto` collapses from the two-tier `hundred`/
    `sixty_plus` win-rate system (2026-08-28 → 2026-08-31 → today) down to ONE rule:
    the best-by-`cum_profit` candidate, across every symbol/interval/source, clearing
    `_clears_target` — there is no more 100%-WR baseline tier at all; a symbol with no
    target-clearing candidate gets no leg, same final fallback as always.
    `_param_reload_loop`'s already-running-leg pause gate switches from
    `min_win_rate=_MIN_WR_60PLUS` to `require_target=True` — same pause mechanism
    (reuses the pre-existing staleness-pause path), new trigger condition.
    `_parse_result_file` drops `require_perfect_wr`/`min_win_rate`/`min_ret_pct`
    entirely (the last was already dead/unused before this change) in favor of one
    `require_target` param that calls `bt._clears_target` directly against the raw
    parsed JSON dict — no need to thread `total_ret_pct`/`max_dd_pct` through
    `extra_out` the way `cum_loss`/`cum_profit` were, since the gate check happens
    before the function ever needs to expose those fields to a caller. `win_rate`
    remains in every result file and is still shown in the Backtest tab table — purely
    informational now, gates nothing.
  - **GUI text updated** to stop implying win-rate is any part of the bar: the Home tab
    "no symbols qualify" status, the Paper/Live tabs' empty-state labels, and several
    docstrings (`TradingLeg`, `_report_missed_trades`, `_load_combo`,
    `_load_all_worthy_crypto`) all now describe the ret/cum_loss/DD targets instead of
    "100%-WR, or 60%+-WR" — these had ALSO been caught stale once already earlier the
    same day (right after the 60%-WR pause mechanism shipped) before being fully
    replaced by this change.
  - Verified: a direct unit test of `_clears_target` confirmed it correctly rejects
    `None` and plain status strings (`"queued"`, `"sweep 88400/400100"`) without
    raising, confirmed the boundary semantics (`total_ret_pct==15.0` inclusive,
    `cum_loss==5.0`/`max_dd_pct==-5.0` both exclusive, matching the `$5`/`5%` cap
    language), and — using the EXACT numbers from the real screenshot that motivated
    this — confirmed all three candidates classify exactly as analyzed (the two
    failing candidates correctly rejected, the ETHUSDT 15m searched candidate correctly
    passing). An end-to-end `_load_all_worthy_crypto` test reproducing that same
    screenshot's three result files confirmed it selects ETHUSDT 15m searched as the
    ONLY leg. A separate test of `_load_result_for_symbol` confirmed
    `require_target=False` (rescue-scan style) sees every candidate regardless of
    target-clearing status, while `require_target=True` (pause-gate style) sees only
    the one that clears the target.
- **Backtest cadence tightened, `PARAM_RELOAD_S` tightened, then `LOOP_INTERVAL`
  corrected again — added 2026-09-01, explicit user ask** ("also retest every 30
  minutes. reload every 40 minutes if no open positions. if open position wait till
  close then load new params"), **`bt.LOOP_INTERVAL` corrected same day** ("200k
  combos per run. not 400k!!!! run again 60 minutes after last run finished"):
  `PARAM_RELOAD_S` (how often `_param_reload_loop` re-checks for a fresh/better
  result) dropped from 3.5h to 40 minutes — `RESULT_MAX_AGE_S` auto-derives from
  `PARAM_RELOAD_S` (`RESULT_MAX_AGE_S = PARAM_RELOAD_S`), so the staleness window
  tightened to match automatically, no separate edit needed. The "if open position
  wait till close then load new params" part required NO code change:
  `_param_reload_loop` already had exactly this wait-for-flat loop before a reload is
  ever applied (`while not stop_ev.is_set(): p_flat = combo.partial.position is None;
  ...; if p_flat and live_flat: break; time.sleep(60)`), confirmed by re-reading it
  rather than assumed. `bt.LOOP_INTERVAL` itself went 2h → 30min (first pass, "retest
  every 30 minutes") → **60min** (corrected same day, "run again 60 minutes after last
  run finished") — but the 30min→60min change turned out to be a value-only fix, not a
  semantics fix: `BacktestRunner._run()` already computed
  `next_run_ts = time.time() + interval_s` at the moment the PREVIOUS cycle's own work
  (sweep + missed-trade check) finished, never at that cycle's start — so "wait N
  minutes after the last run finished" was already the actual behavior even under the
  30-minute value, confirmed by reading that code rather than assumed; only the number
  needed to change to match what the user actually wanted.
- **`N_RANDOM` now means TOTAL combos per run, not per entry source — added
  2026-09-01, explicit user ask** ("200k combos per run. not 400k!!!!"). Before this
  fix, `_gen_combos` (inside `optimize_symbol_interval`) was called once per entry
  source (pine, searched) and each call independently drew the FULL `N_RANDOM` (200000
  from config) — so a config saying "200k combos" actually tested 400k combos per call
  (200k pine + 200k searched), silently double what the config value implied. New
  `N_RANDOM_PER_SOURCE = max(1, N_RANDOM // 2)` is what `_gen_combos` actually draws
  per call now, so the two sources together total exactly `N_RANDOM`. The config key
  `n_random` (200000, unchanged) now means what it says on its face — no config file
  edit was needed, only the code's interpretation of that number. Verified: a real
  `optimize_symbol_interval` call with `N_RANDOM` shrunk to 400 for test speed returned
  `new_combo_count == 400` (not 800), confirming the fix holds under real execution,
  not just by inspecting the constant's value.
- **The exe is named `unified_combo_trader_grid.exe`, not `eth_trader.exe`**
  (see the `.spec` file's own comment) — deliberate, since the original
  `unified_combo_gui` repo may run its own live mainnet process on the same machine at
  the same time, and a shared process image name would let either repo's
  kill-by-image-name build routine accidentally kill the OTHER repo's live trading
  process. Never rename this back to match without re-checking that risk.
- **This is a separate, independent DB/data directory** — `data/` was not copied from
  the source repo, so `data/eth_trader_params.db`/`eth_trader_paper.db` start
  empty here. No migration code exists for the old `tp_mult`/`partial_lvl`/
  `partial_done`/`signal_source` schema columns — the DB schema was written directly
  in its final grid-shaped form (`grid_px`, `grid_level_frac`, `grid_filled`,
  `orig_qty` on `paper_position`/`live_position`) since there was nothing to migrate.
  `grid_level_frac` itself was later superseded by the per-level `grid_fracs` JSON
  column (2026-08-28, ALTER TABLE migration — see the per-level grid bullet above) —
  this repo's `paper_position` table DID need a real migration for that one, unlike
  every column mentioned in this bullet's original fork-time context.
- **Real API keys were copied from the source repo at fork time** (2026-08-28,
  explicit user choice — "source and keys only," not the full data/git history) — this
  repo can authenticate against the same Bybit account as `unified_combo_gui` without
  re-entering keys. Both repos' `keys/*.dat` are independent files on disk (copied, not
  shared/symlinked) and both remain gitignored here exactly as in the source repo.

## Non-negotiable invariants (unchanged from the source repo)

- **MAINNET ONLY. Never Bybit demo/testnet, anywhere, for any reason. Not even by
  asking.** This is a live trading app with real money. Every `HTTP(...)`/
  `WebSocket(...)` call in `eth_trader.py` and `eth_trader_bt.py` must be
  `demo=False`/`testnet=False`, unconditionally, no fallback branch, no config toggle,
  no test-only exception. Before adding or touching *any* session/API-client
  construction, grep both files for `demo=` and `testnet=` and confirm every match is
  still `False`.
- **Backtest auto-starts on launch; paper/live never do.** `MainWindow.__init__` calls
  `BacktestTab.auto_start()` immediately on window construction — zero clicks,
  repeating every `bt.LOOP_INTERVAL` (2h) for the process's life. Paper and Live are
  unaffected: `_paper_running` still starts `False`, Start Paper is still a user click,
  and the live-trading confirmation dialog still fires there.
- **No plaintext key files, ever.** On macOS (this port), keys live in the macOS
  Keychain (`security` CLI, service `unified-combo-grid`) — no `keys/*.dat` files are
  written at all. (Windows DPAPI/`keys/demo.dat`/`keys/live.dat` is the original
  `unified_combo_gui`/pre-port behavior, kept in the code via a `sys.platform` branch
  for portability, not in use on this checkout.) Entered/managed on the Home tab either
  way.
- **Never push `keys/` or any key file to the repo, especially while the user has real
  keys saved.** `.gitignore` excludes `keys/*.dat` (belt-and-suspenders — on macOS
  nothing is ever written there, but the same repo's code still supports Windows).
  Before any `git add`/publish/commit, confirm `keys/` still shows as ignored
  (`git status --ignored`) rather than assuming the `.gitignore` entry is enough on its
  own.
- **`LEVERAGE = 11`, hardcoded, same for every symbol.** Not config-driven, no
  per-symbol exchange lookup.
- **Crypto only.** No stock/tokenized-equity support.
- **Every symbol is tested at every configured interval; one winning interval per
  symbol.** `bt.CRYPTO_INTERVALS` defaults to `["30"]` only (as of 2026-09-01, explicit
  user ask: "30m candles only. trade only 30m candles"). History: `["5","15","30"]` →
  `["5"]` only (briefly, part of 2026-08-31, when the 1-minute exit feed was removed)
  → `["5","15"]` (same day, "add 15m to it") → `["30"]` only (2026-09-01, this change —
  5m and 15m dropped entirely). `_load_all_worthy_crypto()`/`_load_result_for_symbol()`
  pick, per symbol, the single best-scoring interval per the selection rule below —
  with only one interval configured, this degenerates to "whichever entry source wins
  on the one configured interval," but the mechanism itself still supports more than
  one if config is widened again.
- **Single selection rule — SUPERSEDED 2026-09-01, see "Win-rate abandoned as a
  selection criterion" further above: win_rate is no longer part of the rule at all.**
  Kept below as the historical record of the win-rate-based system's own history
  (2026-08-28 → 2026-08-31), for context on how the current rule was arrived at —
  replaced 2026-08-28, explicit user ask: "if cuml less
  than $5 in bt and WR 80% and above but pnl higher than 100wr params then trade 80%
  params. remove all other methods of selection." This fully replaced an earlier
  three-tier system (a since-removed $1-cum_loss "low-risk" tier, the 100%-WR tier,
  and an 80%-WR/20%-total_ret_pct fallback tier — none of that exists anymore). The
  win-rate floor for the second tier was lowered from 80% to 60% on 2026-08-31
  (explicit user ask, "60 replaces 80" — see the "Retry-until-60%-WR sweep" bullet
  above; the tier's shape/mechanics are otherwise byte-identical to this 2026-08-28
  rule, only the threshold moved). Exactly two candidates are looked up per symbol in
  `_load_all_worthy_crypto()`:
  - `hundred`: the best-by-sharpe 100%-win-rate candidate, if any — still the
    baseline/default trade.
  - `sixty_plus` (named `eighty_plus` before 2026-08-31): the best-by-`cum_profit`
    candidate clearing BOTH `win_rate>=_MIN_WR_60PLUS` (60%, inclusive — was
    `_MIN_WR_80PLUS`/80%) AND `cum_loss<_MAX_CUML_60PLUS` ($5, strict — a candidate
    whose total backtested losses equal exactly $5 does NOT qualify; constant renamed
    from `_MAX_CUML_80PLUS`, same $5 value), if any.
  A symbol trades `sixty_plus` instead of `hundred` only when `sixty_plus` exists AND
  (`hundred` doesn't exist, OR `sixty_plus`'s `cum_profit` is strictly higher than
  `hundred`'s) — confirmed via explicit clarifying questions before implementing:
  cum_profit (dollars, not `total_ret_pct`) is the comparison metric; a symbol with no
  100%-WR candidate at all still gets a leg from a qualifying `sixty_plus` candidate
  (nothing to beat = wins by default); a symbol with neither a 100%-WR nor a
  qualifying 60%+ candidate gets **no leg at all** — there is no other fallback (an
  already-open leg's separate pause-on-sub-60%-WR behavior, added the same day, is a
  different mechanism — see the "Retry-until-60%-WR sweep" bullet above).
  Verified with an isolated 6-scenario test (override case, non-override case, the
  cum_loss==$5 exclusion boundary, the no-100%-WR default-win case, the
  no-qualifying-candidate-at-all exclusion case, and the WR==80% inclusive boundary —
  all passed) before considering this done, since it changes which params real
  capital actually trades on. Re-verified with a 5-scenario test after the 60%-floor
  change (see above) — same coverage, updated threshold.
- **Current selection rule (as of 2026-09-01): `bt._clears_target` only — win_rate
  gates nothing.** A symbol gets a leg iff at least one of its (interval, source)
  result files has `total_ret_pct>=15` AND `cum_loss<5` AND `max_dd_pct>-5`
  (`bt.TARGET_MIN_RET_PCT`/`TARGET_MAX_CUM_LOSS`/`TARGET_MAX_DD_PCT`); the best-by-
  `cum_profit` such candidate across every symbol/interval/source wins. No 100%-WR
  baseline tier, no win-rate floor of any kind — see the "Win-rate abandoned as a
  selection criterion" bullet above for the full detail and the data that drove this.
- **Current config is ETHUSDT-only, and capital is a single slot, not split across
  legs.** `data/eth_trader_config.json` and `eth_trader_bt.py`'s
  `_DEFAULT_CONFIG` both carry `"symbols": ["ETHUSDT"]`, `CAPITAL_TIERS = [0.97]`.
  Redo the correlation/backtest-quality/liquidity/autocorrelation analysis documented
  in the source repo's history before adding a symbol back — a single 100%-WR backtest
  result alone is not sufficient justification.
- **Capital-slot system**: at most `len(CAPITAL_TIERS)` symbols may hold capital at
  once. Whichever symbol signals an entry first claims the fraction; any other symbol
  that signals while the slot is held skips its order entirely (paper and live) until
  the occupant symbol goes fully flat.
- **The result file backing an OPEN position is frozen** — never overwritten by the
  backtest sweep while that position is open. `_protected_entry_source(symbol,
  interval)` (renamed back from the Grid fork's brief plain-bool `_position_is_open`
  once the "pine" entry source made this a 2-way choice again — see that bullet above)
  returns `None`/`"searched"`/`"pine"`/the `"ALL"` sentinel (ambiguous provenance,
  protect both), checked before every `optimize_symbol_interval()` call; the OOS
  retest and file save are skipped entirely for whichever source(s) are protected, but
  the IS sweep still runs for it (keeps the DB's `param_runs` cache warm).
  `AtrPartialPaperBot.entry_source` is restored from `paper_position.entry_source` on
  load (not from whatever the constructor was called with) so an already-open position
  never gets retroactively relabeled onto a different entry source mid-flight.
- **Live positions have no exchange-side stop-loss.** Deliberately accepted risk. The
  paper bot's own signal (SL hit or the last grid level filling) is the only thing that
  ever closes a live position — `ComboTrader._force_reconcile_paper_from_live()` exists
  specifically so a restart can never leave a real live slice with no paper position
  managing it.
- **"Stop Paper Trading" → "Close Position(s) & Stop" closes through the paper bot's
  own `_close()`, never `LiveExecutor.mark_closed()` directly** (ported from
  `unified_combo_gui`'s 2026-08-28 fix — a real incident there: closing only the live
  exchange slice left the paper bot's own DB row untouched, so a restart reloaded a
  stale "still open" position and a later manual cleanup recorded a fabricated paper
  loss at whatever price the market had moved to since). `_on_stop_paper`'s
  "Close Position(s) & Stop" path in `MainWindow` calls
  `leg.combo.partial._close(price, "stopped_by_user")` under `combo._lock`, which
  clears the paper side AND mirrors to the real exchange via its own existing
  `if self.live: self.live.mark_closed(...)` call — the same path every other close
  (SL/GRID/MANUAL) already uses. Never reintroduce a path that calls
  `LiveExecutor.mark_closed()`/`.partial_exit()` without going through the paper bot's
  own state first.
- **`LiveExecutor.enter()`/`.partial_exit()`/`.mark_closed()` must always be called
  with `self.BOT_ID`, never a literal string.**
- **Never call `save_keys()`/`delete_keys()` on real data while testing.** Use
  in-memory monkeypatches instead.
- **`optimize_symbol_interval()` writes straight to the real `DATA_DIR` — there is no
  scratch/test mode.** Monkeypatch `bt.DATA_DIR` to a scratch folder before calling it
  (or anything that writes result JSONs) outside the real app.
- **Backtest in-sample window is strictly hard-capped at 7 days** (`IS_DAYS = max(1,
  min(7, int(_cfg.get("is_days", 7))))` in `eth_trader_bt.py`). History: originally
  hard-capped at 2 days, explicit user ask — "it must be strictly two days data for
  bt" — raised to 7 on 2026-09-01, explicit user ask, "i want it to backtest 7 days".
  Editing `data/eth_trader_config.json`'s `is_days` beyond 7 cannot raise this
  further; the code itself enforces the ceiling. Both `_DEFAULT_CONFIG` and the real
  config file carry `is_days: 7`. Don't raise this cap again without being asked —
  note the source repo's own `IS_DAYS` cap has drifted over time there too (2→3 days);
  this fork's cap moves only on explicit request, same as before. `_max_pages()`
  scales its Bybit pagination dynamically off `IS_DAYS`/`GC_WARMUP_BARS`, so raising
  this cap needed no separate fetch-window code change — confirmed against a real
  ETHUSDT 30m fetch (5000 bars available, ~2706 needed for the new 7-day IS + up-to
  -2.5-day OOS + `GC_WARMUP_BARS` settling window at 30m).
- **Leg selection must filter to `sym in bt.SYMBOLS`**, not just scan every result file
  on disk — a symbol removed from config leaves stale result files behind; delete them
  too if a symbol is ever removed.
- **Packaging is onedir, not onefile — on macOS this means a real `.app` bundle.**
  `eth_trader_mac.spec` builds `dist/ETH Trader.app` (via `BUNDLE()`
  wrapping the same onedir `COLLECT()` the Windows spec produces at
  `dist/unified_combo_trader_grid/`). Unlike Windows, `data`/`keys` inside the built
  `.app` (`Contents/MacOS/data`, `Contents/MacOS/keys`) are **real directories**, not
  junctions — a fresh build's `data/` starts empty and the real accumulated one must be
  copied in by hand (see the mac `uc-build` skill). `keys/` is vestigial on macOS (see
  the macOS-port note at the top of this file) so there's nothing to preserve there.
- **A deploy-staging copy of the built app, if one is ever made for distribution
  (mirroring the Windows `unified_combo_trader_grid_dist/` pattern), must stay
  permanently keyless and dataless** — don't copy the real `data/` into it. On macOS
  there's no key file to accidentally bundle (Keychain items aren't files, they can't
  be copied into a zip this way), but the real `data/` (trade history, params DB) still
  shouldn't ship in a distributable build.
- **Backtest simulation loop is JIT-compiled with numba** (`_sim_grid_jit` in
  `eth_trader_bt.py`, the Grid fork's replacement for the source repo's
  `_sim_partial_jit`/`_sim_stop_jit`) — falls back to a pure-Python twin when
  `record_entries=True` or numba isn't available. **Verified 2026-08-28**: a
  standalone hand-computation test (entry, multi-level fills, breakeven/staircase
  stop trail, forced end-of-data close, both long and short) matched `_sim_grid_jit`
  exactly; a 3000-bar random-walk cross-check against real indicator computation found
  the pure-Python and JIT branches agree exactly on all 14 result fields across 48 real
  trades; a full `AtrPartialPaperBot` lifecycle test (entry → two grid fills via the
  1-minute fast path → DB save/reload round-trip → final close on the last grid level)
  produced a PnL identical to the standalone backtest simulation for the same price
  path — confirming live and backtest agree exactly, the property this app is built
  around.
- **"pine" entry source verified 2026-08-28, re-verified same day after the
  fixed-param-vs-searched correction above**: confirmed `gaussian_channel_midline`'s
  `1.414`-vs-`math.sqrt(2)` produces a tiny (~4e-6 relative) but genuinely nonzero
  difference; confirmed `compute_partial_signals` with `entry_source="pine"` responds
  to param changes (k_len/gc_period), proving it no longer ignores `params`; confirmed
  that with `bt.PINE_GC_SQRT2` temporarily patched to the exact `math.sqrt(2)`,
  "searched" and "pine" produce byte-identical signals given identical params —
  proving the sqrt2 constant is the ONLY difference between the two sources; confirmed
  `_parse_result_file` reports pine's OWN searched gc_period/gc_poles from its result
  file's `params` dict (not any fixed default); confirmed `_load_result_for_symbol`
  picks the best across both `_searched`/`_pine` suffixed files; confirmed the
  `paper_position.entry_source` DB migration
  (`ALTER TABLE`, needed since this repo's DB already has real trade history — a real
  live ETHUSDT trade closed via SL before this feature existed) leaves existing rows
  completely untouched, tested against a real copy of the production DB.
- **Backtest tab stale-numeric-column display bug fixed 2026-09-01** — user-reported
  via screenshot: "why is it that when i run the tests wthin 6 hours the 40% return
  for example does not get retested and still a winner". Root cause was purely a GUI
  display bug, not a backtest/selection bug: `BacktestTab.refresh()`'s status-string
  branch (used when `status_dict[key]` is a plain string like "no OOS winners" rather
  than a result dict) only ever wrote column 2 (Status) — it never cleared columns
  4–15 (Sharpe/Ret%/DD%/WR%/etc.), so once a row had displayed a real dict result on
  some earlier cycle, those numeric columns kept showing that stale snapshot
  indefinitely across every later cycle that failed to produce a fresh result, since
  `_on_start()` only blanks the whole table once per `BacktestRunner` lifetime (the
  same runner loops cycle after cycle without ever being recreated). Cosmetic only —
  real trading/selection always re-checks `RESULT_MAX_AGE_S` against the saved
  result file's own timestamp, never the GUI's displayed numbers — but actively
  misleading to look at, which is what triggered the user's question. Fixed by
  clearing `range(4, len(_BT_COLS))` whenever the string-status branch runs.
- **"Previously-winning params are always retested, ahead of random sampling" — added
  2026-09-01, two-part explicit user ask.** First ask, "make sure previous wining
  params are also tested" (raised as a direct follow-up to the display-bug report
  above, once it became clear PINE's "40% return" row was actually stale rather than
  a live re-verified result): initial implementation only re-read the SINGLE
  currently-saved-on-disk result file per (symbol, interval, source) each cycle.
  Second ask, same day, correcting scope: "shouldnt it be recorded with all
  previously tested winning params and always tested again before random tests??" —
  a single current file is not enough, because a later cycle that finds something
  merely mediocre (or nothing) OVERWRITES that file, permanently losing the earlier
  genuine winner's params with no other record of them (`db_load_top`'s top-
  `N_TOP_RETEST`-by-all-time-IS-score carry-forward is a RANKING-based guarantee, not
  an absolute one — a combo that won a real past OOS retest can still be outscored on
  raw IS score by other, possibly-overfit combos and silently drop out of every
  future sweep's candidate pool). Fixed by adding a new, permanent, never-pruned
  `winning_params` SQLite table (`eth_trader_bt.py`, migrated via `db_init()`'s
  existing `CREATE TABLE IF NOT EXISTS` + column-migration pattern) that records
  every distinct (by `_param_key`, deduplicated) param combo that has EVER cleared
  `_clears_target` and been saved as a (symbol, interval, entry_source)'s live result
  file — `db_save_winner(symbol, interval, src, candidate)` (called from
  `optimize_symbol_interval` right after `best_result[src]` is written to disk, ONLY
  when `_clears_target(best_result[src])` is true — the non-clearing
  best-available-when-nothing-cleared-target fallback candidate is deliberately never
  recorded here, since baking a non-winner into the permanent priority-retest list
  would be pointless: it should keep competing via ordinary random search on its own
  merits) and `db_load_winners(symbol, interval, src)` (returns ALL historical
  winners for that exact (symbol, interval, src), unranked and unlimited — unlike
  `db_load_top`, deliberately no `LIMIT`, since the whole point is an absolute
  guarantee every past winner keeps getting retested forever, not just the highest-
  ranked ones). `optimize_symbol_interval` now backfills the table from whatever is
  CURRENTLY saved on disk (if it clears target) at the start of every call — handles
  both a first-ever winning cycle and an already-deployed instance's pre-existing
  result files that predate this table — then builds each source's combo list as
  `top_params + winners_by_src[src] + new_random_combos`, i.e. every historical
  winner is unconditionally included ahead of (before) that cycle's random sampling.
  `winning_params`' schema mirrors `param_runs`' grid/gc/flip columns exactly
  (including the otherwise-unused legacy `grid_atr_mult`/`grid_level_frac` columns —
  needed only because `_grid_select_cols()`'s `COALESCE(grid_dist_i, grid_atr_mult,
  1.0)` expressions are shared with `param_runs` and reference those column names;
  omitting them made every query against the new table raise `sqlite3.
  OperationalError`, silently swallowed by the existing `except sqlite3.Error: return
  []` convention as a false "no winners" — caught and fixed during verification, not
  by user report). Verified: a scratch-DB round-trip test confirms save/load,
  dedup-on-identical-resave, accumulation of multiple distinct winners (not
  overwriting), per-entry-source isolation, that non-clearing candidates are never
  recorded, and that a pre-existing on-disk result file gets correctly backfilled;
  a full real end-to-end `optimize_symbol_interval` call against live ETHUSDT 30m
  market data (via a `ThreadPoolExecutor` so the actual `_combo_worker` calls could be
  spied on in-process) confirmed a pre-seeded historical winner's exact params were
  genuinely included in and tested by a real sweep, not merely present in a list
  that's never consumed.

## Working here (macOS)

- **Before killing any running `ETH Trader.app` / `unified_combo_trader_grid`
  process for a rebuild, check for an open real live position first** — `SELECT * FROM
  live_position` in the RUNNING app's own `data/eth_trader_paper.db` (on macOS,
  that's `/Applications/ETH Trader.app/Contents/MacOS/data/
  eth_trader_paper.db` if it's installed there — not necessarily this dev
  checkout's own `data/`, which is a separate, usually-stale copy; confirm with
  `lsof -p <pid> | grep '\.db'` which file the running process actually has open).
  Empty `live_position` = safe to kill.
- Build: `pyinstaller eth_trader_mac.spec --noconfirm` (from the project
  venv — `source .venv/bin/activate` first, or `python3 -m PyInstaller ...`). Output is
  `dist/ETH Trader.app` (a bundle) plus `dist/unified_combo_trader_grid/` (the
  onedir folder the bundle wraps). No UPX step — the mac spec sets `upx=False`
  deliberately (UPX is a Windows/Linux-oriented compressor; not required, not used).
  Use `/uc-build` for the full build → preserve-data → launch-verify → deploy sequence
  (the skill itself is mac-native as of this note — see its own file).
- After any `rm -rf dist`, the fresh `dist/ETH Trader.app/Contents/MacOS/data`
  starts **empty** — there are no junctions to "recreate" on macOS. The real
  accumulated `data/` (trade DBs, params DB, result JSONs, logs) must be copied in by
  hand from wherever the previously-running instance kept it before trusting a run
  from the new build. `keys/` needs no such copy — it's vestigial on macOS (see the
  macOS-port note at the top of this file); real keys live in the macOS Keychain and
  survive a rebuild automatically.
- Compile + lint before considering any edit done: `python3 -m py_compile
  eth_trader.py eth_trader_bt.py && python3 -m pyflakes
  eth_trader.py eth_trader_bt.py` (from the project venv — `pyflakes`
  isn't a stdlib module, `pip install pyflakes` into `.venv` if missing).
- Check for concurrent PyInstaller builds before starting a new one (`ps aux | grep -i
  pyinstaller`).
- **Killing a process is a normal POSIX `kill`/`kill -9` (or `pkill -f "unified_combo
  \|ETH Trader"`) on macOS — no git-bash/Windows unreliability to work around,
  no `taskkill`/`tasklist`/PowerShell.** Still sweep every matching PID, not just the
  main one — a running instance's backtest sweep spawns several
  `--multiprocessing-fork` worker processes (`ps aux | grep -i unified_combo` lists
  them all); verify all are gone after killing (`ps aux | grep -i unified_combo` comes
  back empty, ignoring the grep itself).
- This repo is a separate, independent private GitHub repo from `unified_combo_gui`
  (and from the Windows Grid-fork repo this was ported from) — don't assume actions
  taken in one apply to the others (git history, releases, collaborators are all
  independent).
- **Verify a symbol exists on Bybit before adding it to config** — a plausible-looking
  symbol can simply not exist; check with a read-only `get_tickers(category="linear",
  symbol=...)` call first.
