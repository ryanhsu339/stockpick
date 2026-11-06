#!/usr/bin/env python3
"""Regenerate data/sp500_constituents.json, the repo-committed snapshot of
which companies are in the S&P 500 and each one's sector/industry, that
stock_price.fetch_sp500_industry_returns reads at request time. Run weekly by
its own scheduled GitHub Action.

Membership is scraped from Wikipedia's maintained table, since S&P doesn't
publish it for free. Sector and industry come from Yahoo, the same `info`
fields the Companies tab shows on a ticker's own page, so a stock's heatmap
tile carries the name its page does. That's one request per company, which is
why it happens here and not at request time.

Each company is kept once: the index holds a few companies through two share
classes (GOOGL/GOOG, FOXA/FOX, NWSA/NWS), and Yahoo reports the whole
company's market cap on each class, so keeping both would double-count it.
"""
import json
from concurrent.futures import ThreadPoolExecutor

import requests
import yfinance as yf
from bs4 import BeautifulSoup

from stock_price import SP500_SNAPSHOT_PATH

_WIKIPEDIA_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
_USER_AGENT = "Mozilla/5.0 (compatible; PublicCompanyTracker/1.0)"
_YAHOO_WORKERS = 4
# Well under the index's ~500, just to catch a scrape that "succeeded" on a
# reshaped page -- the snapshot is only written once this passes, so a failed
# run leaves the committed one as it was.
_MIN_COMPANIES = 450


def fetch_sp500_symbols_live():
    """Yahoo-style symbols of the index's companies, one share class each."""
    response = requests.get(_WIKIPEDIA_URL, headers={"User-Agent": _USER_AGENT}, timeout=30)
    response.raise_for_status()
    table = BeautifulSoup(response.text, "html.parser").find("table", id="constituents")
    if table is None:
        raise RuntimeError("Wikipedia's S&P 500 page no longer has a 'constituents' table")

    rows = table.find_all("tr")
    # split/join: the header cells can use non-breaking spaces.
    headers = [" ".join(cell.get_text().split()) for cell in rows[0].find_all("th")]
    symbol_col, cik_col = headers.index("Symbol"), headers.index("CIK")

    symbols, seen_ciks = [], set()
    for row in rows[1:]:
        cells = [cell.get_text(strip=True) for cell in row.find_all("td")]
        if cells[cik_col] in seen_ciks:
            continue
        seen_ciks.add(cells[cik_col])
        # Yahoo writes share classes with a dash (BRK-B, not BRK.B).
        symbols.append(cells[symbol_col].replace(".", "-"))
    return symbols


def _fetch_classification(symbol):
    """{symbol, sector, industry}, or None if Yahoo doesn't have both."""
    try:
        info = yf.Ticker(symbol).info
    except Exception:
        return None
    if not info.get("sector") or not info.get("industry"):
        return None
    return {"symbol": symbol, "sector": info["sector"], "industry": info["industry"]}


def _load_previous_snapshot():
    try:
        companies = json.loads(SP500_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    return {c["symbol"]: c for c in companies}


def main():
    symbols = fetch_sp500_symbols_live()
    previous = _load_previous_snapshot()

    with ThreadPoolExecutor(max_workers=_YAHOO_WORKERS) as pool:
        fetched = dict(zip(symbols, pool.map(_fetch_classification, symbols)))
    # Second, one-at-a-time pass: most misses are Yahoo throttling the burst.
    for symbol in [s for s, company in fetched.items() if company is None]:
        fetched[symbol] = _fetch_classification(symbol)

    # A company Yahoo still won't classify keeps its last known one, so a bad
    # response for it doesn't drop it from the map until next week's run.
    companies, kept, dropped = [], [], []
    for symbol in symbols:
        company = fetched[symbol] or previous.get(symbol)
        if company is None:
            dropped.append(symbol)
            continue
        if fetched[symbol] is None:
            kept.append(symbol)
        companies.append(company)
    if len(companies) < _MIN_COMPANIES:
        raise RuntimeError(f"Only classified {len(companies)} of {len(symbols)} S&P 500 companies")

    SP500_SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SP500_SNAPSHOT_PATH.write_text(json.dumps(companies, indent=1), encoding="utf-8")
    print(f"Wrote {len(companies)} companies to {SP500_SNAPSHOT_PATH}")
    if kept:
        print(f"Kept the previous classification for {len(kept)}: {', '.join(kept)}")
    if dropped:
        print(f"Dropped {len(dropped)} with no classification: {', '.join(dropped)}")


if __name__ == "__main__":
    main()
