"""
SBI Full Dashboard - PROTOTYPE (comprehensive single-stock view)
========================================================================
Combines everything built so far for SBIN into one dashboard:
- Daily candlestick chart with EMA50/200, Bollinger Bands, and S/R zones
  drawn directly on it (Plotly - Streamlit's basic charts can't do this)
- All 8 daily technical indicators (reused, already tested against known
  values and cross-checked against a real chart earlier)
- Live LTP/VWAP/OI/aggression from the FUTURES contract, polled every 10s
  (same proven-reliable REST endpoint the main scanner uses)
- Live history logged to its own CSV, separate from the main scanner

Standalone - doesn't touch any existing files, no git/Streamlit Cloud
changes needed.

Run with:
    streamlit run sbi_full_dashboard.py

Install (one extra package beyond what's already needed):
    pip install plotly

Change TARGET_SYMBOL below to analyze a different stock instead.
"""
import csv
import json
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

IST = ZoneInfo("Asia/Kolkata")
TARGET_SYMBOL = "SBIN"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.txt")
PREP_DIR = os.path.join(SCRIPT_DIR, "prep_output")
HISTORY_DIR = os.path.join(SCRIPT_DIR, "stock_history")
FULL_QUOTES_URL = "https://api.upstox.com/v2/market-quote/quotes"
POLL_INTERVAL_SEC = 10
INTRADAY_LOOKBACK_DAYS = 15  # ~10-11 trading days, ~750-825 five-min bars - plenty for EMA200 to stabilize


# ---------------------------------------------------------------------------
# Indicator math - same tested functions as sbi_technical_analysis.py
# ---------------------------------------------------------------------------
def ema_series(values, period):
    if len(values) < period:
        return [None] * len(values)
    k = 2 / (period + 1)
    result = [None] * (period - 1)
    sma_seed = sum(values[:period]) / period
    result.append(sma_seed)
    prev = sma_seed
    for price in values[period:]:
        current = price * k + prev * (1 - k)
        result.append(current)
        prev = current
    return result


def sma_series(values, period):
    result = [None] * (period - 1)
    for i in range(period - 1, len(values)):
        result.append(sum(values[i - period + 1:i + 1]) / period)
    return result


def bollinger_bands(closes, period=20, num_std=2):
    middle = sma_series(closes, period)
    upper, lower = [], []
    for i in range(len(closes)):
        if middle[i] is None:
            upper.append(None)
            lower.append(None)
            continue
        window = closes[i - period + 1:i + 1]
        mean = middle[i]
        variance = sum((x - mean) ** 2 for x in window) / period
        std = variance ** 0.5
        upper.append(mean + num_std * std)
        lower.append(mean - num_std * std)
    return middle, upper, lower


def rsi_series(closes, period=14):
    n = len(closes)
    if n < period + 1:
        return [None] * n
    result = [None] * period
    gains, losses = [], []
    for i in range(1, period + 1):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    rs = avg_gain / avg_loss if avg_loss != 0 else float("inf")
    result.append(100 - (100 / (1 + rs)))
    for i in range(period + 1, n):
        change = closes[i] - closes[i - 1]
        gain, loss = max(change, 0), max(-change, 0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        rs = avg_gain / avg_loss if avg_loss != 0 else float("inf")
        result.append(100 - (100 / (1 + rs)))
    return result


def macd_series(closes, fast=12, slow=26, signal=9):
    ema_fast = ema_series(closes, fast)
    ema_slow = ema_series(closes, slow)
    macd_line = [(f - s) if (f is not None and s is not None) else None for f, s in zip(ema_fast, ema_slow)]
    valid_macd = [m for m in macd_line if m is not None]
    signal_valid = ema_series(valid_macd, signal)
    none_count = len(macd_line) - len(valid_macd)
    signal_line = [None] * none_count + signal_valid
    return macd_line, signal_line


def true_range_series(candles):
    tr = [None]
    for i in range(1, len(candles)):
        high, low = candles[i][2], candles[i][3]
        prev_close = candles[i - 1][4]
        tr.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return tr


def atr_series(candles, period=14):
    tr = true_range_series(candles)
    n = len(tr)
    result = [None] * n
    if n < period + 1:
        return result
    valid_tr = tr[1:period + 1]
    avg_tr = sum(valid_tr) / period
    result[period] = avg_tr
    prev = avg_tr
    for i in range(period + 1, n):
        current = (prev * (period - 1) + tr[i]) / period
        result[i] = current
        prev = current
    return result


def stochastic_oscillator(candles, k_period=14, d_period=3):
    n = len(candles)
    k_values = [None] * n
    for i in range(k_period - 1, n):
        window = candles[i - k_period + 1:i + 1]
        highest_high = max(c[2] for c in window)
        lowest_low = min(c[3] for c in window)
        close = candles[i][4]
        if highest_high == lowest_low:
            k_values[i] = 50.0
        else:
            k_values[i] = (close - lowest_low) / (highest_high - lowest_low) * 100
    valid_k = [v for v in k_values if v is not None]
    d_valid = sma_series(valid_k, d_period)
    none_count = len(k_values) - len(valid_k)
    d_values = [None] * none_count + d_valid
    return k_values, d_values


def adx_series(candles, period=14):
    n = len(candles)
    if n < period * 2:
        return [None] * n, [None] * n, [None] * n
    plus_dm_raw, minus_dm_raw = [None], [None]
    tr_raw = true_range_series(candles)
    for i in range(1, n):
        up_move = candles[i][2] - candles[i - 1][2]
        down_move = candles[i - 1][3] - candles[i][3]
        plus_dm_raw.append(up_move if (up_move > down_move and up_move > 0) else 0)
        minus_dm_raw.append(down_move if (down_move > up_move and down_move > 0) else 0)

    def wilder_smooth(series, period):
        result = [None] * len(series)
        if len(series) < period + 1:
            return result
        first_sum = sum(v for v in series[1:period + 1])
        result[period] = first_sum
        prev = first_sum
        for i in range(period + 1, len(series)):
            current = prev - (prev / period) + series[i]
            result[i] = current
            prev = current
        return result

    smoothed_plus_dm = wilder_smooth(plus_dm_raw, period)
    smoothed_minus_dm = wilder_smooth(minus_dm_raw, period)
    smoothed_tr = wilder_smooth(tr_raw, period)

    plus_di, minus_di, dx = [None] * n, [None] * n, [None] * n
    for i in range(n):
        if smoothed_tr[i] and smoothed_plus_dm[i] is not None:
            plus_di[i] = 100 * smoothed_plus_dm[i] / smoothed_tr[i]
            minus_di[i] = 100 * smoothed_minus_dm[i] / smoothed_tr[i]
            di_sum = plus_di[i] + minus_di[i]
            if di_sum > 0:
                dx[i] = 100 * abs(plus_di[i] - minus_di[i]) / di_sum

    valid_dx = [v for v in dx if v is not None]
    adx = [None] * n
    if len(valid_dx) >= period:
        start_idx = next(i for i, v in enumerate(dx) if v is not None)
        first_adx = sum(valid_dx[:period]) / period
        adx[start_idx + period - 1] = first_adx
        prev = first_adx
        for i in range(start_idx + period, n):
            if dx[i] is not None:
                current = (prev * (period - 1) + dx[i]) / period
                adx[i] = current
                prev = current
    return adx, plus_di, minus_di


def supertrend_series(candles, period=10, multiplier=3):
    n = len(candles)
    atr = atr_series(candles, period)
    supertrend, direction = [None] * n, [None] * n
    final_upper, final_lower = [None] * n, [None] * n
    for i in range(n):
        if atr[i] is None:
            continue
        hl2 = (candles[i][2] + candles[i][3]) / 2
        basic_upper = hl2 + multiplier * atr[i]
        basic_lower = hl2 - multiplier * atr[i]
        prev_final_upper = final_upper[i - 1] if i > 0 and final_upper[i - 1] is not None else basic_upper
        prev_final_lower = final_lower[i - 1] if i > 0 and final_lower[i - 1] is not None else basic_lower
        prev_close = candles[i - 1][4] if i > 0 else candles[i][4]
        final_upper[i] = basic_upper if (basic_upper < prev_final_upper or prev_close > prev_final_upper) else prev_final_upper
        final_lower[i] = basic_lower if (basic_lower > prev_final_lower or prev_close < prev_final_lower) else prev_final_lower
        close = candles[i][4]
        prev_supertrend = supertrend[i - 1] if i > 0 else None
        prev_direction = direction[i - 1] if i > 0 else None
        if prev_supertrend is None:
            if close <= final_upper[i]:
                supertrend[i], direction[i] = final_upper[i], "down"
            else:
                supertrend[i], direction[i] = final_lower[i], "up"
        elif prev_direction == "down":
            if close <= final_upper[i]:
                supertrend[i], direction[i] = final_upper[i], "down"
            else:
                supertrend[i], direction[i] = final_lower[i], "up"
        else:
            if close >= final_lower[i]:
                supertrend[i], direction[i] = final_lower[i], "up"
            else:
                supertrend[i], direction[i] = final_upper[i], "down"
    return supertrend, direction


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------
def load_config():
    config = {}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            for line in f:
                line = line.strip()
                if line and "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    config[k.strip()] = v.strip()
    return config


def get_access_token(config):
    try:
        if "ACCESS_TOKEN" in st.secrets:
            return st.secrets["ACCESS_TOKEN"]
    except Exception:
        pass
    return config.get("ACCESS_TOKEN") or config.get("UPSTOX_ACCESS_TOKEN")


def load_today_prep_entry(symbol):
    files = sorted(
        f for f in os.listdir(PREP_DIR) if f.startswith("banknifty_prep_") and f.endswith(".json")
    )
    if not files:
        return None
    with open(os.path.join(PREP_DIR, files[-1])) as f:
        data = json.load(f)
    return data.get("stocks", {}).get(symbol)


def fetch_intraday_5min_candles(instrument_key, access_token, days_back=INTRADAY_LOOKBACK_DAYS):
    """5-minute candles, same URL pattern as the daily version but with
    unit='minutes', interval=5 instead of unit='days', interval=1.
    Confirmed via Upstox's own V3 docs: minute/hour data is available back
    to Jan 2022 - we only pull the last couple weeks though, since that's
    already far more than any of our indicators (max EMA200) need to
    stabilize on a 5-min bar."""
    to_date = datetime.now().strftime("%Y-%m-%d")
    from_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    url = f"https://api.upstox.com/v3/historical-candle/{instrument_key}/minutes/5/{to_date}/{from_date}"
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    resp = requests.get(url, headers=headers, timeout=20)
    resp.raise_for_status()
    candles = resp.json().get("data", {}).get("candles", [])
    candles.sort(key=lambda c: c[0])
    return candles


def fetch_quote(instrument_key, access_token):
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    resp = requests.get(
        FULL_QUOTES_URL, headers=headers, params={"instrument_key": instrument_key}, timeout=15,
    )
    resp.raise_for_status()
    for entry in resp.json().get("data", {}).values():
        if entry.get("instrument_token") == instrument_key:
            return entry
    return None


def history_path(symbol):
    os.makedirs(HISTORY_DIR, exist_ok=True)
    date_str = datetime.now(IST).strftime("%Y-%m-%d")
    return os.path.join(HISTORY_DIR, f"{symbol}_full_5min_{date_str}.csv")


def append_history_row(path, row):
    file_exists = os.path.exists(path)
    with open(path, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["timestamp", "ltp", "vwap", "volume", "oi", "tbq", "tsq"])
        writer.writerow(row)


def aggression_label(tbq, tsq):
    if not tbq or not tsq:
        return "no_data"
    ratio = tbq / tsq
    if ratio > 1.3:
        return "buying_aggression"
    elif ratio < 0.77:
        return "selling_aggression"
    return "balanced"


def oi_direction(oi_open, oi_current):
    if oi_open is None or oi_current is None or oi_open == 0:
        return "no_data"
    change_pct = (oi_current - oi_open) / oi_open * 100
    if change_pct > 0.5:
        return "up"
    elif change_pct < -0.5:
        return "down"
    return "flat"


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
st.set_page_config(page_title=f"{TARGET_SYMBOL} Full Dashboard", layout="wide")
st.title(f"{TARGET_SYMBOL} - Full Dashboard (5-minute, prototype)")

entry = load_today_prep_entry(TARGET_SYMBOL)
if entry is None:
    st.error(f"No prep data for {TARGET_SYMBOL}. Run morning_prep_banknifty.py first.")
    st.stop()

cash_key = entry.get("instrument_key")
futures_key = entry.get("futures_instrument_key")
config = load_config()
access_token = get_access_token(config)
if not access_token:
    st.error("No ACCESS_TOKEN found.")
    st.stop()

# --- 5-minute intraday data + indicators (SAME indicator math as the
# daily version - these functions are timeframe-agnostic, they just
# operate on whatever candle list they're given) ---
candles = fetch_intraday_5min_candles(cash_key, access_token)
closes = [c[4] for c in candles]
dates = [c[0][:16].replace("T", " ") for c in candles]  # YYYY-MM-DD HH:MM (full timestamp, not just date - 5-min bars need time resolution)

ema50 = ema_series(closes, 50)
ema200 = ema_series(closes, 200)
bb_mid, bb_upper, bb_lower = bollinger_bands(closes)
rsi = rsi_series(closes, 14)
macd_line, macd_signal = macd_series(closes)
atr = atr_series(candles, 14)
stoch_k, stoch_d = stochastic_oscillator(candles)
adx, plus_di, minus_di = adx_series(candles, 14)
st_val, st_dir = supertrend_series(candles, 10, 3)

current_price = closes[-1]
all_support = entry.get("support_zones", [])
all_resistance = entry.get("resistance_zones", [])
# BUG FIX: previously took the 3 lowest resistance zones and 3 highest
# support zones from the FULL historical list, without checking they're
# actually on the correct side of the current price - if the stock moved
# a lot from parts of its history, this could show "resistance" sitting
# below current price (nonsensical). Now filtering to the correct side
# first, then taking the nearest few from there.
support_zones = sorted(
    (z for z in all_support if z["level"] < current_price),
    key=lambda z: -z["level"],
)[:3]
resistance_zones = sorted(
    (z for z in all_resistance if z["level"] > current_price),
    key=lambda z: z["level"],
)[:3]

# --- Candlestick chart with overlays ---
st.subheader("5-Minute Chart - EMA, Bollinger Bands, S/R Zones")
fig = go.Figure()
fig.add_trace(go.Candlestick(
    x=dates, open=[c[1] for c in candles], high=[c[2] for c in candles],
    low=[c[3] for c in candles], close=closes, name=TARGET_SYMBOL,
))
fig.add_trace(go.Scatter(x=dates, y=ema50, name="EMA50", line=dict(color="orange", width=1)))
fig.add_trace(go.Scatter(x=dates, y=ema200, name="EMA200", line=dict(color="blue", width=1)))
fig.add_trace(go.Scatter(x=dates, y=bb_upper, name="BB Upper", line=dict(color="gray", width=1, dash="dot")))
fig.add_trace(go.Scatter(x=dates, y=bb_lower, name="BB Lower", line=dict(color="gray", width=1, dash="dot")))

for zone in resistance_zones:
    fig.add_hline(y=zone["level"], line_color="red", line_dash="dash", opacity=0.5,
                   annotation_text=f"R {zone['level']}")
for zone in support_zones:
    fig.add_hline(y=zone["level"], line_color="green", line_dash="dash", opacity=0.5,
                   annotation_text=f"S {zone['level']}")

fig.update_layout(height=600, xaxis_rangeslider_visible=False)
st.plotly_chart(fig, use_container_width=True)

# --- Daily indicators summary ---
st.subheader("5-Minute Technical Indicators")
st.caption(
    "Same period counts as the daily version (EMA50/200, RSI14, BB20, "
    "ATR14, Stochastic14, ADX14, Supertrend 10,3) but applied to 5-minute "
    "bars now, so they represent much shorter real-world spans - e.g. "
    "EMA50 here is roughly the last 4 hours, not the last 50 days."
)
col1, col2, col3, col4 = st.columns(4)
col1.metric("EMA50", f"{ema50[-1]:.2f}" if ema50[-1] else "n/a")
col1.metric("EMA200", f"{ema200[-1]:.2f}" if ema200[-1] else "n/a")
col2.metric("RSI(14)", f"{rsi[-1]:.1f}" if rsi[-1] else "n/a")
col2.metric("ATR(14)", f"{atr[-1]:.2f}" if atr[-1] else "n/a")
col3.metric("Stochastic %K", f"{stoch_k[-1]:.1f}" if stoch_k[-1] else "n/a")
col3.metric("ADX(14)", f"{adx[-1]:.1f}" if adx[-1] else "n/a")
col4.metric("Supertrend", f"{st_val[-1]:.2f}" if st_val[-1] else "n/a", st_dir[-1] if st_dir[-1] else "")
if macd_line[-1] is not None:
    col4.metric("MACD Histogram", f"{macd_line[-1] - macd_signal[-1]:.3f}")

# --- Live data (futures - LTP/VWAP/OI/aggression) ---
st.subheader("Live (Futures)")
if "oi_open" not in st.session_state:
    st.session_state.oi_open = None

if futures_key:
    try:
        quote = fetch_quote(futures_key, access_token)
        if quote:
            ltp = quote.get("last_price")
            vwap = quote.get("average_price")
            volume = quote.get("volume")
            oi_current = quote.get("oi")
            tbq = quote.get("total_buy_quantity")
            tsq = quote.get("total_sell_quantity")

            if st.session_state.oi_open is None and oi_current:
                st.session_state.oi_open = oi_current

            oi_dir = oi_direction(st.session_state.oi_open, oi_current)
            aggr = aggression_label(tbq, tsq)

            lcol1, lcol2, lcol3, lcol4 = st.columns(4)
            lcol1.metric("LTP", ltp)
            lcol2.metric("VWAP", vwap)
            lcol3.metric("OI Direction", oi_dir)
            lcol4.metric("Aggression", aggr)

            append_history_row(
                history_path(TARGET_SYMBOL),
                [datetime.now(IST).strftime("%H:%M:%S"), ltp, vwap, volume, oi_current, tbq, tsq],
            )
        else:
            st.warning("No live quote data returned.")
    except Exception as e:
        st.error(f"Error fetching live quote: {type(e).__name__}: {e}")
else:
    st.warning("No futures_instrument_key found for this stock.")

# --- Live history table ---
hpath = history_path(TARGET_SYMBOL)
if os.path.exists(hpath):
    st.subheader("Live History (today)")
    hdf = pd.read_csv(hpath)
    st.line_chart(hdf[["timestamp", "ltp", "vwap"]].set_index("timestamp"))
    st.dataframe(hdf.sort_values("timestamp", ascending=False), use_container_width=True, hide_index=True)

st.caption(f"Auto-refreshing every {POLL_INTERVAL_SEC}s.")
time.sleep(POLL_INTERVAL_SEC)
st.rerun()
