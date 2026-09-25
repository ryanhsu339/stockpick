#!/usr/bin/env python3
"""Regenerate data/thirteenf_manager_directory.json, the local directory
of every SEC 13F-HR filer's CIK and name that thirteenf.search_managers
searches locally instead of querying SEC live on every keystroke.

Run with --quarters-back 8 (or more) once, for a full backfill. After
that, the scheduled GitHub Action runs it with the default of 1 -- a
filer only needs to show up in one recent quarter's index to stay
listed, so re-scanning the whole history every day would just waste
bandwidth. Existing entries not seen in the scanned window are kept
as-is; only filers actually seen get their name refreshed (catches
renames, e.g. Balyasny Asset Management -> Longaeva Partners L.P.).
"""
import argparse
import json

from thirteenf import _MANAGER_DIRECTORY_PATH, build_manager_directory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quarters-back", type=int, default=1,
                         help="How many recent quarters to scan and merge in (default: 1).")
    args = parser.parse_args()

    existing = {}
    if _MANAGER_DIRECTORY_PATH.exists():
        with open(_MANAGER_DIRECTORY_PATH, encoding="utf-8") as f:
            for m in json.load(f):
                existing[m["cik"]] = m["name"]

    fresh = build_manager_directory(quarters_back=args.quarters_back)
    for m in fresh:
        existing[m["cik"]] = m["name"]

    directory = [{"cik": cik, "name": name} for cik, name in existing.items()]
    directory.sort(key=lambda m: m["name"].lower())

    _MANAGER_DIRECTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    _MANAGER_DIRECTORY_PATH.write_text(json.dumps(directory), encoding="utf-8")
    print(f"Wrote {len(directory)} managers ({len(fresh)} seen in the last "
          f"{args.quarters_back} quarter(s)) to {_MANAGER_DIRECTORY_PATH}")


if __name__ == "__main__":
    main()
