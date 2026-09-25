#!/usr/bin/env python3
"""Regenerate data/thirteenf_top_managers.json: fully precomputed
build_holdings_comparison results for the top N managers by AUM, ranked
from SEC data across every manager in the local search directory (see
thirteenf.build_top_managers / get_latest_total_value) -- guaranteeing
every result is a real, already-searchable 13F-HR filer.

This is much heavier than the other snapshot scripts -- a lightweight
AUM-ranking fetch for every manager in the directory (thousands of
them), then a full two-quarter holdings comparison (several SEC
fetches each, one of them often a large XML document) for the top N --
so it runs on its own weekly schedule (see
.github/workflows/refresh-top-managers.yml) rather than daily.
"""
import argparse
import json

from thirteenf import _TOP_MANAGERS_SNAPSHOT_PATH, build_top_managers


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top-n", type=int, default=300,
                         help="How many of the highest-AUM managers to fully precompute (default: 300).")
    args = parser.parse_args()

    results = build_top_managers(top_n_managers=args.top_n)

    _TOP_MANAGERS_SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _TOP_MANAGERS_SNAPSHOT_PATH.write_text(json.dumps(results), encoding="utf-8")
    print(f"Wrote {len(results)} of the top {args.top_n} managers by AUM to {_TOP_MANAGERS_SNAPSHOT_PATH}")


if __name__ == "__main__":
    main()
