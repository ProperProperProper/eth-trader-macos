#!/usr/bin/env python3
"""
ETH Trader — PAPER/LIVE TRADER
One strategy (ATR_PARTIAL, grid exit) runs per qualifying crypto symbol — one leg per
symbol whose best (interval, entry_source) backtest result currently has the highest
net profit (cum_profit - cum_loss; see LegTrader/TradingEngine). Capital is a fixed set
of slots (CAPITAL_TIERS), not an even split across legs — see README.md sec.6 & 8.
Reads from eth_trader_results_{sym}_{iv}m.json in data/.
Crypto-only Bybit linear perpetuals.
Optional live execution: enter live API keys on the Home tab to mirror paper trades live.

MAINNET ONLY. Every session below (paper, backtest, live) must connect with
demo=False/testnet=False, unconditionally, no fallback. This is a live app handling
real money — do not add a demo/testnet path without the user's explicit prior
authorization. Getting this wrong once already caused paper trading and backtesting to
silently run against Bybit's fake demo-account data instead of real mainnet state.
"""
import os, sys, time, json, math, threading, logging, sqlite3, traceback, multiprocessing, ctypes, queue, subprocess
if sys.platform == "win32":
    import ctypes.wintypes as wintypes
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from logging.handlers import RotatingFileHandler
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool

import numpy as np
from pybit.unified_trading import HTTP, WebSocket

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QTabWidget, QWidget,
    QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QPushButton, QFrame, QGroupBox, QTableWidget,
    QTableWidgetItem, QHeaderView,
    QScrollArea, QMessageBox, QTextEdit, QCheckBox, QLineEdit,
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QBrush, QFont, QPalette

# Same directory, same data/config — the trader drives it in-process for the Backtest
# tab instead of the user launching eth_trader_bt.exe separately beforehand.
import eth_trader_bt as bt

# ── Paths ─────────────────────────────────────────────────────────────────────
if getattr(sys, "frozen", False):
    _DIR = os.path.dirname(sys.executable)
else:
    _DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)
KEYS_DIR = os.path.join(_DIR, "keys")   # credentials live here only, never under data/
os.makedirs(KEYS_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "eth_trader_paper.db")


# ── Secure key storage (Windows DPAPI / macOS Keychain) ────────────────────────
# No plaintext key file is ever written on either platform. On Windows, CryptProtectData
# ties the ciphertext to this Windows user account (+ machine) — same mechanism Windows
# itself uses for saved Wi-Fi passwords and Credential Manager entries. On macOS, the
# direct native equivalent is the login Keychain (`security` CLI, generic-password
# items) — tied to this macOS user account, unlockable only via that account's login
# credentials, with no separate encryption key of our own to manage or leak. Nothing is
# auto-loaded at startup: keys are entered and saved explicitly from the Home tab, and
# the app never auto-starts trading.
if sys.platform == "win32":
    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _dpapi_protect(data: bytes) -> bytes:
        buf = ctypes.create_string_buffer(data, len(data))
        in_blob  = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        out_blob = _DATA_BLOB()
        if not ctypes.windll.crypt32.CryptProtectData(
                ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)):
            raise ctypes.WinError()
        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(out_blob.pbData)

    def _dpapi_unprotect(blob: bytes) -> bytes:
        buf = ctypes.create_string_buffer(blob, len(blob))
        in_blob  = _DATA_BLOB(len(blob), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        out_blob = _DATA_BLOB()
        if not ctypes.windll.crypt32.CryptUnprotectData(
                ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)):
            raise ctypes.WinError()
        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(out_blob.pbData)

    _DEMO_KEYS_ENC = os.path.join(KEYS_DIR, "demo.dat")
    _LIVE_KEYS_ENC = os.path.join(KEYS_DIR, "live.dat")

    def _keys_path(live: bool) -> str:
        return _LIVE_KEYS_ENC if live else _DEMO_KEYS_ENC

    def has_keys(live: bool = False) -> bool:
        return os.path.exists(_keys_path(live))

    def save_keys(api_key: str, api_secret: str, live: bool = False):
        blob = json.dumps({"api_key": api_key, "api_secret": api_secret}).encode("utf-8")
        enc  = _dpapi_protect(blob)
        with open(_keys_path(live), "wb") as f:
            f.write(enc)

    def delete_keys(live: bool = False):
        path = _keys_path(live)
        if os.path.exists(path):
            os.remove(path)

    def load_keys_secure(live: bool = False):
        """Returns (api_key, api_secret), or ("", "") if unset/unreadable."""
        path = _keys_path(live)
        if not os.path.exists(path):
            return "", ""
        try:
            with open(path, "rb") as f:
                enc = f.read()
            d = json.loads(_dpapi_unprotect(enc).decode("utf-8"))
            return d.get("api_key", ""), d.get("api_secret", "")
        except Exception:
            return "", ""

elif sys.platform == "darwin":
    # macOS Keychain, via the `security` CLI — one generic-password item per key slot,
    # service name scoped to this app so it never collides with another app's items.
    _KEYCHAIN_SERVICE = "unified-combo-grid"

    def _keychain_account(live: bool) -> str:
        return "live" if live else "demo"

    def has_keys(live: bool = False) -> bool:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", _KEYCHAIN_SERVICE,
             "-a", _keychain_account(live)],
            capture_output=True)
        return r.returncode == 0

    def save_keys(api_key: str, api_secret: str, live: bool = False):
        blob = json.dumps({"api_key": api_key, "api_secret": api_secret})
        account = _keychain_account(live)
        # -U updates in place if the item already exists, so re-saving never leaves a
        # stale duplicate item behind.
        subprocess.run(
            ["security", "add-generic-password", "-s", _KEYCHAIN_SERVICE,
             "-a", account, "-w", blob, "-U"],
            check=True, capture_output=True)

    def delete_keys(live: bool = False):
        subprocess.run(
            ["security", "delete-generic-password", "-s", _KEYCHAIN_SERVICE,
             "-a", _keychain_account(live)],
            capture_output=True)  # not checked — fine if there was nothing to delete

    def load_keys_secure(live: bool = False):
        """Returns (api_key, api_secret), or ("", "") if unset/unreadable."""
        r = subprocess.run(
            ["security", "find-generic-password", "-s", _KEYCHAIN_SERVICE,
             "-a", _keychain_account(live), "-w"],
            capture_output=True, text=True)
        if r.returncode != 0:
            return "", ""
        try:
            d = json.loads(r.stdout.strip())
            return d.get("api_key", ""), d.get("api_secret", "")
        except Exception:
            return "", ""

else:
    raise RuntimeError(f"Unsupported platform for secure key storage: {sys.platform}")

# Shared config with the backtester. Leverage MUST be read from the same place and
# capped the same way, or positions get sized differently from what was backtested.
_CONFIG_PATH = os.path.join(DATA_DIR, "eth_trader_config.json")
_ENTRY_HOURS_UTC = None   # (start_h, end_h) inclusive or None = no restriction
_CFG = {}
try:
    if os.path.exists(_CONFIG_PATH):
        with open(_CONFIG_PATH, encoding="utf-8-sig") as _cf:
            _CFG = json.load(_cf) or {}
        _eh = _CFG.get("entry_hours_utc")
        if isinstance(_eh, (list, tuple)) and len(_eh) == 2:
            _ENTRY_HOURS_UTC = (int(_eh[0]), int(_eh[1]))
except Exception:
    pass

def _entry_allowed() -> bool:
    """Return True if a new entry is currently allowed — crypto trades unrestricted
    24/7 except for the optional global entry_hours_utc window (stock/NYSE-hours gating
    removed 2026-08-22 along with stock support entirely)."""
    if _ENTRY_HOURS_UTC is None:
        return True
    h = datetime.now(timezone.utc).hour
    start, end = _ENTRY_HOURS_UTC
    if start <= end:
        return start <= h < end
    return h >= start or h < end   # wraps midnight

CATEGORY              = "linear"
TAKER_FEE             = 0.00055
MARGIN_HEADROOM       = 0.98
LIVE_MARGIN_HEADROOM  = 0.98   # 98% of live_balance * LiveExecutor.equity_fraction per bot
SEED_BARS        = 600
WS_STALE_S       = 120
COOLDOWN_S       = 1800
RECONCILE_S      = 180
# Capital slots (added 2026-08-25, single-slot as of the same day): rather than
# splitting funds evenly across every worthy symbol upfront, only ONE symbol may hold
# capital at a time — whichever signals an entry first claims the single slot (97% of
# funds). It co-owns that slot between its PARTIAL and STOP bots (each gets
# slot_fraction/2, same split already used for the shared live position). Any other
# symbol that signals while the slot is held skips its order entirely, paper and live,
# until the slot frees up (its occupant symbol goes fully flat) and gets reclaimed by
# whichever symbol signals next. List form kept (not a bare float) so claim_slot's
# tier-search loop needs no special-casing if a second tier is ever reintroduced.
CAPITAL_TIERS    = [0.97]
PARAM_RELOAD_S   = 40 * 60  # explicit user ask, "reload every 40 minutes if no open
                     # positions" (was 3.5h) — a leg only actually applies a reload once
                     # flat (see _param_reload_loop's own wait-for-flat loop below,
                     # already pre-existing behavior matching "if open position wait
                     # till close then load new params" — this constant just controls
                     # how often that check happens, not whether it waits for flat).
# A result is only ever "worth trading" if it reflects the most recent backtest run —
# the whole point of backtesting is to capture what's currently worth trading, not what
# was worth trading a day ago. Tied to PARAM_RELOAD_S: a specific (symbol, interval)
# pair can go up to roughly one full cycle without being re-swept (backtest cycles run
# every bt.LOOP_INTERVAL after the previous cycle's own work finishes, 60min as of
# 2026-09-01 (see bt.LOOP_INTERVAL's own docstring for the "after finish, not from
# start" semantics and the 30min→60min correction) — though a
# symbol's own retry-until-target loop, see bt._clears_target, can make one pass take
# much longer than that on a hard-to-qualify symbol), so using the same window as the
# reload cadence tolerates that without being so tight it rejects results that are
# merely "not the very latest tick."
RESULT_MAX_AGE_S = PARAM_RELOAD_S
POSITION_SETTLE_S     = 60   # ignore "position missing" this soon after an entry
POSITION_MISS_STRIKES = 2    # consecutive misses before declaring a manual close
LEVERAGE = 11   # hardcoded for every symbol — no per-symbol lookup

def _sym_lev(sym: str) -> int:
    return LEVERAGE

# Grid fork: STOP strategy and the single fixed TP/SL + stochastic partial-exit removed
# entirely. Exit is now a grid of ATR-multiple take-profit levels (grid_levels searched
# levels, each with its own independently-searched grid_dist_i/grid_frac_i, added
# 2026-08-28 — see eth_trader_bt.grid_level_prices' docstring), with the stop
# trailed to the previous filled level after each fill. Only one bot (AtrPartialPaperBot)
# exists; it is always live-capable.

_log = logging.getLogger("eth_trader_paper")
_log.setLevel(logging.DEBUG)
_log.propagate = False
_fh = RotatingFileHandler(os.path.join(DATA_DIR, "eth_trader_paper.log"),
                          maxBytes=5*1024*1024, backupCount=3, encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                                    datefmt="%Y-%m-%d %H:%M:%S"))
_log.addHandler(_fh)


def _thread_excepthook(args):
    msg = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
    _log.critical(f"Unhandled thread exception in '{(args.thread or threading.current_thread()).name}':\n{msg}")
    try:
        with open(os.path.join(DATA_DIR, "crash_eth_trader_paper.log"), "a", encoding="utf-8") as f:
            f.write(f"\n{'='*60}\n{datetime.now(timezone.utc).isoformat()}\n{msg}\n")
    except Exception: pass

threading.excepthook = _thread_excepthook



# ── Keys + Session ────────────────────────────────────────────────────────────
def make_session():
    """Paper trading's session — mainnet always. Paper never places real orders through
    this session (only LiveExecutor does, on the separate live key), so there is no
    safety reason to point it at Bybit's demo environment; using mainnet means the
    balance/market data it reads is real, not Bybit's simulated demo account state.
    Returns (session, error). Never raises/exits — engine start is user-triggered from
    the Home tab, and a missing/bad key must fail back to that tab, not crash the whole
    app out from under any other tab already in use."""
    k, s = load_keys_secure(live=False)
    if not k or not s:
        return None, "No paper API keys saved — enter them on the Home tab"
    try:
        sess = HTTP(api_key=k, api_secret=s, demo=False)
        r    = sess.get_wallet_balance(accountType="UNIFIED")
        if r.get("retCode",-1) != 0:
            return None, f"Wallet check failed: {r.get('retMsg','')}"
        _log.info(f"Session OK (mainnet key=...{k[-4:]})")
        return sess, None
    except Exception as e:
        return None, f"Cannot connect: {e}"

_NO_RETRY = {110007, 110006, 110012, 110013, 110017, 110025}
_RATE_LIMIT_CODES = {10006, 10018}
_API_SEM = threading.Semaphore(5)

def _api(fn, *args, _retry_exc=True, **kwargs):
    """Call a pybit endpoint with bounded retries.

    _retry_exc=False for non-idempotent calls (order placement): a transport error
    does not mean the exchange rejected the request, so resending can fill twice.
    Retrying on a rate-limit retCode stays safe either way — a throttled request
    never reached the matching engine.
    """
    for attempt in range(3):
        with _API_SEM:
            try: r = fn(*args, **kwargs)
            except Exception:
                if not _retry_exc or attempt == 2: raise
                time.sleep(0.5*(attempt+1)); continue
        if isinstance(r, dict):
            rc = r.get("retCode", 0)
            if rc in _RATE_LIMIT_CODES: time.sleep(1+attempt); continue
            if rc in _NO_RETRY: return r
        return r
    return None

def fetch_balance(session):
    try:
        r = _api(session.get_wallet_balance, accountType="UNIFIED")
        for acct in (r or {}).get("result",{}).get("list",[]):
            for c in acct.get("coin",[]):
                if c.get("coin") == "USDT":
                    raw = c.get("equity") or c.get("walletBalance") or "0"
                    b = float(raw)
                    if b >= 0.01: return b
    except Exception as e: _log.warning(f"Balance fetch: {e}")
    return None


def make_live_session():
    k, s = load_keys_secure(live=True)
    if not k or not s:
        _log.error("Live keys missing — enter them on the Home tab"); return None
    try:
        sess = HTTP(api_key=k, api_secret=s, demo=False)
        r    = sess.get_wallet_balance(accountType="UNIFIED")
        if r.get("retCode", -1) != 0:
            _log.error(f"Live session wallet check failed: {r.get('retMsg','')}"); return None
        _log.info(f"Live session OK key=...{k[-4:]}")
        return sess
    except Exception as e:
        _log.error(f"Live session connect failed: {e}"); return None


def _bt_make_session():
    """Backtest-tab session creation — mainnet always, no demo fallback. Returns
    (None, error) — never raises/exits, since a missing/bad key must fail back to the
    Backtest tab, not kill the whole GUI out from under the Paper/Live tabs that may
    already be trading. Prefers the live key if one is saved (for scaling
    bt.INITIAL_EQUITY to the real wallet balance so backtests size against actual
    capital instead of the config's placeholder value); falls back to the paper key
    otherwise — either way the connection itself is mainnet."""
    is_live = has_keys(live=True)
    k, s = load_keys_secure(live=is_live)
    if not k or not s:
        return None, "No API keys saved — enter them on the Home tab"
    try:
        sess = HTTP(api_key=k, api_secret=s, demo=False)
        r = sess.get_wallet_balance(accountType="UNIFIED")
        if r.get("retCode", -1) != 0:
            return None, f"Session ping failed: {r.get('retMsg','')}"
        if is_live:
            try:
                coins = r["result"]["list"][0]["coin"]
                usdt  = next((c for c in coins if c["coin"] == "USDT"), None)
                bal   = float(usdt["walletBalance"]) if usdt else 0.0
                if bal >= 1.0:
                    bt.INITIAL_EQUITY = bal
            except Exception:
                pass
        return sess, None
    except Exception as e:
        return None, f"Cannot connect: {e}"

def _decimals(step):
    s = f"{step:.10f}".rstrip("0")
    return len(s.split(".")[1]) if "." in s else 0


# ── Live executor ─────────────────────────────────────────────────────────────
class LiveExecutor:
    """Mirror orders to the live Bybit account whenever the paper bot signals.
    self.live_pos is a dict keyed by bot_id -> that bot's own slice ({side, entry, qty,
    orig_qty, grid_filled, ...}); a bot with no key is flat. Kept as a dict (not a bare
    single slice) for the _unattributed/reconcile machinery below, even though there's
    only ever one live-capable bot per symbol (Grid fork — PARTIAL). A special
    "_unattributed" pseudo-bot_id key holds a slice reconcile_on_start found on the
    exchange with unknown/ambiguous provenance (see its docstring) — the first real bot
    to signal an exit on that side claims it.
    """
    def __init__(self, session, symbol, equity_fraction=0.5, db=None):
        self.session              = session
        self.symbol               = symbol
        self.db                   = db       # for persisting closed live trades — see
                                              # _load_live_trades/_save_live_trade; None
                                              # is tolerated (falls back to in-memory only)
        self.equity_fraction      = equity_fraction  # share of total account balance
                                                        # this leg's bots are allowed to
                                                        # size entries from —
                                                        # 1/(2*total_legs), however many
                                                        # crypto legs are running
        self.lot_step             = 0.001
        self.min_qty              = 0.001
        self.max_mkt_qty          = None    # set from instrument info; None = no cap
        self.min_notional         = 0.0     # minimum order notional value
        self._qty_dp              = 3
        self.effective_leverage   = _sym_lev(symbol)
        self._lock                = threading.Lock()
        self.live_pos             = {}       # bot_id -> that bot's own live slice; see
                                              # class docstring. Absent key = flat.
        self._entry_in_flight     = False    # reserved between the check and the order ack
        self._entry_unconfirmed   = None     # {bot_id, side, price} while an entry's fill
                                              # state is unknown and unverifiable — blocks
                                              # new entries until reconciled
        self._miss_strikes        = 0        # consecutive polls that did not see the position
        self.balance              = 0.0
        self.live_trades          = []       # history of closed live trades
        self.cum_live_pnl         = 0.0     # cumulative realized live PnL
        self._load_live_trades()             # restore across restarts — see docstring
        self._load_live_positions()          # restore each bot's own open slice
        self.log_msgs             = deque(maxlen=20)
        # enter()/partial_exit()/mark_closed() used to each spawn a brand-new unpooled
        # daemon thread per call (added 2026-08-23: replaced with one persistent worker
        # draining a queue). A fresh-thread-per-signal design had no explicit ordering
        # guarantee between calls — correctness relied entirely on internal lock
        # contention inside _do_enter/_do_partial/_do_close to serialize correctly.
        # Entry/partial/close are mutually exclusive states for one shared position
        # anyway (you can't close what hasn't entered), so a single serial worker per
        # LiveExecutor both removes the unbounded-thread-creation concern and makes that
        # already-intended "only one live action in flight at a time" invariant
        # explicit instead of emergent. Daemon thread — no shutdown call needed, it dies
        # with the process like the ad-hoc threads it replaces.
        self._work_q = queue.Queue()
        threading.Thread(target=self._worker_loop, daemon=True,
                         name=f"live-worker-{symbol}").start()

    def _worker_loop(self):
        while True:
            fn, args = self._work_q.get()
            try:
                fn(*args)
            except Exception as e:
                self.log(f"live worker: unhandled error in {fn.__name__}: {e}", "ERROR")
            finally:
                self._work_q.task_done()

    def log(self, msg, level="INFO"):
        self.log_msgs.appendleft(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}")
        getattr(_log, level.lower(), _log.info)(f"LIVE: {msg}")

    def setup(self):
        self.effective_leverage = _sym_lev(self.symbol)
        try:
            r    = self.session.get_instruments_info(category=CATEGORY, symbol=self.symbol)
            info = (r or {}).get("result", {}).get("list", [{}])[0]
            lf   = info.get("lotSizeFilter", {})
            self.lot_step     = float(lf.get("qtyStep",        self.lot_step))
            self.min_qty      = float(lf.get("minOrderQty",    self.min_qty))
            self.min_notional = float(lf.get("minNotionalValue", 0) or 0)
            raw_max_mkt       = lf.get("maxMktOrderQty") or lf.get("maxOrderQty")
            if raw_max_mkt:
                self.max_mkt_qty = float(raw_max_mkt)
            self._qty_dp      = _decimals(self.lot_step)
            self.log(f"Instrument: lot_step={self.lot_step} min_qty={self.min_qty} "
                     f"max_mkt_qty={self.max_mkt_qty} min_notional={self.min_notional} "
                     f"leverage={self.effective_leverage}")
        except Exception as e:
            self.log(f"Instrument info error: {e}", "WARNING")
        lev = str(self.effective_leverage)
        # switch_margin_mode (per-symbol isolated/cross) is not called here on purpose:
        # this account is a Unified Trading Account, and Bybit rejects switch-isolated
        # on UTA unconditionally (ErrCode 100028, "unified account is forbidden") — margin
        # mode on a UTA is account-level, not settable per symbol via this endpoint. It
        # failed with this exact error on every single leg setup, every symbol, every
        # session in the logs, never once succeeding — a permanently-doomed call, not an
        # intermittent one. set_leverage below is unaffected and does the part that
        # matters (it already succeeds independently of margin mode).
        try:
            self.session.set_leverage(
                category=CATEGORY, symbol=self.symbol,
                buyLeverage=lev, sellLeverage=lev)
        except Exception as e:
            if "110043" not in str(e):
                self.log(f"set_leverage: {e}", "WARNING")

    def _load_live_trades(self):
        """Restore this symbol's closed-live-trade history from the DB (added 2026-08-24
        — previously live_trades/cum_live_pnl were in-memory only and silently reset to
        empty/zero on every restart, even though the real close and its real P&L had
        already happened and were sitting in the log as text only). No-op if db wasn't
        given (e.g. a standalone/test LiveExecutor)."""
        if self.db is None:
            return
        try:
            rows = self.db.execute(
                "SELECT timestamp, side, entry, exit_price, pnl, reason, bot_id, qty "
                "FROM live_trades WHERE symbol=? ORDER BY id", (self.symbol,)).fetchall()
        except Exception as e:
            self.log(f"_load_live_trades: {e}", "WARNING")
            return
        for ts, side, entry, exit_px, pnl, reason, bot_id, qty in rows:
            self.live_trades.append({"side": side, "entry": entry, "exit": exit_px,
                                     "pnl": pnl, "reason": reason, "ts": ts,
                                     "bot_id": bot_id, "qty": qty})
            self.cum_live_pnl += pnl

    def _save_live_trade(self, side, entry, exit_px, pnl, reason, ts, bot_id, qty):
        """Persist one closed live trade so it survives a restart — mirrors paper's own
        _save_trade/paper_trades pattern. No-op if db wasn't given."""
        if self.db is None:
            return
        try:
            with _DB_LOCK:
                self.db.execute(
                    "INSERT INTO live_trades (symbol,timestamp,side,entry,exit_price,pnl,reason,bot_id,qty) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (self.symbol, ts, side, entry, exit_px, pnl, reason, bot_id, qty))
                self.db.commit()
        except Exception as e:
            self.log(f"_save_live_trade: {e}", "WARNING")

    def _load_live_positions(self):
        """Restore each bot's own open live slice from the DB. No-op if db wasn't given.
        grid_filled/orig_qty (Grid fork) replace the old single partial_done bool —
        orig_qty is needed to size each grid level's close as a fraction of the
        ORIGINAL entry qty, matching the paper side's own grid math."""
        if self.db is None:
            return
        try:
            rows = self.db.execute(
                "SELECT bot_id, side, entry, qty, orig_qty, grid_filled, open_ts "
                "FROM live_position WHERE symbol=?", (self.symbol,)).fetchall()
        except Exception as e:
            self.log(f"_load_live_positions: {e}", "WARNING")
            return
        for bot_id, side, entry, qty, orig_qty, grid_filled, open_ts in rows:
            self.live_pos[bot_id] = {
                "side": side, "entry": entry, "qty": qty,
                "orig_qty": orig_qty if orig_qty else qty, "grid_filled": grid_filled or 0,
                "open_ts": open_ts, "open_mono": time.monotonic(),
            }

    def _save_live_position(self, bot_id):
        """Persist one bot's own live slice (upsert). No-op if db wasn't given or the
        slice no longer exists in memory (call _delete_live_position for that case)."""
        if self.db is None:
            return
        pos = self.live_pos.get(bot_id)
        if pos is None:
            return
        try:
            with _DB_LOCK:
                self.db.execute(
                    "INSERT OR REPLACE INTO live_position "
                    "(bot_id,symbol,side,entry,qty,orig_qty,grid_filled,open_ts) VALUES (?,?,?,?,?,?,?,?)",
                    (bot_id, self.symbol, pos["side"], pos["entry"], pos["qty"],
                     pos.get("orig_qty", pos["qty"]), pos.get("grid_filled", 0), pos["open_ts"]))
                self.db.commit()
        except Exception as e:
            self.log(f"_save_live_position: {e}", "WARNING")

    def _delete_live_position(self, bot_id):
        if self.db is None:
            return
        try:
            with _DB_LOCK:
                self.db.execute("DELETE FROM live_position WHERE bot_id=?", (bot_id,))
                self.db.commit()
        except Exception as e:
            self.log(f"_delete_live_position: {e}", "WARNING")

    def _claim_unattributed_locked(self, bot_id):
        """Call only while already holding self._lock. If reconcile_on_start left a
        slice with unknown provenance under the "_unattributed" pseudo-bot_id (see its
        docstring), let the first real bot to signal a partial/close claim it — same
        spirit as the pre-split app's opener-can-be-None fallback. Returns True if a
        slice now exists for bot_id (either already did, or was just claimed)."""
        if bot_id in self.live_pos:
            return True
        un = self.live_pos.pop("_unattributed", None)
        if un is None:
            return False
        self.live_pos[bot_id] = un
        return True

    def reconcile_on_start(self, partial_bot):
        """Adopt a position that's already open on the exchange before any new entry
        can fire. Needed because Stop-without-closing (see MainWindow._on_stop_paper)
        can leave a live position open with self.live_pos cleared on the next Start —
        without this, the next entry signal would stack a new order on top of it
        instead of recognizing it's already open, doubling live exposure.

        Two cases, checked in order:
        1. DB-persisted per-bot slices (loaded by _load_live_positions before this runs)
           already sum to the exchange's real qty within tolerance — trust them as-is.
           This is the normal case going forward, since every entry/partial/close now
           persists its own slice immediately (see _save_live_position).
        2. They don't (a crash between a live fill and its DB write) — fall back to
           attributing the unexplained qty to PARTIAL (the only bot, Grid fork) if its
           paper position shows a matching open side. If it doesn't match either, hold
           it under the "_unattributed" pseudo-bot_id — the first real bot to signal an
           exit on that side claims it (_claim_unattributed_locked) rather than
           permanently stranding a position no bot's signal can ever close. The
           reconstructed slice's orig_qty is set to whatever qty the exchange shows
           (grid_filled=0) since the real original entry size can't be recovered —
           conservative in that a grid level may re-fire for less than it originally
           would have, never for more than the real remaining position.
        """
        try:
            r = _api(self.session.get_positions, category=CATEGORY, symbol=self.symbol)
            items = (r or {}).get("result", {}).get("list", [])
        except Exception as e:
            self.log(f"reconcile_on_start: position query failed: {e}", "ERROR")
            return
        found = next((it for it in items if float(it.get("size", 0) or 0) > 0), None)
        if found is None:
            if self.live_pos:
                self.log(f"reconcile_on_start: DB had open slice(s) "
                         f"{list(self.live_pos)} but exchange shows no position — "
                         f"clearing stale local state", "WARNING")
                with self._lock:
                    self.live_pos = {}
            return

        side  = "long" if found.get("side") == "Buy" else "short"
        qty   = float(found.get("size", 0))
        entry = float(found.get("avgPrice", 0) or 0.0)

        known_qty = sum(v["qty"] for v in self.live_pos.values() if v["side"] == side)
        tol = max(self.lot_step, qty * 0.01)
        if self.live_pos and abs(known_qty - qty) <= tol:
            with self._lock:
                self._miss_strikes = 0
                for pos in self.live_pos.values():
                    pos["open_mono"] = time.monotonic()
            self.log(f"reconcile_on_start: DB-persisted slice(s) {list(self.live_pos)} "
                     f"(qty={known_qty}) match exchange qty={qty} — adopted as-is")
            return

        # Fallback: unknown or mismatched provenance. Paper position state is restored
        # from the DB before this runs — PARTIAL is the only live-capable bot.
        now_ts = datetime.now(timezone.utc).isoformat()
        with self._lock:
            if partial_bot.position and partial_bot.position.get("side") == side:
                self.live_pos[partial_bot.BOT_ID] = {"side": side, "entry": entry, "qty": qty,
                    "orig_qty": qty, "grid_filled": 0, "open_ts": now_ts, "open_mono": time.monotonic()}
                self.log(f"reconcile_on_start: adopted unattributed {side} qty={qty} "
                         f"into {partial_bot.BOT_ID}", "WARNING")
            else:
                self.live_pos["_unattributed"] = {"side": side, "entry": entry, "qty": qty,
                    "orig_qty": qty, "grid_filled": 0, "open_ts": now_ts, "open_mono": time.monotonic()}
                self.log(f"reconcile_on_start: found {side} qty={qty} but no paper bot "
                         f"claims it — held as unattributed, first bot to signal an "
                         f"exit will claim it", "ERROR")
            self._miss_strikes = 0
            adopted = list(self.live_pos.keys())
        for bot_id in adopted:
            self._save_live_position(bot_id)

    def fetch_balance(self):
        try:
            r = _api(self.session.get_wallet_balance, accountType="UNIFIED")
            for acct in (r or {}).get("result", {}).get("list", []):
                for c in acct.get("coin", []):
                    if c.get("coin") == "USDT":
                        b = float(c.get("equity") or c.get("walletBalance") or "0")
                        if b >= 0.01:
                            self.balance = b; return b
        except Exception as e:
            self.log(f"Balance fetch: {e}", "WARNING")
        return self.balance

    def _round_qty(self, qty):
        """Floor to lot_step. May return < min_qty — callers must check before ordering."""
        q = math.floor(qty / self.lot_step) * self.lot_step
        return round(q, self._qty_dp)

    def _qty_str(self, qty):
        return f"{qty:.{self._qty_dp}f}"

    def _order(self, what, side, qty, reduce_only):
        """Send one market order. Returns the retCode, or None if the outcome is
        UNKNOWN (transport error after the request may have reached the exchange).
        Never resend on None — reconcile against the exchange instead.
        """
        try:
            r = _api(self.session.place_order, _retry_exc=False,
                     category=CATEGORY, symbol=self.symbol,
                     side=side, orderType="Market", qty=self._qty_str(qty),
                     reduceOnly=reduce_only, positionIdx=0)
        except Exception as e:
            self.log(f"{what}: send failed, fill state UNKNOWN — "
                     f"reconciling from exchange: {e}", "ERROR")
            return None
        rc = (r or {}).get("retCode", -1)
        if rc != 0:
            self.log(f"{what} rc={rc}: {(r or {}).get('retMsg','')}", "ERROR")
        return rc

    def enter(self, bot_id, side, price):
        self._work_q.put((self._do_enter, (bot_id, side, price)))

    def _do_enter(self, bot_id, side, price):
        # Reserve under the lock: even though each bot now gets its own slice (added
        # 2026-08-24 — see class docstring) rather than racing for one shared slot,
        # entries still serialize through this single flag/lock so balance/qty math and
        # the _entry_unconfirmed guard can't race between the two bots' signals landing
        # on the same bar.
        with self._lock:
            if bot_id in self.live_pos or self._entry_in_flight or self._entry_unconfirmed is not None:
                self.log(f"{bot_id}: position already open or unconfirmed, skip entry"); return
            other_sides = {v["side"] for v in self.live_pos.values()}
            if other_sides and side not in other_sides:
                self.log(f"{bot_id}: skip entry — conflicts with the other bot's "
                         f"existing {'/'.join(other_sides)} position (one-way account "
                         f"can't hold both directions at once)", "ERROR")
                return
            pre_qty = sum(v["qty"] for v in self.live_pos.values())
            self._entry_in_flight = True
        try:
            self._enter_locked(bot_id, side, price, pre_qty)
        finally:
            with self._lock:
                self._entry_in_flight = False

    def _enter_locked(self, bot_id, side, price, pre_qty=0.0):
        bal   = self.fetch_balance()
        # PARTIAL is the only bot per symbol (Grid fork) — equity_fraction already
        # represents this leg's full capital allocation. The dict-keyed live_pos model
        # (multiple bot_ids possible) is kept as-is for the _unattributed/reconcile
        # machinery, not removed — it just only ever has one real entry per symbol.
        alloc = bal * self.equity_fraction * LIVE_MARGIN_HEADROOM
        qty   = self._round_qty(alloc * self.effective_leverage / price)
        # Cap to exchange market order qty limit
        if self.max_mkt_qty and qty > self.max_mkt_qty:
            qty = self._round_qty(self.max_mkt_qty)
            self.log(f"{bot_id}: qty capped to maxMktOrderQty {self.max_mkt_qty}", "WARNING")
        if qty < self.min_qty:
            self.log(f"{bot_id}: qty {qty} < min {self.min_qty}, skip", "WARNING"); return
        # Notional value check
        if self.min_notional > 0 and qty * price < self.min_notional:
            self.log(f"{bot_id}: notional {qty*price:.2f} < min {self.min_notional}, skip", "WARNING"); return

        order_side = "Buy" if side == "long" else "Sell"
        rc = self._order(f"{bot_id} {side.upper()} entry", order_side, qty, False)
        if rc is None:
            # Unknown fill state. The exchange now reflects pre_qty (the other bot's
            # already-committed slice, if any) plus whatever this order actually did —
            # take the delta as this bot's own share rather than adopting the full
            # exchange qty, which would double-count the other bot's slice.
            actual_total = self._live_qty(side)
            if actual_total is None:
                # The reconciliation query itself failed too — we still don't know if the
                # order filled. Do NOT assume "not filled": block new entries until a later
                # poll can resolve it, instead of risking a second order stacking on top of
                # an untracked live position.
                with self._lock:
                    self._entry_unconfirmed = {"bot_id": bot_id, "side": side, "price": price}
                self.log(f"{bot_id}: entry outcome unknown and position query failed — "
                         f"blocking new entries until reconciled", "ERROR")
                return
            actual = round(actual_total - pre_qty, self._qty_dp)
            if actual < self.min_qty:
                self.log(f"{bot_id}: no new position found after unknown send — "
                         f"treating as not filled", "WARNING")
                return
            self.log(f"{bot_id}: adopted live position qty={actual} after unknown send",
                     "WARNING")
            qty = actual
        elif rc != 0:
            return

        with self._lock:
            self.live_pos[bot_id] = {
                "side": side, "entry": price, "qty": qty, "orig_qty": qty, "grid_filled": 0,
                "open_ts": datetime.now(timezone.utc).isoformat(),
                "open_mono": time.monotonic(),   # grace window for poll_positions
            }
            self._miss_strikes = 0
        self._save_live_position(bot_id)
        self.log(f"{bot_id} {side.upper()} qty={qty} ~{price:.5f} bal={bal:.2f}")

    def partial_exit(self, bot_id, frac):
        """Close `frac` of the position's ORIGINAL entry qty (orig_qty) — one call per
        grid level fill (Grid fork; was a single hardcoded 50% call in the old ATR
        TP/SL design). The caller (AtrPartialPaperBot) already tracks its own
        grid_filled count and only calls this once per level, so this doesn't need its
        own level-identity guard — just the usual "already flat" check."""
        self._work_q.put((self._do_partial, (bot_id, frac)))

    def _do_partial(self, bot_id, frac):
        claimed = False
        with self._lock:
            if bot_id not in self.live_pos:
                had_unattr = "_unattributed" in self.live_pos
                self._claim_unattributed_locked(bot_id)
                claimed = had_unattr and bot_id in self.live_pos
            pos = self.live_pos.get(bot_id)
            if pos is None: return
            qty = pos["qty"]; side = pos["side"]
            orig_qty = pos.get("orig_qty", qty)
        if claimed:
            self._delete_live_position("_unattributed")
            self._save_live_position(bot_id)

        close_qty = min(orig_qty * frac, qty)
        close_qty = round(math.floor(close_qty / self.lot_step) * self.lot_step, self._qty_dp)
        if self.max_mkt_qty and close_qty > self.max_mkt_qty:
            close_qty = round(math.floor(self.max_mkt_qty / self.lot_step) * self.lot_step, self._qty_dp)
        if close_qty < self.min_qty:
            self.log(f"{bot_id}: grid partial qty {close_qty} < min, skip", "WARNING"); return

        close_side = "Sell" if side == "long" else "Buy"
        rc = self._order(f"{bot_id} grid partial", close_side, close_qty, True)
        if rc is None:
            # Unknown fill state. Resending would shed another chunk. Query the exchange
            # and take the delta as this bot's own remaining qty (other bots' slices, if
            # any, must be subtracted first).
            actual_total = self._live_qty(side)
            if actual_total is None:
                self.log(f"{bot_id}: grid partial outcome unknown and position query "
                         f"failed — left for the next bar", "WARNING"); return
            with self._lock:
                other_qty = sum(v["qty"] for k, v in self.live_pos.items()
                                 if k != bot_id and v.get("side") == side)
            this_actual = round(actual_total - other_qty, self._qty_dp)
            shrank = this_actual < qty - self.lot_step / 2
            with self._lock:
                if bot_id in self.live_pos:
                    self.live_pos[bot_id]["qty"] = max(this_actual, 0.0)
                    if shrank:
                        self.live_pos[bot_id]["grid_filled"] = pos.get("grid_filled", 0) + 1
                        self.live_pos[bot_id].pop("partial_retry_pending", None)
                    else:
                        # Flag it for poll_positions to retry with the same fraction —
                        # store it since a later retry call carries no argument of its own.
                        self.live_pos[bot_id]["partial_retry_pending"] = True
                        self.live_pos[bot_id]["_pending_frac"] = frac
            self._save_live_position(bot_id)
            if shrank:
                self.log(f"{bot_id}: grid partial outcome unknown, own qty={this_actual} "
                         f"adopted", "WARNING")
            else:
                self.log(f"{bot_id}: grid partial outcome unknown, own qty unchanged at "
                         f"{this_actual} — will retry via poll", "WARNING")
            return
        if rc != 0:
            return

        with self._lock:
            if bot_id in self.live_pos:
                self.live_pos[bot_id]["grid_filled"] = pos.get("grid_filled", 0) + 1
                self.live_pos[bot_id]["qty"]          = round(qty - close_qty, self._qty_dp)
                self.live_pos[bot_id].pop("partial_retry_pending", None)
        self._save_live_position(bot_id)
        self.log(f"{bot_id} grid partial {close_qty} closed, rem={round(qty - close_qty, self._qty_dp)}")

    def _live_qty(self, side):
        """Query actual position size from exchange. Returns float or None on error."""
        try:
            r        = _api(self.session.get_positions, category=CATEGORY, symbol=self.symbol)
            expected = "Buy" if side == "long" else "Sell"
            for it in (r or {}).get("result", {}).get("list", []):
                if it.get("side") == expected:
                    return float(it.get("size", 0))
            return 0.0
        except Exception as e:
            self.log(f"_live_qty: {e}", "WARNING")
            return None

    def mark_closed(self, bot_id, reason="paper", price=0.0):
        self._work_q.put((self._do_close, (bot_id, reason, price)))

    def _do_close(self, bot_id, reason, price=0.0):
        claimed = False
        with self._lock:
            if bot_id not in self.live_pos:
                had_unattr = "_unattributed" in self.live_pos
                self._claim_unattributed_locked(bot_id)
                claimed = had_unattr and bot_id in self.live_pos
            pos = self.live_pos.get(bot_id)
            if pos is None: return
            side  = pos["side"]
            entry = pos["entry"]
        if claimed:
            self._delete_live_position("_unattributed")
            self._save_live_position(bot_id)

        # Query the exchange fresh rather than trusting our own tracked qty — this bot's
        # own share is the exchange's total for this side minus whatever the other
        # bot's slice (if any) still holds, which also self-corrects for any drift
        # between our internal ledger and the real exchange.
        actual_total = self._live_qty(side)
        if actual_total is None:
            self.log(f"{bot_id}: close ({reason}): position query failed, will retry "
                     f"via poll", "WARNING")
            return
        with self._lock:
            other_qty = sum(v["qty"] for k, v in self.live_pos.items()
                             if k != bot_id and v.get("side") == side)
        my_qty = round(actual_total - other_qty, self._qty_dp)
        if my_qty < self.min_qty:
            self.log(f"{bot_id}: close ({reason}): no live position found for this "
                     f"bot's slice, clearing")
            with self._lock: self.live_pos.pop(bot_id, None)
            self._delete_live_position(bot_id)
            return

        close_side = "Sell" if side == "long" else "Buy"
        rc = self._order(f"{bot_id} close ({reason})", close_side, my_qty, True)
        if rc is None:
            # Leave the slice set: the close is reduceOnly so a later retry cannot flip
            # the position, and poll_positions will confirm if this one did land. Record
            # the intended reason so a later reconciliation doesn't mislabel it "MANUAL".
            with self._lock:
                if bot_id in self.live_pos:
                    self.live_pos[bot_id]["pending_reason"] = reason
            self.log(f"{bot_id}: close ({reason}): outcome unknown, holding position "
                     f"state for the next poll", "WARNING")
            return
        if rc != 0:
            return

        saved = None
        with self._lock:
            if bot_id in self.live_pos:
                exit_px  = price if price > 0 else entry
                fee_est  = exit_px * my_qty * TAKER_FEE
                pnl_est  = ((exit_px - entry) if side == "long" else (entry - exit_px)) * my_qty - fee_est
                ts       = datetime.now(timezone.utc).isoformat()[:19]
                pnl_r    = round(pnl_est, 2)
                self.live_trades.append({"side": side, "entry": entry, "exit": exit_px,
                                         "pnl": pnl_r, "reason": reason, "ts": ts,
                                         "bot_id": bot_id, "qty": my_qty})
                self.cum_live_pnl += pnl_est
                self.log(f"{bot_id}: closed ({reason}) {side} qty={my_qty} pnl≈{pnl_est:+.2f}")
                del self.live_pos[bot_id]
                saved = (side, entry, exit_px, pnl_r, reason, ts, bot_id, my_qty)
        self._delete_live_position(bot_id)
        if saved:
            self._save_live_trade(*saved)

    def _reconcile_unconfirmed_entry(self):
        """Resolve an entry whose fill state was left unknown by a double transport
        failure. Runs on the same poll cadence as poll_positions until the exchange
        query succeeds, then either adopts the position or confirms it never filled.
        """
        with self._lock:
            pending = self._entry_unconfirmed
        if pending is None:
            return
        actual_total = self._live_qty(pending["side"])
        if actual_total is None:
            return   # still unknown — try again on the next poll
        with self._lock:
            if self._entry_unconfirmed is None:
                return   # resolved by another path already
            other_qty = sum(v["qty"] for v in self.live_pos.values()
                             if v.get("side") == pending["side"])
            actual = round(actual_total - other_qty, self._qty_dp)
            adopted_bot_id = None
            if actual >= self.min_qty:
                self.live_pos[pending["bot_id"]] = {
                    "side": pending["side"], "entry": pending["price"], "qty": actual,
                    "orig_qty": actual, "grid_filled": 0,
                    "open_ts": datetime.now(timezone.utc).isoformat(),
                    "open_mono": time.monotonic(),
                }
                self._miss_strikes = 0
                adopted_bot_id = pending["bot_id"]
            self._entry_unconfirmed = None
        if adopted_bot_id:
            self._save_live_position(adopted_bot_id)
            self.log(f"{adopted_bot_id}: reconciled unconfirmed entry, adopted "
                     f"qty={actual}", "WARNING")
        else:
            self.log(f"{pending['bot_id']}: reconciled unconfirmed entry, "
                     f"confirmed not filled", "INFO")

    def _retry_pending_partial(self):
        """Resend a grid partial exit whose outcome was left unknown by a transport
        failure, using the same fraction as the original attempt (Grid fork — a
        position can have several grid partials over its life, not just one)."""
        with self._lock:
            pending = [(bid, pos.get("_pending_frac", 0.0)) for bid, pos in self.live_pos.items()
                       if bid != "_unattributed" and pos.get("partial_retry_pending")]
        for bot_id, frac in pending:
            if frac <= 0:
                continue
            self.log(f"{bot_id}: retrying stalled grid partial exit", "WARNING")
            self._do_partial(bot_id, frac)

    def poll_positions(self):
        """Returns a list of (bot_id, pos_copy) for any bot slice whose side is no
        longer visible on the exchange after enough consecutive misses — i.e. manually
        closed. Bybit's one-way position closing is all-or-nothing per side, so every
        currently-tracked slice on a side that disappears is reported together (there's
        no way to manually close just one bot's internal slice on the exchange).

        A single missing read is not proof: an entry may not be visible yet, and a
        failed or throttled query carries no information. Believing either one would
        clear a slice while the position is still open on the exchange, leaving it
        untracked and never closed. So require POSITION_MISS_STRIKES consecutive
        misses, and ignore the first POSITION_SETTLE_S after any slice's entry.
        """
        try:
            self._reconcile_unconfirmed_entry()
            self._retry_pending_partial()
            with self._lock:
                if not self.live_pos or self._entry_in_flight:
                    self._miss_strikes = 0
                    return []
                settle_ok = all(
                    time.monotonic() - pos.get("open_mono", 0.0) >= POSITION_SETTLE_S
                    for pos in self.live_pos.values())
            if not settle_ok:
                return []

            r = _api(self.session.get_positions, category=CATEGORY, symbol=self.symbol)
            if not isinstance(r, dict) or r.get("retCode", -1) != 0:
                return []                      # no information — do not count a strike
            items = r.get("result", {}).get("list", [])
            open_sides = {it.get("side","").upper() for it in items if float(it.get("size",0)) > 0}

            with self._lock:
                if not self.live_pos:
                    self._miss_strikes = 0
                    return []
                tracked_sides = {pos["side"] for pos in self.live_pos.values()}
                missing = {s for s in tracked_sides
                           if ("BUY" if s == "long" else "SELL") not in open_sides}
                if not missing:
                    self._miss_strikes = 0
                    return []
                self._miss_strikes += 1
                if self._miss_strikes < POSITION_MISS_STRIKES:
                    self.log(f"position not visible ({self._miss_strikes}/"
                             f"{POSITION_MISS_STRIKES}) — confirming before declaring "
                             f"a manual close", "WARNING")
                    return []
                self.log("MANUAL CLOSE confirmed — position gone externally", "WARNING")
                closed = [(bid, dict(pos)) for bid, pos in self.live_pos.items()
                          if pos["side"] in missing]
                for bid, _ in closed:
                    del self.live_pos[bid]
                self._miss_strikes = 0
            for bid, _ in closed:
                self._delete_live_position(bid)
            return closed
        except Exception as e:
            self.log(f"poll_positions: {e}", "WARNING")
            return []

    def record_manual_close(self, bot_id, pos, price):
        """Record one bot's slice closed, as discovered via poll_positions. If _do_close
        had already sent a close with a known reason (TP/SL/paper) whose outcome was
        unknown, use that reason instead of assuming this was actually a manual close."""
        reason  = pos.get("pending_reason") or "MANUAL"
        side    = pos["side"]
        entry   = pos["entry"]
        qty     = pos.get("qty", 0)
        exit_px = price if price > 0 else entry
        fee_est = exit_px * qty * TAKER_FEE
        pnl_est = ((exit_px - entry) if side == "long" else (entry - exit_px)) * qty - fee_est
        ts      = datetime.now(timezone.utc).isoformat()[:19]
        pnl_r = round(pnl_est, 2)
        with self._lock:
            self.live_trades.append({"side": side, "entry": entry, "exit": exit_px,
                                     "pnl": pnl_r, "reason": reason, "ts": ts,
                                     "bot_id": bot_id, "qty": qty})
            self.cum_live_pnl += pnl_est
        self._save_live_trade(side, entry, exit_px, pnl_r, reason, ts, bot_id, qty)
        label = "MANUAL" if reason == "MANUAL" else f"reconciled ({reason})"
        self.log(f"{bot_id}: {label}: {side} entry={entry:.5f} exit≈{exit_px:.5f} "
                 f"pnl≈{pnl_est:+.2f}", "WARNING")

    def mtm(self, bot_id, price):
        with self._lock:
            pos = self.live_pos.get(bot_id)
        if not pos or not price: return 0.0
        entry = pos["entry"]; qty = pos["qty"]
        return (price - entry) * qty if pos["side"] == "long" else (entry - price) * qty

    def mtm_total(self, price):
        """Combined unrealized P&L across every bot's slice — for a symbol-level summary."""
        with self._lock:
            positions = list(self.live_pos.values())
        if not price: return 0.0
        total = 0.0
        for pos in positions:
            entry, qty = pos["entry"], pos["qty"]
            total += (price - entry) * qty if pos["side"] == "long" else (entry - price) * qty
        return total


# ── Combo result loader ───────────────────────────────────────────────────────
def _iter_result_files():
    """Every current (non-OOS) eth_trader_results_*.json file's full path - the
    shared file-discovery step for _load_combo/_load_all_worthy_crypto, both of which
    need to scan every symbol. _load_result_for_symbol scopes to one symbol's own
    filenames directly instead, since it already knows exactly which files it wants."""
    for fname in os.listdir(DATA_DIR):
        if not (fname.startswith("eth_trader_results_") and fname.endswith(".json")):
            continue
        if "_oos" in fname:
            continue
        yield os.path.join(DATA_DIR, fname)


def _parse_result_file(path, require_target=False, max_age_s=None, extra_out=None):
    """Parse one result file into the normalized 8-tuple every leg-selection function
    below returns: (symbol, interval, params, gc_period, gc_poles, leverage, sharpe,
    entry_source). leverage falls back to LEVERAGE and entry_source falls back to
    "searched" for result files written before that field existed. extra_out (added
    2026-08-28): optional dict — if given, populated with
    {"cum_loss", "cum_profit"} read from the same JSON parse this function already does,
    so a caller that needs those fields too (leg-selection ranking) doesn't have to
    reopen/reparse the file a second time. Populated as soon as the file loads
    successfully, before any of the gates below — a caller should still treat extra_out
    as meaningless unless this function's own return value is not None. Returns None if
    the file fails the shared sanity gate (>=1 trade, positive return, positive
    sharpe), fails `bt._clears_target` when require_target=True (added 2026-09-01,
    replacing every win_rate-based gate this function used to support —
    require_perfect_wr/min_win_rate/min_ret_pct are gone entirely, see
    _clears_target's docstring for why win_rate stopped being a selection criterion),
    is older than max_age_s (added 2026-08-23 — found in production: a symbol's
    qualifying result file can sit unrefreshed for well over a day if a backtest cycle
    gets interrupted before reaching it, e.g. by the app being killed mid-sweep, yet
    _load_all_worthy_crypto had no concept of staleness and would happily treat a
    day-old result exactly the same as a 10-minute-old one — the whole point of
    backtesting is to capture what's currently worth trading, not what was worth
    trading a day ago), its symbol/interval isn't currently configured
    (sym in bt.SYMBOLS / iv in bt.CRYPTO_INTERVALS - fixed 2026-08-22: a result file for
    a symbol later removed from config, e.g. a leftover stock result from before stock
    support was removed, was otherwise silently treated as a valid candidate and this
    caused a live-armed stock leg to actually start), or the file can't be parsed at
    all. This parse+filter logic used to be three independent hand-copies (one per
    caller below) - the sym-in-bt.SYMBOLS fix above had to be applied to all three
    separately when it was found; centralizing it here means a future safety fix only
    needs to happen once."""
    try:
        with open(path) as f:
            d = json.load(f)
        if extra_out is not None:
            extra_out["cum_loss"] = float(d.get("cum_loss", 999999.0))
            extra_out["cum_profit"] = float(d.get("cum_profit", -999999.0))
        ret = float(d.get("total_ret_pct", -999))
        sh  = float(d.get("sharpe", -999))
        tr  = int(d.get("trades", 0))
        if tr < 1 or ret <= 0 or sh <= 0:
            return None
        if require_target and not bt._clears_target(d):
            return None
        if max_age_s is not None:
            run_ts = d.get("run_ts")
            if not run_ts:
                return None
            age_s = (datetime.now(timezone.utc) - datetime.fromisoformat(run_ts)).total_seconds()
            if age_s > max_age_s:
                return None
        sym, iv = d.get("symbol"), d.get("interval")
        if sym is None or iv is None or iv not in bt.CRYPTO_INTERVALS or sym not in bt.SYMBOLS:
            return None
        p_dict = d["params"] if "params" in d else d
        entry_source = d.get("entry_source", "searched")
        # gc_period/gc_poles are always p_dict's own searched values — "pine" only
        # differs from "searched" in the GC formula's sqrt2 constant (see
        # _bt_combo_pair's entry_source docstring), it searches every entry param
        # exactly like "searched" does.
        gc_p  = int(p_dict.get("gc_period", 144)); gc_pl = int(p_dict.get("gc_poles", 4))
        return (sym, iv, p_dict, gc_p, gc_pl, int(d.get("leverage", LEVERAGE)), sh, entry_source)
    except Exception:
        return None


def _load_combo():
    """Return the single best-by-sharpe result across every symbol and every currently
    -tested interval (30m only as of 2026-09-01 — see bt.CRYPTO_INTERVALS for whatever
    the live config actually says). Only used for the
    very-first-run "is there anything at all yet" wait gate now - real leg selection
    goes through _load_all_worthy_crypto()/_load_result_for_symbol() instead, which
    additionally requires clearing bt._clears_target (see _load_all_worthy_crypto's
    docstring)."""
    scored = [r for r in (_parse_result_file(p) for p in _iter_result_files()) if r]
    if not scored: return None
    # Key on sharpe (index 6) only - a plain tuple sort falls through to the params
    # dict on a tie.
    scored.sort(key=lambda t: t[6], reverse=True)
    return scored[0]


# Selection rule for _load_all_worthy_crypto — REPLACED ENTIRELY 2026-09-01 (explicit
# user ask, after reviewing a real Backtest tab screenshot together: "what should the
# clear % be? analyse the data. i want to profit return 15% or more and loss under 5
# usdt cuml. dd is a factor too" → "total_ret_pct>=15% AND cum_loss<$5 AND DD tighter
# than 8%" confirmed via AskUserQuestion, DD ceiling confirmed at 5%). This fully
# replaces the win-rate-based hundred/sixty_plus two-tier system that preceded it
# (2026-08-28 "80% WR" rule → 2026-08-31 "60 replaces 80" → this) — win_rate is no
# longer a selection criterion at all, purely informational now. The data that
# motivated dropping it: of 3 real OOS candidates reviewed together, only the one
# clearing these three targets (20.1% ret, $3.28 cum_loss, -3.4% DD) also happened to
# have the LOWEST win_rate (40%) of the three — proving win_rate doesn't track what
# actually matters here; this grid+breakeven-trail strategy's shape (fewer, larger wins
# offsetting many small/breakeven losses) is exactly what a win-rate floor fights. See
# bt._clears_target for the current state of that gate — SUPERSEDED again 2026-09-03,
# explicit user ask ("remove all gates. best params pnl wins"): every numeric
# return/cum_loss/DD floor described in this comment block is gone, `_clears_target`
# now just checks `r` is a real result dict, and `_load_all_worthy_crypto`'s own
# best-by-cum_profit ranking is the entire selection rule. This file only ever calls
# that one shared predicate, never reimplements the check locally. See
# _param_reload_loop for the separate "already-running leg" side: an existing leg
# pauses new entries (not its open position) only on staleness now, since there's no
# target left to fail.


def _load_all_worthy_crypto():
    """Return one entry per symbol whose best-qualifying currently-tested interval
    (30m only as of 2026-09-01, see bt.CRYPTO_INTERVALS) clears bt._clears_target (see the selection-rule
    comment above) — each such symbol gets its own concurrent trading leg with its own
    params and its own winning interval (one winning interval per symbol, not one leg
    per qualifying interval). Filters to `sym in bt.SYMBOLS` (see _load_combo's
    docstring for the concrete failure this guards against). Also requires the result
    be within RESULT_MAX_AGE_S of now (see _parse_result_file's docstring) — a symbol
    only ever gets a leg off its most recent backtest run, never a stale one.

    Single per-symbol lookup: the best-by-(cum_profit - cum_loss) candidate (across
    every tested interval and entry source) clearing bt._clears_target, if any — added
    2026-09-04, explicit user ask after a pure cum_profit ranking picked a combo with
    an 83% win rate but a single $121 loss (-41% DD) over a sibling combo with only a
    $1.37 cum_loss, because the former's raw cum_profit happened to be higher
    ("highest cumP lowest CumL is the winning param for paper trading" — ranking on
    net profit rather than gross profit alone weighs the loss side back in, without
    reintroducing a hard reject gate or position sizing). A symbol with no
    target-clearing candidate at all gets no leg — there is no other fallback (no more
    100%-WR baseline tier; that tier is gone along with the rest of the win-rate-based
    system). Returns a list of the same 7-tuples _load_combo() returns."""
    best = {}  # symbol -> (net_profit, 7-tuple)
    for path in _iter_result_files():
        extra = {}
        r = _parse_result_file(path, require_target=True, max_age_s=RESULT_MAX_AGE_S,
                                extra_out=extra)
        if r is None:
            continue
        net_profit = extra["cum_profit"] - extra["cum_loss"]
        sym = r[0]
        cur = best.get(sym)
        if cur is None or net_profit > cur[0]:
            best[sym] = (net_profit, r)
    return [v[1] for v in best.values()]


def _load_result_for_symbol(symbol, require_fresh=False, require_target=False):
    """Best current qualifying result for one specific symbol, across whichever of
    bt.CRYPTO_INTERVALS (30m only as of 2026-09-01) currently scores best for it — no cross-symbol
    competition, unlike _load_combo(). Used two ways, with different freshness/target
    needs:

    - Ongoing param reload for an already-running leg (_param_reload_loop) passes
      require_fresh=True AND require_target=True (added 2026-08-31 as
      min_win_rate=_MIN_WR_60PLUS, switched to bt._clears_target 2026-09-01 — see that
      function's docstring for why win_rate stopped being the gate; the PAUSE behavior
      itself is unchanged, explicit user ask, "if there is no params above 60wr then
      paper pauses trading until there is") — a leg must never keep refreshing onto a
      stale OR non-target-clearing result any more than it should have started on one;
      if nothing fresh and qualifying is available it should pause new entries instead
      (see PARAM_RELOAD_S/RESULT_MAX_AGE_S), not silently adopt old numbers or keep
      trading a symbol whose best current result no longer clears the target. The leg's
      OWN already-open position is completely unaffected either way — only new entries
      pause, exactly like the pre-existing staleness pause this reuses.
    - The open-position rescue scan in TradingEngine._run() passes require_fresh=False,
      require_target=False (the defaults) deliberately — its whole purpose is to keep
      managing a position that's already open even though its symbol no longer freshly
      or qualifyingly scores, and refusing to hand it *any* params at all would defeat
      that; a symbol being rescued is expected to be stale/non-qualifying by definition.

    Every leg is locked to the symbol it started with for its whole session (never a
    *different* symbol), but its winning interval can still change between reload
    cycles if a different interval starts scoring better — the existing sym_changed
    handling in _param_reload_loop already re-seeds/reconnects correctly for an
    interval-only change on the same symbol, so no separate branch is needed for that.
    Returns None if that symbol has no qualifying (and, if required, fresh/target-
    clearing) result at any tested interval — callers already handle None as "keep
    current params, pause new entries" (see _param_reload_loop)."""
    if symbol not in bt.SYMBOLS:
        return None
    max_age_s = RESULT_MAX_AGE_S if require_fresh else None
    best = None
    best_net = None
    for iv in bt.CRYPTO_INTERVALS:
        # Each entry source has its own result file (added 2026-08-28 — see
        # bt.PINE_GC_SQRT2's docstring) — check both and let the best-by-
        # (cum_profit - cum_loss) win, matching _load_all_worthy_crypto's ranking
        # (2026-09-04) so a reload can never switch a leg onto a different "winning"
        # definition than the one that originally selected it.
        for src in ("searched", "pine"):
            path = os.path.join(DATA_DIR, f"eth_trader_results_{symbol}_{iv}m_{src}.json")
            if not os.path.exists(path):
                continue
            extra = {}
            r = _parse_result_file(path, max_age_s=max_age_s, require_target=require_target,
                                    extra_out=extra)
            # Cross-check against what the filename implied, matching the pre-refactor
            # per-function validation - the file's own internal symbol/interval fields
            # (already matched against bt.SYMBOLS/bt.CRYPTO_INTERVALS inside
            # _parse_result_file) must also agree with the filename this loop constructed.
            if r is None or r[0] != symbol or r[1] != iv:
                continue
            net = extra["cum_profit"] - extra["cum_loss"]
            if best is None or net > best_net:
                best, best_net = r, net
    return best


def _protected_entry_source(symbol, interval):
    """Whichever entry_source currently backs an OPEN position for this exact (symbol,
    interval) — passed to bt.optimize_symbol_interval as protected_source so the
    backtest sweep never overwrites the on-disk result file backing a live position
    ("never make a strat which is in use on a position stale... do not change params
    when there is a position open", strengthened: "a strict no update paper params
    when live position is open"). Reads the DB directly rather than any in-memory
    TradingEngine reference — BacktestRunner has no coupling to TradingEngine, and a
    position persists across restarts via the DB regardless of whether paper/live is
    even running in this process right now.

    Primary source: paper_position, which carries interval+entry_source. Strict
    fallback: if this symbol has ANY open REAL live position at all but paper_position
    has no matching row for this exact interval (should be impossible given
    LegTrader._force_reconcile_paper_from_live's startup repair, but "strict" means
    never assume that repair already ran) — returns the sentinel "ALL" rather than
    None, so the caller protects BOTH entry sources for this interval instead of
    silently protecting neither just because which one is ambiguous. Returns None only
    when there is genuinely no open paper position for this interval AND no open live
    position for this symbol at all."""
    try:
        db = get_db()
        row = db.execute(
            "SELECT entry_source FROM paper_position WHERE symbol=? AND interval=? "
            "AND entry_source IS NOT NULL LIMIT 1", (symbol, interval)).fetchone()
        if row:
            return row[0]
        live_row = db.execute(
            "SELECT 1 FROM live_position WHERE symbol=? LIMIT 1", (symbol,)).fetchone()
        return "ALL" if live_row else None
    except Exception as e:
        _log.warning(f"_protected_entry_source({symbol},{interval}): {e}")
        return None


def _load_worthy_plus_open_positions():
    """_load_all_worthy_crypto(), extended with any symbol that currently holds an open
    paper position but no longer qualifies as worthy. Without this, _report_missed_trades
    permanently stops checking a symbol the moment it drops out of the worthy set — even
    though it may still have an open position and unresolved missed-signal history — the
    same gap TradingEngine._run()'s rescue scan already closes for leg creation, applied
    here to the report instead. require_fresh=False (via _load_result_for_symbol's
    default) since a rescued symbol is expected to be stale by definition."""
    results = _load_all_worthy_crypto()
    worthy_syms = {r[0] for r in results}
    try:
        db = get_db()
        open_syms = {sym for (sym,) in
                     db.execute("SELECT DISTINCT symbol FROM paper_position").fetchall()}
    except Exception as e:
        _log.warning(f"_load_worthy_plus_open_positions: paper_position scan failed: {e}")
        return results
    for sym in open_syms - worthy_syms:
        r = _load_result_for_symbol(sym)
        if r:
            results.append(r)
    return results


# ── DB ────────────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS paper_trades (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp  TEXT, symbol TEXT, interval TEXT,
            strategy   TEXT, side TEXT, entry REAL, exit_price REAL,
            qty REAL, pnl REAL, reason TEXT, partial INTEGER, bars_held INTEGER
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS live_trades (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol     TEXT, timestamp TEXT, side TEXT,
            entry REAL, exit_price REAL, pnl REAL, reason TEXT,
            bot_id TEXT, qty REAL
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS live_position (
            bot_id     TEXT PRIMARY KEY,
            symbol     TEXT, side TEXT, entry REAL, qty REAL, orig_qty REAL,
            grid_filled INTEGER, open_ts TEXT
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS paper_position (
            bot_id     TEXT PRIMARY KEY,
            symbol     TEXT, interval TEXT, side TEXT,
            entry REAL, sl REAL, qty REAL, orig_qty REAL, fee REAL,
            equity REAL, peak_equity REAL, open_ts TEXT,
            grid_px TEXT, grid_level_frac REAL, grid_filled INTEGER, partial_pnl REAL
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bot_state (
            key TEXT PRIMARY KEY, value TEXT
        )""")
    # Migration: entry_source records which entry-signal generator (searched/pine —
    # added 2026-08-28, see bt.PINE_GC_SQRT2's docstring) opened this position, so
    # the backtest sweep can tell which on-disk result file it must never overwrite
    # while the position it backs is still open. CREATE TABLE IF NOT EXISTS above is a
    # no-op against the real DB this repo already has live/paper trade history in (a
    # real trade closed via SL before this feature existed), so this column needs an
    # explicit ALTER TABLE, unlike the schema-was-still-empty columns added earlier in
    # the Grid fork's history.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(paper_position)")}
    if "entry_source" not in cols:
        conn.execute("ALTER TABLE paper_position ADD COLUMN entry_source TEXT")
        _log.info("DB migrated: paper_position.entry_source added")
    # Migration: grid_fracs is a JSON list of per-level close-fractions (added
    # 2026-08-28, see bt.grid_level_prices' docstring) — replaces the old single
    # grid_level_frac REAL applied uniformly to every level. The old column stays in
    # the schema (never dropped) purely so an already-open position from before this
    # change keeps a readable fallback — _load_state reconstructs grid_fracs for such a
    # position by replicating its old scalar across every one of its grid_px levels,
    # exactly reproducing its old uniform-grid behavior for the rest of that trade.
    if "grid_fracs" not in cols:
        conn.execute("ALTER TABLE paper_position ADD COLUMN grid_fracs TEXT")
        _log.info("DB migrated: paper_position.grid_fracs added")
    # Migration: grid_unwound is a JSON list of per-level booleans tracking which
    # levels have used their one-time cross-down TP-capture (added 2026-08-28, see
    # _next_grid_unwind_idx's docstring) — a mechanism entirely separate from the
    # stop-loss, which this migration/column has no effect on whatsoever. NULL for an
    # already-open pre-2026-08-28 position defaults to "nothing unwound yet" on load.
    if "grid_unwound" not in cols:
        conn.execute("ALTER TABLE paper_position ADD COLUMN grid_unwound TEXT")
        _log.info("DB migrated: paper_position.grid_unwound added")
    # Migration: entry_atr/peak_price (added 2026-09-04, trailing-TP — see
    # _manage_exit's trail_tp_mult check and bt.PARAM_SPACE["trail_tp_mult"]'s
    # docstring). entry_atr is fixed at entry (same convention grid_px/sl already
    # use); peak_price is the best price reached since entry, updated every tick.
    # NULL for an already-open pre-2026-09-04 position: entry_atr defaults to 0.0 on
    # load, which _manage_exit's own `entry_atr > 0.0` guard treats as "trailing TP
    # disabled" — the only honest default for a position that predates this feature,
    # same pattern grid_fracs/grid_unwound already use for their own predecessors.
    if "entry_atr" not in cols:
        conn.execute("ALTER TABLE paper_position ADD COLUMN entry_atr REAL")
        _log.info("DB migrated: paper_position.entry_atr added")
    if "peak_price" not in cols:
        conn.execute("ALTER TABLE paper_position ADD COLUMN peak_price REAL")
        _log.info("DB migrated: paper_position.peak_price added")
    conn.commit()
    return conn


def _report_missed_trades(sess, crypto_results):
    """Periodic missed-trade report (added 2026-08-22): for every currently-qualifying
    (clearing the ret/cum_loss/DD targets — see _load_all_worthy_crypto) crypto
    symbol — plus any symbol with an open paper position even if it's
    since fallen out of the worthy set, via _load_worthy_plus_open_positions (added
    2026-08-24, closes a gap where a symbol's unresolved missed-signal history silently
    stopped being checked the moment it stopped qualifying) — replay the last 2 days with
    its own already-selected winning params (bt.replay_recent_trades — no new parameter
    search, reuses what's on file) and
    diff the resulting entries against paper's real trade log for that symbol over the
    same window. Logs one WARNING per backtest-predicted entry with no matching real
    paper trade. Runs once per backtest cycle (see BacktestRunner._run), using its own
    short-lived DB connection — log-only, no UI panel, per explicit instruction.

    This is an early-warning heuristic, not a precise audit: paper_trades only stores
    each trade's *close* timestamp + bars_held (no separate entry timestamp column), so a
    real trade's entry time is reconstructed as close_ts - bars_held*interval_minutes —
    a match is any real trade of the same side whose reconstructed entry falls within 1.5
    bar-durations of the simulated entry. Exits are deliberately not compared (see
    replay_recent_trades's docstring) — only "did an entry happen at all"."""
    db = None
    try:
        db = get_db()
        cutoff = datetime.now(timezone.utc) - timedelta(days=2)
        cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")
        for sym, iv, params, gc_p, gc_pl, lev, sh, entry_source in crypto_results:
            try:
                predicted = bt.replay_recent_trades(sess, sym, iv, params, lev, days=2,
                                                     entry_source=entry_source)
            except Exception as e:
                _log.warning(f"Missed-trade replay failed for {sym}: {e}")
                continue
            if not predicted:
                continue

            rows = db.execute(
                "SELECT timestamp, side, bars_held FROM paper_trades WHERE symbol=? AND timestamp >= ?",
                (sym, cutoff_str)).fetchall()
            real_entries = []
            iv_min = int(iv)
            for ts_str, side, bars_held in rows:
                try:
                    close_ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                except Exception:
                    continue
                real_entries.append((side, close_ts - timedelta(minutes=iv_min * (bars_held or 0))))

            tol_s = iv_min * 60 * 1.5
            for p in predicted:
                matched = any(side == p["side"] and abs((entry_ts - p["entry_ts"]).total_seconds()) <= tol_s
                              for side, entry_ts in real_entries)
                if not matched:
                    _log.warning(
                        f"MISSED TRADE?: {sym} {iv}m {p['side'].upper()} "
                        f"signal at {p['entry_ts'].strftime('%Y-%m-%d %H:%M')} UTC — no matching "
                        f"real paper trade in the last 2 days")
    except Exception as e:
        _log.warning(f"Missed-trade report failed: {e}")
    finally:
        if db is not None:
            db.close()


# ── Indicators ────────────────────────────────────────────────────────────────
# _sma/_atr/_stoch_k/_chop/_gc_filt/gc_midline used to be hand-duplicated copies of
# bt._sma/bt._atr_wilder/bt._stoch_raw_k/bt._chop_index/bt._gc_filt9x/
# bt.gaussian_channel_midline (found 2026-08-23 to be byte-identical algorithms under
# different names) — live's tick-based signal computation must produce exactly the same
# entry/exit decisions the backtest validated, or a live position can open on a signal
# no backtest ever actually confirmed. Importing the shared implementations instead of
# maintaining a second copy means a future fix to either only has to happen once.


def compute_partial_signals(hi, lo, cl, params, entry_source="searched"):
    """Entry signal (Grid fork — the STOP strategy and the stochastic partial-exit
    trigger are both gone; exits are grid-based, computed at entry time in tick()
    itself, not here). entry_source (added 2026-08-28, corrected 2026-08-28 — see
    bt.PINE_GC_SQRT2's docstring): both "searched" and "pine" use params' own
    k_len/k_smooth/d_smooth/ob/os/chop_len/chop_thr/gc_period/gc_poles (all
    backtester-optimized for either source alike) — "pine" differs ONLY in the
    Gaussian Channel formula, using the "Stochastic Triple Filter [ATP]" Pine
    Script's hardcoded 1.414 constant instead of the mathematically exact
    math.sqrt(2). atr_p/stop_mult (the exit side) always come from params either
    way."""
    k_len = int(params["k_len"]); k_sm = int(params["k_smooth"]); d_sm = int(params["d_smooth"])
    ob    = float(params["ob"]); os_  = float(params["os"])
    c_len = int(params["chop_len"]); c_thr = float(params["chop_thr"])
    gc_p  = int(params.get("gc_period", 144)); gc_pl = int(params.get("gc_poles", 4))
    gc_sqrt2 = bt.PINE_GC_SQRT2 if entry_source == "pine" else None
    atr_p = int(params["atr_p"]); stop_m = float(params["stop_mult"])

    gm    = bt.gaussian_channel_midline(hi, lo, cl, gc_p, gc_pl, sqrt2=gc_sqrt2)
    gd    = np.diff(gm, prepend=gm[0]); rising = gd>0; falling = gd<0

    raw_k = bt._stoch_raw_k(hi, lo, cl, k_len)
    k_arr = bt._sma(raw_k, k_sm); d_arr = bt._sma(k_arr, d_sm)
    ci    = bt._chop_index(hi, lo, cl, c_len); atr_arr = bt._atr_wilder(hi, lo, cl, atr_p)

    k_p = np.roll(k_arr,1); d_p = np.roll(d_arr,1)
    val = ~np.isnan(k_arr)&~np.isnan(d_arr)&~np.isnan(k_p)&~np.isnan(d_p)
    cup = val & (k_arr>d_arr) & (k_p<=d_p); cdn = val & (k_arr<d_arr) & (k_p>=d_p)
    cup[0] = cdn[0] = False; ci_ok = ci < c_thr

    buy  = cup & (k_arr<=os_) & rising  & ci_ok
    sell = cdn & (k_arr>=ob)  & falling & ci_ok

    return buy, sell, atr_arr, stop_m


def seed_bars(session, symbol, interval, n=SEED_BARS):
    all_b = {}; end_ms = None
    for _ in range(4):
        kw = dict(category=CATEGORY, symbol=symbol, interval=interval, limit=min(n,1000))
        if end_ms: kw["end"] = end_ms
        r = session.get_kline(**kw); raw = r.get("result",{}).get("list",[])
        if not raw: break
        for b in raw: all_b[int(b[0])] = b
        if len(raw) < 1000: break
        end_ms = min(int(b[0]) for b in raw) - 1
    bars = sorted(all_b.values(), key=lambda x:int(x[0]))[-n:]
    return {"ts":[int(b[0]) for b in bars], "open":[float(b[1]) for b in bars],
            "high":[float(b[2]) for b in bars], "low":[float(b[3]) for b in bars],
            "close":[float(b[4]) for b in bars]}


# ── Bot classes ───────────────────────────────────────────────────────────────
# TradingEngine._run() shares ONE sqlite3 connection (from get_db()) across every bot on
# every leg. Each leg's bots are driven from that leg's own WS callback threads, plus a
# shared reconcile thread that never took LegTrader._lock — so two bots' _save_state/
# _load_state/_save_trade calls can hit the same Connection object at the same instant
# from different threads. sqlite3's check_same_thread=False only disables the same-thread
# assertion; it does NOT make concurrent use of one Connection safe. Found 2026-08-21 as
# "DB save: bad parameter or other API misuse"/"cannot start a transaction within a
# transaction" warnings, worse once multiple same-interval concurrent legs became common
# (their bar-closes land on the same wall-clock moment on separate threads). Fixed
# 2026-08-22 with one process-wide lock serializing every DB access below — cheap since
# each call is a single fast INSERT/UPDATE/DELETE, and correctness matters far more here
# than the negligible serialization cost.
_DB_LOCK = threading.Lock()


def _hit_sl(pos, price):
    return (pos["side"]=="long" and price<=pos["sl"]) or (pos["side"]=="short" and price>=pos["sl"])


def _next_grid_hit(pos, price):
    """True if `price` has reached the next unfilled grid level (Grid fork — replaces
    the old single fixed-TP check). pos["grid_px"] is the full list of level prices
    computed at entry; pos["grid_filled"] is how many have already closed."""
    filled = pos["grid_filled"]
    if filled >= len(pos["grid_px"]):
        return False
    lvl = pos["grid_px"][filled]
    return (pos["side"]=="long" and price>=lvl) or (pos["side"]=="short" and price<=lvl)


def _next_grid_unwind_idx(pos, price):
    """Index of the next down-unwind-eligible level `price` has crossed STRICTLY
    below (long) / above (short), or -1 if none (added 2026-08-28, explicit user ask:
    "i want it to close on a cross down the grid" / "the stop loss stays the same as
    original. this is purely capturing TP" — separate from and never affecting
    _hit_sl). Scans from the most recently filled level (`grid_filled - 1`) downward,
    skipping any level already unwound, stopping at the first level whose price
    hasn't been crossed — correct without an explicit stop-at-sl special case because
    grid_px is monotonically increasing with index (see bt.grid_level_prices) and
    _manage_exit only reaches this scan after _hit_sl has already returned False for
    this price, so in practice this can only ever surface the single level directly
    above wherever the stop currently sits — mirrors _sim_grid_jit's down-unwind loop
    exactly, see that function's own docstring for the full reasoning."""
    grid_px = pos["grid_px"]; filled = pos["grid_filled"]
    unwound = pos.get("grid_unwound") or [False] * len(grid_px)
    side = pos["side"]
    ui = filled - 1
    while ui >= 0:
        lvl = grid_px[ui]
        crossed = (side == "long" and price < lvl) or (side == "short" and price > lvl)
        if not crossed:
            return -1
        if not unwound[ui]:
            return ui
        ui -= 1
    return -1


class _PaperBotBase:
    """Base class for AtrPartialPaperBot (the only paper bot in the Grid fork — kept as
    a base/subclass split rather than folded flat, in case a second strategy variant is
    ever added again). Holds log()/reconcile()/_manage_exit()/cum_pnl/
    cum_loss/mtm() — everything that doesn't depend on the grid-specific position
    shape. __init__/_state_sig/_save_state/_load_state/_save_trade/tick/_partial_grid/
    _close stay on the subclass."""

    def log(self, msg, level="INFO"):
        self.log_msgs.appendleft(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}")
        getattr(_log, level.lower(), _log.info)(f"{self._LOG_TAG}: {msg}")

    def _try_claim_capital(self, sym):
        """Claim this symbol's capital slot before opening a new position (added
        2026-08-25 — see CAPITAL_TIERS/TradingEngine.claim_slot). Returns False if every
        slot is already held by other symbols — the caller must skip the entry entirely
        (paper and live both), rather than opening a paper position with no real capital
        reasoning behind it. On a fresh claim (this symbol didn't already hold a slot),
        re-baselines this bot's own equity to the claimed fraction of the current paper
        balance and updates the shared live executor's equity_fraction the same way.
        Full fraction, not halved — PARTIAL is the only bot per symbol (Grid fork).
        Re-entering into a slot this symbol already holds is a no-op past the claim
        itself: equity keeps compounding normally, never reset."""
        if self.engine is None:
            return True   # no engine wired (e.g. a standalone/test bot) — don't block
        # is_fresh_claim (see claim_slot's own docstring) isn't needed here -- the
        # self._slot_frac != frac check just below already achieves the same
        # re-baseline-only-on-change behavior for this bot's own lifetime.
        frac, _ = self.engine.claim_slot(sym)
        if frac is None:
            self.log("No capital slot available (both held by other symbols) — "
                     "skipping entry", "WARNING")
            return False
        if self._slot_frac != frac:
            self._slot_frac = frac
            self.equity = self.engine.balance * frac
            self.log(f"Claimed capital slot: {frac:.0%} of funds "
                     f"(${self.equity:.2f})")
        if self.live:
            self.live.equity_fraction = frac
        return True

    def _manage_exit(self, price, buy_now=False, sell_now=False, atr=0.0, stop_m=0.0,
                      sym=None, iv=None):
        """SL + grid-level-crossing exit logic, called from tick() on every
        entry-timeframe (5m) bar close — no separate faster feed any more (explicit
        user ask, "get rid of 1 minute candle shit. same for entries as exit").
        Returns True if the position closed. Handles multiple levels crossing in one
        price move (a gap) via the while loop, same as the backtest's bar-by-bar
        simulation does.

        buy_now/sell_now/atr/stop_m/sym/iv (added 2026-08-31 for reverse-and-flip —
        see _flip's docstring): this bar's already-computed entry signal, needed only
        when params["flip_on_signal"] is on. Defaults keep every OTHER caller (there
        are none today, but future callers that don't care about flipping) working
        without having to thread the whole signal tuple through.

        Trailing TP (added 2026-09-04, explicit user ask: "i want grid and trailing
        tp" -> "i want this built in") — checked after SL and flip, before the
        grid-fill/unwind loops: tracks p["peak_price"] (the best price reached since
        entry, updated every call) and closes the ENTIRE remaining position once price
        retraces params["trail_tp_mult"]*p["entry_atr"] from that peak. Guarded on both
        trail_tp_mult>0 (0.0 is the legacy/disabled default via params.get) and
        entry_atr>0 (a reconstructed position from _force_reconcile_paper_from_live can
        legitimately have entry_atr=0 when no real ATR was computable — this guard is
        what stops that case from closing on literally the next tick). Mirrors
        _sim_grid_jit's/_bt_combo_pair's trailing-TP branch exactly — same guard
        structure, same "peak beyond entry" favorable-only check."""
        p = self.position
        if not p: return False
        if p["side"] == "long":
            p["peak_price"] = max(p.get("peak_price", p["entry"]), price)
        else:
            p["peak_price"] = min(p.get("peak_price", p["entry"]), price)
        if _hit_sl(p, price):
            self._close(price, "SL")
            return True
        flip_on = bool(int(self.params.get("flip_on_signal", 0)))
        if flip_on and ((p["side"] == "long" and sell_now)
                        or (p["side"] == "short" and buy_now)):
            self._flip(price, atr, stop_m, sym, iv)
            return True
        trail_mult = float(self.params.get("trail_tp_mult", 0.0))
        entry_atr = p.get("entry_atr", 0.0)
        if trail_mult > 0.0 and entry_atr > 0.0:
            peak = p["peak_price"]; ep = p["entry"]
            if p["side"] == "long":
                favorable = peak > ep
                retraced = peak - price
            else:
                favorable = peak < ep
                retraced = price - peak
            if favorable and retraced >= trail_mult * entry_atr:
                self._close(price, "TRAIL_TP")
                return True
        while _next_grid_hit(p, price):
            if p["grid_filled"] >= len(p["grid_px"]) - 1:
                self._close(price, "GRID")
                return True
            self._partial_grid(price)
            p = self.position
        while True:
            ui = _next_grid_unwind_idx(p, price)
            if ui < 0:
                break
            if self._partial_unwind(price, ui):
                return True
            p = self.position
        return False

    def _flip(self, price, atr, stop_m, sym, iv):
        """Reverse-and-flip (added 2026-08-31, explicit user ask: "reverse-and-flip" —
        if the entry signal flips against an open position before the stop is hit,
        close it and immediately open the opposite side at the same price instead of
        just waiting for the ATR-distance stop to eventually trigger — the same move
        that would have stopped out the old position becomes the entry for the new
        one. Only ever reached from _manage_exit, and only after the stop-loss check
        already came back False this bar (SL always wins if both would trigger the
        same bar) — mirrors _sim_grid_jit's flip branch exactly, including doing no
        grid/unwind check on the freshly-opened position this same call (a position
        that just opened this instant can't have crossed a grid level yet regardless,
        same as any other fresh entry from tick()'s own flat branch).

        atr<=0 (can happen on a thin/just-seeded bar buffer, same guard tick() already
        uses for a fresh entry) aborts the re-open — the old position still closes, the
        bot is just flat until the next bar's entry check picks a fresh signal up
        normally; it does not retry the flip itself, since the underlying condition
        (invalid ATR) isn't something retrying the same bar would fix."""
        old_side = self.position["side"]
        self._close(price, "FLIP")
        if atr <= 0:
            return
        new_side = "short" if old_side == "long" else "long"
        levels = int(self.params.get("grid_levels", 4))
        grid_dists = [float(self.params.get(f"grid_dist_{i+1}", self.params.get("grid_atr_mult", 1.0)))
                      for i in range(levels)]
        grid_fracs = [float(self.params.get(f"grid_frac_{i+1}", self.params.get("grid_level_frac", 0.25)))
                      for i in range(levels)]
        if not self._try_claim_capital(sym): return
        ntl = self.equity * self.lev * MARGIN_HEADROOM
        qty = ntl/price; fee = ntl*TAKER_FEE; self.equity -= fee
        side_mult = 1 if new_side == "long" else -1
        grid_px = bt.grid_level_prices(price, atr, side_mult, levels, grid_dists)
        sl = price - stop_m*atr if new_side == "long" else price + stop_m*atr
        self.position = {"symbol":sym,"interval":iv,"side":new_side,"entry":price,
                          "sl":sl,"qty":qty,"orig_qty":qty,"fee":fee,
                          "grid_px":grid_px,"grid_fracs":grid_fracs,"grid_filled":0,
                          "grid_unwound":[False]*levels,
                          "entry_atr":atr,"peak_price":price,
                          "partial_pnl":0.0,"bars":0,
                          "open_ts":datetime.now(timezone.utc).isoformat()}
        self._save_state()
        self.log(f"FLIP->{new_side.upper()} entry={price:.4f} sl={sl:.4f} "
                 f"grid={['%.4f'%g for g in grid_px]}")
        if self.live: self.live.enter(self.BOT_ID, new_side, price)

    def reconcile(self, kline_stale):
        p = self.position
        self.log(f"Reconcile: pos={'open' if p else 'flat'} trades={len(self.trades)} eq={self.equity:.2f}"
                 + (" [KLINE STALE]" if kline_stale else ""), "WARNING" if kline_stale else "INFO")
        # Skip the DB write if nothing changed since the last save — entries/partials/
        # closes already save immediately at the point of change, so this heartbeat call
        # is almost always a no-op write. Added 2026-08-22: with N legs x 2 bots hitting
        # _DB_LOCK every RECONCILE_S regardless of state, most of those writes were
        # redundant.
        if self._state_sig() != self._last_saved_sig:
            self._save_state()

    @property
    def cum_pnl(self):
        return sum(t["pnl_allin"] for t in self.trades)

    @property
    def cum_loss(self):
        return sum(t["pnl_allin"] for t in self.trades if t["pnl_allin"] < 0)

    def mtm(self, price=None):
        p = self.position
        if not p: return 0.0
        px = price if price else p["entry"]
        return ((px-p["entry"]) if p["side"]=="long" else (p["entry"]-px))*p["qty"] - p["fee"]


class AtrPartialPaperBot(_PaperBotBase):
    """The one remaining strategy (Grid fork — ATR_STOP removed entirely). Entry:
    searched-GC stochastic/chop signal (compute_partial_signals). Exit: a grid of
    ATR-multiple take-profit levels computed once at entry (grid_levels searched levels,
    each with its own independently-searched grid_dist_i/grid_frac_i — see
    bt.grid_level_prices' docstring), each closing that level's own fraction of the
    ORIGINAL entry qty except the last (which closes whatever remains), with the stop
    trailed to the previous filled level after each fill (breakeven after the first)."""
    BOT_ID = "partial"
    _LOG_TAG = "PARTIAL"

    def __init__(self, params, equity, db, lev=None, live=None, bot_id=None, entry_source="searched"):
        self.params = params; self.equity = equity; self.peak_equity = equity; self.db = db
        self.lev = lev if lev is not None else LEVERAGE
        self.live = live
        self.BOT_ID = bot_id or self.__class__.BOT_ID
        # Which entry-signal generator currently backs this leg — "searched" or "pine"
        # (both backtester-optimized; "pine" differs only in the Gaussian Channel's
        # 1.414-vs-sqrt2 constant, matching the "Stochastic Triple Filter [ATP]" Pine
        # Script — added 2026-08-28, see bt.PINE_GC_SQRT2's docstring). Kept in sync
        # with the winning result by _param_reload_loop. Persisted in paper_position so
        # the backtest sweep can tell, without any direct reference to this running bot,
        # which on-disk result file must never be overwritten while the position it
        # backs is still open.
        self.entry_source = entry_source
        self.position = None; self.trades = []
        self._last_close_ts = 0.0
        self._bar_count = 0; self.log_msgs = deque(maxlen=30)
        self._last_saved_sig = None
        # True when this leg's backtest result has gone stale (no fresh re-sweep within
        # RESULT_MAX_AGE_S — see _param_reload_loop) or was force-included by the
        # open-position rescue scan despite no longer qualifying. Blocks new entries
        # only — an already-open position (SL/grid checks, the whole `if
        # self.position:` branch of tick()) is completely unaffected either way.
        self.entries_paused = False
        # Capital-slot bookkeeping (added 2026-08-25): engine is set post-construction
        # by TradingEngine._run() (never None in practice, but default kept for a
        # standalone/test bot); _slot_frac tracks which CAPITAL_TIERS fraction this
        # bot's symbol currently holds, so equity is only re-baselined the moment a
        # *new* occupancy starts, never on every re-entry into an already-held slot.
        self.engine     = None
        self._slot_frac = None
        self._load_state()
        self._last_saved_sig = self._state_sig()

    def _state_sig(self):
        p = self.position
        # peak_price (added 2026-09-04 for trailing-TP — see _manage_exit) is included
        # here so the periodic reconcile() heartbeat actually notices and persists it:
        # it updates every tick a position is open, independent of any trade event
        # (entry/fill/unwind/close already save immediately on their own), so without
        # it in this signature a restart between reconcile cycles could silently lose
        # up to RECONCILE_S worth of "how favorable did this get" memory.
        pos_sig = (p["side"], p["entry"], p["qty"], p.get("sl"), p.get("grid_filled"),
                   p.get("partial_pnl"), p.get("peak_price")) if p else None
        return (pos_sig, round(self.equity, 8), round(self.peak_equity, 8), len(self.trades))

    def _save_state(self):
        try:
            with _DB_LOCK:
                p = self.position
                if p:
                    self.db.execute("""
                        INSERT OR REPLACE INTO paper_position
                        (bot_id,symbol,interval,side,entry,sl,qty,orig_qty,fee,equity,peak_equity,
                         open_ts,grid_px,grid_level_frac,grid_filled,partial_pnl,entry_source,grid_fracs,
                         grid_unwound,entry_atr,peak_price)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (self.BOT_ID, p["symbol"], p["interval"], p["side"],
                         p["entry"], p.get("sl",0), p["qty"], p.get("orig_qty", p["qty"]),
                         p.get("fee",0), self.equity, self.peak_equity, p.get("open_ts",""),
                         json.dumps(p.get("grid_px", [])), p.get("grid_fracs", [0.25])[0]
                         if p.get("grid_fracs") else 0.25,
                         p.get("grid_filled", 0), p.get("partial_pnl", 0.0), self.entry_source,
                         json.dumps(p.get("grid_fracs", [])),
                         json.dumps(p.get("grid_unwound", [])),
                         p.get("entry_atr", 0.0), p.get("peak_price", p["entry"])))
                else:
                    self.db.execute("DELETE FROM paper_position WHERE bot_id=?", (self.BOT_ID,))
                    self.db.execute("INSERT OR REPLACE INTO bot_state VALUES (?,?)",
                                    (f"equity_{self.BOT_ID}", str(self.equity)))
                    self.db.execute("INSERT OR REPLACE INTO bot_state VALUES (?,?)",
                                    (f"peak_{self.BOT_ID}", str(self.peak_equity)))
                self.db.commit()
            self._last_saved_sig = self._state_sig()
        except Exception as e: self.log(f"DB save: {e}", "WARNING")

    def _load_state(self):
        try:
            with _DB_LOCK:
                # Load history first — it must survive a restart that happens mid-position.
                rows = self.db.execute(
                    "SELECT side,entry,exit_price,pnl,reason,partial FROM paper_trades WHERE strategy=? ORDER BY id",
                    (self.BOT_ID,)
                ).fetchall()
                row = self.db.execute(
                    "SELECT symbol,interval,side,entry,sl,qty,COALESCE(orig_qty,qty),fee,equity,"
                    "peak_equity,open_ts,COALESCE(grid_px,'[]'),COALESCE(grid_level_frac,0.25),"
                    "COALESCE(grid_filled,0),COALESCE(partial_pnl,0),entry_source,grid_fracs,"
                    "grid_unwound,COALESCE(entry_atr,0.0),peak_price "
                    "FROM paper_position WHERE bot_id=?", (self.BOT_ID,)).fetchone()
                eq_r = pk_r = None
                if not row:
                    eq_r = self.db.execute("SELECT value FROM bot_state WHERE key=?",
                                           (f"equity_{self.BOT_ID}",)).fetchone()
                    pk_r = self.db.execute("SELECT value FROM bot_state WHERE key=?",
                                           (f"peak_{self.BOT_ID}",)).fetchone()
            for r in rows:
                self.trades.append({"side":r[0],"entry":r[1],"exit":r[2],
                                    "pnl_allin":r[3],"reason":r[4],"partial":bool(r[5])})
            if row:
                (sym,iv,side,entry,sl,qty,orig_qty,fee,eq,pk,ots,grid_px_json,
                 grid_level_frac,grid_filled,ppnl,saved_entry_source,grid_fracs_json,
                 grid_unwound_json,entry_atr,peak_price) = row
                self.equity = eq; self.peak_equity = pk
                # Restore the entry_source the OPEN position actually persisted, not
                # whatever the constructor was called with — a leg that reloads onto a
                # fresh result mid-position must not retroactively relabel an
                # already-open position's entry source (matches the position-freeze
                # invariant: params/entry_source never change while a position is open).
                if saved_entry_source:
                    self.entry_source = saved_entry_source
                grid_px = json.loads(grid_px_json)
                # grid_fracs (added 2026-08-28, per-level close fractions — see
                # bt.grid_level_prices' docstring): NULL for any position opened before
                # this column existed — reconstruct by replicating the old uniform
                # grid_level_frac scalar across every one of this position's grid_px
                # levels, exactly reproducing its old uniform-grid behavior for the
                # rest of that already-open trade.
                grid_fracs = (json.loads(grid_fracs_json) if grid_fracs_json
                              else [grid_level_frac] * len(grid_px))
                # grid_unwound (added 2026-08-28, cross-down TP-capture — see
                # _next_grid_unwind_idx's docstring): NULL for any position opened
                # before this column existed — defaults to "nothing unwound yet" (all
                # False), the same conservative assumption used for a freshly-adopted
                # live position with no local history.
                grid_unwound = (json.loads(grid_unwound_json) if grid_unwound_json
                                 else [False] * len(grid_px))
                self.position = {"symbol":sym,"interval":iv,"side":side,"entry":entry,
                                 "sl":sl,"qty":qty,"orig_qty":orig_qty,"fee":fee,"bars":0,
                                 "open_ts":ots or "","grid_px":grid_px,
                                 "grid_fracs":grid_fracs,"grid_filled":grid_filled,
                                 "grid_unwound":grid_unwound,
                                 "entry_atr":entry_atr or 0.0,
                                 "peak_price":peak_price if peak_price is not None else entry,
                                 "partial_pnl":ppnl}
                self.log(f"Restored: {side} entry={entry:.5f} qty={qty:.4f} "
                         f"grid={grid_filled}/{len(self.position['grid_px'])} "
                         f"entry_source={self.entry_source}")
                return
            if eq_r: self.equity = float(eq_r[0])
            if pk_r: self.peak_equity = float(pk_r[0])
        except Exception as e: self.log(f"DB load: {e}", "WARNING")

    def _save_trade(self, sym, iv, side, entry, exit_px, qty, pnl, reason, partial, bars):
        try:
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            with _DB_LOCK:
                self.db.execute(
                    "INSERT INTO paper_trades (timestamp,symbol,interval,strategy,side,entry,exit_price,qty,pnl,reason,partial,bars_held) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ts, sym, iv, self.BOT_ID, side, entry, exit_px, qty, pnl, reason, int(partial), bars))
                self.db.commit()
        except Exception as e: self.log(f"DB trade: {e}", "WARNING")

    def tick(self, hi, lo, cl, sym, iv, entry_signal):
        n = len(cl)
        if n < 50: return
        buy, sell, atr_arr, stop_m = entry_signal
        price = cl[-1]; self._bar_count += 1

        if self.position:
            p = self.position; p["bars"] = p.get("bars",0)+1
            atr_now = atr_arr[-1] if not np.isnan(atr_arr[-1]) else 0.0
            self._manage_exit(price, bool(buy[-1]), bool(sell[-1]), atr_now, stop_m, sym, iv)
        else:
            if self.entries_paused: return
            if time.time()-self._last_close_ts < COOLDOWN_S: return
            if not _entry_allowed(): return
            atr = atr_arr[-1] if not np.isnan(atr_arr[-1]) else 0
            levels = int(self.params.get("grid_levels", 4))
            # Per-level distance/fraction (added 2026-08-28, see bt.grid_level_prices'
            # docstring) — grid_level_prices is the SAME function _bt_combo_pair's pure
            # -Python twin calls, so live and backtest can never silently diverge on how
            # these prices are built. Falls back to the old scalar grid_atr_mult/
            # grid_level_frac if this leg's params predate the per-level keys (a result
            # file saved before 2026-08-28 that's still fresh enough to be in use).
            grid_dists = [float(self.params.get(f"grid_dist_{i+1}", self.params.get("grid_atr_mult", 1.0)))
                          for i in range(levels)]
            grid_fracs = [float(self.params.get(f"grid_frac_{i+1}", self.params.get("grid_level_frac", 0.25)))
                          for i in range(levels)]
            if buy[-1] and atr > 0:
                if not self._try_claim_capital(sym): return
                ntl = self.equity * self.lev * MARGIN_HEADROOM
                qty = ntl/price; fee = ntl*TAKER_FEE; self.equity -= fee
                grid_px = bt.grid_level_prices(price, atr, 1, levels, grid_dists)
                self.position = {"symbol":sym,"interval":iv,"side":"long","entry":price,
                                  "sl":price-stop_m*atr,"qty":qty,"orig_qty":qty,"fee":fee,
                                  "grid_px":grid_px,"grid_fracs":grid_fracs,"grid_filled":0,
                                  "grid_unwound":[False]*levels,
                                  "entry_atr":atr,"peak_price":price,
                                  "partial_pnl":0.0,"bars":0,
                                  "open_ts":datetime.now(timezone.utc).isoformat()}
                self._save_state()
                self.log(f"LONG  entry={price:.4f} sl={price-stop_m*atr:.4f} "
                         f"grid={['%.4f'%g for g in grid_px]}")
                if self.live: self.live.enter(self.BOT_ID, "long", price)
            elif sell[-1] and atr > 0:
                if not self._try_claim_capital(sym): return
                ntl = self.equity * self.lev * MARGIN_HEADROOM
                qty = ntl/price; fee = ntl*TAKER_FEE; self.equity -= fee
                grid_px = bt.grid_level_prices(price, atr, -1, levels, grid_dists)
                self.position = {"symbol":sym,"interval":iv,"side":"short","entry":price,
                                  "sl":price+stop_m*atr,"qty":qty,"orig_qty":qty,"fee":fee,
                                  "grid_px":grid_px,"grid_fracs":grid_fracs,"grid_filled":0,
                                  "grid_unwound":[False]*levels,
                                  "entry_atr":atr,"peak_price":price,
                                  "partial_pnl":0.0,"bars":0,
                                  "open_ts":datetime.now(timezone.utc).isoformat()}
                self._save_state()
                self.log(f"SHORT entry={price:.4f} sl={price+stop_m*atr:.4f} "
                         f"grid={['%.4f'%g for g in grid_px]}")
                if self.live: self.live.enter(self.BOT_ID, "short", price)

    def _partial_grid(self, price):
        """Close one grid level's worth of the position (that level's OWN
        independently-searched fraction of the ORIGINAL entry qty — grid_fracs[filled],
        added 2026-08-28, replacing a single grid_level_frac shared by every level —
        capped to whatever remains) and trail the stop up to the previous filled
        level's price (breakeven after the first fill). Never called for the LAST
        level — _manage_exit routes that straight to _close() instead, so the position
        is always fully closed by the time all levels have filled."""
        p = self.position
        filled_idx = p["grid_filled"]
        frac = p.get("grid_fracs", [0.25] * len(p["grid_px"]))[filled_idx]
        qty_i = min(p["orig_qty"] * frac, p["qty"])
        fee_i = price * qty_i * TAKER_FEE
        pnl_i = ((price-p["entry"]) if p["side"]=="long" else (p["entry"]-price)) * qty_i - fee_i
        entry_fee_i = p.get("fee", 0.0) * (qty_i / p["qty"])
        self.equity += pnl_i; self.peak_equity = max(self.peak_equity, self.equity)
        p["partial_pnl"] = p.get("partial_pnl", 0.0) + pnl_i - entry_fee_i
        p["fee"] = p.get("fee", 0.0) - entry_fee_i
        p["qty"] -= qty_i
        p["grid_filled"] += 1
        filled = p["grid_filled"]
        # Breakeven after the first fill, then trail to the previous filled level's
        # price after each subsequent one — profit already banked at a lower level can
        # never be given back once a higher one fills. Matches eth_trader_bt.py's
        # _sim_grid_jit exactly.
        p["sl"] = p["entry"] if filled == 1 else p["grid_px"][filled-2]
        self._save_state()
        self.log(f"GRID L{filled}/{len(p['grid_px'])} {p['side']} exit={price:.4f} "
                 f"qty={qty_i:.4f} pnl={pnl_i:+.2f} rem={p['qty']:.4f} sl->{p['sl']:.4f}")
        if self.live: self.live.partial_exit(self.BOT_ID, frac)

    def _partial_unwind(self, price, ui):
        """Cross-down TP-capture (added 2026-08-28, explicit user ask — see
        _next_grid_unwind_idx's docstring). Closes level `ui`'s OWN grid_frac (of
        CURRENT remaining qty, not original — this is a fresh partial close, not
        "undoing" the earlier up-fill; that qty/profit is already banked) and marks
        it unwound so it can't fire a second time. The stop-loss (`p["sl"]`) is NEVER
        touched here — it keeps whatever value _partial_grid/entry already gave it,
        completely unaffected by unwind events, per explicit user ask ("the stop loss
        stays the same as original"). Returns True if this unwind fully closed the
        position (qty hit ~0), in which case the caller must stop looping — mirrors
        _sim_grid_jit's own qty_rem<=1e-12 check exactly."""
        p = self.position
        frac = p.get("grid_fracs", [0.25] * len(p["grid_px"]))[ui]
        qty_i = min(p["orig_qty"] * frac, p["qty"])
        fee_i = price * qty_i * TAKER_FEE
        pnl_i = ((price-p["entry"]) if p["side"]=="long" else (p["entry"]-price)) * qty_i - fee_i
        entry_fee_i = p.get("fee", 0.0) * (qty_i / p["qty"])
        self.equity += pnl_i; self.peak_equity = max(self.peak_equity, self.equity)
        p["partial_pnl"] = p.get("partial_pnl", 0.0) + pnl_i - entry_fee_i
        p["fee"] = p.get("fee", 0.0) - entry_fee_i
        p["qty"] -= qty_i
        unwound = p.get("grid_unwound") or [False] * len(p["grid_px"])
        unwound[ui] = True
        p["grid_unwound"] = unwound
        self.log(f"UNWIND L{ui+1}/{len(p['grid_px'])} {p['side']} exit={price:.4f} "
                 f"qty={qty_i:.4f} pnl={pnl_i:+.2f} rem={p['qty']:.4f}")
        if self.live: self.live.partial_exit(self.BOT_ID, frac)
        if p["qty"] <= 1e-9:
            self._close(price, "UNWIND")
            return True
        self._save_state()
        return False

    def _close(self, price, reason):
        p = self.position; fee = price*p["qty"]*TAKER_FEE
        pnl = ((price-p["entry"]) if p["side"]=="long" else (p["entry"]-price))*p["qty"] - fee
        pnl_ai = p.get("partial_pnl", 0.0) + pnl - p["fee"]
        self.equity += pnl; self.peak_equity = max(self.peak_equity, self.equity)
        had_partial = p.get("grid_filled", 0) > 0
        self.trades.append({"side":p["side"],"entry":p["entry"],"exit":price,
                             "pnl_allin":round(pnl_ai,4),"reason":reason,
                             "partial":had_partial,
                             "ts":datetime.now(timezone.utc).isoformat()[:19]})
        self._save_trade(p["symbol"], p["interval"], p["side"], p["entry"], price,
                         p["qty"], round(pnl_ai,4), reason, had_partial, p.get("bars",0))
        self.log(f"CLOSE {reason} {p['side']} exit={price:.4f} pnl={pnl_ai:+.2f} eq={self.equity:.2f}")
        self.position = None
        self._last_close_ts = time.time(); self._save_state()
        if self.live: self.live.mark_closed(self.BOT_ID, reason, price)


# ── Leg Trader (shared kline + portfolio) ──────────────────────────────────────
class LegTrader:
    def __init__(self, session, symbol, interval, partial_bot):
        self.session  = session; self.symbol = symbol; self.interval = interval
        self.partial  = partial_bot
        # entry_source is a property below (reads self.partial.entry_source) rather
        # than a separately-tracked field — the bot itself is the single source of
        # truth for which entry-signal generator it's using, so there's nothing to
        # keep in sync here.
        self.bars     = {"ts":[],"open":[],"high":[],"low":[],"close":[]}
        self._lock    = threading.Lock()
        self._last_ts = 0; self._last_kline_ts = time.time()
        self._stopped = threading.Event()
        self._force_reconnect = False
        self._peak_combined = self.partial.equity
        self._display_price = 0.0
        self.engine = None   # set post-construction by TradingEngine._run()

    def start(self):
        _log.info(f"Seeding {SEED_BARS} bars for {self.symbol} {self.interval}m...")
        with self._lock:
            self.bars = seed_bars(self.session, self.symbol, self.interval)
            self._display_price = self.bars["close"][-1] if self.bars["close"] else 0.0
        self._force_reconcile_paper_from_live()
        _log.info(f"Seeded {len(self.bars['close'])} bars. Starting WS...")
        threading.Thread(target=self._ws_loop, daemon=True, name=f"ws-{self.symbol}").start()

    def _force_reconcile_paper_from_live(self):
        """Restart safety net (after a real incident where a live position sat open
        with no active paper-side management able to close it): if the exchange has a
        real open slice for the bot but its own paper position is missing — never
        restored (paper_position row lost/never saved), or left under LiveExecutor's
        "_unattributed" pseudo-bot_id because reconcile_on_start couldn't match it at
        startup — force-reconstruct a paper position for it here and now, rather than
        leaving it invisible and waiting on a future signal that may never come. Live
        has no exchange-side stop-loss; paper's own signal is the ONLY thing that can
        ever close a live position, so a live slice with no paper position behind it
        has NO path to closing itself. Uses the live slice's own real entry price/side/
        qty and its own open_ts (the live position's real entry time) for the
        reconstructed position's age, and computes a grid fresh from the just-seeded
        bars using this bot's current params — the same formula tick() already uses for
        a brand-new entry — starting from grid_filled=0 (the real fill count can't be
        recovered from the exchange, only the current remaining qty, matching the same
        approximation reconcile_on_start already makes for orig_qty in this scenario)."""
        b = self.bars
        if len(b["close"]) < 50:
            return
        hi = np.array(b["high"]); lo = np.array(b["low"]); cl = np.array(b["close"])
        bot = self.partial
        if bot.position is not None or bot.live is None:
            return
        live_pos = bot.live.live_pos.get(bot.BOT_ID)
        if live_pos is None:
            with bot.live._lock:
                claimed = bot.live._claim_unattributed_locked(bot.BOT_ID)
            if claimed:
                bot.live._delete_live_position("_unattributed")
                bot.live._save_live_position(bot.BOT_ID)
                live_pos = bot.live.live_pos.get(bot.BOT_ID)
        if live_pos is None:
            return

        entry = live_pos["entry"]; side = live_pos["side"]
        levels = int(bot.params.get("grid_levels", 4))
        # Per-level distance/fraction (added 2026-08-28, see bt.grid_level_prices'
        # docstring) — same fallback-to-legacy-scalar pattern as tick()'s entry block.
        grid_dists = [float(bot.params.get(f"grid_dist_{i+1}", bot.params.get("grid_atr_mult", 1.0)))
                      for i in range(levels)]
        grid_fracs = [float(bot.params.get(f"grid_frac_{i+1}", bot.params.get("grid_level_frac", 0.25)))
                      for i in range(levels)]
        try:
            _, _, atr_arr, stop_m = compute_partial_signals(hi, lo, cl, bot.params,
                                                             entry_source=bot.entry_source)
            atr = atr_arr[-1] if not np.isnan(atr_arr[-1]) else 0
        except Exception as e:
            bot.log(f"force-reconcile: ATR compute failed ({e}), using a fallback "
                    f"fixed-%% band instead of the usual ATR stop", "ERROR")
            atr = 0
        if atr > 0:
            sl = entry - stop_m*atr if side == "long" else entry + stop_m*atr
            grid_px = bt.grid_level_prices(entry, atr, 1 if side == "long" else -1,
                                            levels, grid_dists)
        else:
            # Can't compute a real ATR-based stop from just-seeded bars — fall back to a
            # conservative fixed band rather than leaving the position with no exit
            # levels at all (tick()'s _manage_exit() call would then never trigger an exit).
            sl = entry * (0.95 if side == "long" else 1.05)
            grid_px = [entry * ((1.10 if side == "long" else 0.90))] * levels

        bot.position = {
            "symbol": self.symbol, "interval": self.interval, "side": side,
            "entry": entry, "sl": sl, "qty": live_pos["qty"], "orig_qty": live_pos.get("orig_qty", live_pos["qty"]),
            "fee": 0.0, "grid_px": grid_px, "grid_fracs": grid_fracs,
            "grid_filled": live_pos.get("grid_filled", 0),
            "grid_unwound": [False] * levels, "partial_pnl": 0.0,
            # entry_atr=atr here can legitimately be the 0 fallback above (no real ATR
            # computable from just-seeded bars) — _manage_exit's trailing-TP check
            # guards on entry_atr>0 specifically so a reconstructed position like this
            # one never has trailing TP fire on literally the next tick.
            "entry_atr": atr, "peak_price": entry,
            "bars": 0, "open_ts": live_pos.get("open_ts",
                                                datetime.now(timezone.utc).isoformat()),
        }
        bot._save_state()
        bot.log(f"FORCE-RECONCILED from live: {side} entry={entry:.5f} sl={sl:.5f} "
                f"grid={['%.5f'%g for g in grid_px]} qty={live_pos['qty']} — paper had no "
                f"matching position on restart, real live slice found, now under active "
                f"management", "WARNING")

    def _ws_loop(self):
        while not self._stopped.is_set():
            ws = None
            try:
                ws = WebSocket(testnet=False, demo=False, channel_type="linear",
                               retries=3, ping_interval=20, ping_timeout=10)
                # Give the fresh connection a full stale window to deliver its first
                # message, otherwise a stale-triggered reconnect breaks out immediately
                # and spins the loop.
                self._last_kline_ts = time.time()
                # Capture the symbol/interval this specific subscription is actually
                # bound to, at subscription time — _param_reload_loop can repoint
                # self.symbol/self.interval to a new symbol before this old Bybit
                # subscription is torn down (_force_reconnect is only noticed on the
                # inner loop's next 5s poll), and self._on_kline reading live
                # self.symbol would otherwise mislabel a bar from the OLD subscription
                # as belonging to the NEW symbol. _on_kline checks these against the
                # current self.symbol/self.interval and discards the bar if they've
                # since diverged.
                sub_symbol, sub_interval = self.symbol, self.interval
                ws.kline_stream(interval=int(self.interval), symbol=self.symbol,
                                callback=lambda msg, s=sub_symbol, iv=sub_interval:
                                         self._on_kline(msg, s, iv))
                while not self._stopped.is_set():
                    if self._force_reconnect:
                        self._force_reconnect = False
                        _log.info(f"WS reconnecting → {self.symbol} {self.interval}m"); break
                    if time.time()-self._last_kline_ts > WS_STALE_S:
                        _log.warning(f"{self.symbol} public WS stale, reconnecting"); break
                    time.sleep(5)
            except Exception as e:
                _log.warning(f"{self.symbol} public WS error: {e}"); time.sleep(10)
            finally:
                # Without this the old socket stays subscribed: it leaks a thread and
                # keeps feeding bars (of the old symbol, after a switch) into _on_kline.
                if ws is not None:
                    try: ws.exit()
                    except Exception as e: _log.debug(f"WS close: {e}")

    def _on_kline(self, msg, sub_symbol, sub_interval):
        self._last_kline_ts = time.time()
        for bar in msg.get("data",[]):
            if not bar.get("confirm"): continue
            ts = int(bar["start"])
            with self._lock:
                # This subscription has been superseded by a symbol/interval switch but
                # hasn't been torn down yet — discard rather than feed a foreign-symbol
                # bar into the currently-labeled trader.
                if sub_symbol != self.symbol or str(sub_interval) != str(self.interval):
                    continue
                if ts <= self._last_ts: continue
                self._last_ts = ts
                b = self.bars
                b["ts"].append(ts); b["open"].append(float(bar["open"]))
                b["high"].append(float(bar["high"])); b["low"].append(float(bar["low"]))
                b["close"].append(float(bar["close"]))
                if len(b["close"]) > SEED_BARS+100:
                    for k in b: b[k] = b[k][-SEED_BARS:]
                hi = np.array(b["high"]); lo = np.array(b["low"]); cl = np.array(b["close"])
                entry_signal = compute_partial_signals(hi, lo, cl, self.partial.params,
                                                        entry_source=self.partial.entry_source)
                self.partial.tick(hi, lo, cl, self.symbol, self.interval, entry_signal)
                self._maybe_release_slot()
                if self.partial.equity > self._peak_combined: self._peak_combined = self.partial.equity
                self._display_price = b["close"][-1]

    # _ws_loop_1m/_on_kline_1m removed (explicit user ask, "get rid of 1 minute
    # candle shit. same for entries as exit") — exits now check only on the same
    # entry-timeframe candle as signals (via tick()'s own _manage_exit call), never
    # a faster supplementary feed. Worst-case exit-check latency is bounded by
    # whichever bt.CRYPTO_INTERVALS entry this leg's own symbol is currently trading
    # (30m only as of 2026-09-01 — see eth_trader_bt.py), never faster than 1 minute
    # again regardless of interval.

    def _maybe_release_slot(self):
        """Free this symbol's capital slot the moment the bot goes flat (added
        2026-08-25 — see CAPITAL_TIERS/TradingEngine.claim_slot). Idempotent and cheap
        to call unconditionally after every tick/reconcile pass — a symbol
        that never held a slot, or already released one, is just a no-op dict pop.
        Must be called with self._lock already held by the caller, matching every
        existing call site (tick() mutates position under this lock)."""
        if self.partial.position is None:
            if self.engine:
                self.engine.release_slot(self.symbol)
            self.partial._slot_frac = None

    def reconcile(self):
        stale = time.time()-self._last_kline_ts > WS_STALE_S
        with self._lock:
            self.partial.reconcile(stale)
            self._maybe_release_slot()

    @property
    def entry_source(self): return self.partial.entry_source

    @property
    def ws_ok(self): return time.time()-self._last_kline_ts < WS_STALE_S

    @property
    def combined_equity(self): return self.partial.equity

    @property
    def drawdown_pct(self):
        pk = self._peak_combined
        if pk <= 0: return 0.0
        return (self.combined_equity - pk) / pk * 100

    @property
    def cum_pnl(self): return self.partial.cum_pnl

    @property
    def cum_loss(self): return self.partial.cum_loss

    def last_price(self):
        """Most recent price seen on the entry-timeframe feed, updated on every
        confirmed bar close, while entry/exit signals still only ever act on the
        entry-timeframe bar buffer (self.bars), never on this value."""
        return self._display_price or (self.bars["close"][-1] if self.bars["close"] else 0.0)


# ── TUI ───────────────────────────────────────────────────────────────────────
def _bot_stats(trades):
    wins     = [t["pnl_allin"] for t in trades if t["pnl_allin"] > 0]
    losses   = [t["pnl_allin"] for t in trades if t["pnl_allin"] <= 0]
    n        = len(trades)
    avg_win  = sum(wins)   / len(wins)   if wins   else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    gross_w  = sum(wins);  gross_l = abs(sum(losses))
    pf       = gross_w / gross_l if gross_l > 0 else float("inf")
    wr       = len(wins) / n if n > 0 else 0.0
    expect   = avg_win * wr + avg_loss * (1 - wr)
    max_tp   = max(wins,   default=0.0)
    max_loss = min(losses, default=0.0)
    streak   = 0
    if trades:
        sign = 1 if trades[-1]["pnl_allin"] > 0 else -1
        for t in reversed(trades):
            if (t["pnl_allin"] > 0) == (sign > 0): streak += 1
            else: break
        streak *= sign
    return dict(avg_win=avg_win, avg_loss=avg_loss, pf=pf, wr=wr, expect=expect,
                max_tp=max_tp, max_loss=max_loss, streak=streak,
                gross_w=gross_w, gross_l=gross_l)


# ── GUI theme ─────────────────────────────────────────────────────────────────
_G = "#4caf50"; _R = "#f44336"; _Y = "#ff9800"; _C = "#26c6da"; _M = "#ba68c8"
_W = "#e0e0e0"; _D = "#616161"

# Paper/Live token panels laid out kanban-style: a wrapping grid of per-symbol cards
# instead of one full-width card per row (added 2026-08-23).
KANBAN_COLS = 2

def _set_kanban_card_accent(box, color):
    """Give a leg's QGroupBox a rounded-card look with a colored top accent bar
    (green=long, red=short, amber=mixed-side across a leg's two bots, grey=flat/
    neutral) instead of the app-wide plain QGroupBox border. Applied per-instance via
    setStyleSheet, which overrides the app-wide QGroupBox rule for this one widget
    without touching every other QGroupBox in the app."""
    box.setStyleSheet(f"""
        QGroupBox{{
            border: 1px solid #2a2a2a;
            border-top: 3px solid {color};
            border-radius: 8px;
            margin-top: 8px;
            padding-top: 4px;
            background: #1a1a1a;
            color: {_W};
            font-weight: bold;
        }}
        QGroupBox::title{{
            subcontrol-origin: margin;
            left: 10px;
            padding: 0 6px;
        }}
    """)


def _apply_dark_theme(app):
    app.setStyle("Fusion")
    p = QPalette()
    p.setColor(QPalette.ColorRole.Window,          QColor(18, 18, 18))
    p.setColor(QPalette.ColorRole.WindowText,      QColor(224, 224, 224))
    p.setColor(QPalette.ColorRole.Base,            QColor(30, 30, 30))
    p.setColor(QPalette.ColorRole.AlternateBase,   QColor(40, 40, 40))
    p.setColor(QPalette.ColorRole.Text,            QColor(224, 224, 224))
    p.setColor(QPalette.ColorRole.Button,          QColor(37, 37, 37))
    p.setColor(QPalette.ColorRole.ButtonText,      QColor(224, 224, 224))
    p.setColor(QPalette.ColorRole.Highlight,       QColor(38, 198, 218, 80))
    p.setColor(QPalette.ColorRole.HighlightedText, QColor(224, 224, 224))
    p.setColor(QPalette.ColorRole.Link,            QColor(38, 198, 218))
    p.setColor(QPalette.ColorRole.BrightText,      QColor(255, 255, 255))
    app.setPalette(p)
    app.setStyleSheet("""
        QGroupBox{border:1px solid #2a2a2a;border-radius:4px;margin-top:8px;padding-top:4px;color:#616161;font-weight:bold;}
        QGroupBox::title{subcontrol-origin:margin;left:8px;padding:0 4px;}
        QTabWidget::pane{border:1px solid #2a2a2a;background:#1a1a1a;}
        QTabBar::tab{background:#252525;color:#616161;padding:8px 24px;border:1px solid #2a2a2a;border-bottom:none;}
        QTabBar::tab:selected{background:#1a1a1a;color:#26c6da;border-bottom:2px solid #26c6da;}
        QTabBar::tab:hover{color:#e0e0e0;}
        QTableWidget{background:#1e1e1e;gridline-color:#252525;border:none;alternate-background-color:#232323;}
        QTableWidget::item{padding:2px 6px;}
        QHeaderView::section{background:#252525;color:#616161;border:none;border-bottom:1px solid #2a2a2a;padding:4px 6px;}
        QScrollArea{border:none;}
        QScrollBar:vertical{background:#1e1e1e;width:8px;}
        QScrollBar::handle:vertical{background:#3a3a3a;border-radius:4px;}
        QTextEdit{background:#1a1a1a;border:none;color:#9e9e9e;}
        QFrame#card{background:#1e1e1e;border:1px solid #2a2a2a;border-radius:6px;}
        QLabel#h1{font-size:20px;font-weight:bold;color:#26c6da;}
        QLabel#h2{font-size:13px;font-weight:bold;color:#e0e0e0;}
        QPushButton#exit{background:#b71c1c;color:white;font-size:14px;font-weight:bold;padding:12px 32px;border:none;border-radius:6px;}
        QPushButton#exit:hover{background:#c62828;}
        QPushButton#exit:pressed{background:#7f0000;}
    """)


def _titem(text, color=None, right=False):
    it = QTableWidgetItem(str(text))
    if color:
        it.setForeground(QBrush(QColor(color)))
    it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
    align = Qt.AlignmentFlag.AlignRight if right else Qt.AlignmentFlag.AlignLeft
    it.setTextAlignment(int(align | Qt.AlignmentFlag.AlignVCenter))
    return it


def _pc(v): return _G if v >= 0 else _R
def _dc(v): return _R if v < -5 else (_Y if v < 0 else _G)
def _wc(v): return _G if v >= 0.5 else _R

def _usd(v, sign=False):
    """Format a dollar PnL amount with a leading $ placed after the sign character
    (e.g. "+$12.34"/"-$3.00"), not before it — added after a screenshot showed the
    Backtest tab's cumL/MaxTP/MaxLoss columns with no currency marker at all, easily
    misread as counts or percentages next to the adjacent %-suffixed columns."""
    s = f"{v:+.2f}" if sign else f"{v:.2f}"
    return f"{s[0]}${s[1:]}" if s[0] in "+-" else f"${s}"

def _usd_mag(v):
    """Format a non-negative loss MAGNITUDE (bt.py's cum_loss/max_loss, both always
    >=0 — the sum/max of loss sizes, not a signed PnL) with a leading '-' only when v
    is actually nonzero. A caller that always prepends a literal '-' to _usd(v)
    produces '-$0.00' for a zero-loss candidate; this doesn't."""
    return f"-{_usd(v)}" if v > 0 else _usd(v)


class _StatusBar(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(36)
        ly = QHBoxLayout(self)
        ly.setContentsMargins(12, 0, 12, 0)
        self._lbl = QLabel("—")
        self._lbl.setFont(QFont("Consolas", 12, QFont.Weight.Bold))
        self._lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ly.addWidget(self._lbl)
        self._flat()

    def _apply(self, text, fg, bg, border):
        self.setStyleSheet(f"background:{bg};border:1px solid {border};border-radius:4px;")
        self._lbl.setStyleSheet(f"color:{fg};")
        self._lbl.setText(text)

    def _flat(self):    self._apply("◌  FLAT — awaiting signal", _D, "#181818", "#2a2a2a")
    def _cd(self, t):   self._apply(t, _Y, "#1e1500", "#5d3c00")
    def _long(self, t): self._apply(t, _G, "#0d1f0d", "#1b5e20")
    def _short(self, t):self._apply(t, _R, "#1f0d0d", "#7f0000")

    def refresh(self, bot, price):
        pos = bot.position
        if pos:
            lng = pos["side"] == "long"
            mv  = ((price - pos["entry"]) / pos["entry"] * 100 if lng
                   else (pos["entry"] - price) / pos["entry"] * 100)
            t   = f"  {'▲  LONG' if lng else '▼  SHORT'}    {price:.5f}    {mv:+.2f}%    {pos.get('bars',0)}b"
            self._long(t) if lng else self._short(t)
        else:
            cd = COOLDOWN_S - (time.time() - bot._last_close_ts)
            if cd > 0:
                m, s = int(cd // 60), int(cd % 60)
                self._cd(f"⏳  COOLDOWN — {m}m {s}s remaining")
            else:
                self._flat()


class _SG(QFrame):
    """3-column grid of (key, value) pairs."""
    def __init__(self, rows, parent=None):
        super().__init__(parent)
        g = QGridLayout(self)
        g.setSpacing(4)
        g.setContentsMargins(6, 4, 6, 4)
        self._v = {}
        for r, keys in enumerate(rows):
            for c, k in enumerate(keys):
                kl = QLabel(k)
                kl.setStyleSheet(f"color:{_D};font-size:11px;")
                kl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                vl = QLabel("—")
                vl.setStyleSheet(f"color:{_W};")
                self._v[k] = vl
                g.addWidget(kl, r, c * 2)
                g.addWidget(vl, r, c * 2 + 1)
                g.setColumnStretch(c * 2 + 1, 1)

    def s(self, k, text, color=None):
        if lbl := self._v.get(k):
            lbl.setText(text)
            if color:
                lbl.setStyleSheet(f"color:{color};")


class _TradesTable(QTableWidget):
    def __init__(self, live=False, parent=None):
        cols = (["Time", "Strat", "S", "Entry", "Exit", "Qty", "PnL", "Why"] if live
                else ["Time", "S", "Entry", "Exit", "ΔP%", "PnL", "Why", "Bars"])
        super().__init__(5, len(cols), parent)
        self.setHorizontalHeaderLabels(cols)
        self.verticalHeader().setVisible(False)
        self.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.horizontalHeader().setSectionResizeMode(3 if live else 2, QHeaderView.ResizeMode.Stretch)
        self.horizontalHeader().setSectionResizeMode(4 if live else 3, QHeaderView.ResizeMode.Stretch)
        self.setAlternatingRowColors(True)
        self.setShowGrid(False)
        self.setMaximumHeight(5 * 22 + 34)
        self._live = live
        self._clear()

    def _clear(self):
        for r in range(5):
            for c in range(self.columnCount()):
                self.setItem(r, c, _titem(""))

    def _s(self, r, c, text, color=None, right=True):
        self.setItem(r, c, _titem(text, color, right))

    def fill_paper(self, trades):
        self._clear()
        for i, tr in enumerate(list(reversed(trades))[:5]):
            lng = tr.get("side") == "long"
            dp  = ((tr["exit"] - tr["entry"]) / tr["entry"] * 100 if lng
                   else (tr["entry"] - tr["exit"]) / tr["entry"] * 100)
            p   = tr["pnl_allin"]
            self._s(i, 0, tr.get("ts", "")[-8:], right=False)
            self._s(i, 1, "▲" if lng else "▼", _G if lng else _R, False)
            self._s(i, 2, f"{tr['entry']:.5f}")
            self._s(i, 3, f"{tr['exit']:.5f}")
            self._s(i, 4, f"{dp:+.2f}%", _pc(dp))
            self._s(i, 5, f"{p:+.2f}", _pc(p))
            self._s(i, 6, tr.get("reason", ""), _C, False)
            self._s(i, 7, str(tr.get("bars", "—")))

    def fill_live(self, trades):
        self._clear()
        for i, tr in enumerate(list(reversed(trades))[:5]):
            lng = tr.get("side") == "long"
            p   = tr["pnl"]
            rs  = tr.get("reason", "")
            bot_id = tr.get("bot_id") or ""
            strat  = "PARTIAL" if bot_id.startswith("partial") else \
                     "STOP" if bot_id.startswith("stop") else "—"
            qty    = tr.get("qty")
            self._s(i, 0, tr.get("ts", "")[-8:], right=False)
            self._s(i, 1, strat, _C, False)
            self._s(i, 2, "▲" if lng else "▼", _G if lng else _R, False)
            self._s(i, 3, f"{tr['entry']:.5f}")
            self._s(i, 4, f"{tr['exit']:.5f}")
            self._s(i, 5, f"{qty:g}" if qty else "—")
            self._s(i, 6, f"{p:+.2f}", _pc(p))
            self._s(i, 7, rs, _R if rs == "MANUAL" else _C, False)


class BotPanel(QWidget):
    def __init__(self, title, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setSpacing(6)
        root.setContentsMargins(6, 6, 6, 6)

        h_row = QHBoxLayout()
        h = QLabel(title)
        h.setObjectName("h2")
        h_row.addWidget(h, 1)
        self._close_btn = QPushButton("Close Position")
        self._close_btn.setEnabled(False)
        self._close_btn.clicked.connect(self._on_close_clicked)
        h_row.addWidget(self._close_btn)
        root.addLayout(h_row)

        # Set by refresh() each tick — used only by _on_close_clicked, which fires on
        # the Qt UI thread and must never touch bot.position without trader's own lock
        # (the WS callback thread reads/writes it inside tick() unlocked).
        self._bot        = None
        self._combo      = None
        self._last_price = 0.0

        self._sb = _StatusBar()
        root.addWidget(self._sb)

        pg = QGroupBox("Position")
        pl = QGridLayout(pg)
        pl.setSpacing(4)
        pl.setContentsMargins(6, 4, 6, 4)
        self._pos = {}
        for row_i, pairs in enumerate([
            [("Entry", "—"), ("Qty", "—"), ("Notional", "—")],
            [("SL", "—"),    ("SL dst", "—"), ("MTM", "—")],
            [("NextLvl", "—"), ("NextLvl dst", "—"), ("Age", "—")],
            [("Grid", "—"),  ("Lev", "—"), ("", "")],
        ]):
            for c, (k, v) in enumerate(pairs):
                if not k:
                    continue
                kl = QLabel(k)
                kl.setStyleSheet(f"color:{_D};font-size:11px;")
                kl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                vl = QLabel(v)
                self._pos[k] = vl
                pl.addWidget(kl, row_i, c * 2)
                pl.addWidget(vl, row_i, c * 2 + 1)
                pl.setColumnStretch(c * 2 + 1, 1)
        root.addWidget(pg)

        sg = QGroupBox("Statistics")
        sl = QVBoxLayout(sg)
        sl.setContentsMargins(0, 4, 0, 4)
        self._sg = _SG([
            ["Equity", "DD", "Peak"],
            ["Trades", "WR", "PF"],
            ["cumPnL", "cumLoss", "Expect"],
            ["AvgWin", "AvgLoss", "Streak"],
            ["MaxTP",  "MaxLoss", "Bars"],
        ])
        sl.addWidget(self._sg)
        root.addWidget(sg)

        tg = QGroupBox("Recent Trades")
        tl = QVBoxLayout(tg)
        tl.setContentsMargins(4, 4, 4, 4)
        self._tt = _TradesTable()
        tl.addWidget(self._tt)
        root.addWidget(tg)

        lg = QGroupBox("Log")
        ll = QVBoxLayout(lg)
        ll.setContentsMargins(4, 4, 4, 4)
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumHeight(80)
        self._log.setFont(QFont("Consolas", 9))
        ll.addWidget(self._log)
        root.addWidget(lg)
        root.addStretch()

    def _p(self, k, text, color=None):
        if lbl := self._pos.get(k):
            lbl.setText(text)
            if color:
                lbl.setStyleSheet(f"color:{color};")

    def _on_close_clicked(self):
        bot, trader, price = self._bot, self._combo, self._last_price
        if bot is None or trader is None or not price:
            return
        # Snapshot under the lock so the confirmation dialog (which blocks this thread
        # but not the WS callback thread) can't show stale info — bot.position may have
        # already closed itself (SL/TP) by the time the user clicks Yes.
        with trader._lock:
            pos_now = dict(bot.position) if bot.position else None
        if pos_now is None:
            QMessageBox.information(self, "Close Position",
                "This position already closed on its own.")
            return
        reply = QMessageBox.question(
            self, "Close Position",
            f"Manually close this {bot._LOG_TAG} {pos_now['side'].upper()} position "
            f"now at ~{price:.5f}?\n\nThis closes the paper simulation immediately. If "
            f"this bot's live mirror is still open, it closes that too.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        with trader._lock:
            if bot.position:
                bot._close(price, "MANUAL")
            trader._maybe_release_slot()

    def refresh(self, bot, price, iv, trader=None):
        self._sb.refresh(bot, price)
        pos = bot.position
        self._bot        = bot
        self._combo      = trader
        self._last_price = price
        self._close_btn.setEnabled(pos is not None)
        self._p("Lev", f"{bot.lev:g}x")
        if pos:
            levels = len(pos["grid_px"]); filled = pos["grid_filled"]
            ntl   = pos["qty"] * price
            mtm   = bot.mtm(price)
            sl_d  = abs(price - pos["sl"]) / price * 100 if price else 0.0
            self._p("Entry",    f"{pos['entry']:.5f}")
            self._p("Qty",      f"{pos['qty']:.3f}")
            self._p("Notional", f"${ntl:,.0f}")
            self._p("SL",       f"{pos['sl']:.5f}",    _R)
            self._p("SL dst",   f"-{sl_d:.2f}%",       _R)
            self._p("MTM",      f"{mtm:+.2f}",         _pc(mtm))
            if filled < levels:
                next_px = pos["grid_px"][filled]
                next_d  = abs(next_px - price) / price * 100 if price else 0.0
                self._p("NextLvl",     f"{next_px:.5f}", _G)
                self._p("NextLvl dst", f"+{next_d:.2f}%", _G)
            else:
                self._p("NextLvl", "—", _D); self._p("NextLvl dst", "—", _D)
            self._p("Age",  f"{pos.get('bars',0)}b / {pos.get('bars',0)*iv}m")
            self._p("Grid", f"{filled}/{levels}", _C if filled > 0 else _Y)
        else:
            for k in ["Entry","Qty","Notional","SL","SL dst","MTM","NextLvl","NextLvl dst","Age","Grid"]:
                self._p(k, "—", _D)

        trades = bot.trades
        st     = _bot_stats(trades)
        dd     = (bot.equity - bot.peak_equity) / bot.peak_equity * 100 if bot.peak_equity > 0 else 0.0
        pf_s   = f"{st['pf']:.2f}" if st["pf"] < 99 else "∞"
        sk     = st["streak"]
        sk_s   = f"{sk}W" if sk > 0 else (f"{abs(sk)}L" if sk < 0 else "—")

        self._sg.s("Equity",  f"${bot.equity:,.2f}",     _W)
        self._sg.s("DD",      f"{dd:.2f}%",               _dc(dd))
        self._sg.s("Peak",    f"${bot.peak_equity:,.2f}", _D)
        self._sg.s("Trades",  str(len(trades)),            _W)
        self._sg.s("WR",      f"{st['wr']*100:.1f}%",    _wc(st["wr"]))
        self._sg.s("PF",      pf_s,                       _G if st["pf"] >= 1 else _R)
        self._sg.s("cumPnL",  _usd(bot.cum_pnl, sign=True), _pc(bot.cum_pnl))
        self._sg.s("cumLoss", _usd(bot.cum_loss),            _R)
        self._sg.s("Expect",  _usd(st['expect'], sign=True), _pc(st["expect"]))
        self._sg.s("AvgWin",  _usd(st['avg_win'], sign=True), _G)
        self._sg.s("AvgLoss", _usd(st['avg_loss']),          _R)
        self._sg.s("Streak",  sk_s, _G if sk > 0 else (_R if sk < 0 else _D))
        self._sg.s("MaxTP",   _usd(st['max_tp'], sign=True), _G)
        self._sg.s("MaxLoss", _usd(st['max_loss']),          _R)
        self._sg.s("Bars",    str(bot._bar_count),         _D)

        self._tt.fill_paper(trades)
        self._log.setPlainText("\n".join(list(bot.log_msgs)[:8]))


class BacktestRunner:
    """Drives eth_trader_bt's optimize_symbol_interval() from a background thread,
    in-process, instead of the user running eth_trader_bt.exe separately first.

    With repeat=True it mirrors eth_trader_bt.py's own main() loop: a full pass over
    every symbol/interval, then wait interval_s (default bt.LOOP_INTERVAL, 60min as of
    2026-09-01 — measured from when the previous cycle's own work finished, not from
    when it started) and repeat, until stopped."""
    def __init__(self, repeat=False, interval_s=None):
        self.status        = {}     # "{symbol}_{interval}" -> "queued"/"running .."/result dict
        self.running        = False   # background thread alive (covers the wait phase too)
        self.final_status   = ""
        self.phase          = "idle"  # idle/connecting/running/waiting/stopped/error
        self.next_run_ts    = None    # epoch seconds, set while phase == "waiting"
        self.cycle          = 0
        self.repeat         = repeat
        self.interval_s     = interval_s if interval_s is not None else bt.LOOP_INTERVAL
        self._stop_ev       = threading.Event()
        self._log_q         = deque(maxlen=200)
        self._pool          = None    # set once _run() creates it; used by kill_now()

    def _push_log(self, msg):
        self._log_q.append(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}")

    def drain_log(self):
        out = []
        while self._log_q:
            out.append(self._log_q.popleft())
        return out

    def start(self):
        self.running = True
        threading.Thread(target=self._run, daemon=True, name="backtest").start()

    def stop(self):
        """Graceful stop for the Stop button — the run loop finishes the (symbol,
        interval) pair currently in flight before actually stopping. The GUI stays open
        after this, so letting the in-progress sweep finish cleanly is fine here."""
        self._stop_ev.set()

    def run_now(self):
        """Skip the remaining wait and start the next cycle immediately (added
        2026-08-26, explicit user ask: must be able to force a fresh backtest run at any
        time by clicking the button, not just at startup or after the full
        bt.LOOP_INTERVAL wait, 60min as of 2026-09-01). No-op unless phase is "waiting" — if a cycle is
        already connecting/running there's nothing to skip, and if the runner already
        stopped/errored this can't restart it (BacktestTab._on_start handles that case
        by spinning up a fresh runner instead)."""
        if self.phase == "waiting":
            self.next_run_ts = time.time()

    def kill_now(self):
        """Hard stop for app exit (MainWindow.closeEvent), not the Stop button. A single
        (symbol, interval) sweep can take several minutes, and stop()'s cooperative check
        only happens between pairs — the app can't wait that long to close, so this
        terminates any live ProcessPoolExecutor worker processes immediately instead of
        waiting for them to notice a flag. bt._win_kill_on_close()'s Windows Job Object is
        a second, independent safety net for cases this misses (e.g. a crash instead of a
        normal close) — this is the deterministic primary path, not a backup."""
        self._stop_ev.set()
        pool = self._pool
        if pool is None:
            return
        try:
            for proc in list(getattr(pool, "_processes", {}).values()):
                try:
                    if proc.is_alive():
                        proc.terminate()
                except Exception:
                    pass
        except Exception:
            pass
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass

    def _run(self):
        try:
            self.phase = "connecting"
            sess, err = _bt_make_session()
            if err:
                self.final_status = err
                self.phase = "error"
                self._push_log(f"ERROR: {err}")
                return
            self._push_log(f"Session OK (mainnet, {'live' if has_keys(live=True) else 'paper'} key)")
            bt.db_init()

            # Compile-and-cache the JIT hot path here, single-threaded, BEFORE any
            # worker process spawns — otherwise every worker's first sweep call races to
            # write the same on-disk numba cache file at once, which can corrupt it and
            # crash whichever worker loses the race (see warm_up_jit's own docstring;
            # this is what a first-cold-start BrokenProcessPool on this machine traced
            # back to).
            bt.warm_up_jit()

            # One process pool reused across every (symbol, interval) pair and every
            # cycle for the life of this runner — added 2026-08-22. Previously
            # optimize_symbol_interval() spawned/tore down its own pool 24 times per
            # cycle (one per pair); on Windows that's 24 rounds of fresh-interpreter
            # worker startup instead of one.
            pool = ProcessPoolExecutor(max_workers=bt.N_WORKERS)
            self._pool = pool
            try:
                while True:
                    self.cycle += 1
                    self.phase = "running"
                    cycle_start = time.time()
                    # Every symbol is tried at every configured interval (30m only as of
                    # 2026-09-01, bt.CRYPTO_INTERVALS) — added 2026-08-22, replacing the old
                    # one-fixed-interval-per-symbol scheme. _load_all_worthy_crypto() picks
                    # whichever interval scores best per symbol at leg-selection time; this
                    # loop's job is just to keep every interval's result file current.
                    for sym in bt.SYMBOLS:
                        for iv in bt.CRYPTO_INTERVALS:
                            for src in ("searched", "pine"):
                                self.status[f"{sym}_{iv}_{src}"] = "queued"
                    self._push_log(f"Cycle {self.cycle} starting")
                    _log.info(f"Backtest cycle {self.cycle} starting "
                              f"({len(bt.SYMBOLS)} symbols x {len(bt.CRYPTO_INTERVALS)} intervals "
                              f"x 2 entry sources)")

                    # Per-symbol retry across ALL configured intervals together (moved
                    # here from a per-interval retry loop inside optimize_symbol_interval
                    # itself, 2026-09-01, explicit user ask: "sweep through all 5m 15m
                    # backtests. then assess if non 60wr or above and then run all
                    # again") — a symbol only needs ONE of its intervals to clear the
                    # target (bt._clears_target — win-rate-based gating replaced the same
                    # day, see that function's docstring) to become tradeable via
                    # _load_all_worthy_crypto's per-symbol best-interval pick, so forcing
                    # EVERY interval to individually clear the target (the old
                    # per-interval version of this retry) was wasted work once any one
                    # of them already qualified. No time limit, same as the removed
                    # per-interval version's final form — the only thing that stops a
                    # symbol's retry loop short of qualifying is a full pass across
                    # every interval finding zero genuinely new param combos anywhere
                    # (every space is exhausted), reported back by
                    # optimize_symbol_interval's return value (see its `new_combo_count`
                    # docstring).
                    for sym in bt.SYMBOLS:
                        pass_num = 0
                        while True:
                            pass_num += 1
                            total_new_combos = 0
                            for iv in bt.CRYPTO_INTERVALS:
                                if self._stop_ev.is_set():
                                    self.final_status = "Stopped"
                                    self.phase = "stopped"
                                    self._push_log("Stopped by user")
                                    return
                                self._push_log(f"Running {sym} {iv}m ...")
                                try:
                                    protected = _protected_entry_source(sym, iv)
                                    new_combos = bt.optimize_symbol_interval(
                                        sess, sym, iv, self.status, executor=pool,
                                        protected_source=protected)
                                    total_new_combos += new_combos or 0
                                except BrokenProcessPool as e:
                                    # A dead worker process (OOM, or a native-level crash
                                    # reachable through the njit hot path) poisons this pool
                                    # object permanently — every subsequent submit()/result()
                                    # against it raises the same error. Since this pool is
                                    # reused across every (symbol, interval) pair for the
                                    # whole cycle (not recreated per pair), one crash used to
                                    # silently degrade every remaining pair to "ERROR" for
                                    # the rest of the cycle. Recreate the pool and continue;
                                    # this pair is skipped this cycle (its DB-cached top
                                    # params/tried-set are untouched, so nothing is lost, it
                                    # just retries next cycle) rather than cascading.
                                    _log.error(f"Process pool broken during {sym} {iv}m "
                                              f"({e}) — recreating pool, skipping this pair "
                                              f"for this cycle")
                                    self._push_log(f"{sym} {iv}m: worker crashed, pool recreated")
                                    for src in ("searched", "pine"):
                                        self.status[f"{sym}_{iv}_{src}"] = f"ERROR: worker crashed ({e})"
                                    try: pool.shutdown(wait=False, cancel_futures=True)
                                    except Exception: pass
                                    pool = ProcessPoolExecutor(max_workers=bt.N_WORKERS)
                                    self._pool = pool
                                for src in ("searched", "pine"):
                                    val = self.status.get(f"{sym}_{iv}_{src}")
                                    if isinstance(val, dict):
                                        self._push_log(f"Done {sym} {iv}m [{src}]: sharpe={val['sharpe']:.2f} "
                                                       f"ret={val['total_ret_pct']:.1f}% trades={val['trades']}")
                                    else:
                                        self._push_log(f"{sym} {iv}m [{src}]: {val}")

                            qualifies = any(
                                bt._clears_target(self.status.get(f"{sym}_{iv}_{src}"))
                                or self.status.get(f"{sym}_{iv}_{src}") == "protected (position open)"
                                for iv in bt.CRYPTO_INTERVALS for src in ("searched", "pine"))
                            if qualifies:
                                break
                            if total_new_combos == 0:
                                _log.info(f"{sym}: param space exhausted across all "
                                          f"{len(bt.CRYPTO_INTERVALS)} interval(s) after "
                                          f"{pass_num} pass(es) without any interval/source "
                                          f"clearing the ret/cum_loss/DD targets")
                                self._push_log(f"{sym}: gave up after {pass_num} pass(es), "
                                               f"nothing new left to try")
                                break
                            self._push_log(f"{sym}: pass {pass_num} found nothing clearing "
                                           f"the targets, sweeping all intervals again...")

                    self.final_status = "Complete"
                    cycle_dur = time.time() - cycle_start
                    self._push_log(f"Cycle {self.cycle} complete")
                    _log.info(f"Backtest cycle {self.cycle} complete in {cycle_dur:.0f}s "
                              f"({cycle_dur/60:.1f}min)")
                    if self.repeat and cycle_dur > self.interval_s:
                        _log.warning(f"Backtest cycle {self.cycle} took {cycle_dur:.0f}s, "
                                     f"longer than the {self.interval_s:.0f}s repeat interval — "
                                     f"cycles are running back-to-back with no gap, results may "
                                     f"be staler than the '2h' cadence implies")

                    self._push_log("Checking for trades paper may have missed (last 2 days)...")
                    try:
                        _report_missed_trades(sess, _load_worthy_plus_open_positions())
                    except Exception as e:
                        self._push_log(f"Missed-trade check error: {e}")
                        _log.warning(f"Missed-trade check error: {e}")

                    if not self.repeat:
                        return

                    self.phase = "waiting"
                    self.next_run_ts = time.time() + self.interval_s
                    while time.time() < self.next_run_ts:
                        if self._stop_ev.is_set():
                            self.final_status = "Stopped"
                            self.phase = "stopped"
                            self._push_log("Stopped by user")
                            return
                        time.sleep(1)
            finally:
                pool.shutdown(wait=False, cancel_futures=True)
        except Exception as e:
            self.final_status = f"Error: {e}"
            self.phase = "error"
            self._push_log(f"ERROR: {e}")
            _log.exception(f"Backtest tab run failed: {e}")
        finally:
            self.running = False


_BT_COLS = ["Symbol", "Interval", "Status", "Entry", "Sharpe", "Ret%", "DD%", "CAGR%",
            "Trades", "WR%", "PF", "AvgHold", "cumP", "cumL", "MaxTP", "MaxLoss"]


class BacktestTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setSpacing(6)
        root.setContentsMargins(8, 8, 8, 8)

        ctrl = QHBoxLayout()
        self._start_btn = QPushButton("Run Backtest")
        self._stop_btn  = QPushButton("Stop")
        self._stop_btn.setEnabled(False)
        repeat_h = bt.LOOP_INTERVAL / 3600
        self._repeat_chk = QCheckBox(f"Auto-repeat every {repeat_h:g}h")
        self._status_lbl = QLabel("Idle")
        self._status_lbl.setStyleSheet(f"color:{_D};")
        ctrl.addWidget(self._start_btn)
        ctrl.addWidget(self._stop_btn)
        ctrl.addWidget(self._repeat_chk)
        ctrl.addWidget(self._status_lbl, 1)
        root.addLayout(ctrl)

        self._table = QTableWidget(0, len(_BT_COLS))
        self._table.setHorizontalHeaderLabels(_BT_COLS)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self._table.setAlternatingRowColors(True)
        self._table.setShowGrid(False)
        root.addWidget(self._table, 1)

        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumHeight(160)
        root.addWidget(self._log)

        # One row per (symbol, interval, entry_source) — two rows for a single symbol
        # tested at one interval (added 2026-08-28, "pine" entry source — see
        # bt.PINE_GC_SQRT2's docstring). Each row is its own fully independent
        # backtest candidate (own IS sweep slice, own OOS retest, own saved result
        # file — see optimize_symbol_interval) — not a display split of one result.
        self._rows = {}
        for sym in bt.SYMBOLS:
            for iv in bt.CRYPTO_INTERVALS:
                for src in ("searched", "pine"):
                    r = self._table.rowCount()
                    self._table.insertRow(r)
                    self._table.setItem(r, 0, _titem(sym))
                    self._table.setItem(r, 1, _titem(f"{iv}m"))
                    self._table.setItem(r, 3, _titem(src.upper(), _C))
                    for c in range(2, len(_BT_COLS)):
                        if c != 3:
                            self._table.setItem(r, c, _titem(""))
                    self._rows[(sym, iv, src)] = r

        self._start_btn.clicked.connect(self._on_start)
        self._stop_btn.clicked.connect(self._on_stop)
        self._runner = None

    def auto_start(self):
        """Called once from MainWindow.__init__ — the backtest sweep now starts
        automatically on launch with auto-repeat on (added 2026-08-22), the one
        deliberate exception to "nothing auto-starts": Paper/Live still require an
        explicit Start Paper click, only the backtest itself runs unattended."""
        self._repeat_chk.setChecked(True)
        self._on_start()

    def _on_start(self):
        if self._runner is not None and self._runner.running:
            # A runner is already alive — force an immediate cycle instead of spawning a
            # second overlapping one, which would double up exchange/API calls and DB
            # writes (added 2026-08-26: the button used to just be disabled the whole
            # time a repeating runner was active, so there was no way to force a fresh
            # run without Stop-then-Start, which waits for the in-flight pair to finish
            # first). No-op if a cycle is already connecting/running — nothing to skip.
            self._runner.run_now()
            return
        self._log.clear()
        for (sym, iv, src), row in self._rows.items():
            for c in range(2, len(_BT_COLS)):
                self._table.setItem(row, c, _titem(""))
            self._table.setItem(row, 2, _titem("queued", _D))
            self._table.setItem(row, 3, _titem(src.upper(), _C))
        self._runner = BacktestRunner(repeat=self._repeat_chk.isChecked())
        self._runner.start()
        self._stop_btn.setEnabled(True)
        self._repeat_chk.setEnabled(False)
        self._status_lbl.setText("Starting ...")
        self._status_lbl.setStyleSheet(f"color:{_Y};")

    def _on_stop(self):
        if self._runner is not None:
            self._runner.stop()
            self._stop_btn.setEnabled(False)
            self._status_lbl.setText("Stopping ...")

    def refresh(self):
        r = self._runner
        if r is None:
            return
        for msg in r.drain_log():
            self._log.append(msg)

        # Highlight whichever (sym, iv, src) row currently WINS live selection —
        # added 2026-09-04, explicit user ask ("it should highlight the strat in BT
        # that wins"). Calls the exact same _load_all_worthy_crypto() the live
        # TradingEngine uses to pick a leg, so the highlight always matches what would
        # actually trade right now — reading the real on-disk result files, not a
        # re-derived approximation off this tab's own (possibly mid-sweep) status
        # dict. Recomputed every refresh tick since it's cheap (a handful of small
        # JSON files) and must never lag behind a newly-written result.
        winning_keys = {(w[0], w[1], w[7]) for w in _load_all_worthy_crypto()}
        _WIN_BG = QBrush(QColor(46, 90, 46))
        _NO_BG = QBrush()

        for (sym, iv, src), row in self._rows.items():
            val = r.status.get(f"{sym}_{iv}_{src}")
            if val is None:
                continue
            is_winner = (sym, iv, src) in winning_keys
            if isinstance(val, dict):
                status_txt = "done ★ LIVE" if is_winner else "done"
                self._table.setItem(row, 2,  _titem(status_txt, _G))
                self._table.setItem(row, 4,  _titem(f"{val['sharpe']:.2f}", _pc(val['sharpe'])))
                self._table.setItem(row, 5,  _titem(f"{val['total_ret_pct']:.1f}%", _pc(val['total_ret_pct'])))
                self._table.setItem(row, 6,  _titem(f"{val['max_dd_pct']:.1f}%", _dc(val['max_dd_pct'])))
                self._table.setItem(row, 7,  _titem(f"{val.get('cagr_pct', 0):.1f}%", _C))
                self._table.setItem(row, 8,  _titem(str(val.get('trades', ''))))
                self._table.setItem(row, 9,  _titem(f"{val.get('win_rate', 0)*100:.0f}%", _wc(val.get('win_rate', 0))))
                self._table.setItem(row, 10, _titem(f"{val.get('profit_factor', 0):.2f}"))
                self._table.setItem(row, 11, _titem(f"{val.get('avg_hold', 0):.1f}b"))
                self._table.setItem(row, 12, _titem(_usd(val.get('cum_profit', 0)), _G))
                self._table.setItem(row, 13, _titem(_usd_mag(val.get('cum_loss', 0)), _R))
                self._table.setItem(row, 14, _titem(_usd(val.get('max_tp', 0)), _G))
                self._table.setItem(row, 15, _titem(_usd_mag(val.get('max_loss', 0)), _R))
            else:
                msg = str(val)
                color = (_R if "ERROR" in msg else
                         _D if msg == "queued" else
                         _C if msg == "protected (position open)" else _Y)
                self._table.setItem(row, 2, _titem(msg, color))
                # Clear the numeric columns (added 2026-09-01 — real bug found from a
                # user screenshot: a symbol/source that had a real result on some
                # earlier cycle, then failed to produce one on a LATER cycle — e.g.
                # "no OOS winners" — kept showing that earlier cycle's Sharpe/Ret%/WR%/
                # etc. indefinitely, because this branch used to only ever touch column
                # 2 (Status), never resetting 4-15. Blanking `_on_start()` runs only
                # once per BacktestRunner lifetime (repeat=True keeps the SAME runner
                # looping cycle after cycle without ever recreating it), so a stale
                # numeric row could otherwise survive across arbitrarily many
                # auto-repeat cycles once a source stopped producing a fresh result —
                # cosmetic only (real selection/trading always re-checks
                # RESULT_MAX_AGE_S against the saved file's own timestamp, never the
                # GUI's displayed numbers), but actively misleading to look at.
                for c in range(4, len(_BT_COLS)):
                    self._table.setItem(row, c, _titem(""))

            # Cells in columns 0/1/3 (Symbol/Interval/Entry) are created once in
            # __init__ and never replaced here, so their background survives across
            # refreshes on its own — but every OTHER cell in this row was just
            # recreated above via _titem() (a fresh QTableWidgetItem with no
            # background), so the highlight must be (re)applied to the WHOLE row every
            # tick regardless of whether is_winner just changed, or a freshly-replaced
            # cell in a still-winning row would flash back to no background until the
            # next winning_keys change.
            bg = _WIN_BG if is_winner else _NO_BG
            for c in range(len(_BT_COLS)):
                item = self._table.item(row, c)
                if item is not None:
                    item.setBackground(bg)

        if not r.running:
            self._stop_btn.setEnabled(False)
            self._repeat_chk.setEnabled(True)
            fs = r.final_status
            self._status_lbl.setText(fs)
            self._status_lbl.setStyleSheet(
                f"color:{_G if fs == 'Complete' else (_Y if fs == 'Stopped' else _R)};")
        elif r.phase == "waiting" and r.next_run_ts is not None:
            remain = max(0, int(r.next_run_ts - time.time()))
            hh, mm = remain // 3600, (remain % 3600) // 60
            self._status_lbl.setText(f"Cycle {r.cycle} done — next run in {hh}h {mm:02d}m")
            self._status_lbl.setStyleSheet(f"color:{_C};")
        elif r.phase == "connecting":
            self._status_lbl.setText("Connecting ...")
            self._status_lbl.setStyleSheet(f"color:{_Y};")
        else:
            self._status_lbl.setText(f"Running cycle {r.cycle} ...")
            self._status_lbl.setStyleSheet(f"color:{_C};")


class PaperTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setSpacing(6)
        root.setContentsMargins(8, 8, 8, 8)

        # Any number of leg cards now (one per qualifying crypto symbol, no cap) —
        # laid out kanban-style as a wrapping grid (KANBAN_COLS per row) instead of one
        # full-width card per row, so more legs fit on screen without as much vertical
        # scrolling. Horizontal scroll picks up the slack on a narrow window.
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        inner_v = QVBoxLayout(inner)
        inner_v.setContentsMargins(0, 0, 0, 0)
        inner_v.setSpacing(10)
        self._legs_container = QGridLayout()
        self._legs_container.setSpacing(10)
        inner_v.addLayout(self._legs_container)

        self._leg_widgets = {}   # symbol -> widgets dict
        self._no_legs_lbl = QLabel("No symbols currently have any usable backtest "
                                    "result yet — the best-by-PnL candidate across "
                                    "every symbol/interval/source becomes a leg as "
                                    "soon as one exists, no other requirement.")
        self._no_legs_lbl.setStyleSheet(f"color:{_D};font-size:12px;")
        self._no_legs_lbl.setWordWrap(True)
        self._no_legs_lbl.setVisible(False)
        inner_v.addWidget(self._no_legs_lbl)
        inner_v.addStretch()
        sa.setWidget(inner)
        root.addWidget(sa)

    def _build_leg_block(self, title):
        leg_box = QGroupBox(title)
        _set_kanban_card_accent(leg_box, _D)   # neutral until the first refresh() sets it
        lroot = QVBoxLayout(leg_box)
        lroot.setSpacing(6)
        lroot.setContentsMargins(6, 6, 6, 6)

        pg = QGroupBox("Paper Portfolio")
        pl = QVBoxLayout(pg)
        pl.setContentsMargins(0, 4, 0, 4)
        port = _SG([
            ["Symbol",  "Price",   "Balance"],
            ["Comb.Eq", "Peak",    "DD"],
            ["cumPnL",  "cumLoss", "Trades"],
            ["WR",      "PF",      "Expect"],
        ])
        params_lbl = QLabel("")
        params_lbl.setStyleSheet(f"color:{_D};font-size:10px;")
        params_lbl.setWordWrap(True)
        pl.addWidget(port)
        pl.addWidget(params_lbl)
        lroot.addWidget(pg)

        inner_sa = QScrollArea()
        inner_sa.setWidgetResizable(True)
        inner_sa.setFrameShape(QFrame.Shape.NoFrame)
        panel = BotPanel("ATR GRID")
        inner_sa.setWidget(panel)
        inner_sa.setMinimumHeight(420)
        lroot.addWidget(inner_sa)

        return leg_box, {
            "box": leg_box, "port": port, "params_lbl": params_lbl, "bp": panel,
        }

    def _sync_leg_widgets(self, legs):
        current_ids = [leg.trader.symbol for leg in legs]
        if current_ids == list(self._leg_widgets.keys()):
            return
        for w in self._leg_widgets.values():
            self._legs_container.removeWidget(w["box"])
            w["box"].deleteLater()
        self._leg_widgets = {}
        for i, leg in enumerate(legs):
            title = leg.trader.symbol
            box, w = self._build_leg_block(title)
            self._legs_container.addWidget(box, i // KANBAN_COLS, i % KANBAN_COLS)
            self._leg_widgets[leg.trader.symbol] = w
        self._no_legs_lbl.setVisible(not legs)

    def refresh(self, legs, balance):
        self._sync_leg_widgets(legs)
        for leg in legs:
            w = self._leg_widgets[leg.trader.symbol]
            trader = leg.trader
            price = trader.last_price()
            iv    = int(trader.interval)

            p_pos = trader.partial.position
            if p_pos is None:            accent = _D   # flat
            elif p_pos["side"]=="long":  accent = _G
            else:                        accent = _R
            _set_kanban_card_accent(w["box"], accent)
            all_t = trader.partial.trades
            pst   = _bot_stats(all_t)
            dd    = trader.drawdown_pct
            cp    = trader.cum_pnl
            pf_s  = f"{pst['pf']:.2f}" if pst["pf"] < 99 else "∞"

            port = w["port"]
            port.s("Symbol",  f"{trader.symbol}  {trader.interval}m", _W)
            port.s("Price",   f"{price:.5f}",                        _W)
            port.s("Balance", f"${balance:,.2f}",                    _W)
            port.s("Comb.Eq", f"${trader.combined_equity:,.2f}",      _W)
            port.s("Peak",    f"${trader._peak_combined:,.2f}",       _D)
            port.s("DD",      f"{dd:.2f}%",                          _dc(dd))
            port.s("cumPnL",  _usd(cp, sign=True),                   _pc(cp))
            port.s("cumLoss", _usd(trader.cum_loss),                  _R)
            port.s("Trades",  str(len(all_t)),                       _W)
            port.s("WR",      f"{pst['wr']*100:.1f}%",              _wc(pst["wr"]))
            port.s("PF",      pf_s,                                  _G if pst["pf"] >= 1 else _R)
            port.s("Expect",  _usd(pst['expect'], sign=True),        _pc(pst["expect"]))

            pars = trader.partial.params
            # Per-level grid_dist_i/grid_frac_i (added 2026-08-28 — see
            # bt.grid_level_prices' docstring) replaced the old single gridAtrX/
            # gridFrac scalars; show each active level's own value (falls back to the
            # old scalar for a leg still running on pre-2026-08-28 params).
            _glv = int(pars.get('grid_levels', 4))
            _gd = [pars.get(f'grid_dist_{i+1}', pars.get('grid_atr_mult')) for i in range(_glv)]
            _gf = [pars.get(f'grid_frac_{i+1}', pars.get('grid_level_frac')) for i in range(_glv)]
            _gd_s = ",".join(f"{v:.2f}" if v is not None else "?" for v in _gd)
            _gf_s = ",".join(f"{v:.2f}" if v is not None else "?" for v in _gf)
            w["params_lbl"].setText(
                f"entry={trader.entry_source.upper()}  "
                f"kLen={pars.get('k_len')}  kSm={pars.get('k_smooth')}  dSm={pars.get('d_smooth')}  "
                f"ob={pars.get('ob')}  os={pars.get('os')}  chLen={pars.get('chop_len')}  "
                f"chThr={pars.get('chop_thr')}  atrP={pars.get('atr_p')}  stopX={pars.get('stop_mult')}  "
                f"gridLv={_glv}  gridDist=[{_gd_s}]  "
                f"gridFrac=[{_gf_s}]  gcP={pars.get('gc_period')}  gcPl={pars.get('gc_poles')}"
            )
            w["bp"].refresh(trader.partial, price, iv, trader)


class LiveTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._root = QVBoxLayout(self)
        self._root.setSpacing(8)
        self._root.setContentsMargins(8, 8, 8, 8)

        hdr = QLabel("⚡  LIVE ACCOUNT")
        hdr.setObjectName("h1")
        self._root.addWidget(hdr)

        self._no_keys_lbl = QLabel(
            "No live keys saved.\n\nEnter live API keys on the Home tab to enable live mirroring.")
        self._no_keys_lbl.setStyleSheet(f"color:{_D};font-size:13px;")
        self._no_keys_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._no_keys_lbl.setVisible(False)
        self._root.addWidget(self._no_keys_lbl)

        # Any number of live legs now (one per qualifying crypto symbol, no cap) —
        # laid out kanban-style as a wrapping grid (KANBAN_COLS per row), scrollable,
        # since more than a handful no longer fits one screen.
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        inner_v = QVBoxLayout(inner)
        inner_v.setContentsMargins(0, 0, 0, 0)
        inner_v.setSpacing(10)
        self._legs_container = QGridLayout()
        self._legs_container.setSpacing(10)
        inner_v.addLayout(self._legs_container)
        inner_v.addStretch()
        sa.setWidget(inner)
        self._root.addWidget(sa)
        self._leg_widgets = {}   # symbol -> widgets dict

    def _build_leg_block(self, title):
        leg_box = QGroupBox(title)
        _set_kanban_card_accent(leg_box, _D)   # neutral until the first refresh() sets it
        lroot = QVBoxLayout(leg_box)
        lroot.setSpacing(6)
        lroot.setContentsMargins(6, 6, 6, 6)

        sg = QGroupBox("Account")
        sl = QVBoxLayout(sg)
        sl.setContentsMargins(0, 4, 0, 4)
        stats = _SG([
            ["Live Balance", "Live cumPnL",  "Paper cumPnL"],
            ["Live vs Paper","Live Trades",  "Live MTM"],
        ])
        sl.addWidget(stats)
        lroot.addWidget(sg)

        pg = QGroupBox("Position")
        pl = QVBoxLayout(pg)
        pl.setContentsMargins(0, 4, 0, 4)
        pos_sg = _SG([
            ["Side",    "Entry",  "Qty"],
            ["MTM",     "Move",   "Grid"],
            ["Since",   "Lev",    ""],
        ])
        pl.addWidget(pos_sg)
        lroot.addWidget(pg)

        tg = QGroupBox("Live Trade History")
        tl = QVBoxLayout(tg)
        tl.setContentsMargins(4, 4, 4, 4)
        tt = _TradesTable(live=True)
        tl.addWidget(tt)
        lroot.addWidget(tg)

        lg = QGroupBox("Log")
        ll = QVBoxLayout(lg)
        ll.setContentsMargins(4, 4, 4, 4)
        log = QTextEdit()
        log.setReadOnly(True)
        log.setMaximumHeight(100)
        log.setFont(QFont("Consolas", 9))
        ll.addWidget(log)
        lroot.addWidget(lg)

        return leg_box, {"box": leg_box, "stats": stats, "pos": pos_sg, "tt": tt, "log": log}

    def _sync_leg_widgets(self, live_legs_list):
        current_ids = [leg.trader.symbol for leg in live_legs_list]
        if current_ids == list(self._leg_widgets.keys()):
            return
        for w in self._leg_widgets.values():
            self._legs_container.removeWidget(w["box"])
            w["box"].deleteLater()
        self._leg_widgets = {}
        for i, leg in enumerate(live_legs_list):
            title = leg.trader.symbol
            box, w = self._build_leg_block(title)
            self._legs_container.addWidget(box, i // KANBAN_COLS, i % KANBAN_COLS)
            self._leg_widgets[leg.trader.symbol] = w

    def show_no_keys(self):
        self._no_keys_lbl.setVisible(True)
        for w in self._leg_widgets.values():
            w["box"].setVisible(False)

    def refresh(self, legs):
        live_legs_list = [leg for leg in legs if leg.live_exec]
        if not live_legs_list:
            self.show_no_keys()
            return
        self._no_keys_lbl.setVisible(False)
        self._sync_leg_widgets(live_legs_list)

        for leg in live_legs_list:
            w = self._leg_widgets[leg.trader.symbol]
            w["box"].setVisible(True)
            live_exec = leg.live_exec
            trader     = leg.trader
            price     = trader.last_price()
            trades    = list(live_exec.live_trades)
            cum_live  = live_exec.cum_live_pnl
            cum_pap   = trader.cum_pnl
            delta     = cum_live - cum_pap

            sides = {pos["side"] for k, pos in live_exec.live_pos.items()
                     if k != "_unattributed"}
            if len(sides) > 1:       accent = _Y   # shouldn't happen (one-way guard), flag it
            elif sides == {"long"}:  accent = _G
            elif sides == {"short"}: accent = _R
            else:                    accent = _D
            _set_kanban_card_accent(w["box"], accent)

            stats = w["stats"]
            stats.s("Live Balance",  f"${live_exec.balance:,.2f}", _W)
            stats.s("Live cumPnL",   _usd(cum_live, sign=True),   _pc(cum_live))
            stats.s("Paper cumPnL",  _usd(cum_pap, sign=True),    _pc(cum_pap))
            stats.s("Live vs Paper", _usd(delta, sign=True),      _pc(delta))
            stats.s("Live Trades",   str(len(trades)),              _W)
            live_mtm = live_exec.mtm_total(price)
            stats.s("Live MTM",      _usd(live_mtm, sign=True),   _pc(live_mtm))

            pos_sg = w["pos"]
            bot_id = trader.partial.BOT_ID
            pos = live_exec.live_pos.get(bot_id)
            pos_sg.s("Lev", f"{live_exec.effective_leverage:g}x", _D)
            if pos:
                lng = pos["side"] == "long"
                mtm = live_exec.mtm(bot_id, price)
                mv  = ((price - pos["entry"]) / pos["entry"] * 100 if lng
                       else (pos["entry"] - price) / pos["entry"] * 100)
                grid_filled = pos.get("grid_filled", 0)
                pos_sg.s("Side",    f"{'▲  LONG' if lng else '▼  SHORT'}", _G if lng else _R)
                pos_sg.s("Entry",   f"{pos['entry']:.5f}")
                pos_sg.s("Qty",     str(pos["qty"]))
                pos_sg.s("MTM",     f"{mtm:+.2f}",  _pc(mtm))
                pos_sg.s("Move",    f"{mv:+.2f}%",  _pc(mv))
                pos_sg.s("Grid",    str(grid_filled), _C if grid_filled > 0 else _Y)
                pos_sg.s("Since",   pos.get("open_ts", "")[:16], _D)
            else:
                for k in ["Side","Entry","Qty","MTM","Move","Grid","Since"]:
                    pos_sg.s(k, "—", _D)

            w["tt"].fill_live(trades)
            w["log"].setPlainText("\n".join(list(live_exec.log_msgs)[:10]))


class HomeTab(QWidget):
    def __init__(self, on_exit, on_stop_paper, on_start_paper, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setSpacing(16)
        root.setContentsMargins(24, 24, 24, 24)

        title = QLabel("◆  ETH TRADER")
        title.setObjectName("h1")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(title)

        root.addWidget(self._build_keys_box())

        self._mode_lbl = QLabel("Initializing...")
        self._mode_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._mode_lbl.setStyleSheet(f"color:{_D};font-size:13px;")
        root.addWidget(self._mode_lbl)

        # Engine-wide cards: one shared paper balance across every leg, one uptime clock.
        cards = QHBoxLayout()
        cards.setSpacing(8)
        self._cards = {}
        for k in ["Balance", "Uptime"]:
            card = QFrame()
            card.setObjectName("card")
            cl = QVBoxLayout(card)
            cl.setSpacing(2)
            cl.setContentsMargins(12, 8, 12, 8)
            kl = QLabel(k)
            kl.setStyleSheet(f"color:{_D};font-size:10px;")
            vl = QLabel("—")
            vl.setStyleSheet(f"color:{_W};font-size:14px;font-weight:bold;")
            cl.addWidget(kl)
            cl.addWidget(vl)
            self._cards[k] = vl
            cards.addWidget(card)
        root.addLayout(cards)

        # Per-leg blocks: a dynamic list now (any number of qualifying crypto symbols, no
        # cap), not a fixed pair. self._leg_widgets is rebuilt by refresh() whenever the
        # actual running leg set changes (identified by symbol — that only happens once
        # per Start Paper, never mid-session) rather than being fixed at __init__ time.
        self._legs_container = QVBoxLayout()
        self._legs_container.setSpacing(10)
        root.addLayout(self._legs_container)
        self._leg_widgets = {}   # symbol -> widgets dict
        self._no_legs_lbl = QLabel("No symbols currently have any usable backtest "
                                    "result yet — the best-by-PnL candidate across "
                                    "every symbol/interval/source becomes a leg as "
                                    "soon as one exists, no other requirement.")
        self._no_legs_lbl.setStyleSheet(f"color:{_D};font-size:12px;")
        self._no_legs_lbl.setWordWrap(True)
        self._no_legs_lbl.setVisible(False)
        self._legs_container.addWidget(self._no_legs_lbl)

        root.addStretch()

        run_row = QHBoxLayout()
        run_row.setSpacing(8)
        self._start_btn = QPushButton("START PAPER")
        self._start_btn.setFixedHeight(44)
        self._start_btn.setStyleSheet(
            f"background:{_G};color:white;font-weight:bold;border:none;border-radius:6px;")
        self._start_btn.clicked.connect(on_start_paper)
        self._stop_btn = QPushButton("STOP PAPER")
        self._stop_btn.setFixedHeight(44)
        self._stop_btn.setStyleSheet(
            f"background:{_Y};color:#1a1a1a;font-weight:bold;border:none;border-radius:6px;")
        self._stop_btn.clicked.connect(on_stop_paper)
        run_row.addWidget(self._start_btn)
        run_row.addWidget(self._stop_btn)
        root.addLayout(run_row)

        btn = QPushButton("EXIT APP")
        btn.setObjectName("exit")
        btn.setFixedHeight(52)
        btn.setFixedWidth(200)
        btn.clicked.connect(on_exit)
        root.addWidget(btn, 0, Qt.AlignmentFlag.AlignCenter)

    def _build_keys_box(self):
        box = QGroupBox("API Keys")
        grid = QGridLayout(box)
        grid.setSpacing(6)
        grid.setContentsMargins(10, 10, 10, 10)
        grid.addWidget(QLabel(""), 0, 0)
        for c, h in enumerate(["", "API Key", "API Secret", "", "", "Status"]):
            hl = QLabel(h)
            hl.setStyleSheet(f"color:{_D};font-size:10px;")
            grid.addWidget(hl, 0, c)

        self._key_rows = {}
        for r, (live, name) in enumerate([(False, "Paper"), (True, "Live")], start=1):
            name_lbl = QLabel(name)
            name_lbl.setStyleSheet(f"color:{_W};font-size:12px;")
            key_edit = QLineEdit()
            key_edit.setPlaceholderText("API Key")
            secret_edit = QLineEdit()
            secret_edit.setPlaceholderText("API Secret")
            secret_edit.setEchoMode(QLineEdit.EchoMode.Password)
            save_btn = QPushButton("Save")
            save_btn.setFixedWidth(60)
            del_btn = QPushButton("Delete")
            del_btn.setFixedWidth(60)
            status_lbl = QLabel("")
            status_lbl.setStyleSheet(f"color:{_D};font-size:11px;")
            status_lbl.setFixedWidth(90)

            save_btn.clicked.connect(lambda _, l=live: self._on_save_keys(l))
            del_btn.clicked.connect(lambda _, l=live: self._on_delete_keys(l))

            grid.addWidget(name_lbl,   r, 0)
            grid.addWidget(key_edit,   r, 1)
            grid.addWidget(secret_edit, r, 2)
            grid.addWidget(save_btn,   r, 3)
            grid.addWidget(del_btn,    r, 4)
            grid.addWidget(status_lbl, r, 5)
            grid.setColumnStretch(1, 1)
            grid.setColumnStretch(2, 1)

            self._key_rows[live] = (key_edit, secret_edit, status_lbl)
            self._refresh_key_status(live)

        return box

    def _refresh_key_status(self, live):
        _, _, status_lbl = self._key_rows[live]
        if has_keys(live=live):
            status_lbl.setText("● Configured")
            status_lbl.setStyleSheet(f"color:{_G};font-size:11px;")
        else:
            status_lbl.setText("● Not set")
            status_lbl.setStyleSheet(f"color:{_D};font-size:11px;")

    def _on_save_keys(self, live):
        key_edit, secret_edit, _ = self._key_rows[live]
        k, s = key_edit.text().strip(), secret_edit.text().strip()
        if not k or not s:
            QMessageBox.warning(self, "API Keys", "Enter both an API Key and API Secret before saving.")
            return
        save_keys(k, s, live=live)
        key_edit.clear()
        secret_edit.clear()
        self._refresh_key_status(live)
        QMessageBox.information(self, "API Keys",
            f"{'Live' if live else 'Paper'} keys saved securely.")

    def _on_delete_keys(self, live):
        if not has_keys(live=live):
            return
        dlg = QMessageBox(self)
        dlg.setWindowTitle("Delete API Keys")
        dlg.setText(f"Delete the saved {'live' if live else 'paper'} API keys?")
        dlg.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        dlg.setDefaultButton(QMessageBox.StandardButton.No)
        if dlg.exec() != QMessageBox.StandardButton.Yes:
            return
        delete_keys(live=live)
        key_edit, secret_edit, _ = self._key_rows[live]
        key_edit.clear()
        secret_edit.clear()
        self._refresh_key_status(live)

    def set_run_state(self, running):
        self._start_btn.setEnabled(not running)
        self._stop_btn.setEnabled(running)

    def set_loading(self, msg):
        self._mode_lbl.setText(msg)
        self._mode_lbl.setStyleSheet(f"color:{_Y};font-size:13px;")

    def _build_leg_block(self, title):
        """One leg's box: Symbol/Price/WS cards + a Bot Status StatusBar pair. Factored
        out so refresh() can build however many of these are needed (one per qualifying
        crypto symbol, no cap) instead of a fixed pair."""
        box = QGroupBox(title)
        bl  = QVBoxLayout(box)
        bl.setContentsMargins(8, 8, 8, 8)
        bl.setSpacing(6)

        leg_cards = QHBoxLayout()
        leg_cards.setSpacing(8)
        leg_card_lbls = {}
        for k in ["Symbol", "Price", "WS"]:
            card = QFrame()
            card.setObjectName("card")
            cl = QVBoxLayout(card)
            cl.setSpacing(2)
            cl.setContentsMargins(12, 8, 12, 8)
            kl = QLabel(k)
            kl.setStyleSheet(f"color:{_D};font-size:10px;")
            vl = QLabel("—")
            vl.setStyleSheet(f"color:{_W};font-size:14px;font-weight:bold;")
            cl.addWidget(kl)
            cl.addWidget(vl)
            leg_card_lbls[k] = vl
            leg_cards.addWidget(card)
        bl.addLayout(leg_cards)

        bsl = QGridLayout()
        bsl.setSpacing(6)
        h = QLabel("ATR GRID")
        h.setStyleSheet(f"color:{_D};font-size:11px;")
        h.setAlignment(Qt.AlignmentFlag.AlignCenter)
        bsl.addWidget(h, 0, 0)
        p_sb = _StatusBar()
        bsl.addWidget(p_sb, 1, 0)
        bl.addLayout(bsl)

        return box, {"box": box, "cards": leg_card_lbls, "p_sb": p_sb}

    def _sync_leg_widgets(self, legs):
        """Rebuild self._leg_widgets if the running leg set (identified by symbol) has
        changed since the last refresh — this only actually happens once per Start
        Paper, never mid-session, but refresh() can't know the leg set until the engine
        reports ready, so it can't be built at __init__ time."""
        current_ids = [leg.trader.symbol for leg in legs]
        if current_ids == list(self._leg_widgets.keys()):
            return
        for w in self._leg_widgets.values():
            self._legs_container.removeWidget(w["box"])
            w["box"].deleteLater()
        self._leg_widgets = {}
        for leg in legs:
            title = leg.trader.symbol
            box, w = self._build_leg_block(title)
            self._legs_container.addWidget(box)
            self._leg_widgets[leg.trader.symbol] = w
        self._no_legs_lbl.setVisible(not legs)

    def set_stopped(self):
        self._mode_lbl.setText("[STOPPED]")
        self._mode_lbl.setStyleSheet(f"color:{_D};font-size:14px;font-weight:bold;")
        for w in self._leg_widgets.values():
            w["cards"]["WS"].setText("● OFF")
            w["cards"]["WS"].setStyleSheet(f"color:{_D};font-size:14px;font-weight:bold;")

    def refresh(self, legs, balance, start_ts, mode):
        uptime = time.time() - start_ts
        uh, um = int(uptime // 3600), int((uptime % 3600) // 60)
        mode_c = _Y if "LIVE" in mode else _C
        self._mode_lbl.setText(f"[{mode}]")
        self._mode_lbl.setStyleSheet(f"color:{mode_c};font-size:14px;font-weight:bold;")
        self._cards["Balance"].setText(f"${balance:,.2f}")
        self._cards["Uptime"].setText(f"{uh}h {um}m")

        self._sync_leg_widgets(legs)
        for leg in legs:
            w = self._leg_widgets[leg.trader.symbol]
            trader = leg.trader
            price = trader.last_price()
            ws_ok = trader.ws_ok
            w["cards"]["Symbol"].setText(f"{trader.symbol}  {trader.interval}m")
            w["cards"]["Price"].setText(f"{price:.5f}")
            w["cards"]["WS"].setText("● OK" if ws_ok else "● STALE")
            w["cards"]["WS"].setStyleSheet(
                f"color:{_G if ws_ok else _R};font-size:14px;font-weight:bold;")
            w["p_sb"].refresh(trader.partial, price)


@dataclass
class TradingLeg:
    """One symbol's worth of running state — one leg per crypto symbol with a usable
    backtest result; no selection floor of any kind as of 2026-09-03 (explicit user
    ask, "remove all gates. best params pnl wins") — the best-by-cum_profit candidate
    across every symbol/interval/source simply wins. See TradingEngine._run /
    _load_all_worthy_crypto / bt._clears_target."""
    trader: LegTrader
    live_exec: object            # LiveExecutor | None


class TradingEngine:
    def __init__(self):
        self.legs      = []      # list[TradingLeg]
        self._session  = None    # shared paper/backtest session, one per account
        self.balance   = 0.0
        self.start_ts  = time.time()
        self.stop_ev   = threading.Event()
        self.ready     = False
        self.status    = "Initializing..."
        self._slot_lock    = threading.Lock()
        self._active_slots = {}   # symbol -> CAPITAL_TIERS fraction currently held

    def claim_slot(self, symbol):
        """Try to give `symbol` a capital slot. Returns (fraction, is_fresh_claim):
        fraction is None if every slot in CAPITAL_TIERS is already held by other
        symbols (caller must skip the entry — paper and live — entirely). is_fresh_claim
        is True only the first time this symbol acquires this occupancy (used by callers
        to decide whether to re-baseline paper equity / live's equity_fraction, vs.
        leaving an already-running compounding position alone). The single-slot default
        (CAPITAL_TIERS=[1.0]) means only one symbol can ever hold capital at a time —
        the tier-search loop below still works unmodified if a second tier is ever
        reintroduced."""
        with self._slot_lock:
            if symbol in self._active_slots:
                return self._active_slots[symbol], False
            used = set(self._active_slots.values())
            for frac in CAPITAL_TIERS:
                if frac not in used:
                    self._active_slots[symbol] = frac
                    return frac, True
            return None, False

    def release_slot(self, symbol):
        with self._slot_lock:
            self._active_slots.pop(symbol, None)

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="engine").start()

    def _run(self):
        try:
            if not has_keys(live=False):
                self.status = "No paper API keys saved — enter them on the Home tab"
                return

            self.status = "Loading BT results..."
            any_result = _load_combo()
            if not any_result:
                self.status = "Waiting for BT results (use the Backtest tab)..."
                if not _wait_for_results():
                    self.status = "No BT results after 1 hour"
                    return
                any_result = _load_combo()

            # Every crypto symbol with a current bt._clears_target-clearing backtest
            # result gets its own concurrent leg — no cap, one winning interval per
            # symbol (30m only as of 2026-09-01, see bt.CRYPTO_INTERVALS — best-scoring
            # qualifying interval wins). Crypto only — stock/tokenized-
            # equity support removed 2026-08-22. The leg set and the resulting equity
            # split are decided once here and not re-evaluated for the lifetime of this
            # engine (a fresh TradingEngine is already constructed on every Start Paper
            # click, so Stop/Start picks up newly-qualifying symbols). If literally
            # nothing currently qualifies, the engine still comes up — self.legs just
            # stays empty and nothing trades until the next Start Paper after a
            # qualifying result appears.
            crypto_results = _load_all_worthy_crypto()

            session, err = make_session()
            if err:
                self.status = err
                return
            self._session = session
            db = get_db()

            # want_live/live_sess moved ahead of the equity-fraction calc below (added
            # 2026-08-23) so the rescue scan just after can use live_sess to check the
            # real exchange, not just the local paper DB, before total_legs is finalized.
            want_live = has_keys(live=True)
            live_sess = None
            if want_live:
                self.status = "Setting up live session..."
                live_sess = make_live_session()
                if not live_sess:
                    _log.error("Live session failed — paper only")
                    want_live = False

            # Rescue: a symbol can have an open position (paper-tracked, or a real
            # position found directly on the exchange) without currently qualifying as
            # worthy — e.g. its win rate slipped below 100% since the position opened.
            # Without this, the next Start Paper simply never creates a leg for it again,
            # permanently orphaning the position: live has no exchange-side stop-loss, so
            # the paper bot's own signal is the only thing that ever closes it, and no
            # bot means no signal, ever. Force the leg back in using the symbol's own
            # latest available result (still requires it to remain in bt.SYMBOLS — a
            # symbol dropped from config entirely has no valid params to trade it with
            # automatically and needs manual handling, same as ADAUSDT/HYPEUSDT earlier
            # this session).
            worthy_syms = {r[0] for r in crypto_results}
            rescue_syms = set()
            try:
                for (sym,) in db.execute("SELECT DISTINCT symbol FROM paper_position").fetchall():
                    if sym in bt.SYMBOLS and sym not in worthy_syms:
                        rescue_syms.add(sym)
            except Exception as e:
                _log.warning(f"Rescue scan (paper_position): {e}")
            if want_live:
                for sym in bt.SYMBOLS:
                    if sym in worthy_syms or sym in rescue_syms:
                        continue
                    try:
                        r = _api(live_sess.get_positions, category=CATEGORY, symbol=sym)
                        items = (r or {}).get("result", {}).get("list", [])
                        if any(float(it.get("size", 0) or 0) > 0 for it in items):
                            rescue_syms.add(sym)
                    except Exception as e:
                        _log.warning(f"Rescue scan (exchange {sym}): {e}")
            for sym in rescue_syms:
                r = _load_result_for_symbol(sym)
                if r:
                    crypto_results.append(r)
                    _log.warning(f"Rescue: {sym} has an open position but no longer "
                                 f"qualifies as worthy — keeping its leg alive on its "
                                 f"latest available params so the position isn't "
                                 f"orphaned")
                else:
                    _log.error(f"Rescue: {sym} has an open position with NO available "
                              f"backtest result (removed from config?) — no leg can be "
                              f"created for it automatically. Close/manage it manually.")

            total_legs = len(crypto_results)
            _log.info(f"Engine: {total_legs} worthy crypto leg(s) "
                     f"({', '.join(f'{r[0]}@{r[1]}m' for r in crypto_results) or 'none'}) "
                     f"— capital slot(s): {' / '.join(f'{t:.0%}' for t in CAPITAL_TIERS)}, "
                     f"claimed by whichever symbol signals first")

            # Paper balance always comes from the paper session's own mainnet wallet,
            # never from the live one. The two tracks have to stay independent or the
            # paper side stops being a baseline to compare live against.
            self.status = "Fetching balance..."
            for _ in range(10):
                b = fetch_balance(session)
                if b:
                    self.balance = b
                    break
                time.sleep(5)

            if not self.balance:
                # Starting anyway would size every position off equity 0: qty 0 trades,
                # zeroed stats, and a meaningless equity curve.
                self.status = "Balance unavailable — check API keys/permissions"
                _log.critical("Balance fetch failed after 10 attempts — refusing to "
                              "start bots with zero equity")
                return

            for result in crypto_results:
                sym, iv, params, gc_p, gc_pl, lev, sh, entry_source = result
                self.status = f"Connecting ({sym} {iv}m)..."
                _log.info(f"Combo: {sym} {iv}m sharpe={sh:.2f} entry={entry_source}")

                live_exec = None
                if want_live:
                    live_exec = LiveExecutor(live_sess, sym, equity_fraction=0.0, db=db)
                    live_exec.setup()
                    for _ in range(10):
                        if live_exec.fetch_balance() > 0:
                            break
                        time.sleep(5)

                # Every leg is symbol-scoped: bot_id distinguishes DB rows per symbol
                # (now that multiple crypto legs can coexist), and the leg's own
                # param-reload is locked to this same symbol (see _param_reload_loop).
                # equity=0 here on purpose (added 2026-08-25, capital-slot redesign):
                # unlike the old fixed per-leg split, a symbol's real allocation isn't
                # known until it actually claims a capital slot at entry time (see
                # TradingEngine.claim_slot / tick()'s entry branch) — _load_state below
                # overwrites this with the real restored equity if a position already
                # exists from before a restart.
                partial_bot = AtrPartialPaperBot(params, 0.0, db, lev=lev, live=live_exec,
                                                 bot_id=f"partial_{sym}", entry_source=entry_source)
                partial_bot.engine = self
                if sym in rescue_syms:
                    # Rescued purely to keep managing an existing open position — its
                    # result is stale/no-longer-worthy by definition, so it must not
                    # take brand new entries on that data. Existing-position handling
                    # (SL/grid checks) is untouched by this flag either way.
                    partial_bot.entries_paused = True
                if live_exec:
                    self.status = "Checking for an existing live position..."
                    live_exec.reconcile_on_start(partial_bot)

                # Pre-claim a capital slot for a symbol that already has a real
                # position from before a restart (rescued or otherwise) — a fresh
                # signal from a different symbol must not be able to steal capital
                # that's already committed. Equity is NOT re-baselined here; _load_state
                # (above, inside the bot constructors) already restored the real
                # compounding equity for a resumed position — only bookkeeping.
                if partial_bot.position is not None:
                    frac, _ = self.claim_slot(sym)
                    if frac is not None:
                        partial_bot._slot_frac = frac
                        if live_exec:
                            live_exec.equity_fraction = frac
                    else:
                        _log.error(f"{sym}: has an open position on restart but every "
                                  f"capital slot is already held by other symbols — "
                                  f"more capital is committed than the slot model "
                                  f"allows; existing position is unaffected, just not "
                                  f"reflected in slot bookkeeping")

                trader = LegTrader(session, sym, iv, partial_bot)
                trader.engine = self
                trader.start()
                self.legs.append(TradingLeg(trader=trader, live_exec=live_exec))

                threading.Thread(target=_reconcile_loop, args=(trader, self.stop_ev),
                                 daemon=True, name=f"reconcile-{sym}").start()
                threading.Thread(target=_param_reload_loop,
                                 args=(trader, self.stop_ev, sym),
                                 daemon=True, name=f"param-reload-{sym}").start()

            if any(leg.live_exec for leg in self.legs):
                threading.Thread(target=self._live_poll, daemon=True, name="live-poll").start()
            threading.Thread(target=self._bal_loop, daemon=True, name="balance").start()

            if total_legs == 0:
                self.status = "PAPER — no symbols have any usable backtest result yet"
            else:
                self.status = "PAPER + LIVE" if want_live else "PAPER"
            self.ready  = True

        except Exception:
            self.status = "Engine error — check log"
            _log.critical(f"Engine failed:\n{traceback.format_exc()}")

    def _bal_loop(self):
        while not self.stop_ev.is_set():
            time.sleep(60)
            if self.stop_ev.is_set():
                break
            # Refresh both wallets separately: self.balance is the paper side (one
            # shared account-level number, not per-leg), live_exec.balance per leg is
            # what that leg's live orders size from.
            try:
                b = fetch_balance(self._session)
                if b:
                    self.balance = b
            except Exception as e:
                _log.warning(f"Paper balance refresh: {e}")
            for leg in self.legs:
                if leg.live_exec:
                    try:
                        leg.live_exec.fetch_balance()
                    except Exception as e:
                        _log.warning(f"Live balance refresh ({leg.trader.symbol}): {e}")

    def _live_poll(self):
        while not self.stop_ev.is_set():
            time.sleep(30)
            if self.stop_ev.is_set():
                break
            for leg in self.legs:
                if not leg.live_exec:
                    continue
                try:
                    closed = leg.live_exec.poll_positions()
                    for bot_id, pos in closed:
                        leg.live_exec.record_manual_close(bot_id, pos, leg.trader.last_price())
                except Exception as e:
                    _log.warning(f"Live poll ({leg.trader.symbol}): {e}")

    def shutdown(self):
        self.stop_ev.set()
        for leg in self.legs:
            leg.trader._stopped.set()
            try:
                leg.trader.partial._save_state()
            except Exception:
                pass


# ── Readme / Setup / Disclaimer tabs ────────────────────────────────────────────
# Added 2026-09-04, explicit user ask ("add a readme tab. put the thesis in it. have
# a setup/how to run tab as well. and a disclaimer tab").
def _load_bundled_text(*relative_parts):
    """Reads a text resource bundled via eth_trader_mac.spec's datas= (README.md at
    the repo root, docs/SETUP.md under docs/). Unlike data/ and keys/ (real
    directories THIS APP creates next to its own executable at runtime — see _DIR),
    PyInstaller's own datas= mechanism does NOT land files next to the executable:
    a onedir build puts them under an _internal/ subfolder, and a macOS .app BUNDLE
    puts them in Contents/Resources/ instead — two different layouts, neither of
    which is _DIR itself. Tries every real candidate location in turn rather than
    assuming one; the plain _DIR case still covers running straight from source,
    where the file truly does sit next to eth_trader.py."""
    rel = os.path.join(*relative_parts)
    candidates = [
        os.path.join(_DIR, rel),                                  # running from source
        os.path.join(_DIR, "_internal", rel),                     # onedir (PyInstaller 6.x)
        os.path.join(_DIR, "..", "Resources", rel),                # macOS .app BUNDLE
    ]
    for path in candidates:
        try:
            with open(path, encoding="utf-8") as f:
                return f.read()
        except Exception:
            continue
    return (f"# Could not load {rel}\n\nTried:\n" +
            "\n".join(f"- `{c}`" for c in candidates) +
            "\n\nThis file should be bundled with the app — if it's missing, "
            "the build may need to be redone (see eth_trader_mac.spec's datas=).")


class _MarkdownTab(QWidget):
    """Read-only rendered-Markdown viewer, used for the Readme and Setup tabs. Qt's
    QTextEdit.setMarkdown() renders headings/bold/italic/code/tables/lists but has no
    LaTeX support — the thesis's $...$ math spans show as literal source text here,
    not typeset formulas. For fully rendered math, read README.md directly or the
    published HTML artifact."""
    def __init__(self, content, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        view = QTextEdit()
        view.setReadOnly(True)
        view.setMarkdown(content)
        root.addWidget(view)


_DISCLAIMER_TEXT = """\
# Disclaimer & Risk Notice

**This is not legal advice, and reading this tab does not create any legal \
relationship between you and the author.** It is a plain-language statement of the \
terms under which this software is provided. If you need enforceable legal \
protection, have a qualified lawyer review and adapt this text for your \
jurisdiction before distributing or relying on it.

## No Warranty

This software is provided **"AS IS"**, without warranty of any kind, express or \
implied, including but not limited to warranties of merchantability, fitness for a \
particular purpose, accuracy, or non-infringement. The author makes no guarantee \
that the software is free of bugs, that its backtesting, signal generation, or order \
execution logic is correct, or that it will continue to function correctly against a \
live exchange API that may change without notice.

## Real Financial Risk — Mainnet, Real Money, Leverage

This software connects to Bybit **mainnet only** and, when live trading is enabled, \
places **real orders with real funds** at fixed 11x leverage. Leveraged trading of \
cryptocurrency derivatives carries a substantial risk of loss, up to and including \
the total loss of all capital allocated to it, and losses can occur rapidly. \
**Live positions in this software have no exchange-side stop-loss** — the only \
close mechanism is the software's own signal evaluation, which depends on the \
process continuing to run correctly; a crash, network outage, or bug can leave a \
losing position open with no automatic exit.

## No Guarantee of Performance

Any backtested, paper-traded, or historically reported results (including anything \
in this application's own Backtest tab or its accompanying thesis document) are \
**not a guarantee, projection, or promise of future results**. Past performance — \
simulated or real — is not indicative of future performance. Backtests are subject \
to the limitations this software's own documentation discloses in detail (small \
sample sizes, multiple-comparisons/search-overfitting risk, and others) — read them \
before drawing any conclusion about expected real-world performance.

## Not Financial Advice

Nothing in this software, its configuration, its documentation, or its output \
constitutes financial, investment, tax, or legal advice. It is a piece of software \
that automates a specific trading strategy; whether that strategy is appropriate for \
you, your capital, and your circumstances is a decision only you can make, ideally \
with independent professional advice.

## Your Responsibility

By running this software you acknowledge and agree that:

- You are solely responsible for every trading decision the software makes on your \
behalf, including its consequences.
- You are solely responsible for ensuring that operating this software, and any \
trading it performs, is lawful in your jurisdiction and complies with any \
applicable financial regulation, licensing requirement, or tax obligation.
- You are solely responsible for the security of your own API keys, device, and \
exchange account.
- You will not hold the author liable for any direct, indirect, incidental, \
special, or consequential loss or damage arising from your use of this software, \
including but not limited to trading losses, lost profits, data loss, or software \
defects.
- You use this software entirely at your own risk and by choice.

## Acceptance

Choosing to install, configure, or run this software constitutes acceptance of the \
above terms. If you do not agree with them, do not run this software.
"""


class DisclaimerTab(QWidget):
    """Legal/risk disclaimer, shown as its own tab — gates nothing, purely
    informational (added 2026-09-04, explicit user ask). See _DISCLAIMER_TEXT's own
    header: this is a reasonable, plainly-worded disclaimer for a real-money trading
    tool, not a substitute for actual legal counsel."""
    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        view = QTextEdit()
        view.setReadOnly(True)
        view.setMarkdown(_DISCLAIMER_TEXT)
        root.addWidget(view)


class MainWindow(QMainWindow):
    def __init__(self, engine):
        super().__init__()
        self._eng = engine
        self.setWindowTitle("ETH Trader")
        self.resize(1280, 800)
        self.setMinimumSize(900, 600)

        tabs = QTabWidget()
        self.setCentralWidget(tabs)

        self._home     = HomeTab(on_exit=self._do_exit,
                                  on_stop_paper=self._on_stop_paper,
                                  on_start_paper=self._on_start_paper)
        self._backtest = BacktestTab()
        self._paper    = PaperTab()
        self._live     = LiveTab()
        self._readme   = _MarkdownTab(_load_bundled_text("README.md"))
        self._setup    = _MarkdownTab(_load_bundled_text("docs", "SETUP.md"))
        self._disclaimer = DisclaimerTab()

        tabs.addTab(self._home,     "⌂  Home")
        tabs.addTab(self._backtest, "Backtest")
        tabs.addTab(self._paper,    "◆  Paper")
        tabs.addTab(self._live,     "⚡  Live")
        tabs.addTab(self._readme,   "📖  Readme")
        tabs.addTab(self._setup,    "⚙  Setup")
        tabs.addTab(self._disclaimer, "⚠  Disclaimer")

        if not has_keys(live=True):
            self._live.show_no_keys()

        # Backtest auto-starts on launch, zero clicks, auto-repeating every
        # bt.LOOP_INTERVAL (60min as of 2026-09-01, was 2h) for the life of the process
        # — explicitly requested 2026-08-22, the one deliberate exception to "nothing
        # auto-starts". Paper/Live
        # remain fully manual: Start Paper is still user-triggered below.
        self._backtest.auto_start()

        self._paper_running = False   # Start Paper is still user-triggered
        self._home.set_run_state(False)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(1000)

    def _refresh(self):
        self._backtest.refresh()
        if not self._paper_running:
            return
        eng = self._eng
        if not eng.ready:
            self._home.set_loading(eng.status)
            return
        self._home.refresh(eng.legs, eng.balance, eng.start_ts, eng.status)
        self._paper.refresh(eng.legs, eng.balance)
        self._live.refresh(eng.legs)

    def _on_stop_paper(self):
        if not self._paper_running:
            return
        eng = self._eng
        open_live_legs = [leg for leg in eng.legs
                          if leg.live_exec and leg.live_exec.live_pos]

        if open_live_legs:
            names = ", ".join(leg.trader.symbol for leg in open_live_legs)
            dlg = QMessageBox(self)
            dlg.setWindowTitle("Stop Paper Trading")
            dlg.setIcon(QMessageBox.Icon.Warning)
            dlg.setText(
                f"LIVE position(s) currently open: {names}.\n\n"
                "Live has no exchange-side stop-loss — its only close mechanism is the "
                "paper bot's signal. Stopping paper without closing it first leaves "
                "these positions with NO automatic exit until you restart paper or "
                "close them manually on Bybit.\n\n"
                "Close all live position(s) now as part of stopping?"
            )
            close_btn  = dlg.addButton("Close Position(s) && Stop", QMessageBox.ButtonRole.AcceptRole)
            dlg.addButton("Stop Without Closing", QMessageBox.ButtonRole.DestructiveRole)
            cancel_btn = dlg.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
            dlg.setDefaultButton(close_btn)
            dlg.exec()
            clicked = dlg.clickedButton()
            if clicked is cancel_btn:
                return
            if clicked is close_btn:
                for leg in open_live_legs:
                    # Close through the PAPER bot, not LiveExecutor.mark_closed()
                    # directly (ported from unified_combo_gui's 2026-08-28 fix — found
                    # via a real incident there: the old code closed only the live
                    # exchange slice, leaving the paper bot's own self.position/
                    # paper_position row untouched. On the next restart the paper bot
                    # reloaded that now-stale position from the DB, and the user had to
                    # manually close it again via the Paper tab's Close Position button —
                    # at whatever price the market had since moved to, producing a
                    # fabricated paper P&L with no relation to the real exit that already
                    # happened). bot._close() clears the paper side (equity/trades/DB
                    # row) AND — via its own existing `if self.live:
                    # self.live.mark_closed(...)` call — mirrors to the real exchange
                    # exactly like every other close path (TP/SL/GRID/MANUAL) already
                    # does, so this can't desync again. The dialog's dlg.exec() above
                    # blocks this thread but not the background ones — _live_poll (30s)
                    # or the bot's own SL/grid tick can legitimately close the position
                    # while the dialog is still open, so re-check bot.position is still
                    # set under the lock rather than trusting a snapshot taken before the
                    # dialog appeared.
                    price = leg.trader.last_price()
                    with leg.trader._lock:
                        if leg.trader.partial.position is not None:
                            leg.trader.partial._close(price, "stopped_by_user")
                    leg.trader._maybe_release_slot()
        else:
            dlg = QMessageBox(self)
            dlg.setWindowTitle("Stop Paper Trading")
            dlg.setText("Stop paper trading?")
            dlg.setStandardButtons(
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            dlg.setDefaultButton(QMessageBox.StandardButton.No)
            if dlg.exec() != QMessageBox.StandardButton.Yes:
                return

        eng.shutdown()
        self._paper_running = False
        self._home.set_run_state(False)
        self._home.set_stopped()

    def _on_start_paper(self):
        if self._paper_running:
            return
        if not has_keys(live=False):
            QMessageBox.warning(self, "No API Keys",
                "No paper API keys saved.\n\nEnter and save them in the API Keys "
                "section on this tab first.")
            return
        if has_keys(live=True):
            dlg = QMessageBox(self)
            dlg.setWindowTitle("Live Trading Active")
            dlg.setIcon(QMessageBox.Icon.Warning)
            dlg.setText(
                "LIVE TRADING ACTIVE\n\n"
                "A live API key is saved. Orders will be placed on your LIVE Bybit "
                "account. Losses are REAL.\n\nContinue?"
            )
            dlg.setStandardButtons(
                QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
            dlg.setDefaultButton(QMessageBox.StandardButton.Cancel)
            if dlg.exec() != QMessageBox.StandardButton.Ok:
                return

        self._eng = TradingEngine()
        self._eng.start()
        self._paper_running = True
        self._home.set_run_state(True)
        self._home.set_loading(self._eng.status)

    def _do_exit(self):
        dlg = QMessageBox(self)
        dlg.setWindowTitle("Exit")
        dlg.setText("Stop all bots and exit?")
        dlg.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        dlg.setDefaultButton(QMessageBox.StandardButton.No)
        if dlg.exec() == QMessageBox.StandardButton.Yes:
            self.close()

    def closeEvent(self, event):
        self._timer.stop()
        self._eng.shutdown()
        if self._backtest._runner is not None:
            self._backtest._runner.kill_now()
        event.accept()


def main():
    # Assigns this process to a Windows Job Object with KILL_ON_JOB_CLOSE: if this
    # process ends for any reason (normal exit, crash, Task Manager), Windows itself
    # force-kills every ProcessPoolExecutor worker the Backtest tab spawned, rather than
    # relying on Python-level cleanup racing a daemon thread mid-sweep. Same mechanism
    # eth_trader_bt.exe used standalone; must run before any backtest can start.
    bt._win_kill_on_close()

    app = QApplication(sys.argv)
    _apply_dark_theme(app)
    app.setFont(QFont("Consolas", 11))

    # The backtest sweep auto-starts (see MainWindow.__init__/BacktestTab.auto_start,
    # 2026-08-22) — the one deliberate exception. Paper/live never auto-start: the user
    # enters API keys and clicks Start Paper on the Home tab; the live-trading
    # confirmation (if a live key is saved) happens there too, right before the engine
    # actually starts.
    engine = TradingEngine()
    window = MainWindow(engine)
    window.show()
    app.exec()
    # All state-saving already happened synchronously in MainWindow.closeEvent() above
    # this point. sys.exit() alone is not enough to end the process here: pybit's
    # WebSocket wrapper (and possibly other libs) can leave non-daemon threads running,
    # and Python waits for every non-daemon thread to finish before the interpreter
    # actually terminates — which is why EXIT APP was not killing the process in Task
    # Manager. os._exit() ends the process immediately at the OS level, skipping that wait.
    os._exit(0)



def _wait_for_results(timeout=3600):
    deadline = time.time()+timeout; printed = False
    while time.time() < deadline:
        if _load_combo(): return True
        if not printed:
            print("Waiting for results (use the Backtest tab)...", flush=True)
            printed = True
        time.sleep(10)
    return False

def _reconcile_loop(trader, stop_event):
    while not stop_event.is_set():
        time.sleep(RECONCILE_S)
        if stop_event.is_set(): break
        try: trader.reconcile()
        except Exception as e: _log.error(f"Reconcile error: {e}")


def _param_reload_loop(trader, stop_ev, locked_symbol):
    """Every leg is locked to the symbol it started with for its whole session — added
    2026-08-21 alongside multi-crypto-leg support, since two concurrent crypto legs
    reloading via a class-wide best-overall pick could both drift onto whichever single
    symbol currently scores best, colliding with each other or abandoning the symbol they
    were specifically started for. Reload only ever re-reads *this* symbol's own current
    result (_load_result_for_symbol) — it can never return a different symbol, though its
    winning interval can change (see that function's docstring), which the `sym_changed`
    check below already handles correctly via `old_sym, old_iv = trader.symbol,
    trader.interval`."""
    time.sleep(PARAM_RELOAD_S)
    while not stop_ev.is_set():
        try:
            # require_fresh=True (added 2026-08-23): a leg must never keep refreshing
            # onto — or continuing to trade new entries on — a result that's gone
            # stale (see RESULT_MAX_AGE_S/PARAM_RELOAD_S). require_target=True (added
            # 2026-08-31 as min_win_rate=_MIN_WR_60PLUS, explicit user ask, "if there is
            # no params above 60wr then paper pauses trading until there is"; switched
            # to bt._clears_target 2026-09-01 when win_rate stopped being the gate —
            # same pause, now triggered when this symbol's best current result exists
            # and is fresh but doesn't clear the current targets (total_ret_pct>=15%,
            # cum_loss<$5 — the DD-ratio third leg was removed 2026-09-03, explicit
            # user ask; see bt._clears_target). If nothing fresh AND
            # target-clearing is available, pause new entries but change nothing else:
            # current params stay in place, and any already-open position is completely
            # unaffected — only the `else:` (flat) branch of tick() checks
            # entries_paused.
            result = _load_result_for_symbol(locked_symbol, require_fresh=True,
                                              require_target=True)
            if not result:
                _log.warning(f"Param reload[{locked_symbol}]: no fresh result within "
                             f"{RESULT_MAX_AGE_S/3600:.1f}h clearing the profit/loss/DD "
                             f"targets — pausing new entries until one appears (current "
                             f"params kept, any open position is unaffected)")
                with trader._lock:
                    trader.partial.entries_paused = True
            else:
                sym, iv, params, gc_p, gc_pl, lev, sh, entry_source = result
                while not stop_ev.is_set():
                    p_flat = trader.partial.position is None
                    live_flat = trader.partial.live is None or not trader.partial.live.live_pos
                    if p_flat and live_flat:
                        break
                    time.sleep(60)
                if stop_ev.is_set(): break

                old_sym, old_iv = trader.symbol, trader.interval
                sym_changed = sym != old_sym or iv != old_iv

                # _on_kline always reads self.params under trader._lock
                # (tick() uses it mid-signal-computation) — writing it
                # unlocked could let a bar-close callback observe a torn/partial update.
                #
                # The flatness poll above is unlocked and can be up to 60s stale — a
                # bar-close on the WS callback thread could win trader._lock first and
                # open a fresh position under the OLD params/symbol between that poll
                # passing and this lock actually being acquired. Re-check fresh, now
                # that we hold the same lock _on_kline uses to open a
                # position, so no entry can land mid-check: while we hold trader._lock,
                # tick() cannot run, and paper position only ever flips
                # non-None inside one of those calls. If it's no longer flat, skip
                # applying this cycle's swap entirely — the position keeps running under
                # its current (already-backtested) params/symbol, and the next
                # PARAM_RELOAD_S cycle re-fetches the latest result and tries again.
                skip_this_cycle = False
                with trader._lock:
                    if (trader.partial.position is not None
                            or (trader.partial.live is not None
                                and trader.partial.live.live_pos)):
                        skip_this_cycle = True
                    else:
                        trader.partial.params = params
                        trader.partial.lev    = lev
                        trader.partial.entry_source = entry_source
                    # A fresh result exists (that's why we're in this branch at all) —
                    # unpause entries regardless of skip_this_cycle. If a position just
                    # opened and the params swap itself was deferred to next cycle,
                    # unpausing here is still correct and harmless: the bot is already
                    # in tick()'s `if self.position:` branch, not evaluating entries.
                    trader.partial.entries_paused = False

                if skip_this_cycle:
                    _log.warning(f"Param reload[{locked_symbol}]: position opened just "
                                 f"before the lock was acquired, deferring this reload "
                                 f"to next cycle")
                elif sym_changed:
                    _log.info(f"Param reload: {old_sym} {old_iv}m → {sym} {iv}m sharpe={sh:.2f} "
                              f"entry={entry_source}")
                    trader.symbol   = sym
                    trader.interval = iv
                    # The live executor caches symbol-specific lot/leverage filters —
                    # repoint it, or live orders keep going to the previous symbol.
                    live = trader.partial.live
                    if live is not None:
                        try:
                            live.symbol = sym
                            live.setup()
                            live.log(f"Symbol switched → {sym}")
                        except Exception as e:
                            _log.error(f"Live executor re-setup for {sym} failed: {e}")
                    with trader._lock:
                        trader.bars = seed_bars(trader.session, sym, iv)
                    trader._last_ts = 0
                    trader._force_reconnect = True
                else:
                    _log.info(f"Param reload: {sym} {iv}m params updated sharpe={sh:.2f} "
                              f"entry={entry_source}")
        except Exception as e:
            _log.warning(f"Param reload error: {e}")
        time.sleep(PARAM_RELOAD_S)



if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
