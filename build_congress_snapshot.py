#!/usr/bin/env python3
"""Regenerate the precomputed congress-data snapshots the live app reads
instead of scraping/parsing PTR filings on a real request:
  - data/congress_activity_summary.json (cross-chamber recent trades and
    leaderboard -- see congress_trades.load_activity_summary_snapshot)
  - data/congress_member_positions.json (per-member positions/top
    increases/decreases -- see congress_trades.load_member_positions_snapshot)

Run manually, or on a schedule via
.github/workflows/refresh-congress-snapshot.yml -- that workflow commits
and pushes the refreshed files, which Render then auto-deploys.
"""
import json
from pathlib import Path

from congress_trades import build_activity_summary, build_all_member_positions

ACTIVITY_SNAPSHOT_PATH = Path(__file__).parent / "data" / "congress_activity_summary.json"
MEMBER_POSITIONS_SNAPSHOT_PATH = Path(__file__).parent / "data" / "congress_member_positions.json"


def main():
    # max_workers above the library defaults (5 and 10, both sized for
    # production's 512MB instance) is safe here -- this runs on a CI
    # runner with far more memory headroom, and finishing faster matters
    # more than peak memory for a one-shot job.
    summary = build_activity_summary(force_refresh=True, max_workers=15)
    ACTIVITY_SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    ACTIVITY_SNAPSHOT_PATH.write_text(json.dumps(summary), encoding="utf-8")
    print(f"Wrote {len(summary['recent_trades'])} recent trades and "
          f"{len(summary['leaderboard'])} leaderboard entries to {ACTIVITY_SNAPSHOT_PATH}")

    positions = build_all_member_positions(max_workers=20)
    MEMBER_POSITIONS_SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    MEMBER_POSITIONS_SNAPSHOT_PATH.write_text(json.dumps(positions), encoding="utf-8")
    print(f"Wrote positions for {len(positions)} members to {MEMBER_POSITIONS_SNAPSHOT_PATH}")


if __name__ == "__main__":
    main()
