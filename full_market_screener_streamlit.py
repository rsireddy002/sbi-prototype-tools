"""
Full Market Screener - Streamlit Dashboard
================================================
Web version of full_market_screener.py - same proven logic (imports
SymbolState/evaluate directly from live_scanner_polling.py, doesn't
reimplement), with the QUALITY SETUPS section (stocks genuinely bouncing
off support AND with real room to resistance) shown prominently at the
top, plus the full 208-stock table below.

Uses st.cache_resource to keep the states dict alive across Streamlit's
auto-refresh reruns - this matters here specifically because OI tracking
(oi_open, set on first-seen value) would silently reset to "no change"
every single refresh if the states dict got recreated from scratch each
time, which is exactly what a plain rerun-from-top script would do.

Run with:
    streamlit run full_market_screener_streamlit.py

Requires live_scanner_polling.py in the same folder (imported from).
"""
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from live_scanner_polling import (
    SymbolState, evaluate, load_config, get_access_token,
    load_today_prep, fetch_all_quotes, update_states_from_quotes,
)

IST = ZoneInfo("Asia/Kolkata")
POLL_INTERVAL_SEC = 10
MIN_RESISTANCE_ROOM_PCT = 1.0


def nearest_support_and_resistance(state):
    if state.last_price is None:
        return None, None, None, None
    support_candidates = [
        (z["level"], (state.last_price - z["level"]) / state.last_price * 100)
        for z in state.support_zones if z["level"] < state.last_price
    ]
    resistance_candidates = [
        (z["level"], (z["level"] - state.last_price) / state.last_price * 100)
        for z in state.resistance_zones if z["level"] > state.last_price
    ]
    nearest_support = min(support_candidates, key=lambda x: x[1]) if support_candidates else (None, None)
    nearest_resistance = min(resistance_candidates, key=lambda x: x[1]) if resistance_candidates else (None, None)
    return nearest_support[0], nearest_support[1], nearest_resistance[0], nearest_resistance[1]


@st.cache_resource
def get_states():
    """Built once, persists across every auto-refresh rerun - critical so
    oi_open (first-seen OI this session) doesn't reset to the current
    value on every single refresh, which would make OI direction always
    show 'flat' no matter what's actually happening."""
    config = load_config()
    access_token = get_access_token(config)
    prep_data = load_today_prep()

    states = {}
    states_by_key = {}
    instrument_keys = []
    for symbol, info in prep_data["stocks"].items():
        futures_key = info.get("futures_instrument_key")
        if not futures_key:
            continue
        state = SymbolState(
            symbol, info.get("instrument_key"), futures_key,
            {"support_zones": info.get("support_zones", []), "resistance_zones": info.get("resistance_zones", [])},
            info.get("rvol_baseline", {}),
            trend_indicators=info.get("trend_indicators"),
            prev_close=info.get("prev_close"),
        )
        states[symbol] = state
        states_by_key[futures_key] = state
        instrument_keys.append(futures_key)

    return {"states": states, "states_by_key": states_by_key, "instrument_keys": instrument_keys, "access_token": access_token}


st.set_page_config(page_title="Full Market Screener", layout="wide")
st.title("Full Market Screener")
st.caption("Quality setups = at support (bouncing) AND resistance has real room (>= 1% away)")

app = get_states()
states = app["states"]

try:
    quotes = fetch_all_quotes(app["instrument_keys"], app["access_token"])
    updated = update_states_from_quotes(quotes, app["states_by_key"])
    st.success(f"Connected - updated {updated}/{len(states)} stocks")
except Exception as e:
    st.error(f"Error fetching quotes: {type(e).__name__}: {e}")

candidates = evaluate(states)
candidates_by_symbol = {c["symbol"]: c for c in candidates}

rows = []
for symbol, state in states.items():
    if state.last_price is None:
        continue
    support_level, support_dist, resistance_level, resistance_dist = nearest_support_and_resistance(state)
    candidate = candidates_by_symbol.get(symbol)
    ti = state.trend_indicators or {}
    rows.append({
        "symbol": symbol,
        "ltp": state.last_price,
        "prev_close": state.prev_close,
        "change_pct": round(state.change_pct(), 2) if state.change_pct() is not None else None,
        "vwap": state.vwap(),
        "rvol": round(state.rvol(), 2) if state.rvol() is not None else None,
        "oi": state.oi_current,
        "tbq": state.tbq,
        "tsq": state.tsq,
        "aggression": state.aggression_label(),
        "support": support_level,
        "support_dist_pct": round(support_dist, 2) if support_dist is not None else None,
        "resistance": resistance_level,
        "resistance_dist_pct": round(resistance_dist, 2) if resistance_dist is not None else None,
        "rsi14": ti.get("rsi14"),
        "score": candidate["score"] if candidate else None,
        "pattern": candidate["pattern"] if candidate else None,
    })

quality_setups = [
    r for r in rows
    if r["pattern"] == "support_bounce"
    and r["resistance_dist_pct"] is not None
    and r["resistance_dist_pct"] >= MIN_RESISTANCE_ROOM_PCT
]
quality_setups.sort(key=lambda r: -(r["score"] or 0))

st.subheader(f"Quality Setups ({len(quality_setups)})")
if quality_setups:
    st.dataframe(pd.DataFrame(quality_setups), use_container_width=True, hide_index=True)
else:
    st.info("None right now - either no support_bounce candidates, or resistance is too close on all of them.")

st.subheader("Full Table (all stocks, sorted by score)")
rows_sorted = sorted(rows, key=lambda r: (r["score"] is None, -(r["score"] or 0)))
st.dataframe(pd.DataFrame(rows_sorted), use_container_width=True, hide_index=True)

st.caption(f"Auto-refreshing every {POLL_INTERVAL_SEC}s. Last updated: {datetime.now(IST).strftime('%H:%M:%S')}")
time.sleep(POLL_INTERVAL_SEC)
st.rerun()
