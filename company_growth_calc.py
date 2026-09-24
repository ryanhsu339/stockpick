#!/usr/bin/env python3
"""
company_growth.py — generate a CSV of Sales, EPS, Equity, Cash & ROIC growth
rates for any US company that files with the SEC, using SEC EDGAR's public
XBRL data (data.sec.gov). No API key needed.

USAGE
    python company_growth.py AAPL
    python company_growth.py "Micron Technology"
    python company_growth.py NVDA --years 8 --output nvda.csv

REQUIREMENTS
    Python 3.8+
    pip install requests

IMPORTANT — SEC fair-access policy
    SEC EDGAR requires every request to carry a descriptive User-Agent with a
    real contact (they will rate-limit or block generic/missing ones). Edit
    USER_AGENT below to "YourName YourEmail@example.com" before running this,
    or pass --user-agent "YourName you@example.com" on the command line.

WHAT THIS COMPUTES
    Revenue, net income, total stockholders' equity, and cash & equivalents
    come straight from the company's 10-K filings (as tagged in XBRL).
    ROIC is NOT a reported GAAP figure — it's calculated here as:
        NOPAT = Operating Income x (1 - effective tax rate)
        Invested Capital = Total Debt + Total Equity - Cash & Equivalents
        ROIC = NOPAT / Invested Capital
    (ending-period balances; this is one common convention among several —
    other data providers may use average invested capital and get slightly
    different numbers).

    All growth-rate and ROIC columns are written as DECIMAL FRACTIONS
    (0.2925 = 29.25%), not already-multiplied percentages. That way, if you
    apply a "Percentage" number format to those columns in Excel/Sheets, they
    display correctly instead of being multiplied by 100 twice.

KNOWN LIMITATIONS (read before trusting old data blindly)
    - Earnings growth is computed from total net income (NetIncomeLoss /
      ProfitLoss), not per-share EPS, specifically so stock splits (which
      change share count but not total earnings) don't distort it.
    - Total debt: SEC filers don't use one consistent tag. This script tries
      a "total debt" tag first, then falls back to summing current +
      long-term debt components. If it finds nothing at all, it assumes debt
      = 0 for that year (correct for many debt-free companies, but double
      check for others) and prints a warning.
    - Foreign private issuers that file 20-F instead of 10-K, and companies
      that don't file with the SEC at all, are out of scope — this only
      covers SEC 10-K filers.
    - Very early-stage or newly-public companies may not have `--years`
      worth of 10-K history yet; the script will just return what exists.
"""

import argparse
import sys
import time
from datetime import date

try:
    import requests
except ImportError:
    sys.exit("This script needs the 'requests' package. Install it with:\n"
              "    pip install requests")


class CompanyLookupError(Exception):
    """Raised when a ticker/company-name query can't be resolved to exactly
    one SEC filer. `candidates` is a list of company dicts (same shape as
    the SEC ticker map: "ticker", "title", "cik_str") when the query was
    ambiguous (empty for a plain no-match)."""

    def __init__(self, message, candidates=None):
        super().__init__(message)
        self.candidates = candidates or []


class CompanyDataError(Exception):
    """Raised when a company's SEC XBRL data isn't usable (missing tags,
    not enough overlapping fiscal years, etc.)."""

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

USER_AGENT = "ryan.hsu1993@gmail.com"  # <-- put your real contact here

TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
# ETFs and mutual funds aren't operating companies, so they're absent from
# company_tickers.json above -- they're registered investment companies,
# listed separately here. This file has no fund/company name field (just
# cik/seriesId/classId/symbol), so merged entries below get title=symbol.
MF_TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers_mf.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"

# Ordered fallback lists: the script tries each tag in order until one has data.
REVENUE_TAGS = [
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
    "SalesRevenueGoodsNet",
    "SalesRevenueServicesNet",
]
EQUITY_TAGS = [
    "StockholdersEquity",
    "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
]
CASH_TAGS = [
    "CashAndCashEquivalentsAtCarryingValue",
    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
]
OPINCOME_TAGS = ["OperatingIncomeLoss"]
TAX_TAGS = ["IncomeTaxExpenseBenefit"]
NETINCOME_TAGS = ["NetIncomeLoss", "ProfitLoss"]
DEBT_TOTAL_TAGS = ["LongTermDebt", "DebtLongtermAndShorttermCombinedAmount"]
DEBT_PART_TAGS = ["LongTermDebtNoncurrent", "LongTermDebtCurrent",
                  "LongTermDebtCurrentPortion", "ShortTermBorrowings", "DebtCurrent"]

# Additional line items used only by the income-statement view (build_income_statement).
COST_OF_REVENUE_TAGS = ["CostOfGoodsAndServicesSold", "CostOfRevenue",
                         "CostOfGoodsSold", "CostOfServices"]
GROSS_PROFIT_TAGS = ["GrossProfit"]
OPEX_TAGS = ["OperatingExpenses", "CostsAndExpenses"]
INTEREST_INCOME_EXPENSE_TAGS = ["InterestIncomeExpenseNonoperatingNet",
                                 "InterestIncomeExpenseNet", "InterestExpenseNet"]
OTHER_INCOME_EXPENSE_TAGS = ["OtherNonoperatingIncomeExpense", "NonoperatingIncomeExpense"]
PRETAX_INCOME_TAGS = [
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic",
]
EPS_BASIC_TAGS = ["EarningsPerShareBasic"]
EPS_DILUTED_TAGS = ["EarningsPerShareDiluted"]

# Ordered (label, tag_candidates) rows for the income-statement view. Gross
# Profit and Operating Expense are derived by subtraction in
# build_income_statement when a company doesn't tag them directly.
INCOME_STATEMENT_LINES = [
    ("Total Revenue", REVENUE_TAGS),
    ("Cost of Revenue", COST_OF_REVENUE_TAGS),
    ("Gross Profit", GROSS_PROFIT_TAGS),
    ("Operating Expense", OPEX_TAGS),
    ("Operating Income", OPINCOME_TAGS),
    ("Net Non-Operating Interest", INTEREST_INCOME_EXPENSE_TAGS),
    ("Other Income (Expense)", OTHER_INCOME_EXPENSE_TAGS),
    ("Pretax Income", PRETAX_INCOME_TAGS),
    ("Tax Provision", TAX_TAGS),
    ("Net Income", NETINCOME_TAGS),
    ("Basic EPS", EPS_BASIC_TAGS),
    ("Diluted EPS", EPS_DILUTED_TAGS),
]

# Additional line items used only by the balance-sheet view (build_balance_sheet).
SHORT_TERM_INVESTMENTS_TAGS = ["ShortTermInvestments", "MarketableSecuritiesCurrent",
                                "AvailableForSaleSecuritiesCurrent"]
CURRENT_ASSETS_TAGS = ["AssetsCurrent"]
TOTAL_ASSETS_TAGS = ["Assets"]
CURRENT_LIABILITIES_TAGS = ["LiabilitiesCurrent"]
TOTAL_LIABILITIES_TAGS = ["Liabilities"]

# Ordered (label, tag_candidates) rows for the balance-sheet view. "Total
# Debt" is special-cased in build_balance_sheet to reuse get_debt_series's
# total-tag-then-sum-parts fallback; "Total Liabilities" falls back to
# Total Assets - Total Equity when a company doesn't tag it directly.
BALANCE_SHEET_LINES = [
    ("Cash & Equivalents", CASH_TAGS),
    ("Short-Term Investments", SHORT_TERM_INVESTMENTS_TAGS),
    ("Total Current Assets", CURRENT_ASSETS_TAGS),
    ("Total Assets", TOTAL_ASSETS_TAGS),
    ("Total Current Liabilities", CURRENT_LIABILITIES_TAGS),
    ("Total Debt", DEBT_TOTAL_TAGS),
    ("Total Liabilities", TOTAL_LIABILITIES_TAGS),
    ("Total Stockholders Equity", EQUITY_TAGS),
]

# Additional line items used only by the cash-flow view (build_cash_flow_statement).
DA_TAGS = ["DepreciationDepletionAndAmortization", "DepreciationAmortizationAndAccretionNet",
           "DepreciationAndAmortization"]
SBC_TAGS = ["ShareBasedCompensation"]
OPERATING_CF_TAGS = ["NetCashProvidedByUsedInOperatingActivities",
                      "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"]
CAPEX_TAGS = ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsForCapitalImprovements",
              "PaymentsToAcquireProductiveAssets"]
INVESTING_CF_TAGS = ["NetCashProvidedByUsedInInvestingActivities",
                      "NetCashProvidedByUsedInInvestingActivitiesContinuingOperations"]
FINANCING_CF_TAGS = ["NetCashProvidedByUsedInFinancingActivities",
                      "NetCashProvidedByUsedInFinancingActivitiesContinuingOperations"]
NET_CHANGE_IN_CASH_TAGS = [
    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseIncludingExchangeRateEffect",
    "CashAndCashEquivalentsPeriodIncreaseDecrease", "CashPeriodIncreaseDecrease",
]

# Ordered (label, tag_candidates) rows for the cash-flow view. Capital
# Expenditures is stored as a negative (a use of cash), matching how it's
# conventionally shown on the statement, even though the SEC tag reports a
# positive payment amount — build_cash_flow_statement negates it. Free Cash
# Flow has no tag_candidates; it's derived as Cash from Operations +
# Capital Expenditures (i.e. operating cash flow minus capex).
CASH_FLOW_LINES = [
    ("Net Income", NETINCOME_TAGS),
    ("Depreciation & Amortization", DA_TAGS),
    ("Stock-Based Compensation", SBC_TAGS),
    ("Cash from Operations", OPERATING_CF_TAGS),
    ("Capital Expenditures", CAPEX_TAGS),
    ("Cash from Investing", INVESTING_CF_TAGS),
    ("Cash from Financing", FINANCING_CF_TAGS),
    ("Net Change in Cash", NET_CHANGE_IN_CASH_TAGS),
    ("Free Cash Flow", None),
]


# ---------------------------------------------------------------------------
# SEC lookups
# ---------------------------------------------------------------------------

def sec_get(session, url):
    resp = session.get(url, headers={"User-Agent": USER_AGENT,
                                      "Accept-Encoding": "gzip, deflate"},
                        timeout=30)
    resp.raise_for_status()
    return resp.json()


_ticker_cache = {"companies": None, "loaded_at": 0}
TICKER_CACHE_TTL = 24 * 3600  # SEC republishes this file periodically, not per-request


def load_ticker_map(session, use_cache=True):
    if use_cache and _ticker_cache["companies"] is not None \
            and (time.time() - _ticker_cache["loaded_at"]) < TICKER_CACHE_TTL:
        return _ticker_cache["companies"]
    data = sec_get(session, TICKER_MAP_URL)
    # data is {"0": {"cik_str":..., "ticker":..., "title":...}, "1": {...}, ...}
    companies = list(data.values())

    # Merge in ETFs/mutual funds so tickers like "SOXX" resolve too. Best
    # effort: if SEC's fund file is unreachable, just fall back to operating
    # companies only rather than failing the whole ticker map.
    try:
        mf_data = sec_get(session, MF_TICKER_MAP_URL)
        existing_tickers = {c["ticker"].upper() for c in companies}
        seen_fund_tickers = set()
        for cik, _series_id, _class_id, symbol in mf_data.get("data", []):
            symbol = symbol.upper()
            if not symbol or symbol in existing_tickers or symbol in seen_fund_tickers:
                continue
            seen_fund_tickers.add(symbol)
            companies.append({"cik_str": cik, "ticker": symbol, "title": symbol, "is_fund": True})
    except (requests.RequestException, ValueError, KeyError):
        pass

    _ticker_cache["companies"] = companies
    _ticker_cache["loaded_at"] = time.time()
    return companies


def resolve_company(query, companies):
    """Return the single matching company dict, or raise CompanyLookupError
    (with a human-readable message, including candidates for an ambiguous
    name match) if the query doesn't resolve to exactly one."""
    q = query.strip()
    q_upper = q.upper()
    exact = [c for c in companies if c["ticker"].upper() == q_upper]
    if len(exact) == 1:
        return exact[0]

    q_lower = q.lower()
    name_matches = [c for c in companies if q_lower in c["title"].lower()]
    if len(name_matches) == 1:
        return name_matches[0]
    if len(name_matches) > 1:
        message = f"Multiple companies match '{query}' ({len(name_matches)}). Pick one:"
        raise CompanyLookupError(message, candidates=name_matches)

    raise CompanyLookupError(f"No company found matching '{query}'.")


def search_companies(query, companies, limit=8):
    """Rank companies for a live-typeahead search box: exact ticker match,
    then ticker-starts-with, then name-starts-with, then name-contains.
    Within each group, shorter tickers sort first (a proxy for
    "established company" — SEC's ticker file carries no market-cap or
    popularity signal to rank by, but well-known names tend to hold short
    tickers while micro-caps and funds tend to have longer ones), then
    alphabetically. Returns up to `limit` company dicts, or [] for a query
    shorter than 2 characters."""
    q = query.strip()
    if len(q) < 2:
        return []
    q_upper = q.upper()
    q_lower = q.lower()

    exact, ticker_prefix, name_prefix, name_contains = [], [], [], []
    for c in companies:
        ticker = c["ticker"].upper()
        title_lower = c["title"].lower()
        if ticker == q_upper:
            exact.append(c)
        elif ticker.startswith(q_upper):
            ticker_prefix.append(c)
        elif title_lower.startswith(q_lower):
            name_prefix.append(c)
        elif q_lower in title_lower:
            name_contains.append(c)

    ranked = []
    for group in (exact, ticker_prefix, name_prefix, name_contains):
        group.sort(key=lambda c: (len(c["ticker"]), c["ticker"]))
        ranked.extend(group)
        if len(ranked) >= limit:
            break
    return ranked[:limit]


# ---------------------------------------------------------------------------
# XBRL parsing
# ---------------------------------------------------------------------------

def _extract_series(entries, instant):
    """From a list of raw XBRL fact entries, keep only 10-K (or 10-K/A)
    filings, and for duration facts only full-fiscal-year periods (350-380
    days). When several entries cover the same period end (e.g. because a
    later 10-K restated it for a stock split), keep the one with the latest
    'filed' date. Returns {end_date_str: value}."""
    best = {}  # end -> (val, filed)
    for e in entries:
        form = e.get("form", "")
        if not form.startswith("10-K"):
            continue
        end = e.get("end")
        start = e.get("start")
        if instant:
            if start:
                continue
        else:
            if not start or not end:
                continue
            try:
                days = (date.fromisoformat(end) - date.fromisoformat(start)).days
            except ValueError:
                continue
            if not (350 <= days <= 380):
                continue
        filed = e.get("filed", "")
        val = e.get("val")
        if val is None:
            continue
        if end not in best or filed > best[end][1]:
            best[end] = (val, filed)
    return {end: v[0] for end, v in best.items()}


def get_series(facts, tag_candidates, instant):
    """Merge every candidate tag into one series, keyed by fiscal year-end.
    Companies sometimes switch which XBRL tag they report a concept under
    (e.g. after an accounting standard update), so a single tag's history
    can have gaps that another tag fills. When more than one tag has a
    value for the same period end, the earlier tag in tag_candidates wins.
    Returns (series_dict, tags_used_str)."""
    merged = {}
    used_tags = []
    for tag in tag_candidates:
        node = facts.get(tag)
        if not node:
            continue
        units = node.get("units", {})
        entries = units.get("USD") or units.get("USD/shares")
        if not entries:
            continue
        series = _extract_series(entries, instant)
        if not series:
            continue
        used_tags.append(tag)
        for end, val in series.items():
            if end not in merged:
                merged[end] = val
    return merged, ("+".join(used_tags) if used_tags else None)


def get_debt_series(facts):
    series, tag = get_series(facts, DEBT_TOTAL_TAGS, instant=True)
    if series:
        return series, tag
    # fall back: sum whichever current/non-current debt components exist
    combined = {}
    used_tags = []
    for tag in DEBT_PART_TAGS:
        node = facts.get(tag)
        if not node:
            continue
        entries = node.get("units", {}).get("USD")
        if not entries:
            continue
        part = _extract_series(entries, instant=True)
        if part:
            used_tags.append(tag)
            for end, val in part.items():
                combined[end] = combined.get(end, 0) + val
    return combined, ("+".join(used_tags) if used_tags else None)


def _raw_duration_entries(facts, tag_candidates, form_prefix):
    """Like get_series, but returns every raw duration-fact entry for the
    first matching tag (not deduped by period end) — needed for TTM, where
    a single end date can have both a single-quarter and a year-to-date
    fact that must be told apart by their differing start dates."""
    for tag in tag_candidates:
        node = facts.get(tag)
        if not node:
            continue
        entries = node.get("units", {}).get("USD") or node.get("units", {}).get("USD/shares")
        if not entries:
            continue
        filtered = [e for e in entries
                    if e.get("form", "").startswith(form_prefix)
                    and e.get("val") is not None and e.get("start") and e.get("end")]
        if filtered:
            return filtered, tag
    return [], None


def _shift_one_year(iso_date):
    d = date.fromisoformat(iso_date)
    try:
        return d.replace(year=d.year - 1).isoformat()
    except ValueError:  # Feb 29 with no leap year one year back
        return d.replace(year=d.year - 1, day=28).isoformat()


def compute_ttm(facts, tag_candidates, annual_series, most_recent_annual_end):
    """Trailing-twelve-months = latest full fiscal year + the most recent
    10-Q's year-to-date figure - the same year-to-date period a year
    earlier. Returns None if a 10-Q hasn't been filed since the last 10-K,
    or a matching prior-year comparative period can't be found."""
    annual_val = annual_series.get(most_recent_annual_end)
    if annual_val is None:
        return None

    q_entries, _ = _raw_duration_entries(facts, tag_candidates, "10-Q")
    if not q_entries:
        return None
    latest_end = max(e["end"] for e in q_entries)
    if latest_end <= most_recent_annual_end:
        return None  # no quarter filed past the latest 10-K yet

    same_end = [e for e in q_entries if e["end"] == latest_end]

    def duration_days(e):
        return (date.fromisoformat(e["end"]) - date.fromisoformat(e["start"])).days

    # the year-to-date fact is the longest-duration one reported for this end
    # (a 3-month quarter fact may also exist for the same end date)
    same_end.sort(key=lambda e: (duration_days(e), e.get("filed", "")))
    current_ytd = same_end[-1]
    dur = duration_days(current_ytd)
    if not (60 <= dur <= 300):
        return None

    target_start = _shift_one_year(current_ytd["start"])
    target_end = _shift_one_year(current_ytd["end"])

    def close(d1, d2, tol_days=5):
        return abs((date.fromisoformat(d1) - date.fromisoformat(d2)).days) <= tol_days

    all_entries, _ = _raw_duration_entries(facts, tag_candidates, "10-")
    prior = [e for e in all_entries
             if close(e["start"], target_start) and close(e["end"], target_end)]
    if not prior:
        return None
    prior.sort(key=lambda e: e.get("filed", ""))
    prior_val = prior[-1]["val"]

    return annual_val - prior_val + current_ytd["val"]


def _raw_instant_entries(facts, tag_candidates, form_prefix):
    """Like _raw_duration_entries, but for instant (balance-sheet) facts —
    which carry an 'end' and no 'start'."""
    for tag in tag_candidates:
        node = facts.get(tag)
        if not node:
            continue
        entries = node.get("units", {}).get("USD") or node.get("units", {}).get("USD/shares")
        if not entries:
            continue
        filtered = [e for e in entries
                    if e.get("form", "").startswith(form_prefix)
                    and e.get("val") is not None and e.get("end") and not e.get("start")]
        if filtered:
            return filtered, tag
    return [], None


def compute_mrq(facts, tag_candidates, most_recent_annual_end):
    """Most-recent-quarter snapshot: the latest instant value from a 10-Q
    filed after the latest 10-K. Returns (value, period_end) or (None, None)
    if no such 10-Q balance exists for this tag."""
    q_entries, _ = _raw_instant_entries(facts, tag_candidates, "10-Q")
    if not q_entries:
        return None, None
    candidates = [e for e in q_entries if e["end"] > most_recent_annual_end]
    if not candidates:
        return None, None
    latest_end = max(e["end"] for e in candidates)
    same_end = [e for e in candidates if e["end"] == latest_end]
    same_end.sort(key=lambda e: e.get("filed", ""))
    return same_end[-1]["val"], latest_end


# ---------------------------------------------------------------------------
# Computation
# ---------------------------------------------------------------------------

def build_rows(facts, years):
    rev, rev_tag = get_series(facts, REVENUE_TAGS, instant=False)
    eq, _ = get_series(facts, EQUITY_TAGS, instant=True)
    cash, _ = get_series(facts, CASH_TAGS, instant=True)
    opinc, _ = get_series(facts, OPINCOME_TAGS, instant=False)
    tax, _ = get_series(facts, TAX_TAGS, instant=False)
    ni, _ = get_series(facts, NETINCOME_TAGS, instant=False)
    debt, debt_tag = get_debt_series(facts)

    if not rev:
        raise CompanyDataError("Could not find any revenue data for this company in "
                                "SEC XBRL. It may not be a standard 10-K filer.")
    debt_warning = None
    if debt_tag is None:
        debt_warning = ("Note: no debt tag found — assuming total debt = $0 for ROIC. "
                         "Verify this is correct for this company.")

    # fiscal year-end dates we can build a full row for
    common_ends = sorted(set(rev) & set(eq) & set(cash))
    if not common_ends:
        raise CompanyDataError("Revenue, equity, and cash data don't share any common "
                                "fiscal year-end dates — can't build rows.")

    ends = common_ends[-(years + 1):]  # need one extra year as the base for growth
    if len(ends) < 2:
        raise CompanyDataError("Not enough fiscal years of history found to compute growth rates.")

    rows = []
    roic_by_end = {}
    for end in ends:
        d_val = debt.get(end, 0)
        e_val = eq.get(end)
        c_val = cash.get(end)
        oi = opinc.get(end)
        tx = tax.get(end)
        n_val = ni.get(end)

        roic = None
        if oi is not None and tx is not None and n_val is not None and e_val is not None and c_val is not None:
            pretax = n_val + tx
            rate = (tx / pretax) if pretax else 0.0
            nopat = oi * (1 - rate)
            ic = d_val + e_val - c_val
            if ic:
                roic = nopat / ic
        roic_by_end[end] = roic

        rows.append({
            "end": end,
            "fy_label": end[:4],  # calendar year the fiscal period ends in
            "revenue_m": rev.get(end, 0) / 1e6,
            "net_income_m": (n_val or 0) / 1e6 if n_val is not None else None,
            "equity_m": (e_val or 0) / 1e6,
            "cash_m": (c_val or 0) / 1e6,
            "roic": roic,
        })

    # growth rates as decimal fractions, using prior row as base
    for i in range(1, len(rows)):
        prev, cur = rows[i - 1], rows[i]

        def growth(curv, prevv):
            if curv is None or prevv is None or prevv == 0:
                return None
            return (curv - prevv) / abs(prevv)

        cur["revenue_growth"] = growth(cur["revenue_m"], prev["revenue_m"])
        cur["earnings_growth"] = growth(cur["net_income_m"], prev["net_income_m"])
        cur["equity_growth"] = growth(cur["equity_m"], prev["equity_m"])
        cur["cash_growth"] = growth(cur["cash_m"], prev["cash_m"])
        cur["roic_change"] = (cur["roic"] - prev["roic"]) if (cur["roic"] is not None and prev["roic"] is not None) else None

    return rows, debt_warning  # rows[0] is the base year with blank growth columns


def build_income_statement(facts, years=4):
    """Build an income-statement breakdown: `years` annual columns (from
    10-K filings) plus a trailing-twelve-months column when a 10-Q filed
    after the latest 10-K makes one computable.

    Returns (periods, line_items):
      periods    — column labels, e.g. ["2022-12-31", ..., "2025-12-31", "TTM"]
      line_items — [(row_label, {period: value_or_None}), ...] in display
                   order; dollar figures are raw USD, EPS is per-share.
    Raises CompanyDataError if there's no revenue data at all."""
    series = {}
    for label, tags in INCOME_STATEMENT_LINES:
        s, _ = get_series(facts, tags, instant=False)
        series[label] = s

    rev_series = series["Total Revenue"]
    if not rev_series:
        raise CompanyDataError("Could not find any revenue data for this company in "
                                "SEC XBRL. It may not be a standard 10-K filer.")

    ends = sorted(rev_series)[-years:]
    if not ends:
        raise CompanyDataError("Not enough annual data to build an income statement.")

    # Fill in Gross Profit / Operating Expense by subtraction where a
    # company doesn't tag them directly.
    for end in ends:
        if series["Gross Profit"].get(end) is None \
                and rev_series.get(end) is not None and series["Cost of Revenue"].get(end) is not None:
            series["Gross Profit"][end] = rev_series[end] - series["Cost of Revenue"][end]
        if series["Operating Expense"].get(end) is None \
                and series["Gross Profit"].get(end) is not None and series["Operating Income"].get(end) is not None:
            series["Operating Expense"][end] = series["Gross Profit"][end] - series["Operating Income"][end]

    most_recent_end = ends[-1]
    ttm = {}
    for label, tags in INCOME_STATEMENT_LINES:
        ttm[label] = compute_ttm(facts, tags, series[label], most_recent_end)
    if ttm["Gross Profit"] is None and ttm["Total Revenue"] is not None and ttm["Cost of Revenue"] is not None:
        ttm["Gross Profit"] = ttm["Total Revenue"] - ttm["Cost of Revenue"]
    if ttm["Operating Expense"] is None and ttm["Gross Profit"] is not None and ttm["Operating Income"] is not None:
        ttm["Operating Expense"] = ttm["Gross Profit"] - ttm["Operating Income"]

    has_ttm = any(v is not None for v in ttm.values())
    periods = list(ends) + (["TTM"] if has_ttm else [])

    line_items = []
    for label, _ in INCOME_STATEMENT_LINES:
        row = {end: series[label].get(end) for end in ends}
        if has_ttm:
            row["TTM"] = ttm[label]
        line_items.append((label, row))

    return periods, line_items


def build_balance_sheet(facts, years=4):
    """Build a balance-sheet snapshot: `years` annual columns (from 10-K
    filings, as of each fiscal year-end) plus a most-recent-quarter column
    when a 10-Q filed after the latest 10-K makes one available.

    Returns (periods, line_items, mrq_period):
      periods    — column labels in chronological order, e.g.
                   ["2023-12-31", ..., "2025-12-31", "2026-06-30"]
      line_items — [(row_label, {period: value_or_None}), ...] in display
                   order; all figures are raw USD.
      mrq_period — the period label that's a most-recent-quarter snapshot
                   rather than a fiscal year-end (or None if unavailable),
                   so callers can flag that column distinctly.
    Raises CompanyDataError if there's no Total Assets data at all."""
    series = {}
    for label, tags in BALANCE_SHEET_LINES:
        if label == "Total Debt":
            s, _ = get_debt_series(facts)
        else:
            s, _ = get_series(facts, tags, instant=True)
        series[label] = s

    assets_series = series["Total Assets"]
    if not assets_series:
        raise CompanyDataError("Could not find any balance sheet data (Total Assets) "
                                "for this company in SEC XBRL.")

    ends = sorted(assets_series)[-years:]
    if not ends:
        raise CompanyDataError("Not enough annual data to build a balance sheet.")

    # Fill in Total Liabilities by subtraction where a company doesn't tag
    # it directly (ignores noncontrolling interests — an approximation).
    for end in ends:
        if series["Total Liabilities"].get(end) is None \
                and assets_series.get(end) is not None and series["Total Stockholders Equity"].get(end) is not None:
            series["Total Liabilities"][end] = assets_series[end] - series["Total Stockholders Equity"][end]

    most_recent_end = ends[-1]
    mrq_vals = {}
    mrq_ends = {}
    mrq_period = None
    for label, tags in BALANCE_SHEET_LINES:
        val, end = compute_mrq(facts, tags, most_recent_end)
        mrq_vals[label] = val
        mrq_ends[label] = end
        if end and (mrq_period is None or end > mrq_period):
            mrq_period = end
    if mrq_vals["Total Liabilities"] is None and mrq_period is not None \
            and mrq_vals["Total Assets"] is not None and mrq_vals["Total Stockholders Equity"] is not None:
        mrq_vals["Total Liabilities"] = mrq_vals["Total Assets"] - mrq_vals["Total Stockholders Equity"]
        mrq_ends["Total Liabilities"] = mrq_period

    periods = list(ends) + ([mrq_period] if mrq_period else [])

    line_items = []
    for label, _ in BALANCE_SHEET_LINES:
        row = {end: series[label].get(end) for end in ends}
        if mrq_period:
            # Not every line is necessarily re-tagged in the same 10-Q --
            # a line whose own most-recent value is from an EARLIER
            # quarter than mrq_period (the max across all lines) has no
            # real figure for the current quarter yet. Leaving it blank
            # here is correct; writing that stale value under mrq_period's
            # column would silently mislabel it as current.
            row[mrq_period] = mrq_vals[label] if mrq_ends.get(label) == mrq_period else None
        line_items.append((label, row))

    return periods, line_items, mrq_period


def build_cash_flow_statement(facts, years=4):
    """Build a cash-flow breakdown: `years` annual columns (from 10-K
    filings) plus a trailing-twelve-months column when a 10-Q filed after
    the latest 10-K makes one computable. Same duration/TTM approach as
    build_income_statement — see that function's docstring for why.

    Returns (periods, line_items):
      periods    — column labels, e.g. ["2022-12-31", ..., "2025-12-31", "TTM"]
      line_items — [(row_label, {period: value_or_None}), ...] in display
                   order, raw USD; Capital Expenditures is negative (a use
                   of cash) and Free Cash Flow is derived, not tagged.
    Raises CompanyDataError if there's no operating-cash-flow data at all."""
    # Capital Expenditures is kept as the raw, positive SEC "payments" value
    # throughout — TTM math (compute_ttm re-fetches raw quarterly facts
    # under the hood) and Free Cash Flow both need that sign. It's flipped
    # to negative only in the final display rows below.
    series = {}
    for label, tags in CASH_FLOW_LINES:
        if tags is None:  # Free Cash Flow — derived below
            series[label] = {}
            continue
        s, _ = get_series(facts, tags, instant=False)
        series[label] = s

    op_cf_series = series["Cash from Operations"]
    if not op_cf_series:
        raise CompanyDataError("Could not find any operating cash flow data for this "
                                "company in SEC XBRL. It may not be a standard 10-K filer.")

    ends = sorted(op_cf_series)[-years:]
    if not ends:
        raise CompanyDataError("Not enough annual data to build a cash-flow statement.")

    capex_series = series["Capital Expenditures"]
    for end in ends:
        if op_cf_series.get(end) is not None and capex_series.get(end) is not None:
            series["Free Cash Flow"][end] = op_cf_series[end] - capex_series[end]

    most_recent_end = ends[-1]
    ttm = {}
    for label, tags in CASH_FLOW_LINES:
        if tags is None:
            continue
        ttm[label] = compute_ttm(facts, tags, series[label], most_recent_end)
    if ttm.get("Cash from Operations") is not None and ttm.get("Capital Expenditures") is not None:
        ttm["Free Cash Flow"] = ttm["Cash from Operations"] - ttm["Capital Expenditures"]
    else:
        ttm["Free Cash Flow"] = None

    has_ttm = any(v is not None for v in ttm.values())
    periods = list(ends) + (["TTM"] if has_ttm else [])

    def display_val(label, v):
        return -v if (label == "Capital Expenditures" and v is not None) else v

    line_items = []
    for label, _ in CASH_FLOW_LINES:
        row = {end: display_val(label, series[label].get(end)) for end in ends}
        if has_ttm:
            row["TTM"] = display_val(label, ttm[label])
        line_items.append((label, row))

    return periods, line_items


def compute_dcf(base_fcf_m, growth_pct, terminal_growth_pct, discount_pct, years,
                 net_debt_m, shares_out_m):
    """A textbook two-stage discounted cash flow: grow `base_fcf_m` at
    `growth_pct` for `years` years, discount each year's projected FCF at
    `discount_pct`, add a Gordon-growth terminal value (also discounted
    back), then bridge Enterprise Value -> Equity Value -> fair value per
    share. All dollar figures are in millions except the returned
    fair_value_per_share, which is dollars per share.

    Raises ValueError if discount_pct <= terminal_growth_pct -- the
    terminal value formula (FCF * (1+tg) / (r - tg)) is undefined at
    r == tg and negative (nonsensical) below it.

    Returns a dict:
      year_columns   — [(col_id, label), ...] for the projection years
      fcf_row        — {col_id: projected FCF ($M)}
      discount_row   — {col_id: discount factor}
      pv_row         — {col_id: present value of that year's FCF ($M)}
      terminal_value — undiscounted terminal value ($M)
      pv_terminal    — discounted terminal value ($M)
      enterprise_value, equity_value — ($M)
      fair_value_per_share — $/share, or None if shares_out_m is falsy
    """
    g = growth_pct / 100.0
    tg = terminal_growth_pct / 100.0
    r = discount_pct / 100.0
    years = int(years)
    if r <= tg:
        raise ValueError("Discount rate must be greater than the terminal growth rate.")

    year_columns = []
    fcf_row, discount_row, pv_row = {}, {}, {}
    fcf = base_fcf_m
    pv_sum = 0.0
    for y in range(1, years + 1):
        fcf = fcf * (1 + g)
        discount_factor = 1 / ((1 + r) ** y)
        pv = fcf * discount_factor
        pv_sum += pv
        col_id = f"year_{y}"
        year_columns.append((col_id, f"Year {y}"))
        fcf_row[col_id] = fcf
        discount_row[col_id] = discount_factor
        pv_row[col_id] = pv

    terminal_value = fcf * (1 + tg) / (r - tg)
    pv_terminal = terminal_value / ((1 + r) ** years)

    enterprise_value = pv_sum + pv_terminal
    equity_value = enterprise_value - net_debt_m
    fair_value_per_share = (equity_value / shares_out_m) if shares_out_m else None

    return {
        "year_columns": year_columns,
        "fcf_row": fcf_row,
        "discount_row": discount_row,
        "pv_row": pv_row,
        "terminal_value": terminal_value,
        "pv_terminal": pv_terminal,
        "enterprise_value": enterprise_value,
        "equity_value": equity_value,
        "fair_value_per_share": fair_value_per_share,
    }


def fmt(v, nd=4):
    return "" if v is None else f"{v:.{nd}f}"


CSV_HEADER = ["Fiscal Year End", "Revenue ($M)", "Revenue Growth (%)",
              "Net Income ($M)", "Earnings Growth (%)", "Total Equity ($M)",
              "Equity Growth (%)", "Cash & Equivalents ($M)",
              "Cash Growth (%)", "ROIC (%)", "ROIC Change (pp)"]


def csv_row(r):
    return [
        r["end"],
        round(r["revenue_m"]),
        fmt(r.get("revenue_growth")),
        round(r["net_income_m"]) if r["net_income_m"] is not None else "",
        fmt(r.get("earnings_growth")),
        round(r["equity_m"]),
        fmt(r.get("equity_growth")),
        round(r["cash_m"]),
        fmt(r.get("cash_growth")),
        fmt(r["roic"]),
        fmt(r.get("roic_change")),
    ]


def build_csv_text(rows, company_title, ticker):
    """Render rows to CSV text in-memory (used for both the CLI's file
    output and the Dash app's browser download, so both stay identical)."""
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([f"# {company_title} ({ticker}) — generated from SEC EDGAR XBRL data"])
    w.writerow(CSV_HEADER)
    for r in rows:
        w.writerow(csv_row(r))
    return buf.getvalue()


def write_csv(rows, path, company_title, ticker):
    with open(path, "w", newline="") as f:
        f.write(build_csv_text(rows, company_title, ticker))


# ---------------------------------------------------------------------------
# Reusable entry point (used by the CLI below, and by dash_app.py)
# ---------------------------------------------------------------------------

def _resolve_and_fetch_facts(query, session, user_agent):
    """Shared by fetch_growth_data and fetch_income_statement_data: resolve
    `query` to an SEC filer and return (facts, title, ticker)."""
    global USER_AGENT
    if user_agent:
        USER_AGENT = user_agent
    session = session or requests.Session()

    companies = load_ticker_map(session)
    company = resolve_company(query, companies)

    ticker = company["ticker"]
    title = company["title"]
    cik = int(company["cik_str"])

    facts_data = sec_get(session, FACTS_URL.format(cik=cik))
    facts = facts_data.get("facts", {}).get("us-gaap", {})
    if not facts:
        raise CompanyDataError("No us-gaap XBRL facts found for this company.")
    return facts, title, ticker


def fetch_growth_data(query, years=6, session=None, user_agent=None):
    """Resolve `query` (ticker or company name) to an SEC filer and return
    (rows, company_title, ticker, debt_warning). Raises CompanyLookupError
    if the query can't be resolved, or CompanyDataError if the company's
    XBRL data isn't usable. `session` lets callers reuse a requests.Session
    (and its ticker-map cache) across calls; one is created if omitted."""
    facts, title, ticker = _resolve_and_fetch_facts(query, session, user_agent)
    rows, debt_warning = build_rows(facts, years)
    return rows, title, ticker, debt_warning


def fetch_income_statement_data(query, years=4, session=None, user_agent=None):
    """Resolve `query` to an SEC filer and return (periods, line_items,
    company_title, ticker) — see build_income_statement for the shape of
    periods/line_items. Same exceptions as fetch_growth_data."""
    facts, title, ticker = _resolve_and_fetch_facts(query, session, user_agent)
    periods, line_items = build_income_statement(facts, years)
    return periods, line_items, title, ticker


def fetch_balance_sheet_data(query, years=4, session=None, user_agent=None):
    """Resolve `query` to an SEC filer and return (periods, line_items,
    mrq_period, company_title, ticker) — see build_balance_sheet for the
    shape of periods/line_items/mrq_period. Same exceptions as
    fetch_growth_data."""
    facts, title, ticker = _resolve_and_fetch_facts(query, session, user_agent)
    periods, line_items, mrq_period = build_balance_sheet(facts, years)
    return periods, line_items, mrq_period, title, ticker


def fetch_cash_flow_data(query, years=4, session=None, user_agent=None):
    """Resolve `query` to an SEC filer and return (periods, line_items,
    company_title, ticker) — see build_cash_flow_statement for the shape
    of periods/line_items. Same exceptions as fetch_growth_data."""
    facts, title, ticker = _resolve_and_fetch_facts(query, session, user_agent)
    periods, line_items = build_cash_flow_statement(facts, years)
    return periods, line_items, title, ticker


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("company", help="Ticker symbol (e.g. AAPL) or company name (e.g. \"Apple\")")
    parser.add_argument("--years", type=int, default=6,
                         help="Number of years of growth rates to compute (default: 6)")
    parser.add_argument("--output", default=None,
                         help="Output CSV path (default: <TICKER>_growth_rates.csv)")
    parser.add_argument("--user-agent", default=None,
                         help='SEC contact string, e.g. "Jane Doe jane@example.com" '
                              "(required by SEC's fair-access policy; overrides the default in this file)")
    args = parser.parse_args()

    global USER_AGENT
    if args.user_agent:
        USER_AGENT = args.user_agent
    if "example.com" in USER_AGENT:
        print("WARNING: using a placeholder User-Agent. SEC may rate-limit or block "
              "requests without a real contact. Pass --user-agent \"Your Name you@email.com\" "
              "or edit USER_AGENT at the top of this script.\n")

    print(f"Looking up '{args.company}'...")
    try:
        rows, title, ticker, debt_warning = fetch_growth_data(args.company, args.years)
    except (CompanyLookupError, CompanyDataError) as e:
        sys.exit(str(e))
    print(f"Found: {title} ({ticker})")
    if debt_warning:
        print(debt_warning)

    output = args.output or f"{ticker}_growth_rates.csv"
    write_csv(rows, output, title, ticker)

    print(f"\nWrote {len(rows)} fiscal years to {output}\n")
    print(f"{'FY End':<12}{'Rev $M':>10}{'RevGr':>9}{'NetInc $M':>11}{'EarnGr':>9}"
          f"{'Eq $M':>10}{'EqGr':>8}{'Cash $M':>10}{'CashGr':>9}{'ROIC':>8}{'ROICchg':>9}")
    for r in rows:
        def p(v):
            return "" if v is None else f"{v*100:.1f}%"
        print(f"{r['end']:<12}{r['revenue_m']:>10,.0f}{p(r.get('revenue_growth')):>9}"
              f"{(r['net_income_m'] if r['net_income_m'] is not None else float('nan')):>11,.0f}{p(r.get('earnings_growth')):>9}"
              f"{r['equity_m']:>10,.0f}{p(r.get('equity_growth')):>8}"
              f"{r['cash_m']:>10,.0f}{p(r.get('cash_growth')):>9}"
              f"{p(r.get('roic')):>8}{p(r.get('roic_change')):>9}")


if __name__ == "__main__":
    main()
