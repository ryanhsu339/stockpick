#!/usr/bin/env python3
"""Regenerate data/congress_activity_summary.json, the precomputed snapshot
the live app reads instead of scraping/parsing PTR filings on a real
request (see congress_trades.load_activity_summary_snapshot for why).

Run manually, or on a schedule via
.github/workflows/refresh-congress-snapshot.yml -- that workflow commits
and pushes the refreshed file, which Render then auto-deploys.
"""
import json
from pathlib import Path

from congress_trades import build_activity_summary

SNAPSHOT_PATH = Path(__file__).parent / "data" / "congress_activity_summary.json"


def main():
    # max_workers=15 (vs. the library default of 5, sized for production's
    # 512MB instance) is safe here -- this runs on a CI runner with far
    # more memory headroom, and finishing faster matters more than peak
    # memory for a one-shot job.
    summary = build_activity_summary(force_refresh=True, max_workers=15)
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_PATH.write_text(json.dumps(summary), encoding="utf-8")
    print(f"Wrote {len(summary['recent_trades'])} recent trades and "
          f"{len(summary['leaderboard'])} leaderboard entries to {SNAPSHOT_PATH}")


if __name__ == "__main__":
    main()
