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

import html
import json
import os
import re
import sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests

USER_AGENT = "ryan.hsu1993@gmail.com"  # <-- put your real contact here (SEC fair-access policy)

BROWSE_URL = "https://www.sec.gov/cgi-bin/browse-edgar"
ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data"

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
    it's also used for the live-typeahead search box. Results are cached
    on disk per normalized query (see module notes above), so repeat
    searches for the same name -- including across different visitors --
    skip SEC's live endpoint entirely."""
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


def build_holdings_comparison(session, cik, top_n=10):
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
    millions. Cached on disk per CIK (see module notes above) -- a repeat
    lookup of the same manager, by anyone, skips the several live SEC
    round-trips and the XML parsing entirely."""
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


def fetch_manager_comparison_by_cik(cik, session=None, user_agent=None, top_n=10):
    """Same as fetch_manager_comparison, but for a CIK that's already
    known — used when the caller lets the user pick one candidate out of
    an ambiguous name search rather than re-resolving by name. Raises
    FilingDataError (not ManagerLookupError, since there's no name lookup
    to be ambiguous about)."""
    global USER_AGENT
    if user_agent:
        USER_AGENT = user_agent
    session = session or requests.Session()

    result = build_holdings_comparison(session, cik, top_n=top_n)
    result["cik"] = cik
    result["resolved_name"] = result["manager_name"]
    return result
