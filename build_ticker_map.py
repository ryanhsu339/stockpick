#!/usr/bin/env python3
"""Regenerate data/ticker_map.json, the repo-committed snapshot of SEC's
full company + ETF/mutual-fund ticker map that company_growth_calc.
load_ticker_map reads at request time instead of hitting SEC live.

company_tickers.json is a few MB; every gunicorn worker fetching it
independently on its own first request meant enough of them cold-starting
around the same time (e.g. right after a deploy) could trip SEC's rate
limit (429 Too Many Requests) for the whole app. Run daily by the
scheduled GitHub Action alongside the other data snapshots.
"""
import json

from company_growth_calc import _TICKER_MAP_SNAPSHOT_PATH, fetch_ticker_map_live


def main():
    companies = fetch_ticker_map_live()

    _TICKER_MAP_SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _TICKER_MAP_SNAPSHOT_PATH.write_text(json.dumps(companies), encoding="utf-8")
    print(f"Wrote {len(companies)} tickers to {_TICKER_MAP_SNAPSHOT_PATH}")


if __name__ == "__main__":
    main()
