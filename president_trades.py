"""The President's stock and ETF trades, from his OGE Form 278-T Periodic
Transaction Reports (Office of Government Ethics) -- the executive-branch
counterpart to the STOCK Act PTRs congress_trades.py parses.

How these differ from congress_trades.py's House/Senate filings:
  - The PDFs OGE posts are scans with a rough OCR text layer: "Purchase"
    comes through as "lourchaso"/"DUrchalD", dates as "8/312026" or
    "8nno26", amounts as "S1 ODO 001 -$5,000.DOO". Each field is therefore
    matched fuzzily -- the transaction type against purchase/sale, the
    amount snapped to the nearest standard 278-T value range, and a date
    that won't parse falls back to the previous row's (rows come in
    same-day batches), flagged "date_estimated".
  - Rows name the security ("BOOKING HLDGS INC", "WD40CO") but carry no
    ticker, so tickers are resolved by matching names against SEC's
    company list (data/ticker_map.json), plus
    data/president_ticker_overrides.json for what that can't resolve --
    mostly ETFs, whose names SEC's fund ticker file doesn't include.
    An unresolved row keeps its cleaned-up name in place of a ticker.
  - Municipal and corporate bonds, a large share of the filings' dollar
    value, are dropped: this tracker covers stocks and ETFs only.

Amounts are disclosed ranges, so -- exactly as for Congress -- positions
are estimates built from each transaction's range midpoint (see
congress_trades.build_politician_comparison, reused here unchanged).
"""

from __future__ import annotations

import difflib
import io
import json
import re
from datetime import date, datetime
from pathlib import Path

import pdfplumber
import requests

from congress_trades import _session_with_ua, build_politician_comparison

_DATA_DIR = Path(__file__).parent / "data"
FILINGS_PATH = _DATA_DIR / "president_filings.json"
OVERRIDES_PATH = _DATA_DIR / "president_ticker_overrides.json"
SNAPSHOT_PATH = _DATA_DIR / "president_trades.json"
# Parsed rows per filing URL (before ticker resolution) -- a filed 278-T
# never changes, so each PDF is downloaded and parsed only once.
ROWS_CACHE_PATH = _DATA_DIR / "president_filing_rows.json"
TICKER_MAP_PATH = _DATA_DIR / "ticker_map.json"

# Same "chamber|last|first" key shape as the House/Senate dropdowns (see
# dash_app._politician_dropdown_key), so the watchlist and global search
# treat the President like any other member.
PRESIDENT_CANDIDATE = {
    "chamber": "president",
    "first": "Donald",
    "last": "Trump",
    "prefix": "",
    "state_dst": "",
    "display": "Donald J. Trump",
}

TERM_START = date(2025, 1, 20)

# OGE's "Officials' Individual Disclosures" search -- the JSON API behind
# the public table at oge.gov (DataTables server-side protocol).
OGE_API_URL = "https://extapps2.oge.gov/201/Presiden.nsf/API.xsp/v3/rest"
_OGE_FILER_NAME = "Trump, Donald J"
_HREF_RE = re.compile(r"href='([^']+)'>\s*278 Transaction\s*</a>")
_FILENAME_DATE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4}|\d{2})(?!\d)")

# OGE Form 278-T's value categories (the filer's own assets).
AMOUNT_RANGES = [
    (1_001, 15_000), (15_001, 50_000), (50_001, 100_000), (100_001, 250_000),
    (250_001, 500_000), (500_001, 1_000_000), (1_000_001, 5_000_000),
    (5_000_001, 25_000_000), (25_000_001, 50_000_000),
]

# OCR confusions inside numbers.
_DIGIT_FIXES = str.maketrans({"O": "0", "o": "0", "D": "0", "Q": "0", "l": "1", "I": "1", "i": "1",
                              "|": "1", "!": "1", "]": "1", "S": "5", "s": "5", "?": "2", "B": "8"})
# The range separator OCRs as anything dash-like -- most often a bullet.
_DASHES = re.compile(r"\s*[-–—•·�~]+\s*")
_ROW_START = re.compile(r"^(\d{1,4})[.,]?\s+(.+)$")
# Bond rows: a coupon/maturity ("5.00 % Due Jun 1 2027", "DUE 01/01/55"),
# "B/E" (book entry), or municipal-revenue wording.
_BOND_RE = re.compile(r"\d\s*%|\bDu[eo]\s+\S|\bDUE\s*[\dDO]|\bB/[EQ]\b|\bREV\b|\bRFDG\b|\bBDS?\b"
                      r"|\bSCH(?:OOL)? DIST\b|\bCNTY\b|\bAUTH\b|\bMUN\b|\bHLTH\b|\bTRUNK\b|\bTPK\b|\bHSG\b"
                      # Corporate bonds: yield/maturity/call wording, or a
                      # maturity date inside the description.
                      r"|YIELD|MATURITY|CALL?ABLE|TAXBL|TAXABLE|\b\d{1,2}/\d{1,2}/\d{2,4}\b"
                      r"|\bS[EC]N[IT]OR\b|\bN[O0]T[EC]S\b|CONFIRMATION|[DO]ISCRETIONARY"
                      # "NTS" (notes), "BIE" (B/E OCR'd), a coupon "05.656'%",
                      # highway/transit munis.
                      r"|\bNTS\b|\bBIE\b|%|\bHWY|TRANS\w* COMM",
                      re.I)
# The form's own header/footer text ("Note: This is a public form. Do not
# include account numbers...").
_FORM_TEXT_RE = re.compile(r"\bdo\s+not\b|\bin\s*clud|instruction|account\s+num|public\s+form", re.I)
# Money market / cash sweep funds: neither a stock nor an ETF.
_CASH_RE = re.compile(r"\bOBL\w*|MONEY\s*M(?:AR)?KT|MONEY\s*MAR|MONEY FUND|CASH RESERVE|\bSWEEP\b", re.I)


# Broker trade-confirmation text some filings run into the security name
# ("Microsoft Corp Com Solicited Order Discretion Exercised", "Nvidia Com
# Avg Unit Price Transaction Your Broker Acted As..."), OCR'd loosely.
_SOLICITED_RE = re.compile(r"\b(?:un)?sol[\w:]{0,3}[ct][\w:]{0,3}[eo]d\b"
                           # "Solicited Order", however OCR'd: "Solldlod Ordo", "SoUciled Ordor".
                           r"|\b(?:un)?s[o0]\S{4,9}\s+[o0][\w:!']{2,5}(?=\s|$|…)", re.I)
_UNIT_PRICE_RE = re.compile(r"(?:\s\S*a\S*)?\s+\S*unit\s+pr", re.I)


def strip_confirmation_text(description):
    cut = len(description)
    for pattern in (_SOLICITED_RE, _UNIT_PRICE_RE):
        m = pattern.search(description)
        if m:
            cut = min(cut, m.start())
    return description[:cut].strip() or description


def is_bond(description):
    """True for bonds and cash funds -- anything that isn't a stock or ETF."""
    return bool(_BOND_RE.search(description) or _CASH_RE.search(description))


def _letters(token):
    return re.sub(r"[^a-z]", "", token.lower())


def _transaction_type(token):
    """'Purchase'/'Sale' if this token is an OCR'd form of one, else None."""
    t = _letters(token)
    if len(t) >= 5 and ("rch" in t or "urc" in t
                        or difflib.SequenceMatcher(None, t, "purchase").ratio() >= 0.6):
        return "Purchase"
    if 3 <= len(t) <= 5 and difflib.SequenceMatcher(None, t, "sale").ratio() >= 0.5 and t[0] in "sl":
        return "Sale"
    return None


def _is_yes_no(token):
    return _letters(token) in {"yes", "yos", "vos", "ves", "no", "na", "n0", "yas", "vas"}


def _digits(text):
    return re.sub(r"\D", "", text.translate(_DIGIT_FIXES))


def _amount_range(text):
    """Snap an OCR'd "$1 000 001 - $5,000,000" to its 278-T category:
    whichever category's low/high digit strings the OCR'd ones are
    closest to."""
    parts = _DASHES.split(text.strip(), maxsplit=1)
    if len(parts) != 2:
        return None
    # The low bound is at most its last three digit groups ("1 000 001");
    # anything earlier is a stray token from the column before.
    low_txt = " ".join(parts[0].split()[-3:])
    high_txt = parts[1]

    def bound_digits(s):
        s = s.strip()
        return _digits(s[1:] if s[:1] in "$Ss" else s)  # a "$" OCR'd as "S"

    low, high = bound_digits(low_txt), bound_digits(high_txt)
    if not low and not high:
        return None

    def score(rng):
        lo, hi = str(rng[0]), str(rng[1])
        s = difflib.SequenceMatcher(None, low, lo).ratio() + difflib.SequenceMatcher(None, high, hi).ratio()
        # Digit count is the most OCR-robust signal (a "0" misread is
        # common; dropping a whole group isn't).
        return s + 0.5 * (len(low) == len(lo)) + 0.5 * (len(high) == len(hi))

    return max(AMOUNT_RANGES, key=score)


def _parse_date(tokens, year_hint):
    """Best-effort m/d/yyyy out of OCR'd date tokens, else None."""
    raw = "".join(tokens).translate(_DIGIT_FIXES)
    raw = re.sub(r"[hn]", "/", raw)  # "8/10h026", "8nno26": a slash read as h/n
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", raw)
    if not m:
        digits = re.sub(r"\D", "", raw)
        # Year read correctly, slash(es) misread as a digit: "8/3112026".
        y = str(year_hint)
        if digits.endswith(y):
            md = digits[:-4]
            for cut in range(1, len(md)):
                for extra in (0, 1):
                    mm, dd = md[:cut], md[cut + extra:] if extra == 0 else md[cut + 1:]
                    if extra and md[cut] not in "17":
                        continue
                    if mm and dd and 1 <= int(mm) <= 12 and 1 <= int(dd) <= 31 and len(dd) <= 2:
                        try:
                            return date(int(y), int(mm), int(dd))
                        except ValueError:
                            pass
            return None
        return None
    try:
        return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
    except ValueError:
        return None


def parse_278t_rows(lines, filed_date):
    """Transaction rows from one 278-T's page text lines. Returns dicts with
    'description', 'transaction_type', 'transaction_date' (date|None),
    'amount_low'/'amount_high', 'is_bond'. Rows that wrap onto a second
    line keep only their first line's description (the type/date/amount
    columns are always on the first)."""
    rows = []
    for line in lines:
        # The row number usually leads the line, but the 2025 filings put
        # it on a line of its own -- a row is any line with a purchase/sale
        # token followed by an amount range.
        m = _ROW_START.match(line.strip())
        if _FORM_TEXT_RE.search(line):
            continue
        tokens = (m.group(2) if m else line.strip()).split()
        # Rightmost purchase/sale token with at least an amount after it.
        idx = next((i for i in range(len(tokens) - 2, 0, -1) if _transaction_type(tokens[i])), None)
        if idx is None:
            continue
        rest = tokens[idx + 1:]
        yn = next((i for i, t in enumerate(rest) if _is_yes_no(t)), None)
        date_tokens = rest[:yn] if yn is not None else rest[:1]
        amount_text = " ".join(rest[yn + 1:] if yn is not None else rest[1:])
        amount = _amount_range(amount_text) if re.search(r"\d", amount_text) else None
        if not amount:
            continue
        description = " ".join(tokens[:idx])
        trade_date = _parse_date(date_tokens, filed_date.year)
        if trade_date is None:
            # Dates near New Year can carry the prior year.
            trade_date = _parse_date(date_tokens, filed_date.year - 1)
        # A date after the filing or before the term began is an OCR
        # misread. (Not a tighter window: the first filing, Aug 2025,
        # covered trades back to January.)
        if trade_date and not (TERM_START <= trade_date <= filed_date):
            trade_date = None
        rows.append({
            "description": description,
            "transaction_type": _transaction_type(tokens[idx]),
            "transaction_date": trade_date,
            "amount_low": amount[0],
            "amount_high": amount[1],
            "is_bond": is_bond(description),
        })
    # Fill unreadable dates from the previous row: trades are listed in
    # same-day batches.
    last = None
    for r in rows:
        if r["transaction_date"]:
            last = r["transaction_date"]
            r["date_estimated"] = False
        else:
            r["transaction_date"] = last
            r["date_estimated"] = True
    return rows


def parse_278t_pdf(pdf_bytes, filed_date):
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        lines = []
        for page in pdf.pages[1:]:  # page 1 is the filer/certification cover
            lines.extend((page.extract_text() or "").splitlines())
    return parse_278t_rows(lines, filed_date)


# --- Ticker resolution ------------------------------------------------------

_ABBREVIATIONS = {
    "HLDGS": "HOLDINGS", "HLDG": "HOLDING", "INDS": "INDUSTRIES", "SVCS": "SERVICES", "SVC": "SERVICE",
    "FINL": "FINANCIAL", "INTL": "INTERNATIONAL", "TECHS": "TECHNOLOGIES", "TECH": "TECHNOLOGY",
    "MGMT": "MANAGEMENT", "PPTYS": "PROPERTIES", "PPTY": "PROPERTY", "COMMS": "COMMUNICATIONS",
    "COMMUNICATNS": "COMMUNICATIONS", "SYS": "SYSTEMS", "PHARMS": "PHARMACEUTICALS",
    "PHARMACEUTICLS": "PHARMACEUTICALS", "BANCORPORATION": "BANCORP", "GRP": "GROUP", "NATL": "NATIONAL",
    "AMER": "AMERICA", "ENTMT": "ENTERTAINMENT", "SOLUTIONS": "SOLUTIONS", "LABS": "LABORATORIES",
    "MFG": "MANUFACTURING", "INSTR": "INSTRUMENTS", "INSTRS": "INSTRUMENTS", "RES": "RESOURCES",
    "THERAPEUTCS": "THERAPEUTICS", "TR": "TRUST", "RLTY": "REALTY", "INVT": "INVESTMENT",
    "INVTS": "INVESTMENTS", "MTG": "MORTGAGE", "MATLS": "MATERIALS", "CMNTYS": "COMMUNITIES",
    "AWYS": "AIRWAYS", "WHSE": "WAREHOUSE", "ACCEP": "ACCEPTANCE", "SEMICOND": "SEMICONDUCTOR",
    "CONSTR": "CONSTRUCTION", "BK": "BANK", "WTR": "WATER", "PPTY": "PROPERTY",
}
_DROP_WORDS = {
    "INC", "CORP", "CORPORATION", "CO", "COS", "COMPANY", "COMPANIES", "LTD", "LIMITED", "PLC", "LLC", "LP",
    "NV", "SA", "AG", "THE", "CL", "CLASS", "A", "B", "C", "COM", "COMMON", "STOCK", "SHS", "ORD", "ADR",
    "SPONSORED", "NEW", "DEL", "DE", "ISIN", "STK", "REIT", "EQUITY", "EQU", "F", "I", "IP", "AVG", "NON",
    "VOTING", "IRELAND", "UNSOLICITED", "INCDEL",
    # Common OCR misreads of the suffixes above.
    "CORO", "LNC", "PIE", "PLCF",
}
_LARGE_CAP_COUNT = 1500
_LARGE_CAP_CUTOFF = 0.70
# Suffixes the OCR runs into the previous word ("WALMARTINC").
_GLUED_SUFFIX = re.compile(r"^([A-Z0-9]{2,}?)(INCDEL|INC|CORP|PLCF|PLC|LTD)$")


def normalize_name(name):
    s = name.upper().replace("!SHARES", "ISHARES").replace("&", " AND ")
    s = re.sub(r"[^A-Z0-9 ]", " ", s.replace("-", " "))
    words = []
    for w in s.split():
        m = _GLUED_SUFFIX.match(w)
        words.extend(m.groups() if m else [w])
    words = [_ABBREVIATIONS.get(w, w) for w in words]
    return " ".join(w for w in words if w not in _DROP_WORDS)


class TickerResolver:
    """Name -> ticker against SEC's company list, plus manual overrides
    (keyed by normalize_name of the 278-T description)."""

    def __init__(self, ticker_map=None, overrides=None):
        if ticker_map is None:
            ticker_map = json.loads(TICKER_MAP_PATH.read_text(encoding="utf-8"))
        if overrides is None:
            try:
                overrides = json.loads(OVERRIDES_PATH.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                overrides = {}
        self.overrides = {normalize_name(k): v for k, v in overrides.items()}
        # SEC's list is ordered roughly by market cap, so the first entry
        # for a normalized name is the one to keep.
        self.by_name = {}
        for c in ticker_map:
            if c.get("is_fund"):
                continue
            self.by_name.setdefault(normalize_name(c["title"]), c["ticker"])
        # Word sets, for SEC-style reversed names ("DISNEY WALT").
        self.by_words = {}
        for n, t in self.by_name.items():
            self.by_words.setdefault(frozenset(n.split()), t)
        self.names = list(self.by_name)
        # The largest companies (SEC's list is in rough market-cap order)
        # get a looser OCR match -- they make up most of the portfolio, and
        # among ~1,500 names a garble like "NVLOIA" has only one plausible
        # match.
        self.large_caps = self.names[:_LARGE_CAP_COUNT]
        self._cache = {}
        # description -> ticker for matches made by spelling similarity
        # alone, for build_president_snapshot to list for review.
        self.approximate = {}

    def resolve(self, description):
        key = normalize_name(description)
        if key in self._cache:
            return self._cache[key]
        self._cache[key] = ticker = self._resolve(key)
        if ticker and self._last_was_approximate:
            self.approximate[description] = ticker
        return ticker

    def _resolve(self, key):
        self._last_was_approximate = False
        if not key:
            return None
        if key in self.overrides:
            return self.overrides[key] or None
        if key in self.by_name:
            return self.by_name[key]
        if frozenset(key.split()) in self.by_words:
            return self.by_words[frozenset(key.split())]
        # 278-T descriptions are truncated to ~20 characters ("CONSTRUCTION
        # PARTNERS IN"): drop a trailing partial word, then prefix-match.
        words = key.split()
        for k in (key, " ".join(words[:-1]) if len(words) > 1 else None):
            if k and len(k) >= 6:
                if k in self.by_name:
                    return self.by_name[k]
                prefixed = next((n for n in self.names if n.startswith(k + " ")), None)
                if prefixed:
                    return self.by_name[prefixed]
        # OCR noise: a close-enough spelling ("PATTERSON UT1 ENERGY"),
        # against the overrides too ("APPREOATION").
        close = difflib.get_close_matches(key, list(self.overrides), n=1, cutoff=0.85)
        if close:
            return self.overrides[close[0]] or None
        self._last_was_approximate = True
        pool = [n for n in self.names if n[:1] == key[:1]]
        close = difflib.get_close_matches(key, pool, n=1, cutoff=0.88)
        if close:
            return self.by_name[close[0]]
        if len(key) >= 5:
            close = difflib.get_close_matches(key, self.large_caps, n=1, cutoff=_LARGE_CAP_CUTOFF)
            # The first word must be close too, or a shared generic word
            # ("HOLDINGS", "PHARMACEUTICALS") carries the match on its own
            # (LiveRamp Holdings scored as IQVIA Holdings).
            if close and difflib.SequenceMatcher(None, key.split()[0], close[0].split()[0]).ratio() >= 0.6:
                return self.by_name[close[0]]
        return None


# --- Filings and snapshot ---------------------------------------------------

def load_filings():
    """data/president_filings.json: [{"url", "filed": "YYYY-MM-DD"}, ...] --
    every 278-T found so far (see discover_filings), kept so a day the OGE
    API is down still builds from the known filings."""
    try:
        return json.loads(FILINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def save_filings(filings):
    FILINGS_PATH.write_text(json.dumps(sorted(filings, key=lambda f: f["filed"], reverse=True), indent=2) + "\n",
                            encoding="utf-8")


def _filed_date(url, posted):
    """The filing date in the PDF's name ("Donald-J-Trump-08.12.2025-278T",
    "Donald J. Trump 9.3.25 278-T"); else the date OGE posted it."""
    m = _FILENAME_DATE_RE.search(requests.utils.unquote(url.rsplit("/", 1)[-1]))
    if m:
        month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return date(year + 2000 if year < 100 else year, month, day)
        except ValueError:
            pass
    return date.fromisoformat(posted[:10])


def discover_filings(session=None):
    """Every 278-T the President has filed, from OGE's disclosure API.
    Each part of a multi-part filing ("…278T(2).pdf") is its own entry."""
    session = _session_with_ua(session)
    resp = session.get(OGE_API_URL, params={"draw": 1, "start": 0, "length": 500,
                                            "search[value]": _OGE_FILER_NAME.split(",")[0]}, timeout=60)
    resp.raise_for_status()
    filings = {}
    for row in resp.json().get("data", []):
        if row.get("name") != _OGE_FILER_NAME or row.get("title") != "President":
            continue
        m = _HREF_RE.search(row.get("type", ""))
        if not m:
            continue  # annual reports, termination, candidate filings
        url = m.group(1)
        filings[url] = {"url": url, "filed": _filed_date(url, row.get("docDate") or "").isoformat()}
    return list(filings.values())


def fetch_filing_rows(filing, session=None):
    session = _session_with_ua(session)
    resp = session.get(filing["url"], timeout=120)
    resp.raise_for_status()
    return parse_278t_pdf(resp.content, date.fromisoformat(filing["filed"]))


def rows_to_json(rows):
    return [{**r, "transaction_date": r["transaction_date"].isoformat() if r["transaction_date"] else None}
            for r in rows]


def rows_from_json(rows):
    return [{**r, "transaction_date": date.fromisoformat(r["transaction_date"]) if r["transaction_date"] else None}
            for r in rows]


def to_transactions(rows, resolver, filing):
    """Stock/ETF rows -> congress_trades-shaped transactions (bonds dropped)."""
    out = []
    for r in rows:
        r = {**r, "description": strip_confirmation_text(r["description"])}
        # Re-checked here rather than trusting the cached row's is_bond, so
        # a change to _BOND_RE applies without re-parsing the PDFs.
        if is_bond(r["description"]) or not r["transaction_date"] or not normalize_name(r["description"]):
            continue
        ticker = resolver.resolve(r["description"])
        low, high = r["amount_low"], r["amount_high"]
        out.append({
            "ticker": ticker or _display_name(r["description"]),
            "ticker_resolved": bool(ticker),
            "asset_description": r["description"],
            "transaction_type": r["transaction_type"],
            "transaction_date": r["transaction_date"].strftime("%m/%d/%Y"),
            "date_estimated": r["date_estimated"],
            "amount_low": low,
            "amount_high": high,
            "amount_mid": (low + high) / 2,
            "owner": "",
            "filing_url": filing["url"],
        })
    return out


def _display_name(description, limit=60):
    """An unresolved security's name, in place of its ticker."""
    name = " ".join(description.replace("!SHARES", "ISHARES").split())
    return name if len(name) <= limit else name[:limit - 1].rstrip() + "…"


def load_snapshot():
    try:
        return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def build_snapshot(filings_rows, top_n=10):
    """filings_rows: [(filing, transactions), ...] -> the JSON the app reads."""
    transactions = [t for _f, ts in filings_rows for t in ts]
    comparison = build_politician_comparison(transactions, top_n=top_n) if transactions else {
        "top_increases": [], "top_decreases": [], "all_positions": []}
    filings = sorted(({"url": f["url"], "filed": f["filed"], "trades": len(ts),
                       "bought": sum(t["amount_mid"] for t in ts if t["transaction_type"] == "Purchase"),
                       "sold": sum(t["amount_mid"] for t in ts if t["transaction_type"] == "Sale")}
                      for f, ts in filings_rows), key=lambda f: f["filed"], reverse=True)
    trade_dates = [datetime.strptime(t["transaction_date"], "%m/%d/%Y").date() for t in transactions]
    return {
        "candidate": PRESIDENT_CANDIDATE,
        "as_of": filings[0]["filed"] if filings else None,
        "first_trade": min(trade_dates).isoformat() if trade_dates else None,
        "last_trade": max(trade_dates).isoformat() if trade_dates else None,
        "built": datetime.now().date().isoformat(),
        "filings": filings,
        **comparison,
    }
