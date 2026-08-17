"""
SBI Technical Analysis - PROTOTYPE (comprehensive indicator set, one stock)
================================================================================
Fetches SBIN's full historical daily data and computes a broader set of
technical indicators than the main scanner uses: existing ones (EMA50/200,
RSI14, MACD) plus new ones (Bollinger Bands, ATR, Stochastic Oscillator,
ADX, Supertrend). Standalone script, doesn't touch any existing files.

Run with:
    python sbi_technical_analysis.py

Change TARGET_SYMBOL below to analyze a different stock instead.
"""
import json
import os
from datetime import datetime, timedelta

import requests

TARGET_SYMBOL = "SBIN"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.txt")
PREP_DIR = os.path.join(SCRIPT_DIR, "prep_output")
DAILY_LOOKBACK_DAYS = 450  # ~15 months, same as main prep script


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


def get_access_token():
    config = load_config()
    return config.get("ACCESS_TOKEN") or config.get("UPSTOX_ACCESS_TOKEN")


def get_instrument_key(symbol):
    """Pulls the cash-market instrument_key from today's prep JSON, since
    we already resolved it there - avoids re-downloading the instrument
    master just for one stock."""
    files = sorted(
        f for f in os.listdir(PREP_DIR) if f.startswith("banknifty_prep_") and f.endswith(".json")
    )
    if not files:
        raise FileNotFoundError("No prep_output files found - run morning_prep_banknifty.py first.")
    with open(os.path.join(PREP_DIR, files[-1])) as f:
        data = json.load(f)
    entry = data.get("stocks", {}).get(symbol)
    if not entry:
        raise ValueError(f"{symbol} not found in prep data.")
    return entry.get("instrument_key")


def fetch_daily_candles(instrument_key, access_token, days_back=DAILY_LOOKBACK_DAYS):
    to_date = datetime.now().strftime("%Y-%m-%d")
    from_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    url = f"https://api.upstox.com/v3/historical-candle/{instrument_key}/days/1/{to_date}/{from_date}"
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    resp = requests.get(url, headers=headers, timeout=20)
    resp.raise_for_status()
    candles = resp.json().get("data", {}).get("candles", [])
    candles.sort(key=lambda c: c[0])
    return candles  # [timestamp, open, high, low, close, volume, oi]


# ---------------------------------------------------------------------------
# Existing indicators (same proven math as morning_prep_banknifty.py)
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


# ---------------------------------------------------------------------------
# NEW indicators
# ---------------------------------------------------------------------------
def sma_series(values, period):
    result = [None] * (period - 1)
    for i in range(period - 1, len(values)):
        result.append(sum(values[i - period + 1:i + 1]) / period)
    return result


def bollinger_bands(closes, period=20, num_std=2):
    """Returns (middle, upper, lower) series."""
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


def true_range_series(candles):
    """candles: [timestamp, open, high, low, close, volume, oi]."""
    tr = [None]
    for i in range(1, len(candles)):
        high, low = candles[i][2], candles[i][3]
        prev_close = candles[i - 1][4]
        tr.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return tr


def atr_series(candles, period=14):
    """Wilder's smoothed ATR - same smoothing pattern as RSI."""
    tr = true_range_series(candles)
    n = len(tr)
    result = [None] * n
    if n < period + 1:
        return result
    valid_tr = tr[1:period + 1]  # tr[0] is always None
    avg_tr = sum(valid_tr) / period
    result[period] = avg_tr
    prev = avg_tr
    for i in range(period + 1, n):
        current = (prev * (period - 1) + tr[i]) / period
        result[i] = current
        prev = current
    return result


def stochastic_oscillator(candles, k_period=14, d_period=3):
    """Returns (%K series, %D series)."""
    n = len(candles)
    k_values = [None] * n
    for i in range(k_period - 1, n):
        window = candles[i - k_period + 1:i + 1]
        highest_high = max(c[2] for c in window)
        lowest_low = min(c[3] for c in window)
        close = candles[i][4]
        if highest_high == lowest_low:
            k_values[i] = 50.0  # avoid divide-by-zero on a flat window
        else:
            k_values[i] = (close - lowest_low) / (highest_high - lowest_low) * 100
    valid_k = [v for v in k_values if v is not None]
    d_valid = sma_series(valid_k, d_period)
    none_count = len(k_values) - len(valid_k)
    d_values = [None] * none_count + d_valid
    return k_values, d_values


def adx_series(candles, period=14):
    """Average Directional Index - trend strength (not direction). Returns
    (adx, plus_di, minus_di) series."""
    n = len(candles)
    if n < period * 2:
        return [None] * n, [None] * n, [None] * n

    plus_dm_raw = [None]
    minus_dm_raw = [None]
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

    plus_di = [None] * n
    minus_di = [None] * n
    dx = [None] * n
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
    """Returns (supertrend_value series, direction series: 'up'/'down')."""
    n = len(candles)
    atr = atr_series(candles, period)
    supertrend = [None] * n
    direction = [None] * n

    final_upper = [None] * n
    final_lower = [None] * n

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
                supertrend[i] = final_upper[i]
                direction[i] = "down"
            else:
                supertrend[i] = final_lower[i]
                direction[i] = "up"
        elif prev_direction == "down":
            if close <= final_upper[i]:
                supertrend[i] = final_upper[i]
                direction[i] = "down"
            else:
                supertrend[i] = final_lower[i]
                direction[i] = "up"
        else:
            if close >= final_lower[i]:
                supertrend[i] = final_lower[i]
                direction[i] = "up"
            else:
                supertrend[i] = final_upper[i]
                direction[i] = "down"

    return supertrend, direction


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():
    access_token = get_access_token()
    if not access_token:
        print("ERROR: No ACCESS_TOKEN found in config.txt")
        return

    print(f"Resolving instrument key for {TARGET_SYMBOL}...")
    instrument_key = get_instrument_key(TARGET_SYMBOL)
    print(f"  -> {instrument_key}")

    print(f"Fetching {DAILY_LOOKBACK_DAYS} days of daily candles...")
    candles = fetch_daily_candles(instrument_key, access_token)
    print(f"  -> {len(candles)} candles received")

    closes = [c[4] for c in candles]

    print(f"\n=== {TARGET_SYMBOL} Technical Analysis - {datetime.now().strftime('%Y-%m-%d %H:%M')} ===")
    print(f"Latest close: {closes[-1]}")
    print()

    ema50 = ema_series(closes, 50)
    ema200 = ema_series(closes, 200)
    rsi = rsi_series(closes, 14)
    macd_line, macd_signal = macd_series(closes)
    print("--- Existing (already in main scanner) ---")
    print(f"EMA50: {ema50[-1]:.2f}" if ema50[-1] is not None else "EMA50: n/a")
    print(f"EMA200: {ema200[-1]:.2f}" if ema200[-1] is not None else "EMA200: n/a")
    print(f"RSI(14): {rsi[-1]:.2f}" if rsi[-1] is not None else "RSI(14): n/a")
    if macd_line[-1] is not None and macd_signal[-1] is not None:
        print(f"MACD line: {macd_line[-1]:.3f}, Signal: {macd_signal[-1]:.3f}, Histogram: {macd_line[-1] - macd_signal[-1]:.3f}")

    print("\n--- NEW ---")
    bb_mid, bb_upper, bb_lower = bollinger_bands(closes)
    if bb_mid[-1] is not None:
        print(f"Bollinger Bands(20,2): Lower={bb_lower[-1]:.2f}, Mid={bb_mid[-1]:.2f}, Upper={bb_upper[-1]:.2f}")
        pct_b = (closes[-1] - bb_lower[-1]) / (bb_upper[-1] - bb_lower[-1]) * 100 if bb_upper[-1] != bb_lower[-1] else 50
        print(f"  %B (position within bands): {pct_b:.1f}% (0=at lower band, 100=at upper band)")

    atr = atr_series(candles, 14)
    if atr[-1] is not None:
        print(f"ATR(14): {atr[-1]:.2f} ({atr[-1] / closes[-1] * 100:.2f}% of current price)")

    stoch_k, stoch_d = stochastic_oscillator(candles)
    if stoch_k[-1] is not None and stoch_d[-1] is not None:
        print(f"Stochastic(14,3): %K={stoch_k[-1]:.1f}, %D={stoch_d[-1]:.1f}", end="")
        if stoch_k[-1] > 80:
            print(" (overbought)")
        elif stoch_k[-1] < 20:
            print(" (oversold)")
        else:
            print(" (neutral)")

    adx, plus_di, minus_di = adx_series(candles, 14)
    if adx[-1] is not None:
        trend_strength = "strong" if adx[-1] > 25 else "weak/ranging"
        direction_bias = "bullish" if plus_di[-1] > minus_di[-1] else "bearish"
        print(f"ADX(14): {adx[-1]:.1f} ({trend_strength} trend), +DI={plus_di[-1]:.1f}, -DI={minus_di[-1]:.1f} ({direction_bias} bias)")

    st_value, st_direction = supertrend_series(candles, 10, 3)
    if st_value[-1] is not None:
        print(f"Supertrend(10,3): {st_value[-1]:.2f} (direction: {st_direction[-1]})")

    print(f"\nDone - full history: {len(candles)} candles analyzed.")


if __name__ == "__main__":
    main()
