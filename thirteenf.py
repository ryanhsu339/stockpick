#!/usr/bin/env python3
"""
thirteenf.py — pull an institutional investment manager's 13F-HR holdings
from SEC EDGAR and compare the two most recent quarters.

13F filings are a different shape than the XBRL "company facts" data used
by company_growth_calc.py: holdings live in a per-filing "information
table" XML, keyed by CUSIP (not ticker), reported directly under the
manager's own CIK (not the companies it holds). There is no free, official
SEC mapping from CUSIP to ticker, so holdings are identified here by their
SEC-filed issuer name rather than a ticker symbol.

KNOWN LIMITATIONS
    - Security names are the issuer name as the manager typed it into the
      filing (e.g. "ADVANCED MICRO DEVICES INC"), not a ticker — there's no
      free official CUSIP->ticker mapping to resolve one.
    - Only the two most recent non-amendment 13F-HR filings are compared
      (13F-HR/A amendments are skipped); a manager who only just started
      filing won't have a prior quarter to compare against.
    - "Value" is SEC's reported market value for the position. Filings for
      reporting periods before Q1 2023 stated this in thousands of dollars
      rather than whole dollars; get_filing_holdings converts based on
      each filing's own period-of-report, so this is handled correctly
      even for an inactive/deregistered manager whose latest filing on
      file predates the switch.
"""

import concurrent.futures
import csv
import html
import io
import json
import os
import re
import sqlite3
import threading
import time
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests

USER_AGENT = "ryan.hsu1993@gmail.com"  # <-- put your real contact here (SEC fair-access policy)

BROWSE_URL = "https://www.sec.gov/cgi-bin/browse-edgar"
ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data"
FULL_INDEX_URL = "https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{quarter}/form.idx"

_ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}
_INFOTABLE_NS = {"n": "http://www.sec.gov/edgar/document/thirteenf/informationtable"}

# Both manager-name search and per-CIK holdings comparisons hit SEC's live
# endpoints with no caching of their own, so every keystroke and every
# manager selection re-pays that latency even for a manager someone already
# looked up seconds ago. This mirrors congress_trades.py's disk cache: a
# manager's filer-directory listing and its quarter-over-quarter holdings
# both change at most quarterly, so a same-day cache hit is always safe.
_CACHE_DIR = Path(os.environ.get("CACHE_DIR", Path(__file__).parent))
_CACHE_DB_PATH = _CACHE_DIR / "thirteenf_cache.db"
SEARCH_CACHE_TTL_HOURS = 24
HOLDINGS_CACHE_TTL_HOURS = 24


def _cache_db():
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_CACHE_DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS manager_search ("
        "query TEXT PRIMARY KEY, data TEXT NOT NULL, built_at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS holdings_comparison ("
        "cik INTEGER PRIMARY KEY, data TEXT NOT NULL, built_at TEXT NOT NULL)"
    )
    return conn


def _cache_fresh_data(row, max_age_hours):
    if not row:
        return None
    data_json, built_at_str = row
    try:
        built_at = datetime.fromisoformat(built_at_str)
        # A naive timestamp can't be compared against an aware "now" --
        # treat it as unreadable/stale rather than crashing.
        if built_at.tzinfo is None:
            return None
        if datetime.now(timezone.utc) - built_at > timedelta(hours=max_age_hours):
            return None
    except ValueError:
        return None
    try:
        return json.loads(data_json)
    except (TypeError, ValueError):
        return None


def _load_manager_search_from_disk(query, max_age_hours=SEARCH_CACHE_TTL_HOURS):
    try:
        with _cache_db() as conn:
            row = conn.execute(
                "SELECT data, built_at FROM manager_search WHERE query = ?", (query,)
            ).fetchone()
    except sqlite3.Error:
        return None
    return _cache_fresh_data(row, max_age_hours)


def _save_manager_search_to_disk(query, matches):
    try:
        with _cache_db() as conn:
            conn.execute(
                "INSERT INTO manager_search (query, data, built_at) VALUES (?, ?, ?) "
                "ON CONFLICT(query) DO UPDATE SET data = excluded.data, built_at = excluded.built_at",
                (query, json.dumps(matches), datetime.now(timezone.utc).isoformat()),
            )
    except sqlite3.Error:
        pass  # best-effort -- an unwritable cache file shouldn't break the search box


def _load_holdings_comparison_from_disk(cik, max_age_hours=HOLDINGS_CACHE_TTL_HOURS):
    try:
        with _cache_db() as conn:
            row = conn.execute(
                "SELECT data, built_at FROM holdings_comparison WHERE cik = ?", (cik,)
            ).fetchone()
    except sqlite3.Error:
        return None
    return _cache_fresh_data(row, max_age_hours)


def _save_holdings_comparison_to_disk(cik, comparison):
    try:
        with _cache_db() as conn:
            conn.execute(
                "INSERT INTO holdings_comparison (cik, data, built_at) VALUES (?, ?, ?) "
                "ON CONFLICT(cik) DO UPDATE SET data = excluded.data, built_at = excluded.built_at",
                (cik, json.dumps(comparison), datetime.now(timezone.utc).isoformat()),
            )
    except sqlite3.Error:
        pass  # best-effort -- an unwritable cache file shouldn't break the page


class ManagerLookupError(Exception):
    """Raised when a manager-name query can't be resolved to exactly one
    SEC 13F filer. `candidates` is a list of {"cik", "name"} dicts when the
    query was ambiguous (empty for a plain no-match)."""

    def __init__(self, message, candidates=None):
        super().__init__(message)
        self.candidates = candidates or []


class FilingDataError(Exception):
    """Raised when a manager's 13F filings aren't usable (fewer than two
    quarters on file, an unreadable information table, etc.)."""


# Opt-in request throttle for the batch jobs (see build_top_managers):
# SEC allows ~10 requests/second per client and answers anything faster
# with 429s for a while afterwards. 0 (the default) leaves the live app's
# own one-off lookups unthrottled.
_min_request_interval = 0.0
_request_throttle_lock = threading.Lock()
_last_request_at = 0.0


def _throttle():
    global _last_request_at
    if not _min_request_interval:
        return
    with _request_throttle_lock:
        wait = _last_request_at + _min_request_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()


def _get(session, url):
    _throttle()
    resp = session.get(url, headers={"User-Agent": USER_AGENT,
                                      "Accept-Encoding": "gzip, deflate"},
                        timeout=30)
    resp.raise_for_status()
    return resp


# Single exact match: SEC renders a "filerDiv" page with the name and CIK
# inline instead of a results table.
_SINGLE_MATCH_RE = re.compile(
    r'<span class="companyName">([^<]+?)\s*<acronym[^>]*>CIK</acronym>#:\s*'
    r'<a[^>]*>(\d{10})',
    re.IGNORECASE,
)
# Multiple matches: a "tableFile2" results table, one CIK+name per row.
_TABLE_ROW_RE = re.compile(
    r'<a href="/cgi-bin/browse-edgar\?action=getcompany&amp;CIK=(\d{10})[^"]*">'
    r'\d{10}</a></td>\s*<td[^>]*>([^<]+)</td>',
    re.IGNORECASE,
)

FULLTEXT_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
_DISPLAY_NAME_CIK_SUFFIX_RE = re.compile(r"\s*\(CIK\s+\d+\)\s*$", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Local manager directory: search_managers below hits SEC's live endpoint
# per query, so even with disk caching (see module notes further down),
# every new keystroke in the typeahead box is a fresh live round-trip --
# caching only helps when the exact same string was searched before.
# build_manager_directory scans SEC's quarterly master index (a much
# heavier, CI-only batch job -- see build_manager_directory.py) into a
# repo-committed {cik, name} list for every 13F-HR filer, and
# search_managers searches that locally first so the common case (an
# already-known manager) needs no network call at all.
# ---------------------------------------------------------------------------

# A long company name pushes form.idx's later columns right of their
# "usual" position -- this isn't a strictly fixed-width format despite
# appearances -- so this matches from the right (CIK/date/filename are
# all unambiguous fixed-format tokens) instead of slicing fixed offsets.
# Anchoring on "13F-HR" immediately followed by whitespace (not "/A")
# excludes 13F-HR/A amendments and 13F-NT the same way list_13f_filings
# does elsewhere in this file.
_FORM_IDX_13F_HR_RE = re.compile(
    r"^13F-HR\s+(?P<name>.+?)\s+(?P<cik>\d+)\s+(?P<date>\d{4}-\d{2}-\d{2})\s+(?P<file>\S+)\s*$"
)


def _recent_quarters(count=8):
    today = date.today()
    year, quarter = today.year, (today.month - 1) // 3 + 1
    quarters = []
    for _ in range(count):
        quarters.append((year, quarter))
        quarter -= 1
        if quarter == 0:
            quarter, year = 4, year - 1
    return quarters


def _parse_form_idx_13f_filers(text):
    for line in text.splitlines():
        m = _FORM_IDX_13F_HR_RE.match(line.rstrip())
        if m:
            yield {"cik": int(m.group("cik")), "name": m.group("name").strip()}


def build_manager_directory(session=None, quarters_back=8):
    """Scan SEC's quarterly master index for every 13F-HR filer's CIK and
    (most recent) name, oldest quarter first so a filer who renamed ends
    up with its latest name. CI-only batch job (see
    build_manager_directory.py) -- never run from the live app. Returns a
    fresh list for just the scanned window; the script merges that into
    the existing committed directory rather than this function doing it,
    since a daily refresh only needs to rescan the current quarter."""
    session = session or requests.Session()
    directory = {}
    for year, quarter in reversed(_recent_quarters(quarters_back)):
        url = FULL_INDEX_URL.format(year=year, quarter=quarter)
        try:
            resp = _get(session, url)
        except requests.RequestException:
            continue
        for filer in _parse_form_idx_13f_filers(resp.text):
            directory[filer["cik"]] = filer["name"]
    return [{"cik": cik, "name": name} for cik, name in directory.items()]


_MANAGER_DIRECTORY_PATH = Path(__file__).parent / "data" / "thirteenf_manager_directory.json"
_manager_directory_cache = None


def _load_manager_directory():
    """In-process cache of the repo-committed manager directory -- read
    once per worker process, not once per keystroke."""
    global _manager_directory_cache
    if _manager_directory_cache is not None:
        return _manager_directory_cache
    try:
        with open(_MANAGER_DIRECTORY_PATH, encoding="utf-8") as f:
            _manager_directory_cache = json.load(f)
    except (OSError, json.JSONDecodeError):
        _manager_directory_cache = []
    return _manager_directory_cache


def _search_manager_directory(query, limit):
    q = query.strip().lower()
    if not q:
        return []
    matches = [m for m in _load_manager_directory() if q in m["name"].lower()]

    def rank(m):
        name = m["name"].lower()
        if name == q:
            return (0, name)
        if name.startswith(q):
            return (1, name)
        return (2, name)

    matches.sort(key=rank)
    return matches[:limit]


def _fulltext_search_managers(query, session, limit):
    """Fallback for when the registered-name search above finds nothing --
    a filer's registered EDGAR company name can differ from the name
    it's publicly known by (e.g. Balyasny Asset Management's 13F-HR
    filings are under "Longaeva Partners L.P."). SEC's full-text search
    indexes actual filing content rather than just the registered name,
    so it still finds these. Returns the same {"cik", "name"} shape as
    search_managers, deduped by CIK (one filer can have many hits)."""
    url = f"{FULLTEXT_SEARCH_URL}?q={quote(query)}&forms=13F-HR"
    try:
        data = _get(session, url).json()
    except (requests.RequestException, ValueError):
        return []
    matches = {}
    for hit in data.get("hits", {}).get("hits", []):
        source = hit.get("_source", {})
        ciks = source.get("ciks") or []
        names = source.get("display_names") or []
        if not ciks or not names:
            continue
        try:
            cik = int(ciks[0])
        except ValueError:
            continue
        if cik in matches:
            continue
        matches[cik] = {"cik": cik, "name": _DISPLAY_NAME_CIK_SUFFIX_RE.sub("", names[0]).strip()}
        if len(matches) >= limit:
            break
    return list(matches.values())


def search_managers(query, session, limit=100):
    """Search SEC's 13F-HR filer directory for `query` (a manager name).
    Returns up to `limit` {"cik", "name"} dicts — 0, 1, or many; unlike
    resolve_manager this never raises for an ambiguous or empty result, so
    it's also used for the live-typeahead search box.

    Checks the local manager directory first (see module notes above) --
    the common case, an already-known manager, needs no network call at
    all. A miss there (a brand-new filer the directory hasn't picked up
    yet) falls back to SEC's live endpoint, disk-cached per normalized
    query (see module notes further below) so repeat searches -- including
    across different visitors -- still skip the live round-trip."""
    local_matches = _search_manager_directory(query, limit)
    if local_matches:
        return local_matches

    normalized = query.strip().lower()
    cached = _load_manager_search_from_disk(normalized)
    if cached is not None:
        return cached[:limit]

    url = (f"{BROWSE_URL}?action=getcompany&company={quote(query)}"
           "&type=13F-HR&dateb=&owner=include&count=100")
    html_text = _get(session, url).text

    single = _SINGLE_MATCH_RE.search(html_text)
    if single:
        matches = [{"cik": int(single.group(2)), "name": html.unescape(single.group(1)).strip()}]
    else:
        rows = _TABLE_ROW_RE.findall(html_text)
        matches = [{"cik": int(cik), "name": html.unescape(name).strip()} for cik, name in rows]

    if not matches:
        matches = _fulltext_search_managers(query, session, limit)

    _save_manager_search_to_disk(normalized, matches)
    return matches[:limit]


def resolve_manager(query, session):
    """Resolve `query` (a manager name) to (cik, name) for a filer with at
    least one 13F-HR on record. Raises ManagerLookupError (with candidates
    listed, for an ambiguous name) if it doesn't resolve to exactly one."""
    matches = search_managers(query, session)
    if len(matches) == 1:
        return matches[0]["cik"], matches[0]["name"]
    if not matches:
        raise ManagerLookupError(f"No 13F filer found matching '{query}'.")

    message = f"Multiple 13F filers match '{query}' ({len(matches)}). Pick one:"
    raise ManagerLookupError(message, candidates=matches)


def list_13f_filings(session, cik, count=40):
    """Return this filer's 13F-HR filings (excluding /A amendments), most
    recent first, as [{"accession": "0000919574-26-005427", "filing_date":
    "2026-08-14"}, ...]."""
    url = (f"{BROWSE_URL}?action=getcompany&CIK={cik:010d}&type=13F-HR"
           f"&dateb=&owner=include&count={count}&output=atom")
    resp = _get(session, url)
    root = ET.fromstring(resp.content)

    filings = []
    for entry in root.findall("a:entry", _ATOM_NS):
        form = entry.findtext(".//a:filing-type", namespaces=_ATOM_NS) or ""
        if form.strip().upper() != "13F-HR":  # skip 13F-HR/A amendments and 13F-NT
            continue
        acc = entry.findtext(".//a:accession-number", namespaces=_ATOM_NS)
        fdate = entry.findtext(".//a:filing-date", namespaces=_ATOM_NS)
        if acc:
            filings.append({"accession": acc, "filing_date": fdate})
    return filings


def _find_infotable_filename(session, cik, accession_nodash):
    url = f"{ARCHIVES_URL}/{cik}/{accession_nodash}/index.json"
    items = _get(session, url).json().get("directory", {}).get("item", [])
    xml_files = [it["name"] for it in items if it["name"].lower().endswith(".xml")]
    candidates = [f for f in xml_files if f.lower() != "primary_doc.xml"]
    if not candidates:
        return None
    info_named = [f for f in candidates if "info" in f.lower()]
    return (info_named or candidates)[0]


def _local_tag(tag):
    # The info table's default xmlns varies subtly by filer software, so
    # match on local tag name rather than trusting one fixed namespace.
    return tag.rsplit("}", 1)[-1]


def _iter_infotable_rows(session, url):
    """Yields each <infoTable> row element of a 13F information table,
    streamed straight off the HTTP response and discarded as soon as the
    caller moves on. The largest managers' tables are tens of MB of XML
    (BlackRock's has tens of thousands of rows): reading the whole body and
    building the full ElementTree, twice per comparison (two quarters),
    peaked around +250MB for a single Export CSV -- enough on its own to
    push a 512MB instance over its memory limit."""
    _throttle()
    with session.get(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"},
                     timeout=30, stream=True) as resp:
        resp.raise_for_status()
        resp.raw.decode_content = True
        root = None
        for event, el in ET.iterparse(resp.raw, events=("start", "end")):
            if event == "start":
                if root is None:
                    root = el
                continue
            if _local_tag(el.tag) == "infoTable":
                yield el
                # Drop every finished row from the tree (not just this
                # one's children), so memory stays flat across the file.
                root.clear()


def get_filing_holdings(session, cik, accession):
    """Fetch one 13F-HR filing's period-of-report and holdings. Returns
    (period_end_iso, manager_name, holdings) where holdings is a dict
    {cusip: {"issuer": str, "title_of_class": str, "value": float_usd,
    "shares": int}}, with multiple info-table rows for the same CUSIP
    (split across voting authority / accounts) summed together."""
    accession_nodash = accession.replace("-", "")
    base = f"{ARCHIVES_URL}/{cik}/{accession_nodash}"

    primary = ET.fromstring(_get(session, f"{base}/primary_doc.xml").content)
    ns = {"e": "http://www.sec.gov/edgar/thirteenffiler"}
    period_raw = primary.findtext(".//e:periodOfReport", namespaces=ns)
    manager_name = primary.findtext(".//e:filingManager/e:name", namespaces=ns)
    period_iso = None
    if period_raw:
        try:
            period_iso = datetime.strptime(period_raw.strip(), "%m-%d-%Y").date().isoformat()
        except ValueError:
            period_iso = period_raw.strip()

    infotable_name = _find_infotable_filename(session, cik, accession_nodash)
    if not infotable_name:
        raise FilingDataError(f"Could not find an information table in filing {accession}.")
    holdings = {}
    for row in _iter_infotable_rows(session, f"{base}/{infotable_name}"):
        fields = {_local_tag(child.tag): child for child in row}
        issuer = (fields["nameOfIssuer"].text or "").strip() if "nameOfIssuer" in fields else ""
        title_of_class = (fields["titleOfClass"].text or "").strip() if "titleOfClass" in fields else ""
        cusip = (fields["cusip"].text or "").strip() if "cusip" in fields else None
        value_el = fields.get("value")
        value = float(value_el.text) if value_el is not None and value_el.text else 0.0
        shares = 0
        shares_type = None
        shrs_el = fields.get("shrsOrPrnAmt")
        if shrs_el is not None:
            for child in shrs_el:
                tag = _local_tag(child.tag)
                if tag == "sshPrnamt" and child.text:
                    shares = int(float(child.text))
                elif tag == "sshPrnamtType" and child.text:
                    shares_type = child.text.strip()
        if not cusip:
            continue
        # sshPrnamtType "PRN" means this quantity is a bond/note's
        # principal amount, not a share count -- and unlike shares,
        # filers report principal amount in inconsistent units (whole
        # dollars one quarter, thousands the next, for the very same
        # CUSIP), which silently wrecks every downstream calculation
        # that assumes "shares" is a stable, comparable count (delta
        # shares, implied price-per-share, ...). Skipping debt positions
        # entirely means this data only covers equity holdings, which is
        # what "shares"/"ΔShares"/"Portfolio %" already implicitly
        # assume everywhere else in this app.
        if shares_type == "PRN":
            continue
        # SEC "value" was reported in thousands of dollars for periods
        # before Q1 2023 (period end < 2023-01-01), whole dollars from Q1
        # 2023 on. Most managers' latest two quarters are well past that
        # switch, but an inactive/deregistered manager's most recent
        # filing on file can still be old enough to need the conversion --
        # period_iso is this filing's own period-of-report, already
        # parsed above, so this doesn't assume "latest" means "recent."
        value_usd = value * 1000 if period_iso and period_iso < "2023-01-01" else value

        if cusip in holdings:
            holdings[cusip]["value"] += value_usd
            holdings[cusip]["shares"] += shares
        else:
            holdings[cusip] = {"issuer": issuer, "title_of_class": title_of_class,
                                "value": value_usd, "shares": shares}

    return period_iso, manager_name, holdings


_GENERIC_SHARE_CLASS = {"", "COM", "COMMON", "COMMON STOCK", "SHS", "ORD", "ORD SHS"}


def _display_issuer(issuer, title_of_class):
    """Append the share class to the issuer name when it's not just plain
    common stock — otherwise two CUSIPs for the same company (e.g. GOOGL
    and GOOG) would both display as an indistinguishable "ALPHABET INC"."""
    if title_of_class and title_of_class.upper() not in _GENERIC_SHARE_CLASS:
        return f"{issuer} ({title_of_class})"
    return issuer


def _pct_change(cur, prev):
    """Relative percent change cur vs prev. None (rather than +inf) for a
    brand-new position with no prior-quarter base to compare against."""
    if not prev:
        return None
    return (cur - prev) / abs(prev) * 100


def build_holdings_comparison(session, cik, top_n=10, skip_snapshot=False, adjust_splits=True):
    """Compare the two most recent 13F-HR quarters for `cik`. Returns a
    dict: {"latest_period", "previous_period", "manager_name",
    "top_increases", "top_decreases", "all_positions"}.

    top_increases/top_decreases rows: {"issuer", "cusip", "shares_m",
    "prev_shares_m", "delta_shares_m", "delta_shares_value_m", "value_m",
    "portfolio_pct", "delta_pct"}, ranked by delta_shares_value_m -- the
    change in share count priced at the average of the previous and
    current quarter's implied per-share price (or just the one available
    endpoint price for a brand-new or fully-exited position), i.e. an
    approximation of the dollar amount actually bought or sold. A
    position's value/portfolio-weight can rise or fall purely from the
    stock's price moving even with zero trading, so ranking by shares
    isolates actual buying/selling activity -- but a raw share-count change
    means nothing without knowing the stock's price (10,000 shares of a $5
    stock vs. a $500 one are very different trades), so it's the per-share
    price that puts every position's share change on the same ($) footing.
    13F filings don't disclose intra-quarter trade dates/prices, so this is
    necessarily an approximation rather than the actual dollars traded.

    all_positions rows (every current-or-prior holding, sorted by
    portfolio_pct descending): the same fields, plus "delta_shares_pct",
    "share_price", "prev_share_price", "prev_value_m", "delta_value_pct",
    and "prev_portfolio_pct". The delta_*_pct fields are relative percent
    changes (None for a brand-new position, -100 for one fully exited).
    "delta_pct" there is instead a percentage-POINT change in portfolio
    allocation, not a relative percent change. share_price/prev_share_price
    are None for a position with no shares held that quarter (brand-new or
    fully-exited).

    All percents are already-multiplied numbers, e.g. 1.57 for 1.57%;
    shares/value (including delta_shares_m/delta_shares_value_m) are in
    millions. Checks the repo-committed top-300-by-AUM snapshot first
    (see build_top_managers) -- those need no live SEC round-trip at
    all -- then the disk cache (see module notes above), so a repeat
    lookup of any other manager, by anyone, still skips the several live
    SEC round-trips and the XML parsing.

    A snapshot entry's all_positions is capped at _MAX_SNAPSHOT_POSITIONS
    (result["positions_truncated"] says whether this manager hit that
    cap) so the precomputed file stays a sane size -- pass
    skip_snapshot=True to bypass it and fetch the real, complete list
    live instead (see dash_app.py's download_positions, which needs the
    full list rather than the snapshot's capped one)."""
    if not skip_snapshot:
        snapshot_hit = top_managers_snapshot_entry(cik)
        if snapshot_hit is not None:
            return apply_split_adjustments(snapshot_hit, top_n=top_n) if adjust_splits else snapshot_hit

    cached = _load_holdings_comparison_from_disk(cik)
    if cached is not None:
        return apply_split_adjustments(cached, top_n=top_n) if adjust_splits else cached

    filings = list_13f_filings(session, cik, count=40)
    if len(filings) < 2:
        raise FilingDataError("Fewer than two quarterly 13F-HR filings found for this manager.")

    latest_period, manager_name, latest_holdings = get_filing_holdings(
        session, cik, filings[0]["accession"])
    previous_period, _, previous_holdings = get_filing_holdings(
        session, cik, filings[1]["accession"])

    latest_total = sum(h["value"] for h in latest_holdings.values())
    previous_total = sum(h["value"] for h in previous_holdings.values())
    if not latest_total or not previous_total:
        raise FilingDataError("A recent 13F filing reports zero total holdings value.")

    all_cusips = set(latest_holdings) | set(previous_holdings)
    rows = []
    all_positions = []
    for cusip in all_cusips:
        cur = latest_holdings.get(cusip)
        prev = previous_holdings.get(cusip)
        cur_shares = cur["shares"] if cur else 0
        prev_shares = prev["shares"] if prev else 0
        cur_value = cur["value"] if cur else 0.0
        prev_value = prev["value"] if prev else 0.0
        cur_pct = (cur_value / latest_total * 100) if cur else 0.0
        prev_pct = (prev_value / previous_total * 100) if prev else 0.0
        issuer = _display_issuer((cur or prev)["issuer"], (cur or prev)["title_of_class"])
        delta_pct = cur_pct - prev_pct

        # Per-share price: the average of the previous and current
        # quarter's implied price (rather than just one endpoint), which
        # approximates the price paid over the quarter reasonably well
        # without needing actual trade dates/prices 13F doesn't disclose --
        # using only the previous (or only the current) price would skew
        # the dollar figure for any stock that moved a lot intra-quarter.
        # A brand-new or fully-exited position only has one endpoint
        # price, so that one stands in for the average.
        prev_price = (prev_value / prev_shares) if prev_shares else None
        cur_price = (cur_value / cur_shares) if cur_shares else None
        if prev_price is not None and cur_price is not None:
            price_per_share = (prev_price + cur_price) / 2
        else:
            price_per_share = prev_price if prev_price is not None else (cur_price or 0.0)
        delta_shares_value_m = (cur_shares - prev_shares) * price_per_share / 1e6

        rows.append({
            "issuer": issuer,
            "cusip": cusip,
            "shares_m": cur_shares / 1e6,
            "prev_shares_m": prev_shares / 1e6,
            "delta_shares_m": (cur_shares - prev_shares) / 1e6,
            "delta_shares_value_m": delta_shares_value_m,
            "value_m": cur_value / 1e6,
            "portfolio_pct": cur_pct,
            "delta_pct": delta_pct,
        })
        all_positions.append({
            "issuer": issuer,
            "cusip": cusip,
            "shares_m": cur_shares / 1e6,
            "prev_shares_m": prev_shares / 1e6,
            "delta_shares_pct": _pct_change(cur_shares, prev_shares),
            "delta_shares_value_m": delta_shares_value_m,
            "share_price": cur_price,
            "prev_share_price": prev_price,
            "value_m": cur_value / 1e6,
            "prev_value_m": prev_value / 1e6,
            "delta_value_pct": _pct_change(cur_value, prev_value),
            "portfolio_pct": cur_pct,
            "prev_portfolio_pct": prev_pct,
            "delta_pct": delta_pct,
        })

    rows.sort(key=lambda r: r["delta_shares_value_m"], reverse=True)
    top_increases = [r for r in rows if r["delta_shares_value_m"] > 0][:top_n]
    top_decreases = sorted(
        [r for r in rows if r["delta_shares_value_m"] < 0], key=lambda r: r["delta_shares_value_m"]
    )[:top_n]
    all_positions.sort(key=lambda r: r["portfolio_pct"], reverse=True)

    result = {
        "manager_name": manager_name,
        "latest_period": latest_period,
        "previous_period": previous_period,
        "top_increases": top_increases,
        "top_decreases": top_decreases,
        "all_positions": all_positions,
    }
    # Cached unadjusted, so a later split-factor update applies cleanly.
    _save_holdings_comparison_to_disk(cik, result)
    return apply_split_adjustments(result, top_n=top_n) if adjust_splits else result


# ---------------------------------------------------------------------------
# Stock splits: 13F share counts aren't split-adjusted, so a 25-for-1 split
# (Booking Holdings, 2026) shows every holder's share count jumping ~25x --
# which build_holdings_comparison, comparing raw share counts, reports as a
# giant buy priced at the average of the pre- and post-split prices. One
# manager's filings alone can't tell a split from a real buy, so splits are
# detected across all ~1000 managers at once by the weekly job
# (_detect_splits, see build_top_managers.py) and saved to
# thirteenf_splits.json; apply_split_adjustments then restates any
# comparison's previous quarter in post-split terms.
# ---------------------------------------------------------------------------

_SPLITS_PATH = Path(__file__).parent / "data" / "thirteenf_splits.json"
_splits_cache = None


def load_splits():
    """{"latest_period", "previous_period", "factors": {cusip: factor}} --
    see _detect_splits. Empty factors if the file's missing/unreadable."""
    global _splits_cache
    if _splits_cache is None:
        try:
            with open(_SPLITS_PATH, encoding="utf-8") as f:
                _splits_cache = json.load(f)
        except (OSError, json.JSONDecodeError):
            _splits_cache = {"factors": {}}
    return _splits_cache


def _split_adjusted_position(row, factor):
    """`row` (an all_positions entry) with its previous quarter restated
    in post-split shares and prices, and every share-based delta
    recomputed the same way build_holdings_comparison computes it."""
    prev_shares = row["prev_shares_m"] * factor
    prev_price = row["prev_share_price"] / factor if row.get("prev_share_price") else None
    cur_price = row.get("share_price")
    if prev_price is not None and cur_price is not None:
        price_per_share = (prev_price + cur_price) / 2
    else:
        price_per_share = prev_price if prev_price is not None else (cur_price or 0.0)
    return {
        **row,
        "prev_shares_m": prev_shares,
        "prev_share_price": prev_price,
        "delta_shares_pct": _pct_change(row["shares_m"], prev_shares),
        "delta_shares_value_m": (row["shares_m"] - prev_shares) * price_per_share,
    }


def _mover_row(position):
    """A top_increases/top_decreases row from an all_positions entry."""
    return {
        "issuer": position["issuer"],
        "cusip": position["cusip"],
        "shares_m": position["shares_m"],
        "prev_shares_m": position["prev_shares_m"],
        "delta_shares_m": position["shares_m"] - position["prev_shares_m"],
        "delta_shares_value_m": position["delta_shares_value_m"],
        "value_m": position["value_m"],
        "portfolio_pct": position["portfolio_pct"],
        "delta_pct": position["delta_pct"],
    }


def apply_split_adjustments(comparison, splits=None, top_n=10):
    """`comparison` with every split security's previous quarter restated
    in post-split terms (see module notes above) and top_increases/
    top_decreases re-picked accordingly. Only applies when the comparison
    covers exactly the two quarters the split factors were detected for;
    marked "split_adjusted" so it's never applied twice.

    Re-picking draws on all_positions, which for a snapshot entry is
    capped at _MAX_SNAPSHOT_POSITIONS -- the original top lists are folded
    back in too, so nothing that was already a top mover gets lost, and a
    replacement for a removed split artifact is all but certain to sit
    within the cap (it's ranked by portfolio weight)."""
    if comparison.get("split_adjusted"):
        return comparison
    splits = splits if splits is not None else load_splits()
    factors = splits.get("factors") or {}
    if (not factors or comparison.get("latest_period") != splits.get("latest_period")
            or comparison.get("previous_period") != splits.get("previous_period")):
        return comparison
    all_positions = comparison.get("all_positions") or []
    if not any(p.get("cusip") in factors and p.get("prev_shares_m") for p in all_positions):
        return {**comparison, "split_adjusted": True}

    adjusted = [_split_adjusted_position(p, factors[p["cusip"]])
                if p.get("cusip") in factors and p.get("prev_shares_m") else p
                for p in all_positions]
    candidates = {p["cusip"]: _mover_row(p) for p in adjusted}
    for row in comparison.get("top_increases", []) + comparison.get("top_decreases", []):
        if row["cusip"] not in candidates:
            candidates[row["cusip"]] = row
    movers = list(candidates.values())
    top_increases = sorted((r for r in movers if r["delta_shares_value_m"] > 0),
                           key=lambda r: r["delta_shares_value_m"], reverse=True)[:top_n]
    top_decreases = sorted((r for r in movers if r["delta_shares_value_m"] < 0),
                           key=lambda r: r["delta_shares_value_m"])[:top_n]
    return {**comparison, "all_positions": adjusted, "top_increases": top_increases,
            "top_decreases": top_decreases, "split_adjusted": True}


# ---------------------------------------------------------------------------
# Top managers by AUM: fully precomputing build_holdings_comparison for
# every manager in the directory (thousands of them) would mean fetching
# and parsing every one's full information table, which for a large fund
# is itself a large XML document -- far too heavy to do for managers
# almost nobody looks up. The ranking itself, though, comes cheaply from
# SEC's own quarterly Form 13F Data Sets: one ~100MB ZIP covering every
# 13F filing in a three-month window, whose small SUMMARYPAGE table
# already carries each filing's total reported value. One download ranks
# every filer, and only the top few hundred then get a full comparison.
# (This used to scrape aum13f.com's ranking instead, until that site
# started serving an anti-bot page to automated requests.) See
# build_top_managers.py.
# ---------------------------------------------------------------------------

SEC_13F_DATA_SETS_PAGE = "https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets"
_SEC_13F_DATA_SET_LINK_RE = re.compile(r'href="([^"]*form-13f-data-sets/[^"]+_form13f\.zip)"', re.I)
_MAX_SNAPSHOT_POSITIONS = 500
_MANAGER_RANKING_PATH = Path(__file__).parent / "data" / "thirteenf_manager_ranking.json"


_RATE_LIMITED_BACKOFF_S = 60


def _retry(fn, *args, attempts=3, backoff=1.5, **kwargs):
    """Runs fn(*args, **kwargs), retrying on a transient network error --
    SEC occasionally 503s a concurrent batch fetch, which a lone retry
    clears right up. A 429 (rate limited) gets a much longer pause, since
    SEC keeps refusing a client for a while after it trips the limit.
    Used only by the batch jobs below; the live app's single on-demand
    fetches don't need this, a real failure there should surface
    immediately."""
    last_exc = None
    for attempt in range(attempts):
        try:
            return fn(*args, **kwargs)
        except requests.RequestException as e:
            last_exc = e
            if attempt < attempts - 1:
                status = getattr(getattr(e, "response", None), "status_code", None)
                time.sleep(_RATE_LIMITED_BACKOFF_S if status == 429 else backoff * (attempt + 1))
    raise last_exc


def _tsv_rows(zf, name):
    with zf.open(name) as f:
        yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8", errors="replace"), delimiter="\t")


def fetch_sec_13f_ranking(session=None):
    """Rank every 13F filer by total reported portfolio value, from the
    most recent SEC Form 13F Data Set. Returns {"source", "period",
    "managers"}, where managers is [{"cik", "name", "value_b",
    "positions"}] in descending value order.

    Only original 13F-HR filings for the data set's dominant report
    period count (amendments and stray late filings for older quarters
    are skipped -- an amendment can be a partial restatement whose total
    isn't the whole portfolio); a filer with more than one original for
    that period keeps its latest. 13F-NT notices carry no holdings of
    their own and are skipped too. Values are in dollars (SEC switched
    13F reporting from thousands to dollars in 2023)."""
    session = session or requests.Session()
    listing = _get(session, SEC_13F_DATA_SETS_PAGE).text
    # The page lists data sets newest first.
    links = _SEC_13F_DATA_SET_LINK_RE.findall(listing)
    if not links:
        raise FilingDataError("No Form 13F Data Set links found on SEC's data sets page.")
    url = links[0] if links[0].startswith("http") else f"https://www.sec.gov{links[0]}"
    resp = session.get(url, headers={"User-Agent": USER_AGENT}, timeout=600)
    resp.raise_for_status()
    zf = zipfile.ZipFile(io.BytesIO(resp.content))

    submissions = {r["ACCESSION_NUMBER"]: r for r in _tsv_rows(zf, "SUBMISSION.tsv")
                   if r["SUBMISSIONTYPE"] == "13F-HR"}
    if not submissions:
        raise FilingDataError(f"No 13F-HR filings found in {url}.")
    names = {r["ACCESSION_NUMBER"]: r["FILINGMANAGER_NAME"] for r in _tsv_rows(zf, "COVERPAGE.tsv")}
    summaries = {r["ACCESSION_NUMBER"]: r for r in _tsv_rows(zf, "SUMMARYPAGE.tsv")}
    period = Counter(r["PERIODOFREPORT"] for r in submissions.values()).most_common(1)[0][0]

    latest = {}  # cik -> (filing date, manager record)
    for acc, sub in submissions.items():
        summary = summaries.get(acc)
        if sub["PERIODOFREPORT"] != period or not summary or not summary["TABLEVALUETOTAL"]:
            continue
        filed = datetime.strptime(sub["FILING_DATE"], "%d-%b-%Y")
        cik = int(sub["CIK"])
        if cik in latest and latest[cik][0] >= filed:
            continue
        latest[cik] = (filed, {
            "cik": cik,
            "name": (names.get(acc) or "").strip(),
            "value_b": round(float(summary["TABLEVALUETOTAL"]) / 1e9, 3),
            "positions": int(summary["TABLEENTRYTOTAL"] or 0),
        })
    managers = sorted((m for _filed, m in latest.values()), key=lambda m: m["value_b"], reverse=True)
    return {
        "source": url,
        "period": datetime.strptime(period, "%d-%b-%Y").date().isoformat(),
        "managers": managers,
    }


def _snapshot_comparison(session, cik, comparison_top_n):
    # skip_snapshot=True: build_holdings_comparison otherwise returns the
    # manager's entry from the very snapshot being rebuilt, so a refresh
    # would never actually refresh anyone already in it. adjust_splits=
    # False: the job detects this quarter's splits from these raw
    # comparisons itself, then applies them (see build_top_managers.py).
    comparison = _retry(build_holdings_comparison, session, cik, comparison_top_n, skip_snapshot=True,
                        adjust_splits=False)
    # A handful of mega-managers (Morgan Stanley, Citadel, ...) have
    # thousands of positions -- uncapped, this snapshot would run to
    # 100+MB and re-bloat git on every refresh. all_positions is already
    # sorted by portfolio_pct descending, so the cap keeps the positions
    # that actually matter; positions_truncated tells the live app to
    # fetch the real, complete list instead (skip_snapshot=True) for the
    # rare case that needs it, e.g. a full CSV export.
    all_positions = comparison["all_positions"]
    truncated = len(all_positions) > _MAX_SNAPSHOT_POSITIONS
    if truncated:
        comparison = {**comparison, "all_positions": all_positions[:_MAX_SNAPSHOT_POSITIONS]}
    comparison["positions_truncated"] = truncated
    return _round_floats(comparison)


def _round_floats(obj, places=4):
    """Rounds every float in a snapshot entry -- full-precision floats
    (e.g. a portfolio_pct of 3.9032408520942434) made up about a quarter
    of the snapshot file's size for no visible benefit; 4 places is still
    $100 precision on the *_m (millions) fields."""
    if isinstance(obj, float):
        return round(obj, places)
    if isinstance(obj, dict):
        return {k: _round_floats(v, places) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_round_floats(v, places) for v in obj]
    return obj


def build_top_managers(ciks, session=None, comparison_top_n=10, max_workers=4):
    """Fully precompute build_holdings_comparison for each of `ciks`
    (the top of fetch_sec_13f_ranking, see build_top_managers.py).
    CI-only batch job -- never run from the live app. Returns
    (results, failures): results is a dict keyed by str(cik) -- JSON
    object keys must be strings -- to that manager's comparison;
    failures maps each cik that couldn't be built to its error message.

    SEC allows ~10 requests/second and answers anything faster with 429s
    for a while afterwards -- at 15 unthrottled workers, about two thirds
    of the top 300 failed that way in one run. So every SEC request made
    during this build goes through a shared throttle (see _throttle),
    with max_workers just keeping a few requests overlapped. Anything
    that still fails gets one more, sequential attempt at the end."""
    global _min_request_interval
    session = session or requests.Session()
    results, failures = {}, {}
    # ~6.7 requests/second, comfortably under SEC's ~10/s limit (even
    # max_workers alone wasn't enough: 77 of the top 300 still hit 429s).
    previous_interval, _min_request_interval = _min_request_interval, 0.15
    try:
        _build_comparisons(session, ciks, comparison_top_n, max_workers, results, failures)
    finally:
        _min_request_interval = previous_interval
    return results, failures


def _build_comparisons(session, ciks, comparison_top_n, max_workers, results, failures):
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_cik = {pool.submit(_snapshot_comparison, session, cik, comparison_top_n): cik for cik in ciks}
        for future in concurrent.futures.as_completed(future_to_cik):
            cik = future_to_cik[future]
            try:
                results[str(cik)] = future.result()
            except (FilingDataError, requests.RequestException) as e:
                failures[cik] = f"{type(e).__name__}: {e}"
    for cik in list(failures):
        try:
            results[str(cik)] = _snapshot_comparison(session, cik, comparison_top_n)
            del failures[cik]
        except (FilingDataError, requests.RequestException) as e:
            failures[cik] = f"{type(e).__name__}: {e}"


_TOP_MANAGERS_SNAPSHOT_PATH = Path(__file__).parent / "data" / "thirteenf_top_managers.json"
_top_managers_cache = None
_top_managers_index = None  # cik -> byte offset of that manager's line in the snapshot
_top_managers_index_lock = threading.Lock()


def write_top_managers_snapshot(snapshot, path=_TOP_MANAGERS_SNAPSHOT_PATH):
    """Writes `snapshot` ({cik: comparison}) as a JSON object with one
    manager per line -- still plain, valid JSON, but laid out so the live
    app can read a single manager's line on demand (see
    top_managers_snapshot_entry) instead of holding the whole file in
    memory. Loaded whole it was ~125MB resident on a 512MB instance."""
    lines = [f"{json.dumps(str(cik))}:{json.dumps(comparison, separators=(',', ':'))}"
             for cik, comparison in snapshot.items()]
    Path(path).write_text("{\n" + ",\n".join(lines) + "\n}\n", encoding="utf-8", newline="\n")


def _top_managers_snapshot_index():
    """{cik: byte offset} for the snapshot's one-manager-per-line layout
    (see write_top_managers_snapshot), built by one pass over the file's
    lines without parsing any of them. Empty if the file is missing or in
    the older single-line layout, which top_managers_snapshot_entry then
    falls back to loading whole."""
    global _top_managers_index
    if _top_managers_index is not None:
        return _top_managers_index
    with _top_managers_index_lock:
        if _top_managers_index is not None:
            return _top_managers_index
        index = {}
        try:
            with open(_TOP_MANAGERS_SNAPSHOT_PATH, "rb") as f:
                offset = 0
                for line in f:
                    if line.startswith(b'"'):
                        end = line.find(b'":')
                        if end > 0:
                            index[line[1:end].decode("ascii")] = offset
                    offset += len(line)
        except OSError:
            pass
        # A whole-file single line (older layout) has no '"cik":' line
        # starts at all -- treat it as unindexed, not as an empty snapshot.
        _top_managers_index = index
        return index


def top_managers_snapshot_entry(cik):
    """One manager's precomputed comparison from the top-managers snapshot
    (see build_top_managers), or None if they're not in it. Reads just
    that manager's line off disk (~150KB) rather than keeping all ~300
    in memory."""
    index = _top_managers_snapshot_index()
    if not index:
        return _load_top_managers_snapshot().get(str(cik))
    offset = index.get(str(cik))
    if offset is None:
        return None
    try:
        with open(_TOP_MANAGERS_SNAPSHOT_PATH, "rb") as f:
            f.seek(offset)
            line = f.readline()
        return json.loads(line[line.find(b'":') + 2:].rstrip().rstrip(b","))
    except (OSError, ValueError):
        return None


def _load_top_managers_snapshot():
    """The whole top-N-by-AUM snapshot as one dict, cached in-process.
    Live lookups go through top_managers_snapshot_entry instead, which
    only falls back to this for an older single-line snapshot file --
    this is otherwise just for the top-buys fallback in load_top_buys and
    build-time callers."""
    global _top_managers_cache
    if _top_managers_cache is not None:
        return _top_managers_cache
    try:
        with open(_TOP_MANAGERS_SNAPSHOT_PATH, encoding="utf-8") as f:
            _top_managers_cache = json.load(f)
    except (OSError, json.JSONDecodeError):
        _top_managers_cache = {}
    return _top_managers_cache


_MIN_PUBLIC_EQUITY_PORTFOLIO_M = 1000  # $1B -- see min_public_equity_portfolio_m below


# Split detection (see _detect_splits).
_SPLIT_MIN_RATIO = 1.8          # 3-for-2 splits are skipped -- see _detect_splits
_SPLIT_CLUSTER_TOLERANCE = 0.25  # middle half of holders' share-count ratios vs the median
_SPLIT_MAX_PRICE_MOVE = 3       # how far the price may move on top of the split
_SPLIT_MIN_HOLDERS = 5
# A buy can't be worth much more than the whole position it built (only
# the averaged price can push it past 1x, see build_holdings_comparison) --
# anything beyond this is a data error in the filing.
_MAX_BUY_TO_POSITION_RATIO = 2


def _median(values):
    values = sorted(values)
    return values[len(values) // 2]


def _detect_splits(comparisons):
    """{cusip: factor} for every security that looks like it split (or
    reverse-split) between the two quarters. 13F share counts aren't
    split-adjusted, so a 25-for-1 split (Booking Holdings, 2026) shows
    every holder's share count jumping ~25x -- which
    build_holdings_comparison counts as a giant buy, priced at the
    average of the pre- and post-split prices.

    Detected across all holders at once, from their share counts: if the
    median holder's count rose by at least _SPLIT_MIN_RATIO, with the
    middle half of holders tightly clustered around it, that's a split --
    nothing else makes most holders multiply their position by the same
    factor in one quarter. The price only has to move the matching way by
    at least 1/_SPLIT_MAX_PRICE_MOVE of that factor, not match it: KLA's
    2026 10-for-1 split came in a quarter where the stock also roughly
    doubled, so its price ratio was only 4.9x.
    The factor is that median share-count ratio itself rather than a
    rounded "clean" ratio: big managers trade nearly every holding every
    quarter (almost none show an exactly unchanged count), so the median
    holder is the best available "didn't really trade" baseline, and each
    holder's real buying is measured relative to it. Ratios under
    _SPLIT_MIN_RATIO (i.e. 3-for-2 splits, rare today) are left alone:
    at ~1.5x, a stock that fell a third while holders added to it looks
    the same (HubSpot, EPAM and Whirlpool all did in 2026)."""
    price_ratios, share_ratios = {}, {}
    for comparison in comparisons.values():
        for row in comparison.get("all_positions", []):
            price, prev_price = row.get("share_price"), row.get("prev_share_price")
            shares, prev_shares = row.get("shares_m"), row.get("prev_shares_m")
            if not (price and prev_price and shares and prev_shares):
                continue
            price_ratios.setdefault(row["cusip"], []).append(prev_price / price)
            share_ratios.setdefault(row["cusip"], []).append(shares / prev_shares)
    splits = {}
    for cusip, ratios in price_ratios.items():
        if len(ratios) < _SPLIT_MIN_HOLDERS:
            continue
        price_ratio = _median(ratios)
        holder_ratios = sorted(share_ratios[cusip])
        share_ratio = _median(holder_ratios)
        # Compare on the >1 side so a 1-for-10 reverse split looks like 10.
        share_x, price_x = (1 / share_ratio, 1 / price_ratio) if share_ratio < 1 else (share_ratio, price_ratio)
        lower_q = holder_ratios[len(holder_ratios) // 4]
        upper_q = holder_ratios[(3 * len(holder_ratios)) // 4]
        clustered = (abs(lower_q / share_ratio - 1) <= _SPLIT_CLUSTER_TOLERANCE
                     and abs(upper_q / share_ratio - 1) <= _SPLIT_CLUSTER_TOLERANCE)
        if share_x >= _SPLIT_MIN_RATIO and clustered and price_x >= share_x / _SPLIT_MAX_PRICE_MOVE:
            splits[cusip] = share_ratio
    return splits


def detect_split_factors(comparisons):
    """The {"latest_period", "previous_period", "factors"} record saved to
    thirteenf_splits.json (see load_splits) -- _detect_splits over every
    raw comparison covering the most common quarter pair, i.e. the one the
    vast majority of managers just filed for."""
    pairs = Counter((c.get("latest_period"), c.get("previous_period")) for c in comparisons.values())
    if not pairs:
        return {"factors": {}}
    (latest, previous), _count = pairs.most_common(1)[0]
    same_quarters = {cik: c for cik, c in comparisons.items()
                     if (c.get("latest_period"), c.get("previous_period")) == (latest, previous)}
    return {"latest_period": latest, "previous_period": previous, "factors": _detect_splits(same_quarters)}


def _pooled_buys(comparisons, min_public_equity_portfolio_m):
    """Every buy (positive ΔShares Value) across `comparisons` ({cik:
    build_holdings_comparison result}), each tagged with its manager and
    its size relative to that manager's total public equity portfolio --
    see top_buys_across_managers for the full reasoning. Expects
    split-adjusted comparisons (see apply_split_adjustments); a "buy"
    worth far more than the position it built is dropped as a data
    error."""
    pooled = []
    for cik, comparison in comparisons.items():
        manager_name = comparison.get("manager_name", "")
        for row in comparison.get("all_positions", []):
            value_m = row.get("value_m")
            portfolio_pct = row.get("portfolio_pct")
            if row.get("delta_shares_value_m", 0) <= 0 or not value_m or not portfolio_pct:
                continue
            if row["delta_shares_value_m"] > value_m * _MAX_BUY_TO_POSITION_RATIO:
                continue
            # Same value_m/portfolio_pct relationship as pct_of_portfolio
            # below, just solved for the total instead of applied to the
            # buy -- this position is portfolio_pct% of it, so dividing
            # value_m back out by that recovers the whole thing.
            total_portfolio_value_m = value_m / (portfolio_pct / 100)
            if total_portfolio_value_m < min_public_equity_portfolio_m:
                continue
            pct_of_portfolio = row["delta_shares_value_m"] * portfolio_pct / value_m
            pooled.append({
                **row, "manager_name": manager_name, "cik": cik,
                "delta_shares_value_pct_of_portfolio": pct_of_portfolio,
                "total_portfolio_value_m": total_portfolio_value_m,
            })
    return pooled


def top_buys_across_managers(top_n=50, min_public_equity_portfolio_m=_MIN_PUBLIC_EQUITY_PORTFOLIO_M,
                             comparisons=None):
    """The largest buys across every precomputed top-AUM manager
    (see build_top_managers), ranked by ~ΔShares Value as a percentage
    of that manager's OWN total *public equity* portfolio value rather
    than the raw dollar amount -- a $50M buy is a rounding error for a
    $300B index fund but could be a huge conviction bet for a $500M
    fund, so ranking by the raw dollar figure just surfaces mega-funds'
    routine rebalancing over everyone else's actual high-conviction
    moves.

    Managers whose own 13F-derived total public equity portfolio falls
    under min_public_equity_portfolio_m are excluded entirely rather
    than just ranked low: for a tiny portfolio, a single small position
    reads as a massive "% of portfolio" conviction bet. (This mattered
    most when the top-managers list came from aum13f.com's broad
    regulatory-AUM ranking, which included private-equity/VC firms whose
    13F covers only a few incidental IPO shares -- Pathway Capital
    Management ranked top-300 there on just $24M of 13F holdings. The
    SEC 13F-value ranking used now can't produce that, but the floor
    stays as a cheap safeguard.)

    Pools from each manager's full all_positions (not the smaller,
    already-capped top_increases) specifically because that ranking-
    by-dollar cap is exactly what could cut a smaller manager's
    proportionally-huge-but-dollar-modest buy before it ever reaches
    this function. A position's own value_m and portfolio_pct already
    imply the manager's total portfolio value (value_m / (portfolio_pct
    / 100)), so delta_shares_value_m * portfolio_pct / value_m is that
    same ratio applied to the buy itself -- no separate total-portfolio
    field needed.

    Each row is the same shape as an all_positions row, plus
    "manager_name", "cik", "delta_shares_value_pct_of_portfolio" (the
    sort key), and "total_portfolio_value_m" (that manager's total 13F
    portfolio value, same derivation run in reverse) so the table can
    show which manager made the buy, how big a bet it was for them
    specifically, and how large that manager's whole public equity
    portfolio is."""
    if comparisons is None:
        comparisons = _load_top_managers_snapshot()
    pooled = _pooled_buys(comparisons, min_public_equity_portfolio_m)
    pooled.sort(key=lambda r: r["delta_shares_value_pct_of_portfolio"], reverse=True)
    return pooled[:top_n]


def largest_managers_top_buys(top_n=50, min_public_equity_portfolio_m=_MIN_PUBLIC_EQUITY_PORTFOLIO_M,
                              comparisons=None):
    """Each of the `top_n` largest managers' single biggest buy this
    quarter, largest manager first -- the "Fund Size" counterpart to
    top_buys_across_managers. "Largest" is by the same derived public
    equity portfolio total shown on the cards (total_portfolio_value_m),
    not SEC's reported total, which also counts options and bonds. Within
    one manager, the biggest buy by dollars and by % of portfolio are the
    same buy (one shared denominator). A manager with no buys at all this
    quarter just doesn't appear, so the next largest fills its place."""
    if comparisons is None:
        comparisons = _load_top_managers_snapshot()
    biggest = {}
    for row in _pooled_buys(comparisons, min_public_equity_portfolio_m):
        current = biggest.get(row["cik"])
        if current is None or row["delta_shares_value_m"] > current["delta_shares_value_m"]:
            biggest[row["cik"]] = row
    return sorted(biggest.values(), key=lambda r: r["total_portfolio_value_m"], reverse=True)[:top_n]


# best_estimated_returns: a manager needs at least this share of its
# previous-quarter portfolio priced at both quarter-ends to be ranked, and
# at least this many priced positions (a one- or two-stock holding company
# isn't really a "fund" return). A quarter-over-quarter price ratio outside
# _RETURN_MAX_PRICE_RATIO either way is treated as a data error, not a move.
_RETURN_MIN_COVERAGE = 0.9
_RETURN_MIN_POSITIONS = 10
_RETURN_MAX_PRICE_RATIO = 10
_RETURN_MIN_PRICE_QUOTES = 3


BEST_RETURNS_N = 50  # how many the Managers tab's Best Performing Funds card can scroll through


def best_estimated_returns(comparisons, top_n=BEST_RETURNS_N,
                           min_public_equity_portfolio_m=_MIN_PUBLIC_EQUITY_PORTFOLIO_M):
    """The `top_n` managers by estimated quarterly return on their
    previous-quarter 13F portfolio: every position held at the previous
    quarter-end, priced at both quarter-ends, as if it were held unchanged
    through the quarter --

        sum(prev_shares * (latest_price - prev_price)) / sum(prev_shares * prev_price)

    Prices are the median share price (value / shares) every manager in
    `comparisons` reported for that CUSIP at each quarter-end -- robust to
    one filer's reporting error, and it still prices a position this
    manager sold out of entirely (no latest price of its own) as long as
    others held it. Falls back to the manager's own price where fewer than
    _RETURN_MIN_PRICE_QUOTES managers reported one. An estimate only: it
    ignores trading within the quarter, dividends, and fees, and 13F covers
    long US-listed equity only. Expects split-adjusted comparisons (see
    apply_split_adjustments). Each row: cik, manager_name, est_return_pct,
    equity_aum_m (previous quarter), positions, coverage_pct,
    top_contributor, previous_period, latest_period."""
    cur_quotes, prev_quotes = {}, {}
    for comparison in comparisons.values():
        for row in comparison.get("all_positions", []):
            if row.get("shares_m") and row.get("share_price"):
                cur_quotes.setdefault(row["cusip"], []).append(row["share_price"])
            if row.get("prev_shares_m") and row.get("prev_share_price"):
                prev_quotes.setdefault(row["cusip"], []).append(row["prev_share_price"])

    def median_price(quotes, cusip):
        prices = sorted(quotes.get(cusip, ()))
        if len(prices) < _RETURN_MIN_PRICE_QUOTES:
            return None
        mid = len(prices) // 2
        return prices[mid] if len(prices) % 2 else (prices[mid - 1] + prices[mid]) / 2

    results = []
    for cik, comparison in comparisons.items():
        held = [r for r in comparison.get("all_positions", []) if r.get("prev_shares_m")]
        # Total previous-quarter portfolio, recovered from any one position's
        # value and weight (the same derivation _pooled_buys uses) -- not
        # just the sum of `held`, which the snapshot's position cap can cut short.
        sized = next((r for r in held if r.get("prev_value_m") and r.get("prev_portfolio_pct")), None)
        if sized is None:
            continue
        total_prev_m = sized["prev_value_m"] / (sized["prev_portfolio_pct"] / 100)
        if total_prev_m < min_public_equity_portfolio_m:
            continue
        gain = base = 0.0
        contributions = []
        for row in held:
            prev_price = median_price(prev_quotes, row["cusip"]) or row.get("prev_share_price")
            cur_price = median_price(cur_quotes, row["cusip"]) or (
                row.get("share_price") if row.get("shares_m") else None)
            if not prev_price or not cur_price:
                continue
            if not 1 / _RETURN_MAX_PRICE_RATIO <= cur_price / prev_price <= _RETURN_MAX_PRICE_RATIO:
                continue
            position_gain = row["prev_shares_m"] * (cur_price - prev_price)
            gain += position_gain
            base += row["prev_shares_m"] * prev_price
            contributions.append((position_gain, row["issuer"]))
        if len(contributions) < _RETURN_MIN_POSITIONS or not base or base / total_prev_m < _RETURN_MIN_COVERAGE:
            continue
        top_gain, top_issuer = max(contributions)
        results.append({
            "cik": cik,
            "manager_name": comparison.get("manager_name", ""),
            "est_return_pct": gain / base * 100,
            "equity_aum_m": total_prev_m,
            "positions": len(contributions),
            "coverage_pct": min(100.0, base / total_prev_m * 100),
            "top_contributor": top_issuer,
            "top_contributor_pct": top_gain / base * 100,
            "previous_period": comparison.get("previous_period"),
            "latest_period": comparison.get("latest_period"),
        })
    results.sort(key=lambda r: r["est_return_pct"], reverse=True)
    return results[:top_n]


# Both card lists for the Managers tab, precomputed by build_top_managers.py
# across the top ~1000 managers -- far more than the top-300 snapshot
# above, which stays small because the live app holds it in memory.
_TOP_BUYS_PATH = Path(__file__).parent / "data" / "thirteenf_top_buys.json"
_top_buys_cache = None


def load_top_buys():
    """{"conviction": [...], "fund_size": [...], "best_returns": [...]} --
    see top_buys_across_managers/largest_managers_top_buys/
    best_estimated_returns. Read once per
    worker process from the repo-committed file; if it's missing or
    unreadable, falls back to computing both from the top-300 snapshot."""
    global _top_buys_cache
    if _top_buys_cache is not None:
        return _top_buys_cache
    try:
        with open(_TOP_BUYS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        _top_buys_cache = {"conviction": data["conviction"], "fund_size": data["fund_size"],
                           "best_returns": data.get("best_returns", [])}
    except (OSError, json.JSONDecodeError, KeyError):
        _top_buys_cache = {"conviction": top_buys_across_managers(), "fund_size": largest_managers_top_buys(),
                           "best_returns": best_estimated_returns(_load_top_managers_snapshot())}
    return _top_buys_cache


def fetch_manager_comparison(query, session=None, user_agent=None, top_n=10):
    """Resolve `query` to an SEC 13F filer and return
    build_holdings_comparison(...)'s dict plus "cik" and "resolved_name".
    Raises ManagerLookupError or FilingDataError."""
    global USER_AGENT
    if user_agent:
        USER_AGENT = user_agent
    session = session or requests.Session()

    cik, resolved_name = resolve_manager(query, session)
    result = build_holdings_comparison(session, cik, top_n=top_n)
    result["cik"] = cik
    result["resolved_name"] = resolved_name
    return result


def fetch_manager_comparison_by_cik(cik, session=None, user_agent=None, top_n=10, skip_snapshot=False):
    """Same as fetch_manager_comparison, but for a CIK that's already
    known — used when the caller lets the user pick one candidate out of
    an ambiguous name search rather than re-resolving by name. Raises
    FilingDataError (not ManagerLookupError, since there's no name lookup
    to be ambiguous about). skip_snapshot=True forces a real, complete
    live fetch even for a manager the precomputed snapshot has a
    (possibly position-capped) entry for -- see
    build_holdings_comparison and dash_app.py's download_positions."""
    global USER_AGENT
    if user_agent:
        USER_AGENT = user_agent
    session = session or requests.Session()

    result = build_holdings_comparison(session, cik, top_n=top_n, skip_snapshot=skip_snapshot)
    result["cik"] = cik
    result["resolved_name"] = result["manager_name"]
    return result
