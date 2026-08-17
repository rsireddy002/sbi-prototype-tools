"""
Full Market Screener - all 208 stocks, complete data table, plus a
specific filter for "at support with real room to run before resistance"
=================================================================================
Reuses the main scanner's proven SymbolState class and scoring logic
directly (imported, not reimplemented) - lower risk of introducing new
bugs in re-derived calculations. Adds the columns you asked for (Sl No,
LTP, prev_close, change%, VWAP, RVOL, OI, TBQ, TSQ, aggression, nearest
support/resistance levels, RSI, MACD) plus the specific setup you're
looking for: HIGH SCORE candidates that are genuinely bouncing off
support AND have a resistance level at least ~1-2% away (meaning real
room to run before hitting a ceiling, not immediately capped).

Standalone - imports from live_scanner_polling.py but doesn't modify or
restart it; safe to run alongside your existing running scanner.

Run with:
    python full_market_screener.py

Change MIN_RESISTANCE_ROOM_PCT below to adjust the "how much room counts
as enough" threshold.
"""
import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

# Reusing the main scanner's proven, tested classes/functions directly
from live_scanner_polling import (
    SymbolState, evaluate, load_config, get_access_token,
    load_today_prep, fetch_all_quotes, update_states_from_quotes,
    NEAR_ZONE_PCT, RVOL_THRESHOLD,
)

IST = ZoneInfo("Asia/Kolkata")
MIN_RESISTANCE_ROOM_PCT = 1.0  # "at least ~1-2% away" - using 1.0% as the practical floor


def nearest_support_and_resistance(state):
    """Unlike nearest_zone() (which returns only the single closest zone
    overall), we specifically want BOTH the nearest support AND nearest
    resistance separately, since the filter needs to compare them
    against each other (not just find whichever is closest)."""
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


def main():
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

    print(f"Fetching live quotes for {len(instrument_keys)} stocks...")
    quotes = fetch_all_quotes(instrument_keys, access_token)
    updated = update_states_from_quotes(quotes, states_by_key)
    print(f"Updated {updated}/{len(states)} stocks.\n")

    # Reuse the main scanner's proven scoring - gives us score, pattern,
    # bias etc. for every stock that currently qualifies (near a zone,
    # RVOL above threshold).
    candidates = evaluate(states)
    candidates_by_symbol = {c["symbol"]: c for c in candidates}

    rows = []
    for i, (symbol, state) in enumerate(states.items(), 1):
        if state.last_price is None:
            continue

        support_level, support_dist, resistance_level, resistance_dist = nearest_support_and_resistance(state)
        candidate = candidates_by_symbol.get(symbol)
        ti = state.trend_indicators or {}

        rows.append({
            "sl_no": i,
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
            "macd_line": ti.get("macd_line"),
            "macd_signal": ti.get("macd_signal"),
            "score": candidate["score"] if candidate else None,
            "pattern": candidate["pattern"] if candidate else None,
        })

    # --- Full table, sorted by score (candidates first, highest first) ---
    rows_sorted = sorted(rows, key=lambda r: (r["score"] is None, -(r["score"] or 0)))

    print(f"{'#':>3} {'Symbol':<12} {'LTP':>9} {'Chg%':>7} {'RVOL':>6} {'Support':>9} {'S-dist%':>8} "
          f"{'Resist':>9} {'R-dist%':>8} {'RSI':>6} {'Score':>8} {'Pattern':<28}")
    print("-" * 125)
    for r in rows_sorted[:40]:
        print(
            f"{r['sl_no']:>3} {r['symbol']:<12} {r['ltp'] or 0:>9.2f} "
            f"{r['change_pct'] if r['change_pct'] is not None else 0:>7.2f} "
            f"{r['rvol'] if r['rvol'] is not None else 0:>6.2f} "
            f"{r['support'] or 0:>9.2f} {r['support_dist_pct'] if r['support_dist_pct'] is not None else 0:>8.2f} "
            f"{r['resistance'] or 0:>9.2f} {r['resistance_dist_pct'] if r['resistance_dist_pct'] is not None else 0:>8.2f} "
            f"{r['rsi14'] or 0:>6.1f} {r['score'] if r['score'] is not None else 0:>8.2f} {r['pattern'] or '-':<28}"
        )

    # --- THE SPECIFIC FILTER: support_bounce pattern AND resistance has
    # real room (>= MIN_RESISTANCE_ROOM_PCT away) ---
    quality_setups = [
        r for r in rows
        if r["pattern"] == "support_bounce"
        and r["resistance_dist_pct"] is not None
        and r["resistance_dist_pct"] >= MIN_RESISTANCE_ROOM_PCT
    ]
    quality_setups.sort(key=lambda r: -(r["score"] or 0))

    print(f"\n\n=== QUALITY SETUPS: at support (bouncing) AND resistance >= {MIN_RESISTANCE_ROOM_PCT}% away ===")
    if not quality_setups:
        print("None right now - either no support_bounce candidates, or resistance is too close on all of them.")
    else:
        print(f"{'Symbol':<12} {'LTP':>9} {'Support':>9} {'S-dist%':>8} {'Resist':>9} {'R-dist%':>8} {'Score':>8}")
        print("-" * 65)
        for r in quality_setups:
            print(
                f"{r['symbol']:<12} {r['ltp']:>9.2f} {r['support']:>9.2f} {r['support_dist_pct']:>8.2f} "
                f"{r['resistance']:>9.2f} {r['resistance_dist_pct']:>8.2f} {r['score']:>8.2f}"
            )

    # --- Save full table to CSV ---
    import csv
    out_path = os.path.join(SCRIPT_DIR, "eod_logs", f"full_screener_{datetime.now(IST).strftime('%Y-%m-%d_%H%M%S')}.csv")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else [])
        writer.writeheader()
        writer.writerows(rows_sorted)
    print(f"\nFull table saved to: {out_path}")


if __name__ == "__main__":
    main()
