"""A fund's top holdings: Yahoo's current breakdown when Yahoo will serve it,
otherwise the fund's latest SEC N-PORT filing.

Yahoo answers HTTP 401 to fund-data requests from the production server
(seen since 2026-10) while serving the same requests from elsewhere. N-PORT
is the portfolio every registered fund reports to the SEC -- older (the
public filings are quarterly, about two months after quarter end) but always
reachable. Exchange-traded unit trusts like SPY and DIA don't file N-PORT,
so they still depend on Yahoo.
"""
import heapq
import re
import threading
import time
import xml.etree.ElementTree as ET

import pandas as pd
import requests

from company_growth_calc import MF_TICKER_MAP_URL, load_ticker_map, resolve_ticker_for_security, sec_get
from stock_price import HoldingsDataError, fetch_top_holdings as _fetch_yahoo_top_holdings
from thirteenf import ARCHIVES_URL, USER_AGENT, _get, _local_tag, _throttle

_BROWSE_URL = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={series}"
               "&type=NPORT-P&count=10&output=atom")
_FILING_TYPE_RE = re.compile(r"<filing-type>(.*?)</filing-type>", re.DOTALL)
_ENTRY_RE = re.compile(r"<entry>(.*?)</entry>", re.DOTALL)
_FILING_HREF_RE = re.compile(r"<filing-href>.*?/data/(\d+)/(\d+)/", re.DOTALL)

# "Alphabet Inc., Class A", "ALPHABET INC-A"
_SHARE_CLASS_RE = re.compile(r"(?:\bCLASS|\bCL|-)\s*([A-Z])\s*$", re.IGNORECASE)
_TICKER_RE = re.compile(r"[A-Z]{1,5}(?:-[A-Z])?")
# Share classes that SEC's ticker map can't tell apart by name: every class
# has the same registrant title, so name matching can't pick one.
_CUSIP_TICKERS = {"02079K305": "GOOGL", "02079K107": "GOOG", "084670702": "BRK-B", "084670108": "BRK-A"}
_NON_HOLDING_ASSET_CATS = {"STIV"}  # securities-lending cash collateral vehicles

_CACHE_TTL = 12 * 3600  # holdings barely move within a day
_cache = {}
_cache_lock = threading.Lock()
_fetch_locks = {}
_series_ids = None
_series_ids_lock = threading.Lock()


def fetch_top_holdings(ticker, session=None, limit=10):
    """`ticker`'s top holdings as a DataFrame (symbol, name, holding_pct):
    from Yahoo, else from SEC N-PORT with df.attrs["as_of"] set to the
    filing's period (ISO date). If both fail, the last good copy if there is
    one, else raises HoldingsDataError. `symbol` is "" for a holding with no
    resolvable ticker."""
    key = ticker.upper()
    with _cache_lock:
        cached = _cache.get(key)
        if cached and time.time() - cached[0] < _CACHE_TTL:
            return cached[1].copy()
        fetch_lock = _fetch_locks.setdefault(key, threading.Lock())
    with fetch_lock:
        # Another request may have filled it while this one waited.
        with _cache_lock:
            cached = _cache.get(key)
            if cached and time.time() - cached[0] < _CACHE_TTL:
                return cached[1].copy()
        try:
            df = _fetch_uncached(key, session, limit)
        except HoldingsDataError:
            if cached:
                return cached[1].copy()
            raise
        with _cache_lock:
            _cache[key] = (time.time(), df)
        return df.copy()


def _fetch_uncached(ticker, session, limit):
    try:
        return _fetch_yahoo_top_holdings(ticker, limit=limit)
    except HoldingsDataError as e:
        yahoo_error = e
    session = session or requests.Session()
    try:
        return _fetch_sec_top_holdings(ticker, session, limit)
    except (HoldingsDataError, requests.RequestException, ET.ParseError, ValueError) as e:
        print(f"Holdings unavailable for {ticker}: Yahoo: {yahoo_error}; SEC: {e}")
        raise HoldingsDataError("Top holdings aren't available right now.") from e


def _series_id(ticker, session):
    """`ticker`'s SEC series ID, or None if it isn't in SEC's fund list. The
    committed ticker map doesn't keep series IDs, and only this fallback
    needs them, so they're fetched on first use."""
    global _series_ids
    with _series_ids_lock:
        if _series_ids is None:
            rows = sec_get(session, MF_TICKER_MAP_URL)["data"]
            _series_ids = {symbol.upper(): series for _cik, series, _class, symbol in rows if symbol and series}
        return _series_ids.get(ticker)


def _latest_filing_base(series, session):
    """Archive folder URL of the series' latest original N-PORT-P. Amendments
    are skipped: they can restate a much older period."""
    atom = _get(session, _BROWSE_URL.format(series=series)).text
    for entry in _ENTRY_RE.findall(atom):
        filing_type = _FILING_TYPE_RE.search(entry)
        href = _FILING_HREF_RE.search(entry)
        if filing_type and href and filing_type.group(1).strip() == "NPORT-P":
            return f"{ARCHIVES_URL}/{href.group(1)}/{href.group(2)}"
    return None


def _child_text(el, tag):
    for child in el:
        if _local_tag(child.tag) == tag:
            return (child.text or "").strip()
    return ""


def _filing_ticker(inv):
    for child in inv:
        if _local_tag(child.tag) == "identifiers":
            for identifier in child:
                if _local_tag(identifier.tag) == "ticker":
                    return (identifier.get("value") or "").strip().upper()
    return ""


def _iter_positions(session, url):
    """Yields ("period", iso_date) once and ("position", dict) per holding,
    streamed off the response: a large bond fund's filing has tens of
    thousands of positions, too many to build a full tree of on a 512MB
    instance (see thirteenf._iter_infotable_rows)."""
    _throttle()
    with session.get(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"},
                     timeout=60, stream=True) as resp:
        resp.raise_for_status()
        resp.raw.decode_content = True
        root = None
        for event, el in ET.iterparse(resp.raw, events=("start", "end")):
            if event == "start":
                if root is None:
                    root = el
                continue
            tag = _local_tag(el.tag)
            if tag == "repPdDate":
                yield "period", (el.text or "").strip()
            elif tag == "invstOrSec":
                try:
                    pct = float(_child_text(el, "pctVal"))
                except ValueError:
                    pct = 0.0
                if pct > 0 and _child_text(el, "assetCat") not in _NON_HOLDING_ASSET_CATS:
                    yield "position", {"pct": pct, "name": _child_text(el, "name"),
                                       "title": _child_text(el, "title"), "cusip": _child_text(el, "cusip"),
                                       "ticker": _filing_ticker(el)}
                root.clear()


def _top_positions(events, limit):
    period = None

    def positions():
        nonlocal period
        for kind, value in events:
            if kind == "period":
                period = value
            else:
                yield value

    top = heapq.nlargest(limit, positions(), key=lambda p: p["pct"])
    return period, top


def _holding_row(position, companies, known_tickers):
    """{symbol, name, holding_pct} for one N-PORT position. The filing's own
    ticker when it has a real one (most filers leave it out), else matched
    by CUSIP or company name; "" rather than a guess if none match."""
    name = position["name"]
    share_class = _SHARE_CLASS_RE.search(position["title"])
    if share_class:
        name = f"{name.rstrip('.,')}, Class {share_class.group(1).upper()}"

    filed = position["ticker"]
    if filed in known_tickers and _TICKER_RE.fullmatch(filed):
        symbol = filed
    elif position["cusip"] in _CUSIP_TICKERS:
        symbol = _CUSIP_TICKERS[position["cusip"]]
    else:
        lookup_name = f"{position['name']} (CL {share_class.group(1).upper()})" if share_class else position["name"]
        symbol = resolve_ticker_for_security(lookup_name, companies) or ""
    return {"symbol": symbol, "name": name, "holding_pct": position["pct"]}


def _fetch_sec_top_holdings(ticker, session, limit):
    series = _series_id(ticker, session)
    if not series:
        raise HoldingsDataError(f"{ticker} isn't in SEC's fund list.")
    base = _latest_filing_base(series, session)
    if not base:
        raise HoldingsDataError(f"No N-PORT filing for {ticker}.")
    period, top = _top_positions(_iter_positions(session, f"{base}/primary_doc.xml"), limit)
    if not top:
        raise HoldingsDataError(f"{ticker}'s N-PORT filing lists no holdings.")
    companies = load_ticker_map(session)
    known_tickers = {c["ticker"] for c in companies}
    df = pd.DataFrame([_holding_row(p, companies, known_tickers) for p in top],
                      columns=["symbol", "name", "holding_pct"])
    df.attrs["as_of"] = period
    return df
