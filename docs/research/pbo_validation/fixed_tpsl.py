"""
Benchmark control: a conventional single-TP / single-SL exit paired with the EXACT
same entry-signal composition (stochastic K/D crossover + Gaussian Channel regime
filter + Choppiness Index gate, see README.md sec.2.4) the production grid
strategy uses. This isolates the exit-structure's own contribution: same signal,
same data, same windows, same random-search protocol -- the only thing that differs
is what happens after entry. Written as a standalone research script (not part of
the production module) since no single-TP/SL exit path exists in production to reuse.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
import numpy as np
import eth_trader_bt as bt

TAKER_FEE = bt.TAKER_FEE
LEVERAGE = bt.LEVERAGE
MARGIN_HEADROOM = bt.MARGIN_HEADROOM
WIN_FEE_MULT = bt.WIN_FEE_MULT
MIN_WIN_PRICE_PCT = bt.MIN_WIN_PRICE_PCT
MIN_TRADES = bt.MIN_TRADES
MIN_AVG_HOLD = bt.MIN_AVG_HOLD
MIN_RR_RATIO = bt.MIN_RR_RATIO


def sample_fixed_params(space=bt.PARAM_SPACE_SEARCHED):
    p = bt._sample(space)
    p["tp_mult"] = round(np.random.uniform(0.5, 6.0), 2)   # same range family as stop_mult
    return p


def bt_fixed_pair(params, hi, lo, cl, backtest_bars, bpy, lev, initial_equity, entry_source="searched"):
    """Single fixed take-profit / single fixed stop-loss twin of _bt_combo_pair, same
    entry signal, same win-classification rule (WIN_FEE_MULT/MIN_WIN_PRICE_PCT), same
    quality gates (MIN_TRADES/MIN_AVG_HOLD/MIN_RR_RATIO) -- RR here is tp_mult/stop_mult
    directly, the single-target analogue of the grid's cumulative-distance ratio."""
    n = len(cl)
    k_len=int(params["k_len"]); k_sm=int(params["k_smooth"]); d_sm=int(params["d_smooth"])
    ob=float(params["ob"]); os_=float(params["os"]); chop_len=int(params["chop_len"])
    chop_thr=float(params["chop_thr"]); gc_p=int(params["gc_period"]); gc_pl=int(params["gc_poles"])
    atr_p=int(params["atr_p"]); stop_m=float(params["stop_mult"]); tp_m=float(params["tp_mult"])
    gc_sqrt2 = bt.PINE_GC_SQRT2 if entry_source == "pine" else None

    raw_k = bt._stoch_raw_k(hi,lo,cl,k_len); k_arr=bt._sma(raw_k,k_sm); d_arr=bt._sma(k_arr,d_sm)
    ci_arr = bt._chop_index(hi,lo,cl,chop_len); atr_arr = bt._atr_wilder(hi,lo,cl,atr_p)
    k_prev = np.roll(k_arr,1); d_prev = np.roll(d_arr,1)
    valid = ~np.isnan(k_arr)&~np.isnan(d_arr)&~np.isnan(k_prev)&~np.isnan(d_prev)
    cup = valid&(k_arr>d_arr)&(k_prev<=d_prev); cdn = valid&(k_arr<d_arr)&(k_prev>=d_prev)
    cup[0]=cdn[0]=False
    ci_ok = ci_arr<chop_thr
    gm = bt.gaussian_channel_midline(hi,lo,cl,gc_p,gc_pl,sqrt2=gc_sqrt2)
    gd = np.diff(gm, prepend=gm[0])
    buy = cup & (k_arr<=os_) & (gd>0) & ci_ok
    sell = cdn & (k_arr>=ob) & (gd<0) & ci_ok

    start_i = n - backtest_bars
    eq = initial_equity; peak_eq = eq; max_dd = 0.0
    in_pos=False; side=None; ep=sl=tp=qty=0.0
    trades=0; wins=0; gw=gl=0.0; hold_sum=0

    for i in range(max(start_i,1), n):
        if np.isnan(atr_arr[i]): continue
        price = cl[i]
        if not in_pos:
            if buy[i] or sell[i]:
                side = 'long' if buy[i] else 'short'
                atrv = atr_arr[i]
                ntl = eq*lev*MARGIN_HEADROOM
                qty = ntl/price; fee0 = ntl*TAKER_FEE; eq -= fee0
                ep = price; entry_i = i
                if side=='long':
                    sl = ep - stop_m*atrv; tp = ep + tp_m*atrv
                else:
                    sl = ep + stop_m*atrv; tp = ep - tp_m*atrv
                in_pos = True
        else:
            hit_sl = (side=='long' and price<=sl) or (side=='short' and price>=sl)
            hit_tp = (side=='long' and price>=tp) or (side=='short' and price<=tp)
            if hit_sl or hit_tp:
                exit_px = sl if hit_sl else tp
                pnl = (exit_px-ep)*qty if side=='long' else (ep-exit_px)*qty
                fee1 = exit_px*qty*TAKER_FEE
                pnl -= fee1
                total_fees = exit_px*qty*TAKER_FEE + ep*qty*TAKER_FEE
                eq = max(eq+pnl, 0.0)
                trades += 1; hold_sum += (i-entry_i)
                raw_pct = (exit_px-ep)/ep if side=='long' else (ep-exit_px)/ep
                is_win = (pnl > WIN_FEE_MULT*total_fees) and (raw_pct >= MIN_WIN_PRICE_PCT)
                if is_win: wins += 1; gw += pnl
                else: gl += abs(pnl)
                peak_eq = max(peak_eq, eq)
                max_dd = min(max_dd, (eq-peak_eq)/peak_eq if peak_eq>0 else -1.0)
                in_pos = False

    if trades < MIN_TRADES: return None
    avg_hold = hold_sum/trades
    if avg_hold < MIN_AVG_HOLD: return None
    rr = tp_m/max(stop_m, 1e-9)
    if rr < MIN_RR_RATIO: return None
    total_ret_pct = (eq-initial_equity)/initial_equity*100
    if total_ret_pct <= 0: return None
    win_rate = wins/trades
    profit_factor = gw/gl if gl>0 else float("inf")
    # per-trade return proxy for Sharpe -- same convention as the grid's own metric
    # (annualized via bars-per-year and average holding period)
    trades_per_year = bpy/max(avg_hold,1e-9)
    mean_ret = (total_ret_pct/100)/trades
    std_ret = np.std([gw/max(wins,1), -gl/max(trades-wins,1)]) if trades>wins>0 else abs(mean_ret)
    sharpe = (mean_ret/std_ret*np.sqrt(trades_per_year)) if std_ret>0 else 0.0
    # No zero_fill_rate penalty here (unlike the grid's score formula) -- there's no
    # partial-fill concept for a single-target exit, so the penalty term would always
    # be a no-op multiplier of 1.0.
    score = sharpe*np.sqrt(trades/MIN_TRADES)
    return dict(sharpe=sharpe, score=score, total_ret_pct=total_ret_pct, trades=trades,
                win_rate=win_rate, profit_factor=profit_factor, max_dd_pct=max_dd*100)
