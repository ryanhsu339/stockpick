#!/usr/bin/env python3
"""Add today's entry to data/options_ratio_history.json: call and put open
interest, over the next 30 days of expirations, for every S&P 500 stock.
stock_price.load_options_movers compares the two latest entries for the
Companies tab's Call/Put Ratio view of Top Gainers/Losers. Run each weekday
before the US open by its own scheduled GitHub Action.

Yahoo keeps no open-interest history and has no screener for it, so each
stock costs one request for its expiration list plus one per expiration
(about 3,000-4,000 in all). The requests are throttled -- a few workers, a
short pause before each one -- and the misses get a second, one-at-a-time
pass, since most of them are Yahoo pushing back on the burst.

Open interest only changes once a session settles. If nearly every stock's
numbers match the latest entry's (the market was closed yesterday, or Yahoo
hasn't posted the new numbers yet), no entry is added, so the comparison
stays between two real sessions.
"""
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

from stock_price import OPTIONS_HISTORY_PATH, SP500_SNAPSHOT_PATH, fetch_open_interest

_YAHOO_WORKERS = 3
_REQUEST_PAUSE = 0.25
_DAYS_KEPT = 5
# Well under the index's ~500: a run that only reached a fraction of it is
# more likely Yahoo blocking it than real data, and isn't saved.
_MIN_STOCKS = 400
# Share of stocks whose numbers must match the latest entry's for this run
# to count as "nothing new" -- not 100%, since Yahoo can revise a few.
_UNCHANGED_SHARE = 0.9


def _fetch(symbol):
    """[call_oi, put_oi], or None if Yahoo failed or there are no options."""
    try:
        call_oi, put_oi = fetch_open_interest(symbol, pause=_REQUEST_PAUSE)
    except Exception:
        return None
    if not call_oi and not put_oi:
        return None
    return [call_oi, put_oi]


def _load_days():
    try:
        return json.loads(OPTIONS_HISTORY_PATH.read_text(encoding="utf-8"))["days"]
    except FileNotFoundError:
        return []


def main():
    symbols = [c["symbol"] for c in json.loads(SP500_SNAPSHOT_PATH.read_text(encoding="utf-8"))]
    started = time.time()
    with ThreadPoolExecutor(max_workers=_YAHOO_WORKERS) as pool:
        fetched = dict(zip(symbols, pool.map(_fetch, symbols)))
    misses = [s for s, oi in fetched.items() if oi is None]
    for symbol in misses:
        time.sleep(1)
        fetched[symbol] = _fetch(symbol)
    oi = {s: v for s, v in fetched.items() if v is not None}
    print(f"Fetched open interest for {len(oi)} of {len(symbols)} stocks "
          f"in {time.time() - started:.0f}s ({len(misses)} needed a retry)")
    if len(oi) < _MIN_STOCKS:
        raise RuntimeError(f"Only got open interest for {len(oi)} of {len(symbols)} S&P 500 stocks")

    today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    # A re-run on the same day replaces that day's entry rather than
    # comparing against it.
    days = [d for d in _load_days() if d["date"] != today]
    if days:
        latest = days[-1]["oi"]
        shared = [s for s in oi if s in latest]
        unchanged = sum(oi[s] == latest[s] for s in shared)
        if shared and unchanged / len(shared) >= _UNCHANGED_SHARE:
            print(f"{unchanged} of {len(shared)} stocks unchanged since {days[-1]['date']} -- "
                  "no new session to record, leaving the history as it was")
            return

    days.append({"date": today, "oi": oi})
    days = days[-_DAYS_KEPT:]
    OPTIONS_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    # One line per day keeps the daily commit's diff readable.
    lines = ",\n".join(json.dumps(d, separators=(",", ":")) for d in days)
    OPTIONS_HISTORY_PATH.write_text('{"days": [\n' + lines + "\n]}\n", encoding="utf-8")
    print(f"Wrote {today} to {OPTIONS_HISTORY_PATH} ({len(days)} days kept)")


if __name__ == "__main__":
    main()
