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
import html
import json
import os
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
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


def _get(session, url):
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
    info_root = ET.fromstring(_get(session, f"{base}/{infotable_name}").content)

    # The info table's default xmlns varies subtly by filer software, so
    # match on local tag name rather than trusting one fixed namespace.
    def local(tag):
        return tag.rsplit("}", 1)[-1]

    holdings = {}
    for row in info_root:
        if local(row.tag) != "infoTable":
            continue
        fields = {local(child.tag): child for child in row}
        issuer = (fields["nameOfIssuer"].text or "").strip() if "nameOfIssuer" in fields else ""
        title_of_class = (fields["titleOfClass"].text or "").strip() if "titleOfClass" in fields else ""
        cusip = (fields["cusip"].text or "").strip() if "cusip" in fields else None
        value_el = fields.get("value")
        value = float(value_el.text) if value_el is not None and value_el.text else 0.0
        shares = 0
        shrs_el = fields.get("shrsOrPrnAmt")
        if shrs_el is not None:
            for child in shrs_el:
                if local(child.tag) == "sshPrnamt" and child.text:
                    shares = int(float(child.text))
        if not cusip:
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


def build_holdings_comparison(session, cik, top_n=10, skip_snapshot=False):
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
        snapshot_hit = _load_top_managers_snapshot().get(str(cik))
        if snapshot_hit is not None:
            return snapshot_hit

    cached = _load_holdings_comparison_from_disk(cik)
    if cached is not None:
        return cached

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
    _save_holdings_comparison_to_disk(cik, result)
    return result


# ---------------------------------------------------------------------------
# Top managers by AUM: fully precomputing build_holdings_comparison for
# every manager in the directory (thousands of them) would mean fetching
# and parsing every one's full information table, which for a large fund
# is itself a large XML document -- far too heavy to do for managers
# almost nobody looks up. Ranking every filer ourselves from SEC data
# (even just a lightweight per-filer fetch) is still a fetch per filer in
# the whole directory just to throw away all but the top few hundred, so
# this instead reuses aum13f.com's own AUM ranking -- it already tracks
# this across EDGAR/IAPD/CAFR sources -- and only touches SEC for the
# comparisons of the firms that ranking says actually matter. See
# build_top_managers.py.
# ---------------------------------------------------------------------------

AUM13F_BASE_URL = "https://aum13f.com"
_AUM13F_PAGE_SIZE = 30
_MAX_SNAPSHOT_POSITIONS = 500
_AUM13F_ROW_RE = re.compile(
    r'<a href="/firm/([^"]+)">([^<]+)</a>.*?align="right">.*?([\d,]+\.\d+)\s*B</td>',
    re.S,
)
# aum13f.com's firm page lists every EDGAR form type it's found for that
# firm (13F-HR, 13F-NT, Form 3, ...), each with its own CIK -- a firm can
# have more than one CIK across form types (e.g. an insider-ownership
# filing under a related entity), so this matches specifically the
# 13F-HR row rather than the first CIK mentioned anywhere on the page.
_AUM13F_CIK_ROW_RE = re.compile(
    r'<td style="text-align:center;">13F-HR</td>\s*'
    r'<td style="text-align:center;"><a href="https://www\.sec\.gov/cgi-bin/browse-edgar\?CIK=(\d+)'
)


def _retry(fn, *args, attempts=3, backoff=1.5, **kwargs):
    """Runs fn(*args, **kwargs), retrying on a transient network error --
    both SEC and aum13f.com occasionally 503 a concurrent batch fetch,
    which a lone retry clears right up. Used only by the batch jobs
    below; the live app's single on-demand fetches don't need this, a
    real failure there should surface immediately."""
    last_exc = None
    for attempt in range(attempts):
        try:
            return fn(*args, **kwargs)
        except requests.RequestException as e:
            last_exc = e
            if attempt < attempts - 1:
                time.sleep(backoff * (attempt + 1))
    raise last_exc


def fetch_aum13f_ranking(session=None, pages=10):
    """Scrape aum13f.com's AUM-ranked firm listing, 30 firms/page.
    Returns [{"slug", "name", "aum_b"}] in aum13f.com's own descending-
    AUM order."""
    session = session or requests.Session()
    ranking = []
    for page in range(1, pages + 1):
        html_text = _get(session, f"{AUM13F_BASE_URL}/?page={page}").text
        for slug, name, aum in _AUM13F_ROW_RE.findall(html_text):
            ranking.append({"slug": slug, "name": html.unescape(name), "aum_b": float(aum.replace(",", ""))})
    return ranking


def _aum13f_cik_for_firm(session, slug):
    """The CIK aum13f.com's own firm page lists for a 13F-HR filing
    history specifically -- None if this firm doesn't file 13F-HR under
    its own name (aum13f.com's AUM ranking includes advisers ranked by
    broader regulatory AUM, not just 13F filers, so this is common)."""
    html_text = _get(session, f"{AUM13F_BASE_URL}/firm/{slug}").text
    m = _AUM13F_CIK_ROW_RE.search(html_text)
    return int(m.group(1)) if m else None


def build_top_managers(session=None, top_n_managers=300, comparison_top_n=10, max_workers=15):
    """Fully precompute build_holdings_comparison for the top
    `top_n_managers` by AUM, ranked via aum13f.com rather than computing
    that ranking from SEC data ourselves (see module notes above).
    CI-only batch job (see build_top_managers.py) -- never run from the
    live app. Returns a dict keyed by str(cik) -- JSON object keys must
    be strings -- to that manager's build_holdings_comparison result. A
    firm aum13f.com doesn't list a 13F-HR CIK for, or whose comparison
    fetch fails outright, is simply omitted."""
    session = session or requests.Session()
    pages = -(-top_n_managers // _AUM13F_PAGE_SIZE)  # ceiling division
    ranking = fetch_aum13f_ranking(session=session, pages=pages)[:top_n_managers]

    ciks = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_firm = {
            pool.submit(_retry, _aum13f_cik_for_firm, session, firm["slug"]): firm for firm in ranking
        }
        for future in concurrent.futures.as_completed(future_to_firm):
            try:
                cik = future.result()
            except requests.RequestException:
                continue
            if cik is not None:
                ciks.append(cik)

    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_cik = {
            pool.submit(_retry, build_holdings_comparison, session, cik, comparison_top_n): cik
            for cik in ciks
        }
        for future in concurrent.futures.as_completed(future_to_cik):
            cik = future_to_cik[future]
            try:
                comparison = future.result()
            except (FilingDataError, requests.RequestException):
                continue
            # A handful of mega-managers (Morgan Stanley, Citadel, ...)
            # have thousands of positions -- uncapped, this snapshot
            # would run to 100+MB and re-bloat git on every refresh.
            # all_positions is already sorted by portfolio_pct
            # descending, so the cap keeps the positions that actually
            # matter; positions_truncated tells the live app to fetch
            # the real, complete list instead (skip_snapshot=True) for
            # the rare case that needs it, e.g. a full CSV export.
            all_positions = comparison["all_positions"]
            truncated = len(all_positions) > _MAX_SNAPSHOT_POSITIONS
            if truncated:
                comparison = {**comparison, "all_positions": all_positions[:_MAX_SNAPSHOT_POSITIONS]}
            comparison["positions_truncated"] = truncated
            results[str(cik)] = comparison
    return results


_TOP_MANAGERS_SNAPSHOT_PATH = Path(__file__).parent / "data" / "thirteenf_top_managers.json"
_top_managers_cache = None


def _load_top_managers_snapshot():
    """In-process cache of the repo-committed top-N-by-AUM holdings
    comparisons (see build_top_managers) -- read once per worker
    process, not once per lookup."""
    global _top_managers_cache
    if _top_managers_cache is not None:
        return _top_managers_cache
    try:
        with open(_TOP_MANAGERS_SNAPSHOT_PATH, encoding="utf-8") as f:
            _top_managers_cache = json.load(f)
    except (OSError, json.JSONDecodeError):
        _top_managers_cache = {}
    return _top_managers_cache


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
