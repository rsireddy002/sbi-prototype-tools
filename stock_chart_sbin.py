"""
Single-Stock Live Chart - PROTOTYPE (starting with SBIN only)
==================================================================
Standalone tool, completely independent of the main scanners (no shared
code path, no risk to anything currently working). Polls just ONE stock's
live quote every 10s via the same proven-reliable REST endpoint the main
polling scanner uses, logs it to its own history CSV (append-only, unlike
the main scanner's eod_summary.csv which gets overwritten each cycle),
and displays a live-updating chart: LTP + VWAP over time, with horizontal
reference lines for the nearest support/resistance zones from today's
prep data.

Run with:
    streamlit run stock_chart_sbin.py

Change TARGET_SYMBOL below to track a different stock instead.
"""
import csv
import json
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st

IST = ZoneInfo("Asia/Kolkata")

TARGET_SYMBOL = "SBIN"  # change this to track a different stock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.txt")
PREP_DIR = os.path.join(SCRIPT_DIR, "prep_output")
HISTORY_DIR = os.path.join(SCRIPT_DIR, "stock_history")
FULL_QUOTES_URL = "https://api.upstox.com/v2/market-quote/quotes"
POLL_INTERVAL_SEC = 10


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


def history_path(symbol):
    os.makedirs(HISTORY_DIR, exist_ok=True)
    date_str = datetime.now(IST).strftime("%Y-%m-%d")
    return os.path.join(HISTORY_DIR, f"{symbol}_{date_str}.csv")


def append_history_row(path, row):
    file_exists = os.path.exists(path)
    with open(path, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["timestamp", "ltp", "vwap", "volume", "oi"])
        writer.writerow(row)


def fetch_quote(instrument_key, access_token):
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    resp = requests.get(
        FULL_QUOTES_URL, headers=headers,
        params={"instrument_key": instrument_key}, timeout=15,
    )
    resp.raise_for_status()
    data = resp.json().get("data", {})
    for entry in data.values():
        if entry.get("instrument_token") == instrument_key:
            return entry
    return None


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
st.set_page_config(page_title=f"{TARGET_SYMBOL} Live Chart", layout="wide")
st.title(f"{TARGET_SYMBOL} - Live Price Chart (prototype)")

entry = load_today_prep_entry(TARGET_SYMBOL)
if entry is None:
    st.error(f"No prep data found for {TARGET_SYMBOL}. Run morning_prep_banknifty.py first.")
    st.stop()

instrument_key = entry.get("futures_instrument_key")
if not instrument_key:
    st.error(f"No futures_instrument_key for {TARGET_SYMBOL} in prep data.")
    st.stop()

config = load_config()
access_token = get_access_token(config)
if not access_token:
    st.error("No ACCESS_TOKEN found in config.txt or Secrets.")
    st.stop()

# Nearest support/resistance zones for reference lines
support_zones = sorted(entry.get("support_zones", []), key=lambda z: -z["level"])
resistance_zones = sorted(entry.get("resistance_zones", []), key=lambda z: z["level"])
nearest_support = support_zones[0]["level"] if support_zones else None
nearest_resistance = resistance_zones[0]["level"] if resistance_zones else None

# Poll and log one fresh data point this run
try:
    quote = fetch_quote(instrument_key, access_token)
    if quote:
        row = [
            datetime.now(IST).strftime("%H:%M:%S"),
            quote.get("last_price"),
            quote.get("average_price"),
            quote.get("volume"),
            quote.get("oi"),
        ]
        append_history_row(history_path(TARGET_SYMBOL), row)
        st.success(f"Connected - LTP: {quote.get('last_price')}, VWAP: {quote.get('average_price')}")
    else:
        st.warning("Quote fetch succeeded but no matching instrument in response.")
except Exception as e:
    st.error(f"Error fetching quote: {type(e).__name__}: {e}")

# Load and display full history for today
hpath = history_path(TARGET_SYMBOL)
if os.path.exists(hpath):
    df = pd.read_csv(hpath)
    col1, col2, col3 = st.columns(3)
    col1.metric("Prev Close", entry.get("prev_close"))
    col2.metric("Nearest Support", nearest_support)
    col3.metric("Nearest Resistance", nearest_resistance)

    st.subheader("LTP vs VWAP over time")
    chart_df = df[["timestamp", "ltp", "vwap"]].set_index("timestamp")
    st.line_chart(chart_df)
    st.caption(
        f"Support zone at {nearest_support} and resistance zone at "
        f"{nearest_resistance} - not drawn as chart lines yet (Streamlit's "
        f"built-in line_chart doesn't support reference lines directly; a "
        f"future version could use Plotly/Altair for this)."
    )

    st.subheader("Volume over time")
    st.bar_chart(df[["timestamp", "volume"]].set_index("timestamp"))

    st.subheader("Raw history")
    st.dataframe(df.sort_values("timestamp", ascending=False), use_container_width=True, hide_index=True)
else:
    st.info("No history yet - this is the first data point, refresh in a moment.")

st.caption(f"Auto-refreshing every {POLL_INTERVAL_SEC}s. History saved to stock_history/{TARGET_SYMBOL}_<date>.csv")
time.sleep(POLL_INTERVAL_SEC)
st.rerun()
