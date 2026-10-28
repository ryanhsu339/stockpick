#!/usr/bin/env python3
"""Regenerate the precomputed congress-data snapshots the live app reads
instead of scraping/parsing PTR filings on a real request:
  - data/congress_activity_summary.json (cross-chamber recent trades and
    leaderboard -- see congress_trades.load_activity_summary_snapshot)
  - data/congress_member_positions.json (per-member positions/top
    increases/decreases -- see congress_trades.load_member_positions_snapshot)

Run manually, or on a schedule via
.github/workflows/refresh-data-snapshots.yml -- that workflow commits
and pushes the refreshed files, which Render then auto-deploys.

The Senate's disclosure site (efdsearch.senate.gov) has refused automated
access since 2026-10-23. When it does, House data still refreshes and the
previous snapshot's Senate data is kept as-is, with the activity summary's
"senate_as_of" left at the last date Senate data was actually refreshed
(the Politicians tab shows it once it's stale).
"""
import json
import sys
from datetime import date, datetime
from pathlib import Path

import requests

from congress_trades import (PoliticianDataError, _ensure_senate_agreement, _session_with_ua,
                             build_activity_summary, build_all_member_positions)

ACTIVITY_SNAPSHOT_PATH = Path(__file__).parent / "data" / "congress_activity_summary.json"
MEMBER_POSITIONS_SNAPSHOT_PATH = Path(__file__).parent / "data" / "congress_member_positions.json"

# A fresh Senate build with under half the senators the previous snapshot
# had means the site let the agreement through but refused filings partway
# (per-member failures are skipped silently) -- keep the previous data then.
_MIN_SENATE_FRACTION = 0.5


def _load_previous(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _senate_reachable(session):
    try:
        _ensure_senate_agreement(session)
        return True
    except (PoliticianDataError, requests.RequestException) as e:
        print(f"Senate eFD site unavailable: {e}")
        return False


def _trade_date(trade):
    try:
        return datetime.strptime(trade.get("transaction_date") or "", "%m/%d/%Y").date()
    except ValueError:
        return date.min


def _with_previous_senate(summary, positions, prev_summary, prev_positions):
    """House-only `summary`/`positions` merged with the previous snapshot's
    Senate entries."""
    recent = summary["recent_trades"] + [t for t in prev_summary.get("recent_trades", [])
                                         if t.get("chamber") == "senate"]
    recent.sort(key=_trade_date, reverse=True)
    leaderboard = summary["leaderboard"] + [e for e in prev_summary.get("leaderboard", [])
                                            if e.get("chamber") == "senate"]
    leaderboard.sort(key=lambda e: -e["net_estimated_value"])
    merged_positions = {**positions, **{k: v for k, v in prev_positions.items() if k.startswith("senate|")}}
    return {**summary, "recent_trades": recent, "leaderboard": leaderboard}, merged_positions


def _senate_count(positions):
    return sum(1 for k in positions if k.startswith("senate|"))


def main():
    prev_summary = _load_previous(ACTIVITY_SNAPSHOT_PATH, {})
    prev_positions = _load_previous(MEMBER_POSITIONS_SNAPSHOT_PATH, {})
    session = _session_with_ua(requests.Session())

    senate_ok = _senate_reachable(session)
    chambers = ("house", "senate") if senate_ok else ("house",)

    # max_workers above the library defaults (5 and 10, both sized for
    # production's 512MB instance) is safe here -- this runs on a CI
    # runner with far more memory headroom, and finishing faster matters
    # more than peak memory for a one-shot job.
    summary = build_activity_summary(session=session, force_refresh=True, max_workers=15, chambers=chambers)
    positions = build_all_member_positions(session=session, max_workers=20, chambers=chambers)

    if senate_ok and _senate_count(positions) < _MIN_SENATE_FRACTION * _senate_count(prev_positions):
        print(f"Senate refresh looks partial ({_senate_count(positions)} senators vs "
              f"{_senate_count(prev_positions)} before) -- keeping the previous Senate data.")
        senate_ok = False
        summary["recent_trades"] = [t for t in summary["recent_trades"] if t.get("chamber") != "senate"]
        summary["leaderboard"] = [e for e in summary["leaderboard"] if e.get("chamber") != "senate"]
        positions = {k: v for k, v in positions.items() if not k.startswith("senate|")}

    if senate_ok:
        summary["senate_as_of"] = date.today().isoformat()
    else:
        summary, positions = _with_previous_senate(summary, positions, prev_summary, prev_positions)
        summary["senate_as_of"] = prev_summary.get("senate_as_of")
        # A GitHub Actions warning annotation: visible on the run, without
        # failing it every day while the Senate site stays closed.
        print(f"::warning::Senate data not refreshed; keeping Senate data as of "
              f"{summary['senate_as_of'] or 'an unknown date'}.")

    # Before writing anything: an empty build would otherwise replace good
    # committed snapshots.
    if not positions or not summary["recent_trades"]:
        sys.exit("No member positions or trades at all -- leaving the committed snapshots as they are.")

    ACTIVITY_SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    ACTIVITY_SNAPSHOT_PATH.write_text(json.dumps(summary), encoding="utf-8")
    print(f"Wrote {len(summary['recent_trades'])} recent trades and "
          f"{len(summary['leaderboard'])} leaderboard entries to {ACTIVITY_SNAPSHOT_PATH}")
    MEMBER_POSITIONS_SNAPSHOT_PATH.write_text(json.dumps(positions), encoding="utf-8")
    print(f"Wrote positions for {len(positions)} members ({_senate_count(positions)} senators) "
          f"to {MEMBER_POSITIONS_SNAPSHOT_PATH}")


if __name__ == "__main__":
    main()
