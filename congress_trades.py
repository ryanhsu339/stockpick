"""U.S. Congress stock trade disclosures (Periodic Transaction Reports,
i.e. STOCK Act filings), for both chambers:
  - House: PDFs pulled directly from the House Clerk's public disclosure
    site, parsed with pdfplumber using word coordinates (the PDFs' text
    layer alone doesn't linearize reliably -- see _parse_ptr_page).
  - Senate: HTML pages pulled from efdsearch.senate.gov, which requires
    accepting a click-through disclaimer first to get a valid session.

Unlike SEC 13F filings, PTRs are individual buy/sell events reported in a
dollar RANGE (e.g. "$1,001 - $15,000"), not an exact share count, and there
is no quarterly holdings snapshot or portfolio percentage available (a
member's total net worth isn't disclosed). "Equity positions" here are
therefore an ESTIMATE built by accumulating each disclosed transaction's
range midpoint over time (purchases add, sales subtract) -- not a real
statement of current holdings.
"""

from __future__ import annotations

import concurrent.futures
import io
import json
import os
import re
import sqlite3
import threading
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pdfplumber
import requests
from bs4 import BeautifulSoup

HOUSE_INDEX_URL = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
HOUSE_PTR_PDF_URL = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc_id}.pdf"
SENATE_BASE_URL = "https://efdsearch.senate.gov"
_USER_AGENT = "Mozilla/5.0 (compatible; PublicCompanyTracker/1.0)"

# PTR PDFs before this year use an older template with unreliable letter
# casing in some filers' embedded fonts (tickers render as e.g. "(aaPl)"
# instead of "(AAPL)") and other layout quirks; we still attempt them
# case-insensitively but treat anything before this as lower-confidence.
_MIN_RELIABLE_YEAR = 2019

_TTYPE_LABELS = {
    "P": "Purchase",
    "S": "Sale (Full)",
    "S (partial)": "Sale (Partial)",
    "S (full)": "Sale (Full)",
    "E": "Exchange",
}

_HEADER_LABELS = {
    "ID", "Owner", "Asset", "Transaction", "Date", "Notification",
    "Amount", "Cap.", "Type", "Gains", ">", "$200?",
}
_TTYPE_RE = re.compile(r"^(P|S|E)$")
_DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
_TICKER_RE = re.compile(r"\(([A-Za-z][A-Za-z0-9.]{0,6})\)[^()\[\]]{0,20}\[([A-Za-z]{1,4})\]")
_AMOUNT_RE = re.compile(r"\$([\d,]+)\s*-\s*(?:\S+\s+){0,3}?\$([\d,]+)")

_index_cache: dict[int, list[dict]] = {}
# Keyed by House doc_id or Senate doc_url -- a filed PTR never changes, so
# a successful parse (House PDF or Senate HTML) is cached indefinitely.
_ptr_cache: dict[str, list[dict]] = {}


class PoliticianLookupError(Exception):
    def __init__(self, message, candidates=None):
        super().__init__(message)
        self.candidates = candidates or []


class PoliticianDataError(Exception):
    pass


def _session_with_ua(session=None):
    session = session or requests.Session()
    session.headers.setdefault("User-Agent", _USER_AGENT)
    return session


def _recent_years(count=4):
    current = date.today().year
    return list(range(current, current - count, -1))


def _fetch_house_index(year, session):
    if year in _index_cache:
        return _index_cache[year]
    resp = session.get(HOUSE_INDEX_URL.format(year=year), timeout=30)
    if resp.status_code == 404:
        _index_cache[year] = []
        return []
    resp.raise_for_status()
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    raw = zf.read(f"{year}FD.txt").decode("utf-8", errors="replace")
    lines = raw.splitlines()
    header = lines[0].split("\t")
    rows = []
    for line in lines[1:]:
        if not line.strip():
            continue
        parts = line.split("\t")
        parts += [""] * (len(header) - len(parts))
        rows.append(dict(zip(header, parts)))
    _index_cache[year] = rows
    return rows


_roster_cache: list[dict] | None = None


def _scan_house_roster(session, years_back=4):
    """Every House member with at least one PTR filed in recent years,
    deduped by (last, first). Returns candidate dicts with 'chamber',
    'first', 'last', 'prefix', 'state_dst', 'display'.
    """
    seen = {}
    for year in _recent_years(years_back):
        try:
            rows = _fetch_house_index(year, session)
        except Exception:
            continue
        for row in rows:
            if row.get("FilingType") != "P":
                continue
            last = row.get("Last", "").strip()
            first = row.get("First", "").strip()
            if not last:
                continue
            key = (last.lower(), first.lower())
            if key not in seen:
                # "Prefix" is an honorific (Hon., Dr., Mr., ...), not part
                # of the member's name -- keep it out of anything shown to
                # the user.
                seen[key] = {
                    "chamber": "house",
                    "first": first,
                    "last": last,
                    "prefix": row.get("Prefix", "").strip(),
                    "state_dst": row.get("StateDst", "").strip(),
                    "display": f"{first} {last}".strip(),
                }
    return list(seen.values())


def list_all_house_members(session=None, years_back=4, force_refresh=False):
    """The full roster for the dropdown -- House members who filed at
    least one PTR in recent years, sorted by first name then last name.
    Cached in-process since the roster only changes as new filing years
    roll in.
    """
    global _roster_cache
    if _roster_cache is not None and not force_refresh:
        return _roster_cache
    session = _session_with_ua(session)
    roster = _scan_house_roster(session, years_back=years_back)
    roster.sort(key=lambda c: (c["first"].lower(), c["last"].lower()))
    _roster_cache = roster
    return roster


def search_politicians(query, session=None, limit=8, years_back=4):
    """Search House members who have filed at least one PTR in recent
    years, matching by first/last name substring. Returns candidate dicts
    with 'chamber', 'first', 'last', 'prefix', 'state_dst', 'display'.
    """
    session = _session_with_ua(session)
    q = query.strip().lower()
    if not q:
        return []
    roster = _scan_house_roster(session, years_back=years_back)
    candidates = [
        c for c in roster
        if q in c["last"].lower() or q in c["first"].lower()
        or q in f"{c['first']} {c['last']}".lower()
    ]

    def rank(c):
        last = c["last"].lower()
        full = f"{c['first']} {c['last']}".lower()
        if last == q:
            return (0, last)
        if last.startswith(q):
            return (1, last)
        if full.startswith(q):
            return (2, last)
        return (3, last)

    candidates = sorted(candidates, key=rank)
    return candidates[:limit]


def _parse_ptr_page(page):
    words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
    if not words:
        return []
    header_words = [w for w in words if w["text"] in _HEADER_LABELS]
    if not header_words:
        return []
    header_top = min(w["top"] for w in header_words)
    header_bottom = header_top + 30
    footer_words = [w for w in words if w["text"].lower().startswith("abbreviations")]
    footer_top = min((w["top"] for w in footer_words), default=10**9) - 3

    table_words = [w for w in words if header_bottom <= w["top"] < footer_top]
    owner_hdr = next((w for w in header_words if w["text"] == "Owner"), None)
    asset_hdr = next((w for w in header_words if w["text"] == "Asset"), None)
    if not owner_hdr or not asset_hdr:
        return []
    date_tokens = [w for w in table_words if _DATE_RE.match(w["text"])]
    amount_tokens = [w for w in table_words if w["text"].startswith("$")]
    if not date_tokens or not amount_tokens:
        return []
    date_x = min(w["x0"] for w in date_tokens)
    amount_x = min(w["x0"] for w in amount_tokens)
    ttype_words = sorted(
        [w for w in table_words if _TTYPE_RE.match(w["text"]) and w["x0"] < date_x],
        key=lambda w: w["top"],
    )
    if not ttype_words:
        return []
    tops = [w["top"] for w in ttype_words] + [footer_top]

    rows = []
    for i, anchor in enumerate(ttype_words):
        band_top = anchor["top"] - 2
        band_bottom = tops[i + 1] - 2
        same_line = [w for w in table_words if abs(w["top"] - anchor["top"]) < 3]
        ttype_text = anchor["text"]
        for w in same_line:
            if anchor["x0"] < w["x0"] < date_x and "(" in w["text"]:
                ttype_text += " " + w["text"]
        band_words = [w for w in table_words if band_top <= w["top"] < band_bottom]
        asset_words = [w["text"] for w in band_words if asset_hdr["x0"] - 2 <= w["x0"] < date_x - 40]
        amount_words = [w["text"] for w in band_words if w["x0"] >= amount_x - 5]
        owner_words = [w["text"] for w in band_words if owner_hdr["x0"] - 2 <= w["x0"] < asset_hdr["x0"] - 2]
        asset_text = " ".join(asset_words)
        amount_text = " ".join(amount_words)
        # A row with no real ticker (e.g. a private LP's units, which has
        # no "(TICKER)" at all) can still have its own "(partial)"/"(full)"
        # transaction-type suffix bleed into the asset-column word bucket,
        # which otherwise looks exactly like a ticker parenthetical to
        # _TICKER_RE -- skip those specifically rather than misreport a
        # fake "PARTIAL"/"FULL" ticker.
        m_ticker = None
        for candidate_match in _TICKER_RE.finditer(asset_text):
            if candidate_match.group(1).lower() not in ("partial", "full"):
                m_ticker = candidate_match
                break
        m_amount = _AMOUNT_RE.search(amount_text)
        dates_in_band = [w["text"] for w in band_words if _DATE_RE.match(w["text"])]
        if not m_ticker or not m_amount or len(dates_in_band) < 1:
            continue
        try:
            lo = int(m_amount.group(1).replace(",", ""))
            hi = int(m_amount.group(2).replace(",", ""))
        except ValueError:
            continue
        rows.append({
            "owner": " ".join(owner_words) or "Self",
            "ticker": m_ticker.group(1).upper(),
            "asset_type": m_ticker.group(2).upper(),
            "transaction_type": _TTYPE_LABELS.get(ttype_text, ttype_text),
            "transaction_date": dates_in_band[0],
            "amount_low": lo,
            "amount_high": hi,
            "amount_mid": (lo + hi) / 2,
        })
    return rows


def _parse_ptr_pdf(pdf_bytes, doc_id):
    if doc_id in _ptr_cache:
        return _ptr_cache[doc_id]
    rows = []
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                rows.extend(_parse_ptr_page(page))
    except Exception:
        rows = []
    _ptr_cache[doc_id] = rows
    return rows


def get_house_transactions(candidate, session=None, years_back=4):
    """Fetch and parse every PTR filed by this candidate in recent years.
    Returns a flat list of transaction dicts (see _parse_ptr_page), each
    also tagged with 'filing_year' and 'doc_id'.
    """
    session = _session_with_ua(session)
    last = candidate["last"].strip().lower()
    first = candidate["first"].strip().lower()
    transactions = []
    for year in _recent_years(years_back):
        try:
            rows = _fetch_house_index(year, session)
        except Exception:
            continue
        for row in rows:
            if row.get("FilingType") != "P":
                continue
            if row.get("Last", "").strip().lower() != last:
                continue
            if row.get("First", "").strip().lower() != first:
                continue
            if not candidate.get("state_dst"):
                # A candidate resolved from the dropdown's "last|first"
                # value only carries first/last (see dash_app.py) --
                # backfill state_dst from the first matching index row.
                candidate["prefix"] = row.get("Prefix", "").strip()
                candidate["state_dst"] = row.get("StateDst", "").strip()
                candidate["display"] = f"{candidate['first']} {candidate['last']}".strip()
            doc_id = row.get("DocID", "").strip()
            if not doc_id:
                continue
            pdf_url = HOUSE_PTR_PDF_URL.format(year=year, doc_id=doc_id)
            try:
                resp = session.get(pdf_url, timeout=30)
                resp.raise_for_status()
            except Exception:
                continue
            for txn in _parse_ptr_pdf(resp.content, doc_id):
                txn = dict(txn)
                txn["filing_year"] = year
                txn["doc_id"] = doc_id
                transactions.append(txn)
    return transactions


# ---------------------------------------------------------------------------
# Senate: efdsearch.senate.gov. Requires accepting a click-through
# disclaimer once per session before its search endpoints will respond,
# then a DataTables-style JSON search (server-paginated, 100 rows/page)
# lists PTR filings; each filing's own page is a clean HTML table (much
# easier to parse reliably than the House PDFs).
# ---------------------------------------------------------------------------
_SENATE_PAGE_SIZE = 100
_senate_agreed = False
_senate_index_cache: list[dict] | None = None


def _senate_csrf_token(html):
    m = re.search(r"""name=['"]csrfmiddlewaretoken['"] value=['"]([^'"]+)['"]""", html)
    return m.group(1) if m else None


def _ensure_senate_agreement(session):
    global _senate_agreed
    if _senate_agreed:
        return
    resp = session.get(f"{SENATE_BASE_URL}/search/home/", timeout=30)
    resp.raise_for_status()
    token = _senate_csrf_token(resp.text)
    if not token:
        raise PoliticianDataError("Could not load the Senate eFD disclaimer page.")
    resp2 = session.post(
        f"{SENATE_BASE_URL}/search/home/",
        data={"csrfmiddlewaretoken": token, "prohibition_agreement": "1"},
        headers={"Referer": f"{SENATE_BASE_URL}/search/home/"},
        timeout=30,
    )
    resp2.raise_for_status()
    _senate_agreed = True


def _fetch_senate_index(session, years_back=4):
    """Every PTR filed since (today - years_back), across all senators.
    Each row: {'first', 'last', 'doc_url', 'filed_date'}.
    """
    global _senate_index_cache
    if _senate_index_cache is not None:
        return _senate_index_cache
    _ensure_senate_agreement(session)
    search_page = session.get(f"{SENATE_BASE_URL}/search/", timeout=30)
    search_page.raise_for_status()
    token = _senate_csrf_token(search_page.text) or session.cookies.get("csrftoken")
    start_date = date(date.today().year - years_back, 1, 1).strftime("%m/%d/%Y 00:00:00")

    rows = []
    start = 0
    while True:
        payload = {
            "start": str(start),
            "length": str(_SENATE_PAGE_SIZE),
            "report_types": "[11]",  # 11 = Periodic Transaction Report
            "filer_types": "[]",
            "submitted_start_date": start_date,
            "submitted_end_date": "",
            "candidate_state": "",
            "senator_state": "",
            "office_id": "",
            "first_name": "",
            "last_name": "",
            "csrfmiddlewaretoken": token,
        }
        resp = session.post(
            f"{SENATE_BASE_URL}/search/report/data/",
            data=payload,
            headers={"Referer": f"{SENATE_BASE_URL}/search/", "X-Requested-With": "XMLHttpRequest"},
            timeout=30,
        )
        resp.raise_for_status()
        payload_json = resp.json()
        page_rows = payload_json.get("data", [])
        for first, last, _label, link_html, filed_date in page_rows:
            m = re.search(r'href="([^"]+)"', link_html)
            if not m:
                continue
            # Some names carry a stray trailing comma from a dropped
            # "Jr./III"-style suffix field upstream (e.g. "Moran,  ").
            rows.append({
                "first": first.strip().rstrip(",").strip(),
                "last": last.strip().rstrip(",").strip(),
                "doc_url": SENATE_BASE_URL + m.group(1),
                "filed_date": filed_date.strip(),
            })
        total = payload_json.get("recordsTotal", len(rows))
        if len(page_rows) < _SENATE_PAGE_SIZE or len(rows) >= total:
            break
        start += _SENATE_PAGE_SIZE
    _senate_index_cache = rows
    return rows


def _scan_senate_roster(session, years_back=4):
    rows = _fetch_senate_index(session, years_back=years_back)
    seen = {}
    for row in rows:
        if not row["last"]:
            continue
        key = (row["last"].lower(), row["first"].lower())
        if key not in seen:
            seen[key] = {
                "chamber": "senate",
                "first": row["first"],
                "last": row["last"],
                "prefix": "",
                # efdsearch doesn't expose a filer's state anywhere in the
                # search results or the PTR page itself, unlike the House
                # Clerk's index -- left blank rather than guessed.
                "state_dst": "",
                "display": f"{row['first']} {row['last']}".strip(),
            }
    return list(seen.values())


def list_all_senators(session=None, years_back=4, force_refresh=False):
    global _senate_index_cache
    if force_refresh:
        _senate_index_cache = None
    session = _session_with_ua(session)
    roster = _scan_senate_roster(session, years_back=years_back)
    roster.sort(key=lambda c: (c["first"].lower(), c["last"].lower()))
    return roster


def _parse_senate_ptr_html(html):
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if not table or not table.find("tbody"):
        return []
    rows = []
    for tr in table.find("tbody").find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 8:
            continue
        transaction_date = cells[1].get_text(strip=True)
        owner = cells[2].get_text(strip=True) or "Self"
        # An "Exchange" row lists the asset given up ("--" in this column)
        # and the asset received (a real ticker link) -- take whichever
        # ticker link is present; a plain Purchase/Sale row has exactly one.
        ticker_links = cells[3].find_all("a")
        ticker = ticker_links[-1].get_text(strip=True) if ticker_links else None
        asset_type = cells[5].get_text(strip=True)
        ttype_raw = cells[6].get_text(strip=True)
        amount_text = cells[7].get_text(strip=True)
        m_amount = _AMOUNT_RE.search(amount_text)
        if not ticker or not m_amount:
            continue
        try:
            lo = int(m_amount.group(1).replace(",", ""))
            hi = int(m_amount.group(2).replace(",", ""))
        except ValueError:
            continue
        rows.append({
            "owner": owner,
            "ticker": ticker.upper(),
            "asset_type": asset_type,
            "transaction_type": _TTYPE_LABELS.get(ttype_raw, ttype_raw),
            "transaction_date": transaction_date,
            "amount_low": lo,
            "amount_high": hi,
            "amount_mid": (lo + hi) / 2,
        })
    return rows


def _fetch_senate_ptr(doc_url, session):
    if doc_url in _ptr_cache:
        return _ptr_cache[doc_url]
    rows = []
    try:
        resp = session.get(doc_url, timeout=30)
        resp.raise_for_status()
        rows = _parse_senate_ptr_html(resp.text)
    except Exception:
        rows = []
    _ptr_cache[doc_url] = rows
    return rows


def get_senate_transactions(candidate, session=None, years_back=4):
    """Fetch and parse every PTR filed by this senator in recent years."""
    session = _session_with_ua(session)
    last = candidate["last"].strip().lower()
    first = candidate["first"].strip().lower()
    index_rows = _fetch_senate_index(session, years_back=years_back)
    transactions = []
    for row in index_rows:
        if row["last"].lower() != last or row["first"].lower() != first:
            continue
        for txn in _fetch_senate_ptr(row["doc_url"], session):
            txn = dict(txn)
            txn["filed_date"] = row["filed_date"]
            txn["doc_url"] = row["doc_url"]
            transactions.append(txn)
    return transactions


def build_politician_comparison(transactions, top_n=10, recent_days=365):
    """Build the three views used by the tracker UI from a flat list of
    transactions (see get_house_transactions):
      - top_increases: largest recent purchases by ticker
      - top_decreases: largest recent sales by ticker
      - all_positions: cumulative estimated net position per ticker, built
        from ALL available transaction history (not just the recent window)

    "Recent" is measured from today, not from this member's own most recent
    filing -- PTRs trickle in as trades happen rather than on a reliable
    quarterly cadence, so buys and sells are often clustered in different
    months; anchoring the window to whichever transaction type happens to
    be freshest would silently starve the other side.
    """
    def parse_date(s):
        try:
            return datetime.strptime(s, "%m/%d/%Y").date()
        except Exception:
            return None

    today = date.today()
    dated = [(t, parse_date(t["transaction_date"])) for t in transactions]

    def is_recent(d):
        # Fail closed: a transaction whose date didn't parse has unknown
        # age and shouldn't be assumed to fall inside the recent window
        # (matches how build_activity_summary treats the same None case --
        # sorted as the oldest possible date, not the newest).
        if not d:
            return False
        return (today - d).days <= recent_days

    purchases_recent = [
        t for t, d in dated if is_recent(d) and t["transaction_type"] == "Purchase"
    ]
    sales_recent = [
        t for t, d in dated if is_recent(d) and t["transaction_type"].startswith("Sale")
    ]
    top_increases = sorted(purchases_recent, key=lambda t: -t["amount_mid"])[:top_n]
    top_decreases = sorted(sales_recent, key=lambda t: -t["amount_mid"])[:top_n]

    by_ticker: dict[str, dict] = {}
    for t, d in dated:
        ticker = t["ticker"]
        entry = by_ticker.setdefault(ticker, {
            "ticker": ticker,
            "net_estimated_value": 0.0,
            "total_bought": 0.0,
            "total_sold": 0.0,
            "transaction_count": 0,
            "last_transaction_date": None,
        })
        if t["transaction_type"] == "Purchase":
            entry["net_estimated_value"] += t["amount_mid"]
            entry["total_bought"] += t["amount_mid"]
        elif t["transaction_type"].startswith("Sale"):
            entry["net_estimated_value"] -= t["amount_mid"]
            entry["total_sold"] += t["amount_mid"]
        entry["transaction_count"] += 1
        if d and (entry["last_transaction_date"] is None or d > entry["last_transaction_date"]):
            entry["last_transaction_date"] = d

    all_positions = list(by_ticker.values())
    for p in all_positions:
        p["last_transaction_date"] = (
            p["last_transaction_date"].strftime("%m/%d/%Y") if p["last_transaction_date"] else None
        )
    all_positions.sort(key=lambda p: -p["net_estimated_value"])

    return {
        "top_increases": top_increases,
        "top_decreases": top_decreases,
        "all_positions": all_positions,
    }


def fetch_politician_comparison(query, session=None, top_n=10):
    session = _session_with_ua(session)
    candidates = search_politicians(query, session=session, limit=8)
    exact = [
        c for c in candidates
        if c["last"].lower() == query.strip().lower()
        or f"{c['first']} {c['last']}".strip().lower() == query.strip().lower()
    ]
    if len(exact) == 1:
        resolved = exact[0]
    elif len(candidates) == 1:
        resolved = candidates[0]
    elif len(candidates) == 0:
        raise PoliticianLookupError(f"No House member found matching '{query}'.")
    else:
        raise PoliticianLookupError(
            f"Multiple House members match '{query}'.", candidates=candidates
        )
    return fetch_politician_comparison_by_candidate(resolved, session=session, top_n=top_n)


def fetch_politician_comparison_by_candidate(candidate, session=None, top_n=10):
    session = _session_with_ua(session)
    if candidate.get("chamber") == "senate":
        transactions = get_senate_transactions(candidate, session=session)
    else:
        transactions = get_house_transactions(candidate, session=session)
    if not transactions:
        raise PoliticianDataError(
            f"No parseable stock transactions found for {candidate['display']}."
        )
    comparison = build_politician_comparison(transactions, top_n=top_n)
    comparison["candidate"] = candidate
    return comparison


def _member_positions_key(candidate):
    """Matches dash_app.py's _politician_dropdown_key -- the "Look Up a
    Member" dropdown's value is already this same "chamber|last|first"
    string, so the live app can look a member up in the snapshot below
    with zero translation."""
    return f"{candidate['chamber']}|{candidate['last']}|{candidate['first']}"


_MEMBER_POSITIONS_SNAPSHOT_PATH = Path(__file__).parent / "data" / "congress_member_positions.json"


def build_all_member_positions(session=None, max_workers=10, top_n=10):
    """Precompute fetch_politician_comparison_by_candidate for every current
    House member and senator (see build_congress_snapshot.py -- this is a
    CI-only batch job, never run from the live app; a single member's
    years_back=4 fetch is cheap, but ~500 of them sequentially is not).
    Returns a dict keyed by _member_positions_key to that member's
    {"top_increases", "top_decreases", "all_positions", "candidate"}. A
    member with no parseable transactions, or whose fetch fails outright,
    is simply omitted -- load_member_positions_snapshot's caller treats a
    missing key as "no data for this member" rather than an error.
    """
    session = _session_with_ua(session)
    candidates = list_all_house_members(session=session) + list_all_senators(session=session)

    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_candidate = {
            pool.submit(fetch_politician_comparison_by_candidate, c, session=session, top_n=top_n): c
            for c in candidates
        }
        for future in concurrent.futures.as_completed(future_to_candidate):
            candidate = future_to_candidate[future]
            try:
                comparison = future.result()
            except (PoliticianDataError, requests.RequestException):
                continue
            results[_member_positions_key(candidate)] = comparison
    return results


def load_member_positions_snapshot():
    """Load the precomputed per-member positions snapshot (see
    build_all_member_positions). Raises OSError/json.JSONDecodeError if
    it's missing or unreadable -- callers should treat that as "no data
    yet" rather than falling back to a live fetch."""
    with open(_MEMBER_POSITIONS_SNAPSHOT_PATH, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Cross-chamber activity summary: "most recent trades" and "largest
# estimated portfolio" across every House member and senator at once.
#
# A true full-history version of this (parsing every PTR ever filed by
# everyone) isn't computable on demand -- there are ~1,800 House PDFs and
# ~800 Senate pages just in the last 4 years, at roughly 3-4 fetches/sec
# even with a thread pool, which is 15+ minutes. Instead this only looks
# at filings from the last RECENT_ACTIVITY_WINDOW_DAYS: "most recent
# trades" is still exactly that, but "largest estimated portfolio" is
# really "largest estimated activity within that recent window" -- a
# member who trades heavily but only within the last 6 months will rank
# highly; one who made one huge trade 3 years ago and has been quiet since
# won't show up here even though get_house_transactions/
# get_senate_transactions (used for a single selected member) would
# still find that older trade.
# ---------------------------------------------------------------------------
RECENT_ACTIVITY_WINDOW_DAYS = 180
_activity_summary_cache = None
# Guards the build below so concurrent requests (production runs multiple
# gunicorn threads) share one in-flight build instead of each spinning up
# its own ThreadPoolExecutor(max_workers=15) downloading/parsing PDFs --
# several of those running at once is what was blowing past the server's
# memory limit and getting the whole process OOM-killed mid-build.
_activity_summary_lock = threading.Lock()

# The in-memory cache above only survives this one process's lifetime, so a
# server restart (a routine dev-loop occurrence, not just a rare event)
# throws it away and pays the full 1-2 minute cross-chamber PDF/HTML-parsing
# cost again. This on-disk SQLite cache survives restarts -- it's still
# just a cache (a full rebuild, not an incremental update), so
# ACTIVITY_CACHE_TTL_HOURS controls how long a restart can reuse a
# previous build before treating it as stale and rebuilding anyway.
# Defaults to living next to this file for local dev. In production, set
# CACHE_DIR to a mounted persistent-disk path (e.g. Render's "Disk" add-on)
# -- a disk mount replaces whatever was already at that path in the
# container image, so pointing this at the same directory as the deployed
# source code would hide the code itself, not just add the cache file to
# it. A separate directory, mounted only for this, avoids that entirely.
_CACHE_DIR = Path(os.environ.get("CACHE_DIR", Path(__file__).parent))
_CACHE_DB_PATH = _CACHE_DIR / "congress_cache.db"
ACTIVITY_CACHE_TTL_HOURS = 24

# The live web app never runs build_activity_summary itself -- even
# serialized to one build at a time (see _activity_summary_lock), the
# scrape/parse below was enough to OOM-kill the production instance. It
# reads this repo-committed snapshot instead, regenerated on a schedule by
# build_congress_snapshot.py (see that script and
# .github/workflows/refresh-congress-snapshot.yml) on a runner with far
# more memory headroom than production has.
_SNAPSHOT_PATH = Path(__file__).parent / "data" / "congress_activity_summary.json"


def load_activity_summary_snapshot():
    """Load the precomputed cross-chamber activity summary (see module
    notes above). Raises OSError/json.JSONDecodeError if the snapshot is
    missing or unreadable -- callers should treat that as "no data yet"
    rather than falling back to a live rebuild."""
    with open(_SNAPSHOT_PATH, encoding="utf-8") as f:
        return json.load(f)


def _cache_db():
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_CACHE_DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS activity_summary ("
        "id INTEGER PRIMARY KEY CHECK (id = 1), data TEXT NOT NULL, built_at TEXT NOT NULL)"
    )
    return conn


def _load_activity_summary_from_disk(max_age_hours=ACTIVITY_CACHE_TTL_HOURS):
    try:
        with _cache_db() as conn:
            row = conn.execute("SELECT data, built_at FROM activity_summary WHERE id = 1").fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    data_json, built_at_str = row
    try:
        built_at = datetime.fromisoformat(built_at_str)
        # A naive timestamp (e.g. a leftover row from before this cache
        # stored timezone-aware ones) can't be compared against an aware
        # "now" below -- treat it as unreadable/stale rather than crashing.
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


def _save_activity_summary_to_disk(data):
    try:
        with _cache_db() as conn:
            conn.execute(
                "INSERT INTO activity_summary (id, data, built_at) VALUES (1, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET data = excluded.data, built_at = excluded.built_at",
                (json.dumps(data), datetime.now(timezone.utc).isoformat()),
            )
    except sqlite3.Error:
        pass  # best-effort -- an unwritable cache file shouldn't break the page


def _house_filing_rows_since(cutoff, session, years_back=2):
    rows_out = []
    for year in _recent_years(years_back):
        try:
            rows = _fetch_house_index(year, session)
        except Exception:
            continue
        for row in rows:
            if row.get("FilingType") != "P":
                continue
            last = row.get("Last", "").strip()
            first = row.get("First", "").strip()
            doc_id = row.get("DocID", "").strip()
            filing_date_str = row.get("FilingDate", "").strip()
            if not last or not doc_id:
                continue
            try:
                filed = datetime.strptime(filing_date_str, "%m/%d/%Y").date()
            except ValueError:
                continue
            if filed < cutoff:
                continue
            rows_out.append({
                "chamber": "house",
                "first": first,
                "last": last,
                "fetch_arg": (year, doc_id),
                "filed_date": filed,
            })
    return rows_out


def _senate_filing_rows_since(cutoff, session, years_back=2):
    rows_out = []
    for row in _fetch_senate_index(session, years_back=years_back):
        try:
            filed = datetime.strptime(row["filed_date"], "%m/%d/%Y").date()
        except ValueError:
            continue
        if filed < cutoff or not row["last"]:
            continue
        rows_out.append({
            "chamber": "senate",
            "first": row["first"],
            "last": row["last"],
            "fetch_arg": row["doc_url"],
            "filed_date": filed,
        })
    return rows_out


def _parse_one_filing(row, session):
    if row["chamber"] == "house":
        year, doc_id = row["fetch_arg"]
        try:
            resp = session.get(HOUSE_PTR_PDF_URL.format(year=year, doc_id=doc_id), timeout=30)
            resp.raise_for_status()
        except Exception:
            return []
        return _parse_ptr_pdf(resp.content, doc_id)
    return _fetch_senate_ptr(row["fetch_arg"], session)


def build_activity_summary(session=None, window_days=RECENT_ACTIVITY_WINDOW_DAYS,
                            max_workers=5, force_refresh=False):
    """Cross-chamber leaderboard data, built once and cached (this is
    expensive -- see the module notes above). Returns
    {'recent_trades': [...], 'leaderboard': [...]}, both flat lists of
    dicts, each tagged with 'member', 'chamber'.

    max_workers trades build time for peak memory: with hundreds of PTR
    PDFs/pages in flight across the 180-day window, a higher value got the
    production instance OOM-killed even with only one build running at a
    time (see _activity_summary_lock above) -- 5 keeps concurrent
    downloads/parses small enough to fit the 512MB instance.
    """
    global _activity_summary_cache
    if _activity_summary_cache is not None and not force_refresh:
        return _activity_summary_cache
    if not force_refresh:
        disk_cached = _load_activity_summary_from_disk()
        if disk_cached is not None:
            _activity_summary_cache = disk_cached
            return _activity_summary_cache

    with _activity_summary_lock:
        # Another thread may have finished the build (or populated the disk
        # cache) while this one was waiting for the lock -- re-check so a
        # caller that just waited reuses that result instead of running a
        # second concurrent build.
        if _activity_summary_cache is not None and not force_refresh:
            return _activity_summary_cache
        if not force_refresh:
            disk_cached = _load_activity_summary_from_disk()
            if disk_cached is not None:
                _activity_summary_cache = disk_cached
                return _activity_summary_cache

        session = _session_with_ua(session)
        cutoff = date.today() - timedelta(days=window_days)
        filing_rows = _house_filing_rows_since(cutoff, session) + _senate_filing_rows_since(cutoff, session)

        all_transactions = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            future_to_row = {pool.submit(_parse_one_filing, row, session): row for row in filing_rows}
            for future in concurrent.futures.as_completed(future_to_row):
                row = future_to_row[future]
                try:
                    txns = future.result()
                except Exception:
                    continue
                member = f"{row['first']} {row['last']}".strip()
                for txn in txns:
                    txn = dict(txn)
                    txn["member"] = member
                    txn["chamber"] = row["chamber"]
                    txn["first"] = row["first"]
                    txn["last"] = row["last"]
                    txn["member_key"] = (row["chamber"], row["last"].lower(), row["first"].lower())
                    all_transactions.append(txn)

        def parse_date(s):
            try:
                return datetime.strptime(s, "%m/%d/%Y").date()
            except Exception:
                return None

        dated = [(t, parse_date(t["transaction_date"])) for t in all_transactions]
        dated.sort(key=lambda pair: pair[1] or date.min, reverse=True)
        recent_trades = [t for t, _ in dated]

        by_member = {}
        for t in all_transactions:
            entry = by_member.setdefault(t["member_key"], {
                "member": t["member"],
                "chamber": t["chamber"],
                "first": t["first"],
                "last": t["last"],
                "net_estimated_value": 0.0,
                "transaction_count": 0,
            })
            if t["transaction_type"] == "Purchase":
                entry["net_estimated_value"] += t["amount_mid"]
            elif t["transaction_type"].startswith("Sale"):
                entry["net_estimated_value"] -= t["amount_mid"]
            entry["transaction_count"] += 1

        leaderboard = sorted(by_member.values(), key=lambda e: -e["net_estimated_value"])

        _activity_summary_cache = {"recent_trades": recent_trades, "leaderboard": leaderboard}
        _save_activity_summary_to_disk(_activity_summary_cache)
        return _activity_summary_cache
