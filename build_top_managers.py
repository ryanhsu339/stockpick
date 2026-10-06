#!/usr/bin/env python3
"""Regenerate data/thirteenf_manager_ranking.json, data/thirteenf_splits.json,
data/thirteenf_top_buys.json and data/thirteenf_top_managers.json.

1. Ranks every 13F filer by total reported portfolio value from SEC's
   latest quarterly Form 13F Data Set (see thirteenf.fetch_sec_13f_ranking)
   and caches the top of that ranking to thirteenf_manager_ranking.json.
2. Fully builds the quarter-over-quarter holdings comparison for the top
   --pool-n (default 1000) of that ranking, and from all of them computes
   the Managers tab's two card lists (Highest Conviction, Fund Size) into
   thirteenf_top_buys.json -- a small file, so the cards can draw on far
   more managers than the live app could hold in memory. Stock splits are
   detected across the whole pool first (thirteenf_splits.json, also used
   by the live app's own lookups) and every comparison restated for them.
3. Keeps only the top --snapshot-n (default 300) of those comparisons in
   thirteenf_top_managers.json, one manager per line, which the live app
   reads one line at a time for instant manager lookups (see
   thirteenf.write_top_managers_snapshot).

Step 2 is the heavy part -- a full two-quarter holdings comparison
(several SEC fetches each, one of them often a large XML document) for
every one of the pool, throttled to stay under SEC's rate limit -- so this
runs on its own weekly schedule (see
.github/workflows/refresh-top-managers.yml) rather than daily.

Refuses to overwrite either file with a result that looks broken (an
unexpectedly short ranking, or far fewer managers than the snapshot it
would replace) and exits non-zero instead, so a bad run fails the
workflow visibly rather than committing an empty Managers tab.
"""
import argparse
import json
import sys

from thirteenf import (_MANAGER_RANKING_PATH, _SPLITS_PATH, _TOP_BUYS_PATH, _TOP_MANAGERS_SNAPSHOT_PATH, _retry,
                       apply_split_adjustments, build_top_managers, detect_split_factors,
                       fetch_sec_13f_ranking, largest_managers_top_buys, top_buys_across_managers,
                       write_top_managers_snapshot)

# Only the top of the ranking is ever used; the full list (~9,000 filers)
# would just bloat the repo.
_RANKING_KEEP = 1000
# A real quarter's data set has thousands of 13F-HR filers.
_MIN_RANKING_SIZE = 1000
# Some top-N comparisons always fail (a filer with no prior-quarter
# 13F-HR to compare against, a transient SEC error), but losing more than
# half the existing snapshot at once means something is broken.
_MIN_SNAPSHOT_RATIO = 0.5
_TOP_BUYS_N = 50
# Each card list should come out full; well short of that means the pool
# mostly failed to build.
_MIN_TOP_BUYS = 40


# A manager's comparison is dropped as unreliable if either quarter's
# values look off by a units mistake in the filing itself (seen in real
# data: Storebrand's Q1 filing priced Amazon at ~$1.9M/share, ~1000x too
# high, which turned an ordinary quarter into "buys" of 13,000%+ of its
# portfolio and pushed every real top buy off the cards).
_MAX_MEDIAN_PRICE_RATIO = 5     # median prev/current share-price ratio, either direction
# Our derived (equity-only) portfolio total vs SEC's own reported total.
# Only "too high" is checked: SEC's total also counts options and bonds,
# which this data skips, so an options-heavy filer (e.g. a market maker)
# legitimately comes in far *below* it -- but nothing can exceed it.
_MAX_TOTAL_OVER_SEC = 2
# A previous filing that shares almost none of the latest's positions
# covered a different (much narrower) set of accounts -- every position
# outside the overlap then reads as brand new. JPMorgan's Q1 2026 filing,
# e.g., was ~1/1300th of its Q2 one, so 98% of Q2 looked newly bought
# (an "$87.6B NVIDIA buy"); the median manager carries over 97%. Only
# applied to managers with enough positions for the share to mean
# anything -- a VC fund holding a handful of fresh IPO stakes (SpaceX,
# 2026) legitimately carries over none.
_MIN_CARRIED_OVER_SHARE = 0.2
_MIN_POSITIONS_FOR_CARRY_CHECK = 50


def _unreliable_reason(comparison, sec_value_b):
    """Why this comparison's numbers can't be trusted, or None if they look sane."""
    positions = comparison.get("all_positions") or []
    ratios = sorted(p["prev_share_price"] / p["share_price"] for p in positions
                    if p.get("share_price") and p.get("prev_share_price"))
    if ratios:
        median = ratios[len(ratios) // 2]
        if not 1 / _MAX_MEDIAN_PRICE_RATIO <= median <= _MAX_MEDIAN_PRICE_RATIO:
            return f"previous quarter's share prices are a median {median:,.1f}x the latest's (units error?)"
    held = [p for p in positions if p.get("shares_m")]
    if len(held) >= _MIN_POSITIONS_FOR_CARRY_CHECK:
        carried = sum(1 for p in held if p.get("prev_shares_m")) / len(held)
        if carried < _MIN_CARRIED_OVER_SHARE:
            return f"only {carried:.0%} of its positions appear in the previous filing (incomplete filing?)"
    total_m = next((p["value_m"] / (p["portfolio_pct"] / 100) for p in positions
                    if p.get("value_m") and p.get("portfolio_pct")), None)
    if total_m and sec_value_b:
        ratio = total_m / (sec_value_b * 1000)
        if ratio > _MAX_TOTAL_OVER_SEC:
            return f"derived portfolio total is {ratio:,.1f}x SEC's reported total (units error?)"
    return None


def _drop_unreliable(results, managers):
    """Removes (in place) and returns {cik: reason} for every comparison
    _unreliable_reason flags."""
    sec_value = {str(m["cik"]): m["value_b"] for m in managers}
    dropped = {}
    for cik, comparison in list(results.items()):
        reason = _unreliable_reason(comparison, sec_value.get(cik))
        if reason:
            dropped[cik] = reason
            del results[cik]
    return dropped


def _existing_snapshot_size():
    try:
        with open(_TOP_MANAGERS_SNAPSHOT_PATH, encoding="utf-8") as f:
            return len(json.load(f))
    except (OSError, json.JSONDecodeError):
        return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pool-n", type=int, default=1000,
                        help="How many of the highest-value 13F filers to build comparisons for and draw the "
                             "top-buys cards from (default: 1000).")
    parser.add_argument("--snapshot-n", type=int, default=300,
                        help="How many of those to keep in the live app's in-memory lookup snapshot "
                             "(default: 300).")
    args = parser.parse_args()

    ranking = _retry(fetch_sec_13f_ranking)
    managers = ranking["managers"]
    print(f"Ranked {len(managers)} 13F filers for period {ranking['period']} from {ranking['source']}")
    if len(managers) < _MIN_RANKING_SIZE:
        sys.exit(f"Refusing to write: only {len(managers)} filers ranked (expected at least {_MIN_RANKING_SIZE}).")

    pool = managers[:args.pool_n]
    results, failures = build_top_managers([m["cik"] for m in pool])
    names = {m["cik"]: m["name"] for m in managers}
    print(f"Built {len(results)} of the top {args.pool_n}; {len(failures)} failed:")
    for cik, error in failures.items():
        print(f"  {cik} {names.get(cik, '')}: {error[:200]}")

    dropped = _drop_unreliable(results, managers)
    print(f"Dropped {len(dropped)} with unreliable numbers:")
    for cik, reason in dropped.items():
        print(f"  {cik} {names.get(int(cik), '')}: {reason}")

    # build_top_managers returns raw (unadjusted) comparisons, so this
    # quarter's splits can be detected across all of them at once -- then
    # every comparison is restated before anything below uses it.
    splits = detect_split_factors(results)
    print(f"Detected {len(splits['factors'])} stock splits between {splits.get('previous_period')} and "
          f"{splits.get('latest_period')}")
    results = {cik: apply_split_adjustments(c, splits) for cik, c in results.items()}

    top_buys = {
        "period": ranking["period"],
        "conviction": top_buys_across_managers(top_n=_TOP_BUYS_N, comparisons=results),
        "fund_size": largest_managers_top_buys(top_n=_TOP_BUYS_N, comparisons=results),
    }
    snapshot_ciks = {str(m["cik"]) for m in pool[:args.snapshot_n]}
    snapshot = {cik: comparison for cik, comparison in results.items() if cik in snapshot_ciks}

    # Every check before any write, so a bad run leaves all three files as they were.
    for key in ("conviction", "fund_size"):
        if len(top_buys[key]) < _MIN_TOP_BUYS:
            sys.exit(f"Refusing to write: only {len(top_buys[key])} {key} top buys (expected {_TOP_BUYS_N}).")
    existing = _existing_snapshot_size()
    if len(snapshot) < existing * _MIN_SNAPSHOT_RATIO:
        sys.exit(f"Refusing to write: built only {len(snapshot)} snapshot managers, versus {existing} in the "
                 f"current snapshot.")

    _MANAGER_RANKING_PATH.parent.mkdir(parents=True, exist_ok=True)
    _MANAGER_RANKING_PATH.write_text(
        json.dumps({**ranking, "managers": managers[:_RANKING_KEEP]}, indent=1), encoding="utf-8")
    print(f"Wrote the top {min(len(managers), _RANKING_KEEP)} filers to {_MANAGER_RANKING_PATH}")
    _SPLITS_PATH.write_text(json.dumps(splits, indent=1), encoding="utf-8")
    _TOP_BUYS_PATH.write_text(json.dumps(top_buys, indent=1), encoding="utf-8")
    print(f"Wrote {len(top_buys['conviction'])} conviction and {len(top_buys['fund_size'])} fund-size "
          f"top buys to {_TOP_BUYS_PATH}")
    write_top_managers_snapshot(snapshot)
    print(f"Wrote {len(snapshot)} of the top {args.snapshot_n} 13F filers to {_TOP_MANAGERS_SNAPSHOT_PATH}")


if __name__ == "__main__":
    main()
