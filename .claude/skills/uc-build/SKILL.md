---
name: uc-build
description: Build, launch-verify, and deploy ETH Trader.app (macOS). Only ever run this when the user explicitly asks to build/rebuild — never trigger it automatically just because source files changed.
disable-model-invocation: true
---

# Build ETH Trader.app (macOS)

Follow every step in order. Do not skip the verify step — a compile-clean build can
still fail to launch (missing PyInstaller exclude, broken plugin bundling), and a
launch that succeeds with a fresh empty `data/` can still fail once the real
accumulated data is copied back in (schema mismatch, bad JSON, etc).

**This repo's app is deliberately identified as `unified_combo_trader_grid` /
`ETH Trader.app`, not `eth_trader`** (see
`eth_trader_mac.spec`'s own comment) — a sibling repo (`unified_combo_gui`,
or its Windows counterpart) may run its own live mainnet process on the same machine at
the same time, and a shared process image name would let either repo's kill-by-name
build step accidentally kill the OTHER repo's live trading process. Never rename this
back to match without deliberately re-checking that risk.

**Key storage needs no attention here.** Real API keys live in the macOS Keychain
(`security` CLI, service `unified-combo-grid`), not in files — they are untouched by
building, killing, or replacing the app bundle. The `keys/` directory this skill
references below (inside the built `.app`) is a vestigial empty folder kept only for
code-portability with the Windows fork; nothing reads or writes files in it on macOS.

## 0. Pre-flight

- Check no PyInstaller build is already running: `ps aux | grep -i pyinstaller`. Two
  concurrent builds corrupt `build/`/`dist/`. Stop one before starting another.
- `source .venv/bin/activate` (or otherwise ensure the project venv's `python3` is on
  PATH — PyInstaller, pyflakes, and every runtime dependency live there, not in system
  Python).
- `python3 -m py_compile eth_trader.py eth_trader_bt.py && python3 -m
  pyflakes eth_trader.py eth_trader_bt.py` — fix anything this catches
  before spending build time. (`pip install pyflakes` into `.venv` first if it's
  missing — it's not a stdlib module.)
- No UPX step on macOS — `eth_trader_mac.spec` sets `upx=False`
  deliberately (UPX is a Windows/Linux-oriented compressor; skipping it just means a
  larger, uncompressed `.app`, not a build failure). Don't try to install or wire it in.

## 1. Locate and back up the REAL running data before touching anything

Unlike Windows (junctions to a project-root `data/`/`keys/`), on macOS the installed
app's `data/` lives as a **real directory physically inside the bundle**
(`.../Contents/MacOS/data`) — a fresh build's bundle starts with an empty one. If the
currently-running instance has real trade history / a params DB / current result
files, a naive rebuild-and-replace silently orphans all of it.

- Find the running instance and its actual data path:
  ```
  ps aux | grep -i "unified_combo\|ETH Trader" | grep -v grep
  lsof -p <main_pid> | grep -E '\.db|\.log|data/'
  ```
  This tells you the exact `.../Contents/MacOS/data` path in use (normally
  `/Applications/ETH Trader.app/Contents/MacOS/data` if installed there, but
  confirm — don't assume).
- **Check for an open real live position before going any further** — the one
  carve-out to "just run the build routine" standing permission:
  ```
  python3 -c "
  import sqlite3
  con = sqlite3.connect('<that data path>/eth_trader_paper.db')
  print(con.execute('SELECT * FROM live_position').fetchall())
  "
  ```
  Empty result = safe to kill. If it's non-empty, STOP and tell the user — do not kill
  or rebuild over an open live position.
- Copy the real `data/` folder aside as a safety net before doing anything destructive:
  ```
  cp -R "<that data path>" /tmp/uc_build_data_backup_$(date +%s)
  ```
  Report the backup path so it's recoverable if anything below goes wrong.

## 2. Clean shutdown — full sweep, not one PID

```
pkill -f "ETH Trader"
pkill -f "unified_combo_trader_grid"
ps aux | grep -i "unified_combo\|ETH Trader" | grep -v grep    # must come back empty
```
This is a normal POSIX `kill` under the hood — no git-bash/Windows unreliability to
work around. Still verify explicitly: a backtest sweep spawns several
`--multiprocessing-fork` worker processes alongside the main one, and "process not
found" from a single targeted check isn't enough — sweep by name and confirm zero
matches remain.

## 3. Clean build

```
rm -rf build dist __pycache__
python3 -m PyInstaller eth_trader_mac.spec --noconfirm
```
Run this in the background if it might exceed ~2 minutes; check for `Build complete!`
and no `ERROR`/unexpected `WARNING` lines in the output (the numpy.f2py /
scipy._external "not found" warnings are expected — they're the excluded packages' own
optional sub-imports, harmless). Produces both `dist/ETH Trader.app` (the
bundle to actually run) and `dist/unified_combo_trader_grid/` (the onedir folder the
bundle wraps — same files, useful for inspecting what got bundled without launching a
full `.app`).

## 4. Restore the real data into the fresh build

The fresh `dist/ETH Trader.app/Contents/MacOS/data` is empty. Copy the real one
(from step 1's live path, or its backup) into it before this build is treated as "the
real app":
```
cp -R "<real data path>/." "dist/ETH Trader.app/Contents/MacOS/data/"
```
Nothing is needed for `keys/` — real keys live in the Keychain, independent of any
bundle. Verify with a real read, not just a directory listing:
```
python3 -c "
import sqlite3
con = sqlite3.connect('dist/ETH Trader.app/Contents/MacOS/data/eth_trader_paper.db')
print(con.execute('SELECT COUNT(*) FROM paper_trades').fetchone())
"
```

## 5. Launch-verify

Never skip this — "it compiled" is not "it works." Launch the built `.app` directly
(not yet the `/Applications` copy) so a crash's stderr is visible:
```
"dist/ETH Trader.app/Contents/MacOS/unified_combo_trader_grid" &
sleep 8
ps aux | grep -i unified_combo_trader_grid | grep -v grep
```
- Confirm the process is still listed (alive). If it's gone immediately, that's a real
  failure — check the terminal output / `build/unified_combo_trader_grid/
  warn-unified_combo_trader_grid.txt` for a missing-module error before assuming
  anything else.
- Check `dist/ETH Trader.app/Contents/MacOS/data/crash_eth_trader_paper.log`
  does NOT exist / did not just get created.
- Confirm the real data actually loaded — `tail` `eth_trader_paper.log`/
  `eth_trader_bt.log` inside that same `data/` and look for backtest cycles resuming
  sensibly (not starting from a totally cold state) and no new tracebacks.
- Remember this app auto-starts its Backtest sweep on launch (by design) — Paper/Live
  never auto-start, so seeing backtest activity immediately is normal, seeing paper/live
  trading activity immediately would not be.
- Wait a further ~8-10s and re-check the process is still alive before concluding it's
  stable — a build can pass an initial few-second check and still be about to exit.
- When done verifying, kill it again the same way as step 2 before proceeding.

## 6. Deploy

```
rm -rf "/Applications/ETH Trader.app"
cp -R "dist/ETH Trader.app" /Applications/
```
The data was already folded into `dist/ETH Trader.app` in step 4, so this copy
carries it straight into place — no separate junction-recreation step exists on macOS
(there's nothing to junction; `data/` is just a real folder that comes along with the
copy).

Launch the deployed copy for real use:
```
open "/Applications/ETH Trader.app"
```
Re-run the same launch-verify checks from step 5 against this copy (its own `data/` at
`/Applications/ETH Trader.app/Contents/MacOS/data`) before considering the
deploy done.

## 7. Report

Confirm: launch-verify passed on both the `dist/` build and the deployed
`/Applications` copy, the real data was carried over intact (report the trade/position
counts you checked), zero stray `unified_combo_trader_grid`/`ETH Trader`
processes remain from the old build, and where the safety backup from step 1 lives (so
it can be cleaned up later or kept a while longer, at the user's discretion — don't
delete it yourself without asking).
