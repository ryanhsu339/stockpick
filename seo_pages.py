"""Server-rendered, crawlable pages for search engines: one page per
investment manager in the 13F top-managers snapshot (/manager/<slug>) and
per member of Congress in the STOCK Act positions snapshot
(/politician/<slug>), plus index pages listing each set, robots.txt and a
sitemap covering all of them.

The Dash app itself lives at "/" and renders everything client-side, so on
its own it's a single indexable page. These are plain Flask routes beside
it -- no Dash callbacks, no JS bundle -- and they read only the
repo-committed snapshots the app already ships with, never a live SEC or
House/Senate fetch: crawlers can hit thousands of them in a burst, and
production runs one worker with 4 threads. Snapshots only change on a
deploy (which restarts the process), so everything below is cached
in-process for the process's lifetime.
"""
import functools
import html
import json
import re
import threading
from datetime import date
from pathlib import Path

import flask

from congress_trades import load_activity_summary_snapshot, load_member_positions_snapshot
from thirteenf import _top_managers_snapshot_index, top_managers_snapshot_entry

_DATA_DIR = Path(__file__).parent / "data"
_MANAGER_RANKING_PATH = _DATA_DIR / "thirteenf_manager_ranking.json"

# Cloudflare honors s-maxage for anything a Cache Rule marks cacheable, so
# most crawler hits can be answered at the edge without reaching the app.
_CACHE_CONTROL = "public, max-age=3600, s-maxage=21600"

_MAX_HOLDINGS_ROWS = 50
_MAX_POSITION_ROWS = 50
_MAX_RECENT_TRADES = 25
_RELATED_LINKS = 10


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------
def _e(value):
    return html.escape(str(value), quote=True)


def _slugify(text):
    text = text.lower().replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


# Legal-form suffixes that should stay upper-case when an ALL-CAPS 13F
# filer name ("COATUE MANAGEMENT LLC") is title-cased for display.
_KEEP_UPPER = {"LLC", "LP", "LLP", "LLLP", "PLC", "AG", "SA", "NV", "PJSC", "II", "III", "USA", "UK", "US"}
# Short words that are ordinary words, not acronyms like AQR/UBS/FMR.
_SHORT_WORDS = {"CO", "INC", "LTD", "THE", "AND", "OF", "FOR", "NEW", "ONE", "TWO", "BAY", "SUN", "OAK",
                "ART", "SEA", "SKY", "RED", "TOP", "FUND", "BANK", "TRUST", "GROUP", "CORP"}
_LOWER_WORDS = {"OF", "AND", "THE", "FOR", "DE"}
_BRAND_CASE = {"JPMORGAN": "JPMorgan", "ALLIANCEBERNSTEIN": "AllianceBernstein", "BLACKROCK": "BlackRock",
               "PIMCO": "PIMCO", "BNY": "BNY", "TIAA": "TIAA", "CALPERS": "CalPERS", "CALSTRS": "CalSTRS",
               "MCKINLEY": "McKinley", "MCDONALD": "McDonald"}


def _display_name(name):
    """13F filer names are often ALL CAPS with an EDGAR state suffix
    ("BANK OF AMERICA CORP /DE/"); tidy those up. Mixed-case names are
    left as the filer wrote them."""
    name = re.sub(r"\s*/[A-Z]{2,3}/?\s*$", "", name.strip()).rstrip("\\ ")
    if name != name.upper():
        return name
    words = []
    for n, word in enumerate(name.split()):
        core = word.strip(",.")
        if core in _BRAND_CASE:
            words.append(word.replace(core, _BRAND_CASE[core]))
        elif re.fullmatch(r"([A-Z]\.)+,?", word) or core in _KEEP_UPPER or "&" in core                 or (len(core) <= 3 and core not in _SHORT_WORDS)                 or (len(core) <= 4 and not re.search(r"[AEIOUY]", core)):
            words.append(word)
        elif n and core in _LOWER_WORDS:
            words.append(word.lower())
        else:
            words.append(word.capitalize())
    return " ".join(words)


def _money_m(value_m):
    """A figure in $ millions, compactly: $6.73T / $388.9B / $54.7M / $820K."""
    if value_m is None:
        return "—"
    sign = "-" if value_m < 0 else ""
    v = abs(value_m)
    if v >= 1_000_000:
        return f"{sign}${v / 1_000_000:.2f}T"
    if v >= 1000:
        return f"{sign}${v / 1000:.1f}B"
    if v >= 1:
        return f"{sign}${v:.1f}M"
    return f"{sign}${v * 1000:.0f}K"


def _money(value):
    return "—" if value is None else f"{'-' if value < 0 else ''}${abs(value):,.0f}"


def _shares_m(value_m):
    v = abs(value_m)
    sign = "+" if value_m > 0 else "-" if value_m < 0 else ""
    if v >= 1000:
        return f"{sign}{v / 1000:.2f}B"
    if v >= 1:
        return f"{sign}{v:.2f}M"
    return f"{sign}{v * 1_000_000:,.0f}"


def _pct(value, signed=False):
    if value is None:
        return "—"
    return f"{value:+.2f}%" if signed else f"{value:.2f}%"


def _signed_class(value):
    return "up" if (value or 0) > 0 else "down" if (value or 0) < 0 else ""


def _long_date(iso):
    try:
        d = date.fromisoformat(iso)
    except (TypeError, ValueError):
        return iso or ""
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def _quarter_label(iso):
    try:
        d = date.fromisoformat(iso)
    except (TypeError, ValueError):
        return ""
    return f"Q{(d.month - 1) // 3 + 1} {d.year}"


def _us_date_sort_key(mdy):
    """Congress filings date trades as MM/DD/YYYY."""
    try:
        m, d, y = mdy.split("/")
        return int(y), int(m), int(d)
    except (AttributeError, ValueError):
        return 0, 0, 0


def _table(headers, rows, numeric_from=1):
    """headers: column labels; rows: lists of pre-escaped cell HTML.
    Columns from index numeric_from on are right-aligned."""
    head = "".join(f'<th class="{"num" if i >= numeric_from else ""}">{_e(h)}</th>'
                   for i, h in enumerate(headers))
    body = "".join(
        "<tr>" + "".join(f'<td class="{"num" if i >= numeric_from else ""}">{c}</td>'
                         for i, c in enumerate(row)) + "</tr>"
        for row in rows)
    return f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


# ---------------------------------------------------------------------------
# Page shell
# ---------------------------------------------------------------------------
_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="description" content="{description}">
<link rel="canonical" href="{canonical}">
<meta name="theme-color" content="#0f0f0e">
<meta property="og:type" content="website">
<meta property="og:site_name" content="Stockpick">
<meta property="og:url" content="{canonical}">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{description}">
<meta property="og:image" content="{site_url}/assets/og-image.png">
<meta name="twitter:card" content="summary_large_image">
<link rel="icon" href="/assets/favicon.ico">
<link rel="stylesheet" href="/assets/custom.css">
<script>
try {{ if (JSON.parse(localStorage.getItem("theme-store")) === "light")
    document.documentElement.setAttribute("data-theme", "light"); }} catch (e) {{}}
</script>
<script type="application/ld+json">{json_ld}</script>
<style>
body {{ margin: 0; }}
.seo-top {{ display: flex; align-items: center; justify-content: space-between; gap: 12px;
           max-width: 1040px; margin: 0 auto; padding: 18px 16px; box-sizing: border-box; }}
.seo-logo {{ display: flex; align-items: center; gap: 10px; text-decoration: none; }}
.seo-logo span:first-child {{ width: 22px; height: 22px; border-radius: 6px; background: var(--accent); }}
.seo-logo span:last-child {{ font-weight: 700; font-size: 17px; letter-spacing: -0.02em; color: var(--text); }}
.seo-nav {{ display: flex; align-items: center; gap: 16px; font-size: 14px; }}
.seo-nav a {{ color: var(--body-text); text-decoration: none; }}
.seo-nav a:hover {{ color: var(--text); }}
.seo-nav a.cta {{ background: var(--accent); color: #0f0f0e; font-weight: 600; padding: 7px 12px;
                 border-radius: 8px; }}
main {{ max-width: 1040px; margin: 0 auto; padding: 8px 16px 48px; box-sizing: border-box; }}
.crumbs {{ font-size: 13px; color: var(--body-text); margin-bottom: 10px; }}
.crumbs a {{ color: var(--body-text); }}
h1 {{ font-size: 30px; letter-spacing: -0.02em; margin: 0 0 10px; color: var(--text); }}
h2 {{ font-size: 19px; letter-spacing: -0.01em; margin: 36px 0 12px; color: var(--text); }}
p {{ color: var(--body-text); line-height: 1.6; font-size: 15px; margin: 0 0 12px; max-width: 760px; }}
p.note {{ font-size: 13px; }}
.stats {{ display: flex; flex-wrap: wrap; gap: 12px; margin: 18px 0 4px; }}
.stat {{ background: var(--card-bg); border: 1px solid var(--border); border-radius: 12px;
        padding: 12px 16px; min-width: 140px; }}
.stat .label {{ font-size: 12px; color: var(--body-text); }}
.stat .value {{ font-size: 20px; font-weight: 600; color: var(--text); margin-top: 2px; }}
.table-wrap {{ overflow-x: auto; background: var(--card-bg); border: 1px solid var(--border);
              border-radius: 12px; }}
table {{ border-collapse: collapse; width: 100%; font-size: 14px; }}
th, td {{ padding: 9px 14px; text-align: left; border-bottom: 1px solid var(--border); white-space: nowrap; }}
th {{ font-size: 12px; font-weight: 600; color: var(--body-text); }}
td {{ color: var(--text); }}
tr:last-child td {{ border-bottom: none; }}
.num {{ text-align: right; font-family: "IBM Plex Mono", ui-monospace, monospace; }}
.up {{ color: var(--up); }}
.down {{ color: var(--down); }}
.ticker {{ color: var(--security-text); font-weight: 600; }}
.cols {{ display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 20px; }}
.cols h2 {{ margin-top: 28px; }}
@media (max-width: 760px) {{ .cols {{ grid-template-columns: minmax(0, 1fr); }}
                             .seo-nav a.hide-sm {{ display: none; }} h1 {{ font-size: 24px; }}
                             .stats {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }}
                             .stat {{ min-width: 0; }} .stat .value {{ font-size: 17px; }} }}
ul.links {{ list-style: none; padding: 0; margin: 0; columns: 2; column-gap: 24px; }}
@media (max-width: 760px) {{ ul.links {{ columns: 1; }} }}
ul.links li {{ padding: 6px 0; break-inside: avoid; font-size: 14px; color: var(--body-text); }}
ul.links a {{ color: var(--text); }}
.open-app {{ display: inline-block; margin-top: 8px; background: var(--accent); color: #0f0f0e;
            font-weight: 600; padding: 9px 14px; border-radius: 8px; text-decoration: none; font-size: 14px; }}
footer.seo-foot {{ max-width: 1040px; margin: 0 auto; padding: 24px 16px 40px; box-sizing: border-box;
                  font-size: 12px; color: var(--body-text); border-top: 1px solid var(--border); }}
</style>
</head>
<body>
<header class="seo-top">
<a class="seo-logo" href="/"><span></span><span>Stockpick</span></a>
<nav class="seo-nav">
<a class="hide-sm" href="/managers">Managers</a>
<a class="hide-sm" href="/politicians">Politicians</a>
<a class="cta" href="/">Open tracker</a>
</nav>
</header>
<main>
{body}
</main>
<footer class="seo-foot">
Data: SEC EDGAR Form 13F-HR filings and House &amp; Senate Periodic Transaction Reports (STOCK Act).
Figures are derived from public disclosures, may be delayed or incomplete, and are not investment advice.
</footer>
</body>
</html>"""


def _render_page(site_url, path, title, description, body, json_ld, status=200):
    page = _PAGE_TEMPLATE.format(
        title=_e(title), description=_e(description), canonical=_e(f"{site_url}{path}"),
        site_url=_e(site_url), body=body,
        # "</" can't appear inside a <script> block.
        json_ld=json.dumps(json_ld).replace("</", "<\\/"),
    )
    resp = flask.Response(page, status=status, mimetype="text/html")
    if status == 200:
        resp.headers["Cache-Control"] = _CACHE_CONTROL
    return resp


def _breadcrumbs_ld(site_url, crumbs):
    return {
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": i + 1, "name": name, "item": f"{site_url}{path}"}
            for i, (name, path) in enumerate(crumbs)
        ],
    }


def _crumbs_html(crumbs):
    parts = [f'<a href="{_e(path)}">{_e(name)}</a>' for name, path in crumbs[:-1]]
    parts.append(_e(crumbs[-1][0]))
    return f'<nav class="crumbs" aria-label="Breadcrumb">{" › ".join(parts)}</nav>'


# ---------------------------------------------------------------------------
# Data: slug indexes over the snapshots
# ---------------------------------------------------------------------------
_index_lock = threading.Lock()
_manager_index = None      # list of {"slug", "cik", "name", "value_b"}, by reported value desc
_politician_index = None   # list of {"slug", "key", "candidate"}, by last name


def _assign_slugs(items, base_fn, tiebreak_fn):
    """Sets item["slug"] = base_fn(item), appending tiebreak_fn(item)
    wherever two items would otherwise share a slug."""
    counts = {}
    for item in items:
        counts[base_fn(item)] = counts.get(base_fn(item), 0) + 1
    for item in items:
        base = base_fn(item)
        item["slug"] = base if counts[base] == 1 else f"{base}-{tiebreak_fn(item)}"


def _managers():
    global _manager_index
    if _manager_index is not None:
        return _manager_index
    with _index_lock:
        if _manager_index is not None:
            return _manager_index
        try:
            with open(_MANAGER_RANKING_PATH, encoding="utf-8") as f:
                ranking = {str(m["cik"]): m for m in json.load(f)["managers"]}
        except (OSError, ValueError, KeyError):
            ranking = {}
        items = []
        for cik in _top_managers_snapshot_index():
            ranked = ranking.get(cik)
            if ranked:
                items.append({"cik": cik, "name": _display_name(ranked["name"]),
                              "value_b": ranked.get("value_b")})
        items.sort(key=lambda m: -(m["value_b"] or 0))
        _assign_slugs(items, lambda m: _slugify(m["name"]), lambda m: m["cik"])
        _manager_index = items
        return items


def _politicians():
    global _politician_index
    if _politician_index is not None:
        return _politician_index
    with _index_lock:
        if _politician_index is not None:
            return _politician_index
        items = [{"key": key, "candidate": entry["candidate"]}
                 for key, entry in _member_positions().items() if entry.get("all_positions")]
        items.sort(key=lambda p: (p["candidate"]["last"].lower(), p["candidate"]["first"].lower()))
        _assign_slugs(items, lambda p: _slugify(p["candidate"]["display"]),
                      lambda p: p["candidate"]["chamber"])
        _politician_index = items
        return items


@functools.lru_cache(maxsize=1)
def _member_positions():
    try:
        return load_member_positions_snapshot()
    except (OSError, ValueError):
        return {}


def _member_recent_trades(candidate):
    try:
        trades = load_activity_summary_snapshot().get("recent_trades", [])
    except Exception:
        return []
    key = [candidate["chamber"], candidate["last"].lower(), candidate["first"].lower()]
    return [t for t in trades if t.get("member_key") == key]


def _member_title(candidate):
    return "Sen." if candidate["chamber"] == "senate" else "Rep."


def _member_district(candidate):
    """'NY02' -> 'NY-02', 'DC00' -> 'DC', '' -> ''."""
    sd = candidate.get("state_dst") or ""
    if len(sd) >= 2 and sd[2:].strip("0") == "":
        return sd[:2]
    return f"{sd[:2]}-{sd[2:]}" if len(sd) > 2 else sd


# ---------------------------------------------------------------------------
# Manager pages
# ---------------------------------------------------------------------------
def _manager_move_rows(moves):
    rows = []
    for t in moves:
        if not t.get("prev_shares_m"):
            change = '<span class="up">New position</span>'
        elif not t.get("shares_m"):
            change = '<span class="down">Sold out</span>'
        else:
            change = f'<span class="{_signed_class(t["delta_shares_m"])}">{_shares_m(t["delta_shares_m"])} sh</span>'
        rows.append([
            _e(t["issuer"]),
            change,
            f'<span class="{_signed_class(t.get("delta_shares_value_m"))}">{_money_m(t.get("delta_shares_value_m"))}</span>',
            _pct(t.get("portfolio_pct")),
        ])
    return rows


@functools.lru_cache(maxsize=128)
def _manager_page(site_url, slug):
    managers = _managers()
    manager = next((m for m in managers if m["slug"] == slug), None)
    if manager is None:
        return None
    entry = top_managers_snapshot_entry(manager["cik"])
    if not entry:
        return None

    name = manager["name"]
    period = entry.get("latest_period")
    quarter = _quarter_label(period)
    # Sold-out positions stay in the snapshot (value 0) for the change tables.
    positions = sorted((p for p in entry.get("all_positions") or [] if p.get("value_m")),
                       key=lambda p: -p["value_m"])
    shown = positions[:_MAX_HOLDINGS_ROWS]
    top_names = ", ".join(_display_name(p["issuer"]) for p in positions[:3])
    value = _money_m(manager["value_b"] * 1000) if manager.get("value_b") else None
    path = f"/manager/{slug}"

    title = f"{name} 13F Holdings & Portfolio ({quarter}) | Stockpick"
    description = (f"{name}'s 13F portfolio as of {_long_date(period)}"
                   + (f" ({value} reported)" if value else "")
                   + f": top holdings including {top_names}, plus the biggest buys and sells "
                     "versus the prior quarter.")

    stats = [("Quarter", quarter or "—"), ("Reported 13F value", value or "—"),
             ("Largest holding", _display_name(positions[0]["issuer"]) if positions else "—")]
    stats_html = "".join(f'<div class="stat"><div class="label">{_e(k)}</div>'
                         f'<div class="value">{_e(v)}</div></div>' for k, v in stats)

    move_headers = ["Security", "Change", "Value of change", "% of portfolio"]
    holding_rows = [[
        _e(p["issuer"]),
        _money_m(p.get("value_m")),
        _pct(p.get("portfolio_pct")),
        f'<span class="{_signed_class(p.get("delta_shares_pct"))}">{_pct(p.get("delta_shares_pct"), signed=True)}</span>'
        if p.get("prev_shares_m") else '<span class="up">New</span>',
    ] for p in shown]

    i = next(n for n, m in enumerate(managers) if m["slug"] == slug)
    neighbors = [m for m in managers[max(0, i - _RELATED_LINKS // 2):i + _RELATED_LINKS // 2 + 1]
                 if m["slug"] != slug]
    related = "".join(
        f'<li><a href="/manager/{_e(m["slug"])}">{_e(m["name"])}</a>'
        + (f' · {_money_m(m["value_b"] * 1000)}' if m.get("value_b") else "") + "</li>"
        for m in neighbors)

    crumbs = [("Home", "/"), ("Investment Managers", "/managers"), (name, path)]
    body = f"""{_crumbs_html(crumbs)}
<h1>{_e(name)} 13F Holdings</h1>
<p>{_e(name)}'s holdings from its Form 13F-HR filing for the quarter ended {_e(_long_date(period))},
compared with the previous quarter ({_e(_long_date(entry.get("previous_period")))}). 13F filings list the
long U.S. equity positions held by institutional investment managers with over $100 million under management.</p>
<div class="stats">{stats_html}</div>
<a class="open-app" href="/">Explore {_e(name)} in the live tracker →</a>
<div class="cols">
<section><h2>Biggest Buys ({_e(quarter)})</h2>{_table(move_headers, _manager_move_rows(entry.get("top_increases") or []))}</section>
<section><h2>Biggest Sells ({_e(quarter)})</h2>{_table(move_headers, _manager_move_rows(entry.get("top_decreases") or []))}</section>
</div>
<h2>Top {len(shown)} Holdings</h2>
{_table(["Security", "Value", "% of portfolio", "Change in shares"], holding_rows)}
<p class="note">Values are as reported in the filing{", adjusted for stock splits" if entry.get("split_adjusted") else ""}.
Ranked by market value; options and other non-equity positions may be included where the filing lists them.</p>
<h2>Similar-Sized Managers</h2>
<ul class="links">{related}</ul>"""

    json_ld = {"@context": "https://schema.org", "@graph": [
        {"@type": "WebPage", "name": title, "url": f"{site_url}{path}", "description": description,
         "about": {"@type": "Organization", "name": name,
                   "identifier": f"CIK {manager['cik']}"}},
        _breadcrumbs_ld(site_url, crumbs),
    ]}
    return title, description, body, json_ld


@functools.lru_cache(maxsize=1)
def _managers_index_page(site_url):
    managers = _managers()
    rows = [[f'<a href="/manager/{_e(m["slug"])}">{_e(m["name"])}</a>',
             _money_m(m["value_b"] * 1000) if m.get("value_b") else "—"] for m in managers]
    title = "Largest Investment Managers' 13F Holdings | Stockpick"
    description = (f"Latest 13F portfolios for the {len(managers)} largest institutional investment "
                   "managers, including BlackRock, Vanguard, Citadel and Berkshire Hathaway: top "
                   "holdings and quarter-over-quarter buys and sells.")
    crumbs = [("Home", "/"), ("Investment Managers", "/managers")]
    body = f"""{_crumbs_html(crumbs)}
<h1>Investment Manager 13F Holdings</h1>
<p>The {len(managers)} largest institutional investment managers by reported 13F value. Each page shows the
manager's top holdings and biggest buys and sells from its latest Form 13F-HR filing.</p>
{_table(["Manager", "Reported 13F value"], rows)}"""
    json_ld = {"@context": "https://schema.org", "@graph": [
        {"@type": "CollectionPage", "name": title, "url": f"{site_url}/managers", "description": description},
        _breadcrumbs_ld(site_url, crumbs),
    ]}
    return title, description, body, json_ld


# ---------------------------------------------------------------------------
# Politician pages
# ---------------------------------------------------------------------------
def _trade_rows(trades):
    rows = []
    for t in trades:
        kind = t.get("transaction_type") or ""
        cls = "up" if kind.startswith("Purchase") else "down" if kind.startswith("Sale") else ""
        rows.append([
            f'<span class="ticker">{_e(t.get("ticker") or "—")}</span>',
            f'<span class="{cls}">{_e(kind)}</span>',
            _e(t.get("transaction_date") or ""),
            f'{_money(t.get("amount_low"))} – {_money(t.get("amount_high"))}',
        ])
    return rows


@functools.lru_cache(maxsize=128)
def _politician_page(site_url, slug):
    politicians = _politicians()
    member = next((p for p in politicians if p["slug"] == slug), None)
    if member is None:
        return None
    entry = _member_positions().get(member["key"])
    if not entry:
        return None

    c = member["candidate"]
    name, honorific = c["display"], _member_title(c)
    district = _member_district(c)
    chamber = "Senate" if c["chamber"] == "senate" else "House"
    who = f"{honorific} {name}" + (f" ({district})" if district else "")
    positions = entry.get("all_positions") or []
    recent = sorted(_member_recent_trades(c), key=lambda t: _us_date_sort_key(t.get("transaction_date")),
                    reverse=True)[:_MAX_RECENT_TRADES]
    most_traded = [p["ticker"] for p in sorted(positions, key=lambda p: -(p.get("transaction_count") or 0))
                   if p.get("ticker")][:3]
    last_date = max((p.get("last_transaction_date") for p in positions),
                    key=_us_date_sort_key, default=None)
    total_txns = sum(p.get("transaction_count") or 0 for p in positions)
    path = f"/politician/{slug}"

    title = f"{name} Stock Trades & Portfolio | Stockpick"
    description = (f"Stock trades disclosed by {who} under the STOCK Act: {total_txns} transactions across "
                   f"{len(positions)} tickers"
                   + (f", most often {', '.join(most_traded)}" if most_traded else "")
                   + ". Recent purchases and sales and estimated positions.")

    stats = [("Chamber", chamber + (f" · {district}" if district else "")),
             ("Tickers traded", f"{len(positions):,}"), ("Disclosed transactions", f"{total_txns:,}"),
             ("Latest trade", last_date or "—")]
    stats_html = "".join(f'<div class="stat"><div class="label">{_e(k)}</div>'
                         f'<div class="value">{_e(v)}</div></div>' for k, v in stats)

    sections = []
    if recent:
        sections.append(f"<h2>Recent Trades</h2>"
                        f"{_table(['Ticker', 'Type', 'Date', 'Amount'], _trade_rows(recent), numeric_from=3)}")
    increases, decreases = entry.get("top_increases") or [], entry.get("top_decreases") or []
    if increases or decreases:
        # Stacked, not side by side: the amount ranges are too wide for half a column.
        if increases:
            sections.append(f"<h2>Largest Purchases (Last 12 Months)</h2>"
                            f"{_table(['Ticker', 'Type', 'Date', 'Amount'], _trade_rows(increases), numeric_from=3)}")
        if decreases:
            sections.append(f"<h2>Largest Sales (Last 12 Months)</h2>"
                            f"{_table(['Ticker', 'Type', 'Date', 'Amount'], _trade_rows(decreases), numeric_from=3)}")
    shown = positions[:_MAX_POSITION_ROWS]
    position_rows = [[
        f'<span class="ticker">{_e(p.get("ticker") or "—")}</span>',
        f'<span class="{_signed_class(p.get("net_estimated_value"))}">{_money(p.get("net_estimated_value"))}</span>',
        _money(p.get("total_bought")), _money(p.get("total_sold")),
        f'{p.get("transaction_count") or 0:,}', _e(p.get("last_transaction_date") or ""),
    ] for p in shown]
    sections.append(
        f"<h2>Estimated Positions{f' (Top {len(shown)} of {len(positions)})' if len(positions) > len(shown) else ''}</h2>"
        + _table(["Ticker", "Est. net value", "Total bought", "Total sold", "Transactions", "Last trade"],
                 position_rows))

    i = next(n for n, p in enumerate(politicians) if p["slug"] == slug)
    neighbors = [p for p in politicians[max(0, i - _RELATED_LINKS // 2):i + _RELATED_LINKS // 2 + 1]
                 if p["slug"] != slug]
    related = "".join(f'<li><a href="/politician/{_e(p["slug"])}">{_e(p["candidate"]["display"])}</a>'
                      f' · {_e(_member_title(p["candidate"]))}</li>' for p in neighbors)

    crumbs = [("Home", "/"), ("Members of Congress", "/politicians"), (name, path)]
    body = f"""{_crumbs_html(crumbs)}
<h1>{_e(name)} Stock Trades</h1>
<p>Stock transactions disclosed by {_e(who)} in Periodic Transaction Reports filed under the STOCK Act.
Members of Congress must report trades over $1,000 within 45 days, as a dollar range rather than an exact amount.</p>
<div class="stats">{stats_html}</div>
<a class="open-app" href="/">Explore congressional trades in the live tracker →</a>
{"".join(sections)}
<p class="note">Estimated positions accumulate the midpoint of each disclosed purchase (+) and sale (−) range over
all available filing history, so they're approximations, not reported holdings.</p>
<h2>Other Members of Congress</h2>
<ul class="links">{related}</ul>"""

    json_ld = {"@context": "https://schema.org", "@graph": [
        {"@type": "WebPage", "name": title, "url": f"{site_url}{path}", "description": description,
         "about": {"@type": "Person", "name": name,
                   "jobTitle": "U.S. Senator" if c["chamber"] == "senate" else "U.S. Representative"}},
        _breadcrumbs_ld(site_url, crumbs),
    ]}
    return title, description, body, json_ld


@functools.lru_cache(maxsize=1)
def _politicians_index_page(site_url):
    politicians = _politicians()
    positions = _member_positions()

    def rows(chamber):
        out = []
        for p in politicians:
            c = p["candidate"]
            if c["chamber"] != chamber:
                continue
            entry = positions.get(p["key"]) or {}
            all_pos = entry.get("all_positions") or []
            row = [f'<a href="/politician/{_e(p["slug"])}">{_e(c["display"])}</a>']
            if chamber == "house":
                row.append(_e(_member_district(c) or "—"))
            out.append(row + [f'{len(all_pos):,}',
                              f'{sum(x.get("transaction_count") or 0 for x in all_pos):,}'])
        return out

    title = "Congress Stock Trades by Member | Stockpick"
    description = (f"Stock trades disclosed by {len(politicians)} members of the House and Senate under the "
                   "STOCK Act. See each member's recent purchases and sales and estimated positions.")
    crumbs = [("Home", "/"), ("Members of Congress", "/politicians")]

    body = f"""{_crumbs_html(crumbs)}
<h1>Congress Stock Trades by Member</h1>
<p>Members of the House and Senate with stock transactions disclosed in Periodic Transaction Reports under the
STOCK Act. Each page lists the member's recent trades, largest purchases and sales, and estimated positions.</p>
<h2>Senate</h2>
{_table(["Senator", "Tickers", "Transactions"], rows("senate"), numeric_from=1)}
<h2>House of Representatives</h2>
{_table(["Representative", "District", "Tickers", "Transactions"], rows("house"), numeric_from=2)}"""
    json_ld = {"@context": "https://schema.org", "@graph": [
        {"@type": "CollectionPage", "name": title, "url": f"{site_url}/politicians", "description": description},
        _breadcrumbs_ld(site_url, crumbs),
    ]}
    return title, description, body, json_ld


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
def init_seo_pages(server, site_url):
    site_url = site_url.rstrip("/")

    def respond(path, built):
        if built is None:
            return _not_found(path)
        return _render_page(site_url, path, *built)

    def _not_found(path):
        body = ("<h1>Page not found</h1><p>We couldn't find that page. It may have been removed after "
                'the latest filings were processed.</p><ul class="links">'
                '<li><a href="/managers">Investment managers</a></li>'
                '<li><a href="/politicians">Members of Congress</a></li>'
                '<li><a href="/">Open the live tracker</a></li></ul>')
        resp = _render_page(site_url, path, "Page not found | Stockpick", "Page not found.", body,
                            {"@context": "https://schema.org", "@type": "WebPage"}, status=404)
        resp.headers["X-Robots-Tag"] = "noindex"
        return resp

    @server.route("/managers")
    def seo_managers_index():
        return respond("/managers", _managers_index_page(site_url))

    @server.route("/manager/<slug>")
    def seo_manager(slug):
        return respond(f"/manager/{slug}", _manager_page(site_url, slug))

    @server.route("/politicians")
    def seo_politicians_index():
        return respond("/politicians", _politicians_index_page(site_url))

    @server.route("/politician/<slug>")
    def seo_politician(slug):
        return respond(f"/politician/{slug}", _politician_page(site_url, slug))

    # Without these, Dash's catch-all route answers /robots.txt and
    # /sitemap.xml with the app's HTML page. Nothing under /_dash-* is
    # disallowed: Google renders "/" by fetching those scripts and POSTing
    # its callbacks, so blocking them would leave it indexing an empty
    # "Loading..." shell.
    @server.route("/robots.txt")
    def seo_robots_txt():
        body = ("User-agent: *\n"
                "Disallow: /reset-password\n"
                "Disallow: /forgot-password\n"
                f"\nSitemap: {site_url}/sitemap.xml\n")
        return flask.Response(body, mimetype="text/plain")

    @server.route("/sitemap.xml")
    def seo_sitemap_xml():
        paths = ["/", "/managers", "/politicians"]
        paths += [f"/manager/{m['slug']}" for m in _managers()]
        paths += [f"/politician/{p['slug']}" for p in _politicians()]
        urls = "".join(f"  <url><loc>{_e(site_url + p)}</loc></url>\n" for p in paths)
        body = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
                f"{urls}</urlset>\n")
        resp = flask.Response(body, mimetype="application/xml")
        resp.headers["Cache-Control"] = _CACHE_CONTROL
        return resp
