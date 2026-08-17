"""
EOD Delivery Analysis - STANDALONE, end-of-day only
=======================================================
Upstox's API does NOT provide delivery data (confirmed directly by their
own developer support forum - only OHLC/volume are available). NSE
publishes delivery quantity/percentage themselves, once per day, after
market close, via their own "Security-wise Delivery Position" bhavcopy.

This script:
1. Downloads that day's NSE delivery bhavcopy (free, public, no API key
   needed - completely separate from Upstox).
2. Filters to your F&O universe, ranks by delivery percentage.
3. Cross-references against today's flagged candidates
   (eod_logs/eod_candidates_<date>.csv) - stocks that BOTH triggered a
   live signal AND had unusually high delivery% are a stronger "real
   conviction, not just intraday noise" signal worth a second look.

Completely independent of the live scanner - run this once after market
close, not during the trading session (NSE only publishes this data
after the day's session settles).

Run with:
    python eod_delivery_analysis.py
    python eod_delivery_analysis.py --date 17-08-2026   (for a past date)

Install deps:
    pip install requests
"""
import argparse
import csv
import io
import os
from datetime import datetime, timedelta

import requests

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
EOD_LOG_DIR = os.path.join(SCRIPT_DIR, "eod_logs")
PREP_DIR = os.path.join(SCRIPT_DIR, "prep_output")

NSE_HOMEPAGE = "https://www.nseindia.com"
NSE_BHAVDATA_URL_TEMPLATE = (
    "https://archives.nseindia.com/products/content/sec_bhavdata_full_{date}.csv"
)

# NSE blocks requests without browser-like headers and a valid session
# (cookies obtained by visiting the homepage first) - a plain requests.get
# straight to the archive URL will usually get rejected.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/csv,application/csv,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/all-reports",
}


def fetch_nse_delivery_csv(date_str):
    """date_str format: DDMMYYYY (NSE's format for this URL)."""
    session = requests.Session()
    session.headers.update(HEADERS)

    # Visit the homepage first to get valid session cookies - NSE rejects
    # direct requests to the archive URL without this.
    session.get(NSE_HOMEPAGE, timeout=15)

    url = NSE_BHAVDATA_URL_TEMPLATE.format(date=date_str)
    resp = session.get(url, timeout=20)
    resp.raise_for_status()

    if b"<html" in resp.content[:200].lower():
        raise ValueError(
            "NSE returned an HTML page instead of CSV - likely no trading "
            "data for this date (holiday/weekend) or bot detection blocked "
            "the request. Try a different date, or check manually at "
            "https://www.nseindia.com/all-reports"
        )
    return resp.text


def parse_delivery_csv(csv_text):
    """Returns {symbol: {'deliv_qty': int, 'deliv_pct': float, 'volume': int,
    'close': float}}. NSE's CSV has trailing whitespace in column names and
    string values, so everything is stripped."""
    reader = csv.DictReader(io.StringIO(csv_text))
    result = {}
    for row in reader:
        row = {k.strip(): (v.strip() if isinstance(v, str) else v) for k, v in row.items()}
        series = row.get("SERIES", "")
        if series != "EQ":
            continue  # only regular equity series, skip other series types
        symbol = row.get("SYMBOL", "")
        try:
            deliv_qty = int(row.get("DELIV_QTY", "0") or 0)
            deliv_pct = float(row.get("DELIV_PER", "0") or 0)
            volume = int(row.get("TTL_TRD_QNTY", "0") or 0)
            close = float(row.get("CLOSE_PRICE", "0") or 0)
        except ValueError:
            continue
        result[symbol] = {
            "deliv_qty": deliv_qty, "deliv_pct": deliv_pct,
            "volume": volume, "close": close,
        }
    return result


def load_fno_universe():
    """Pulls today's (or latest available) F&O stock list from prep_output,
    so we only rank delivery% within stocks we actually track."""
    files = sorted(
        f for f in os.listdir(PREP_DIR) if f.startswith("banknifty_prep_") and f.endswith(".json")
    )
    if not files:
        return set()
    import json
    with open(os.path.join(PREP_DIR, files[-1])) as f:
        data = json.load(f)
    return set(data.get("stocks", {}).keys())


def load_todays_candidates(date_str_iso):
    """date_str_iso format: YYYY-MM-DD (matches the scanner's CSV naming)."""
    path = os.path.join(EOD_LOG_DIR, f"eod_candidates_{date_str_iso}.csv")
    if not os.path.exists(path):
        return set()
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    return {r["symbol"] for r in rows}


def main():
    parser = argparse.ArgumentParser(description="EOD delivery analysis, cross-referenced with today's candidates")
    parser.add_argument(
        "--date", default=None,
        help="Date in DD-MM-YYYY format (default: today). Must be a trading day.",
    )
    args = parser.parse_args()

    if args.date:
        target_date = datetime.strptime(args.date, "%d-%m-%Y")
    else:
        target_date = datetime.now()

    nse_date_str = target_date.strftime("%d%m%Y")
    iso_date_str = target_date.strftime("%Y-%m-%d")

    print(f"Fetching NSE delivery data for {target_date.strftime('%d-%m-%Y')}...")
    try:
        csv_text = fetch_nse_delivery_csv(nse_date_str)
    except Exception as e:
        print(f"ERROR fetching NSE data: {e}")
        return

    all_delivery = parse_delivery_csv(csv_text)
    print(f"Parsed delivery data for {len(all_delivery)} stocks (all series=EQ).")

    fno_universe = load_fno_universe()
    if fno_universe:
        fno_delivery = {s: d for s, d in all_delivery.items() if s in fno_universe}
        print(f"Filtered to {len(fno_delivery)} stocks in today's F&O universe.")
    else:
        fno_delivery = all_delivery
        print("WARNING: no prep_output F&O universe found - showing all EQ stocks instead.")

    ranked = sorted(fno_delivery.items(), key=lambda kv: kv[1]["deliv_pct"], reverse=True)

    print(f"\n=== Top 20 by Delivery % (F&O universe, {target_date.strftime('%d-%m-%Y')}) ===")
    print(f"{'Symbol':<14} {'Deliv%':>8} {'Deliv Qty':>14} {'Volume':>14} {'Close':>10}")
    print("-" * 65)
    for symbol, d in ranked[:20]:
        print(f"{symbol:<14} {d['deliv_pct']:>7.2f}% {d['deliv_qty']:>14,} {d['volume']:>14,} {d['close']:>10.2f}")

    candidates_today = load_todays_candidates(iso_date_str)
    if candidates_today:
        overlap = [(s, d) for s, d in ranked if s in candidates_today]
        print(f"\n=== Stocks that were BOTH flagged as candidates today AND in top delivery% ===")
        if overlap:
            print(f"{'Symbol':<14} {'Deliv%':>8} {'Rank in Deliv%':>16}")
            print("-" * 45)
            for symbol, d in overlap[:20]:
                rank = next(i for i, (s, _) in enumerate(ranked, 1) if s == symbol)
                print(f"{symbol:<14} {d['deliv_pct']:>7.2f}% {rank:>16}")
        else:
            print("  None - no overlap between today's candidates and high-delivery stocks.")
    else:
        print(f"\nNo candidates CSV found for {iso_date_str} - run the live scanner during "
              f"market hours first if you want the cross-reference.")

    out_path = os.path.join(EOD_LOG_DIR, f"delivery_analysis_{iso_date_str}.csv")
    os.makedirs(EOD_LOG_DIR, exist_ok=True)
    with open(out_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["symbol", "deliv_pct", "deliv_qty", "volume", "close", "was_candidate_today"])
        for symbol, d in ranked:
            writer.writerow([
                symbol, d["deliv_pct"], d["deliv_qty"], d["volume"], d["close"],
                symbol in candidates_today,
            ])
    print(f"\nFull ranked list saved to: {out_path}")


if __name__ == "__main__":
    main()
