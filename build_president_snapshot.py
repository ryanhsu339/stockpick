#!/usr/bin/env python3
"""Regenerate data/president_trades.json -- the President's stock and ETF
trades from his OGE Form 278-T filings (see president_trades.py) -- which
the Politicians tab's President view reads instead of parsing PDFs on a
real request.

Filings are discovered through OGE's disclosure API and accumulated in
data/president_filings.json (so an API outage still builds from the known
ones). Each filing's PDF is parsed
once and its rows cached in data/president_filing_rows.json; tickers are
re-resolved from those rows on every run, so an edit to
data/president_ticker_overrides.json takes effect on the next run without
re-downloading anything.

Prints the descriptions it couldn't resolve to a ticker, most-traded
first -- the candidates for president_ticker_overrides.json.

Run manually, or daily via .github/workflows/refresh-data-snapshots.yml.
"""
import json
import sys
from collections import Counter

import requests

from congress_trades import _session_with_ua
from president_trades import (ROWS_CACHE_PATH, SNAPSHOT_PATH, TickerResolver, build_snapshot,
                              discover_filings, fetch_filing_rows, load_filings, normalize_name,
                              rows_from_json, rows_to_json, save_filings, to_transactions)


def main():
    session = _session_with_ua(requests.Session())
    known = {f["url"]: f for f in load_filings()}
    try:
        discovered = discover_filings(session=session)
        new = [f for f in discovered if f["url"] not in known]
        print(f"OGE lists {len(discovered)} 278-T filings ({len(new)} new).")
        known.update({f["url"]: f for f in new})
    except (requests.RequestException, ValueError) as e:
        print(f"::warning::OGE disclosure API unavailable ({e}); using the known filings only.")
    filings = list(known.values())
    if not filings:
        sys.exit("No 278-T filings found.")
    try:
        cache = json.loads(ROWS_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}

    failed = 0
    new_urls = set()
    for f in filings:
        if f["url"] in cache:
            continue
        try:
            rows = fetch_filing_rows(f, session=session)
        except (requests.RequestException, ValueError) as e:
            # Skip it this run and keep the rest; a new filing that won't
            # download yet (OGE posts the listing before the PDF at times)
            # is retried tomorrow.
            print(f"::warning::Couldn't fetch/parse {f['url']}: {e}")
            failed += 1
            continue
        if not rows:
            # Not cached, so it's retried every run: most likely a scan
            # with no text layer at all (the 2026-08-12 filing), which
            # needs OCR this parser doesn't do.
            print(f"::warning::No transactions read from the {f['filed']} filing ({f['url']}) -- "
                  "probably a scan without a text layer.")
            failed += 1
            continue
        print(f"Parsed {len(rows)} rows from the {f['filed']} filing.")
        cache[f["url"]] = rows_to_json(rows)
        new_urls.add(f["url"])

    resolver = TickerResolver()
    filings_rows = [(f, to_transactions(rows_from_json(cache[f["url"]]), resolver, f))
                    for f in filings if f["url"] in cache]
    if not any(ts for _f, ts in filings_rows):
        sys.exit("No stock/ETF transactions parsed -- leaving the committed snapshot as it is.")

    unresolved = Counter(normalize_name(t["asset_description"])
                         for _f, ts in filings_rows for t in ts if not t["ticker_resolved"])
    total = sum(len(ts) for _f, ts in filings_rows)
    print(f"{total} stock/ETF trades, {sum(unresolved.values())} without a resolved ticker.")
    for name, n in unresolved.most_common(25):
        print(f"  unresolved ({n}): {name}")

    # Approximate (spelling-similarity) matches in newly parsed filings --
    # usually right, but worth a look; pin a wrong one in
    # president_ticker_overrides.json.
    new_descriptions = {t["asset_description"] for f, ts in filings_rows if f["url"] in new_urls for t in ts}
    for desc, ticker in sorted(resolver.approximate.items()):
        if desc in new_descriptions:
            print(f"  approximate match: {desc} -> {ticker}")

    snapshot = build_snapshot(filings_rows)
    save_filings(filings)
    ROWS_CACHE_PATH.write_text(json.dumps(cache), encoding="utf-8")
    SNAPSHOT_PATH.write_text(json.dumps(snapshot), encoding="utf-8")
    print(f"Wrote {len(snapshot['all_positions'])} positions from {len(filings_rows)} filings "
          f"to {SNAPSHOT_PATH}")
    if failed:
        print(f"::warning::{failed} filing(s) not refreshed this run.")


if __name__ == "__main__":
    main()
