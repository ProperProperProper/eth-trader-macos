# Setup — Building and Running ETH Trader (macOS)

This is the **macOS fork** of ETH Trader — a desktop app for backtesting, paper
trading, and (optionally) live-mirroring a combo strategy with ATR-multiple grid exits on
Bybit USDT linear perpetuals. Crypto only. For the strategy itself (entry signal, exit
mechanics, parameter search, and an empirical validation of it), see [`README.md`](../README.md).

There is no separate backtester tool — `eth_trader_bt.py` is a plain library the
trader (`eth_trader.py`) imports and drives in-process from its Backtest tab.

**Mainnet only.** Every session this app creates — paper trading, backtesting, and live —
connects to Bybit mainnet exclusively; nothing in this app ever uses Bybit's demo trading
environment. Paper trading places no real orders regardless (it simulates against the
same real market data live trading would use); the mainnet requirement is about reading
real balance/price data, not about paper "actually trading."

## Requirements

- macOS (Apple Silicon or Intel)
- Python 3.12
- A virtual environment with: `PyQt6`, `pybit`, `websocket-client`, `numpy`, `pandas`,
  `numba`, `llvmlite`, `certifi`, `pyinstaller`. No `requirements.txt` exists yet —
  install these directly:
  ```
  python3 -m venv .venv
  source .venv/bin/activate
  python3 -m pip install PyQt6 pybit websocket-client numpy pandas numba llvmlite certifi pyinstaller
  ```
  (`scipy` is only needed for the reproducibility scripts under
  `docs/research/pbo_validation/`, not for the app itself.)

## Running from source (development)

```
source .venv/bin/activate
python3 eth_trader.py
```

`data/` is created automatically next to the script on first launch — nothing to set up
beforehand. Enter your Bybit API key(s) on the Home tab; that's the only manual step, and
the Backtest sweep itself starts automatically the moment the app launches (see "Backtest
auto-start" below).

## Building the .app bundle

```
source .venv/bin/activate
pyinstaller eth_trader_mac.spec --noconfirm
```

Produces `dist/ETH Trader.app` (a real `.app` bundle, via `BUNDLE()`) wrapping
`dist/unified_combo_trader_grid/` (the same onedir folder structure the Windows build
uses). Launch it either by double-clicking in Finder or:

```
open "dist/ETH Trader.app"
```

Named `unified_combo_trader_grid` internally (not `eth_trader`) deliberately —
a sibling Windows-only repo this was forked from may run its own live mainnet process on
the same network at the same time, and a shared process image name would let a
kill-by-image-name step in either repo accidentally kill the other's live trading
process. No UPX step on macOS — the spec sets `upx=False` deliberately (UPX is a
Windows/Linux-oriented compressor; skipping it just means a larger, uncompressed bundle,
not a build failure).

**A fresh build's bundle starts with an empty `data/`.** Unlike a Windows onedir folder,
`Contents/MacOS/data` inside the `.app` is a real directory physically inside the bundle,
not a junction pointing back at a project-root `data/` — so a rebuild does not
automatically carry over an existing installation's trade history, params DB, or backtest
results. If you have a previous install with real data you want to keep, copy its
`Contents/MacOS/data/` into the freshly built bundle before relying on it:

```
cp -R "/Applications/ETH Trader.app/Contents/MacOS/data/." \
      "dist/ETH Trader.app/Contents/MacOS/data/"
```

Then deploy by replacing the installed copy:

```
rm -rf "/Applications/ETH Trader.app"
cp -R "dist/ETH Trader.app" /Applications/
open "/Applications/ETH Trader.app"
```

Before killing a running instance for a rebuild, check it has no open **live** position
first (`SELECT * FROM live_position` in its `eth_trader_paper.db`) — an empty result
means it's safe to kill.

**Testing note:** since the Backtest sweep auto-starts on launch, even a quick test
launch immediately spawns a real mainnet session and `ProcessPoolExecutor` worker
processes. Sweep and kill every matching PID after testing, not just the main one:
```
pkill -f "ETH Trader"
pkill -f "unified_combo_trader_grid"
ps aux | grep -i unified_combo   # confirm empty (ignoring the grep itself)
```

## API keys

Enter keys on the Home tab — a paper key and, optionally, a live key. Both are regular
Bybit mainnet API keys (not Bybit's demo trading product); "paper" just names which slot
it's in, not which environment it connects to.

**Keys live in the macOS Keychain**, not in a file — stored via the `security` CLI under
service name `unified-combo-grid`, accounts `demo`/`live`, tied to your own macOS user
account. No plaintext key file is ever written, and the app itself has no embedded
credentials. The `keys/` directory that exists in this repo (and inside the built `.app`)
is a vestigial empty folder kept only for code-portability with the original
Windows/DPAPI-based fork — nothing reads or writes files there on macOS.

Paper trading always uses the paper key, connected to mainnet. The Backtest tab prefers
the live key when one is saved (to size against real balance), falling back to the paper
key otherwise — either way the connection itself is mainnet.

**Live positions have no exchange-side stop-loss.** Entries/exits are purely
signal-driven from the paper bot's own price-crossing checks, evaluated on the
entry-timeframe bar close — a deliberate design choice, not an oversight (see
`README.md` §10.5 for the full reasoning). A crash, dropped connection, or stale
WebSocket feed while a live position is open leaves it unprotected until the app resumes
or you close it manually on Bybit.

## Strategy summary

One bot, **ATR_PARTIAL**, runs on every leg. Entry is a stochastic K/D cross (from
oversold/overbought) filtered by a Choppiness Index regime filter and a Gaussian Channel
trend direction, generated by one of two competing, independently-optimized entry
sources ("searched" / "pine") per (symbol, interval); exit is a searched grid of
ATR-multiple take-profit levels with a trailing stop, a cross-down unwind mechanism, a
reversal-contingent flip, and a trailing take-profit — see `README.md` for the complete
mathematical formalization, the parameter search methodology, and an empirical
validation of the exit structure against real market data.

Whichever (interval, source) currently has the best net profit (`cum_profit -
cum_loss`) for a symbol is the one paper/live actually trades — checked every reload
cycle, so a symbol can switch sources or intervals automatically as fresh backtest
results come in.

## Concurrent legs and capital

Capital is a fixed set of slots (`CAPITAL_TIERS`, currently a single 97%-of-balance
slot) — **not** an even split across every qualifying symbol. Whichever qualifying
symbol's leg signals an entry first claims the slot; any other symbol that signals while
the slot is held simply skips that entry and waits for the slot to free up. It's entirely
normal for zero legs to trade in a given session if no symbol currently qualifies.

## Backtest auto-start

**The Backtest sweep starts automatically the moment the app launches** — no click
needed — and repeats roughly every hour (`LOOP_INTERVAL`, measured from when the
*previous* cycle finished, not from when it started) for as long as the app stays open.
This is the one part of the app that auto-starts; Paper and Live remain fully manual (see
below).

Each cycle sweeps every configured symbol at every configured interval, saves results,
and keeps re-sweeping a symbol's intervals with fresh random combos until the search
genuinely finds nothing new to try. Every param combo that has ever been a genuine
winner for a given (symbol, interval, source) is permanently recorded (capped to the top
50 by score from the last 7 days for the general search-run history — see
`eth_trader_bt.py`'s `param_runs`/`winning_params` tables) and retested ahead of new
random sampling on every future sweep, so a real past winner never quietly drops out of
rotation — though it can still lose to a fresher candidate, or stop qualifying entirely
if the market has genuinely moved against it since (walk-forward validation is designed
to let exactly that happen).

Clicking **Run Backtest** while the auto-repeating sweep is between cycles forces an
immediate fresh cycle instead of waiting out the rest of the interval.

## Tabs

- **Home** — API key entry/management, Start/Stop Paper controls, one status block per
  active leg (symbol, price, balance, WS health, bot state).
- **Backtest** — the auto-started sweep described above; shows **two rows per (symbol,
  interval)** — one per entry source (searched/pine) — each its own independent
  in-sample-ranked, out-of-sample-retested candidate, with the currently-winning row
  highlighted. Writes `data/eth_trader_results_{symbol}_{interval}m_{searched,pine}.json`
  — never overwritten while a position is trading on it.
- **Paper** — one portfolio block per active leg: stats, current position (entry/SL/
  next grid level/grid fill progress), and trade history for that leg's symbol.
- **Live** — one block per leg that has a live position mirrored to your Bybit account
  when a live key is saved; a single placeholder if no live key is saved at all.

**Paper and Live never auto-start.** Backtest does (see above); everything else opens
idle and needs an explicit Start Paper click. Starting Paper with a live key saved shows
a confirmation dialog first, since orders will be real.

## Config

`data/eth_trader_config.json` (created with defaults on first run):

| Key | Meaning |
|---|---|
| `symbols` | Bybit crypto symbols to backtest/trade — currently `["ETHUSDT"]` only |
| `crypto_intervals` | List of candle intervals (minutes) tested per symbol every cycle |
| `n_random` | Random parameter combos sampled per (symbol, interval, source) per backtest pass — each entry source gets this full amount independently |
| `is_days` | In-sample window length (days) — hard-capped at 7, regardless of what's set here |
| `oos_hours_list` | Out-of-sample walk-forward windows (hours) tested per candidate |
| `min_trades` / `min_avg_hold` | Minimum trade count / average hold time (bars) to keep a candidate |
| `initial_equity` | Backtest starting equity (overridden by real wallet balance when a live/backtest key is available) |
| `entry_hours_utc` | Optional `[start, end]` UTC hour window restricting new entries; `null` = no restriction |

Leverage is **not** config-driven — it's hardcoded at 11x for every symbol. Grid shape
(`grid_levels`/`grid_dist_1..8`/`grid_frac_1..8`) is also not config-driven — it's a
searched parameter per (symbol, interval), same as the entry-signal params.

A `data/locked_combos.json` file can pin one (symbol, interval, source)'s entry-signal
parameters to a fine-tuned neighborhood of a previously-validated operating point while
its exit parameters stay fully searched — see `README.md` §5.2 for the mechanism.

Verify a symbol actually exists on Bybit before adding it — a bad symbol doesn't fail at
config-load time, it just fails every fetch for that symbol, forever, every backtest
cycle.

## Data directory

`data/` (inside the `.app` bundle at `Contents/MacOS/data` once built) holds everything
the app reads/writes at runtime: the config above, `locked_combos.json`,
`eth_trader_results_*.json` (per-symbol-per-interval backtest results, consumed by
the trading engine), `eth_trader_paper.db` (paper/live trade history and state),
`eth_trader_params.db` (backtest parameter search history), and rotating log files
(`eth_trader_bt.log`, `eth_trader_paper.log`, capped at 5MB × 4 files each). None
of it is version controlled.
