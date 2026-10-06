#!/usr/bin/env python3
"""
dash_app.py — small Dash web app around company_growth_calc.py.

Enter a ticker (or company name) and click Generate: it renders a price
chart (with a 1D/5D/1M/3M/6M/1Y/3Y/ALL range picker, from Yahoo Finance)
above a tabbed view of the same Sales / Earnings / Equity / Cash / ROIC
growth table the CLI script writes to CSV (with a button to download that
CSV from the browser), an income-statement breakdown (Revenue through EPS,
with a trailing-twelve-months column), a balance-sheet snapshot (Cash
through Total Equity, with a most-recent-quarter column), and a cash-flow
breakdown (Net Income through Free Cash Flow, with a trailing-twelve-months
column) — each by fiscal year, with the extra column shown when SEC data
makes it computable. A second tab (Investment Manager Tracker) does the
same top-increases/decreases/all-positions breakdown for 13F filers, and a
third (Politician Tracker) does it for members of Congress' STOCK Act
disclosures, House and Senate (see congress_trades.py for that data's caveats).

USAGE
    python dash_app.py
    (then open http://127.0.0.1:8050 in a browser)

REQUIREMENTS
    pip install dash requests yfinance pdfplumber pandas openpyxl
"""

import concurrent.futures
import io
import json
import math
import os
import re
import threading
import time
from datetime import date, timedelta

# For the "worker ready" log line at the bottom of this module -- see
# _log_slow_requests below.
_IMPORT_STARTED = time.perf_counter()

import pandas as pd
import flask
import plotly.graph_objects as go
import requests
from dash import ALL, Dash, Input, Output, State, ctx, dash_table, dcc, html, no_update
from dash.dash_table.Format import Format, Scheme, Sign
from dash.exceptions import PreventUpdate

from company_growth_calc import (
    CompanyDataError,
    CompanyLookupError,
    NoXbrlFactsError,
    compute_dcf,
    fetch_balance_sheet_data,
    fetch_cash_flow_data,
    fetch_growth_data,
    fetch_income_statement_data,
    load_ticker_map,
    resolve_company,
    resolve_ticker_for_security,
    search_companies,
)
from stock_price import (HoldingsDataError, PriceDataError, RANGE_KEYS, fetch_day_movers, fetch_fund_name,
                          fetch_earnings_history, fetch_price_history, fetch_ticker_overview,
                          fetch_top_holdings)
from thirteenf import (
    FilingDataError,
    ManagerLookupError,
    _top_managers_snapshot_index,
    fetch_manager_comparison,
    fetch_manager_comparison_by_cik,
    load_top_buys,
    search_managers,
)
from congress_trades import (
    PoliticianDataError,
    list_all_house_members,
    list_all_senators,
    load_activity_summary_snapshot,
    load_member_positions_snapshot,
)

# One shared session across requests/users so the ticker-map cache in
# company_growth_calc.py is actually reused instead of refetched every time.
_session = requests.Session()

# Both of these lazily cache themselves in memory on first call (see
# load_ticker_map/_top_managers_snapshot_index), reading a repo-committed
# snapshot off disk -- ticker_map.json (~3MB, used by every ticker search
# suggestion) and the line offsets of thirteenf_top_managers.json (~42MB
# on disk, never loaded whole -- lookups read one manager's line at a
# time; used by the Managers tracker, the default landing view). Without this,
# that read+parse cost landed on whichever real request happened to be
# first after a deploy/restart -- e.g. a visitor's first search
# suggestion, or their first page load, sitting noticeably slower than
# every one after it. Called here (module import time, i.e. while
# gunicorn is booting the worker, before it accepts any traffic) so both
# caches are already warm by the time anyone's first request arrives.
# Broadly caught: a failure here (e.g. a missing/corrupt snapshot forcing
# a live SEC fetch that then fails) must never take down the whole app at
# startup -- worst case, it just falls back to today's cold-on-first-use
# behavior for that one cache.
try:
    load_ticker_map(_session)
    _top_managers_snapshot_index()
    load_top_buys()
except Exception:
    pass

# Same reasoning, for the sidebar search box's "Members of Congress"
# results: list_all_house_members/list_all_senators each cache their
# roster in memory on first call, but building it means live requests
# against the House/Senate disclosure sites (House: one ZIP per recent
# year; Senate: a CSRF handshake plus paginated search results) --
# several seconds of network round-trips that otherwise landed on
# whichever visitor's search happened to trigger it first. Two separate
# try/excepts, not one -- House and Senate are unrelated sites, and
# either one being briefly down shouldn't skip warming the other.
try:
    list_all_house_members(session=_session)
except Exception:
    pass
try:
    list_all_senators(session=_session)
except Exception:
    pass

# The Politicians tab's Recent Trades/Leaderboard tables read this same
# repo-committed snapshot (~800KB) on every visit -- see
# load_activity_summary_snapshot's own cache (congress_trades.py) for
# why this needed warming the same as everything else above.
try:
    load_activity_summary_snapshot()
except Exception:
    pass

# Fixed number of fiscal years the Financials tables (Growth Rates, Income
# Statement, Balance Sheet, Cash Flow Statement) pull — previously a user-set
# "Years" input, now fixed since 5 years covers the trend view these tables
# are for.
_FINANCIALS_YEARS = 5

DISPLAY_COLUMNS = [
    {"name": "Fiscal Year End", "id": "end"},
    {"name": "Revenue ($M)", "id": "revenue_m"},
    {"name": "Revenue Growth", "id": "revenue_growth"},
    {"name": "Net Income ($M)", "id": "net_income_m"},
    {"name": "Earnings Growth", "id": "earnings_growth"},
    {"name": "Total Equity ($M)", "id": "equity_m"},
    {"name": "Equity Growth", "id": "equity_growth"},
    {"name": "Cash & Equiv. ($M)", "id": "cash_m"},
    {"name": "Cash Growth", "id": "cash_growth"},
    {"name": "ROIC", "id": "roic"},
    {"name": "ROIC Change (pp)", "id": "roic_change"},
]

# ETFs/mutual funds don't file 10-K XBRL data, so the Financials tables
# don't apply to them -- this shows their top holdings instead (see
# _run_etf_lookup).
_HOLDINGS_PCT_FORMAT = Format(precision=2, scheme=Scheme.fixed)
HOLDINGS_COLUMNS = [
    {"name": "Symbol", "id": "symbol"},
    {"name": "Name", "id": "name"},
    {"name": "% of Fund", "id": "holding_pct", "type": "numeric", "format": _HOLDINGS_PCT_FORMAT},
]

# DCF valuation inputs (Public Company Tracker): (field, label) pairs,
# rendered as one number input per row in the left-side panel, matching
# the Investment Manager Tracker's Filters box. field becomes "dcf-{field}".
_DCF_INPUT_FIELDS = [
    ("base_fcf", "Base FCF ($M)"),
    ("growth_rate", "FCF Growth Rate (%)"),
    ("terminal_growth", "Terminal Growth Rate (%)"),
    ("discount_rate", "Discount Rate / WACC (%)"),
    ("years", "Projection Years"),
    ("net_debt", "Net Debt ($M)"),
    ("shares_out", "Diluted Shares Out. (M)"),
    ("current_price", "Current Price ($)"),
]
# The three bounded, continuously-tunable assumptions become sliders (per
# the redesign); the rest (base FCF, years, net debt, shares out, current
# price) aren't naturally bounded 0-X ranges and stay plain number inputs.
_DCF_SLIDER_FIELDS = {
    "growth_rate": {"min": 0, "max": 20, "step": 0.5},
    "discount_rate": {"min": 6, "max": 14, "step": 0.25},
    "terminal_growth": {"min": 0, "max": 4, "step": 0.25},
}


def _dcf_field_component(field, suffix):
    """update_dcf/update_dcf_2 already read/write every `dcf-{field}{suffix}`
    by its "value" prop regardless of component type, so swapping Input for
    Slider on the three fields above needs no callback changes."""
    comp_id = f"dcf-{field}{suffix}"
    bounds = _DCF_SLIDER_FIELDS.get(field)
    if bounds is None:
        return dcc.Input(id=comp_id, type="number", style=_FILTER_INPUT_STYLE)
    return dcc.Slider(
        id=comp_id, min=bounds["min"], max=bounds["max"], step=bounds["step"],
        value=bounds["min"], marks=None,
        tooltip={"placement": "bottom", "always_visible": True},
    )


# Full-strength --text (not the usual muted --body-text) -- the DCF panel's
# labels sit directly against the page's near-black card background with
# nothing else around them, where --body-text's usual grey reads as too
# low-contrast to scan quickly.
_DCF_FIELD_LABEL_STYLE = {"color": "var(--text)", "fontSize": "12px", "fontWeight": "500",
                          "display": "block", "marginBottom": "3px"}


def _dcf_field_wrapper_style(field):
    # Slider fields show a live value bubble below the track (tooltip,
    # always_visible) that needs more room than a plain number input --
    # without the extra margin here it overlaps the next field down.
    return {"marginBottom": "34px" if field in _DCF_SLIDER_FIELDS else "10px"}


def _pct(v):
    return "" if v is None else f"{v * 100:.1f}%"


def _money(v):
    return "" if v is None else f"{v:,.0f}"


def rows_to_display_records(rows):
    records = []
    for r in rows:
        records.append({
            "end": r["end"],
            "revenue_m": _money(r["revenue_m"]),
            "revenue_growth": _pct(r.get("revenue_growth")),
            "net_income_m": _money(r["net_income_m"]),
            "earnings_growth": _pct(r.get("earnings_growth")),
            "equity_m": _money(r["equity_m"]),
            "equity_growth": _pct(r.get("equity_growth")),
            "cash_m": _money(r["cash_m"]),
            "cash_growth": _pct(r.get("cash_growth")),
            "roic": _pct(r.get("roic")),
            "roic_change": _pct(r.get("roic_change")),
        })
    return records


def _fmt_big_dollars(v):
    if v is None:
        return None
    if v >= 1e12:
        return f"${v / 1e12:.2f}T"
    if v >= 1e9:
        return f"${v / 1e9:.1f}B"
    if v >= 1e6:
        return f"${v / 1e6:.0f}M"
    return f"${v:,.0f}"


def _money_abbrev(v):
    if v is None:
        return ""
    a = abs(v)
    if a >= 1e9:
        return f"{v / 1e9:.2f}B"
    if a >= 1e6:
        return f"{v / 1e6:.0f}M"
    return f"{v:,.0f}"


def _period_label(p, mrq_period=None):
    if p == "TTM":
        return "TTM"
    try:
        label = date.fromisoformat(p).strftime("%m/%d/%Y")
    except ValueError:
        return p
    return f"{label} (MRQ)" if p == mrq_period else label


def financial_table_columns(periods, mrq_period=None):
    return [{"name": "Breakdown", "id": "line"}] + \
        [{"name": _period_label(p, mrq_period), "id": p} for p in periods]


def financial_table_records(periods, line_items):
    records = []
    for label, row in line_items:
        is_eps = "EPS" in label
        rec = {"line": label}
        for p in periods:
            v = row.get(p)
            if v is None:
                rec[p] = ""
            elif is_eps:
                rec[p] = f"{v:.2f}"
            else:
                rec[p] = _money_abbrev(v)
        records.append(rec)
    return records


# Dark-card theme matching the rest of the app (_MANAGER_TABLE_BG etc.):
# vivid green/red for the up/down price line and fill -- muted status
# colors that read fine on a light card (the old _CHART_SURFACE) wash out
# against dark, so these are brighter than a typical "delta" palette.
# These stay the same in both page themes (vivid enough to read on either);
# everything else about a chart's own colors doesn't -- Plotly renders to
# SVG/canvas rather than the DOM, so it can't follow a CSS variable the way
# the rest of the page does, and needs the current theme's actual color
# picked in Python instead. See _CHART_THEMES/_chart_colors and
# toggle_theme's Input into every figure-producing callback.
_PRICE_UP_COLOR = "#4cc38a"
_PRICE_DOWN_COLOR = "#e5675a"
_CHART_THEMES = {
    "dark": {
        "surface": "#161615",
        "gridline": "#232321",
        "axis_line": "#2f2f2c",
        "muted_text": "#85837c",
        "primary_text": "#ecebe6",
        "hover_bg": "#1f1f1c",
        "halo": "rgba(255,255,255,0.35)",
        "forecast_shade": "rgba(255,255,255,0.05)",
    },
    "light": {
        "surface": "#ffffff",
        "gridline": "#eeede8",
        "axis_line": "#d6d5cf",
        "muted_text": "#6e6d66",
        "primary_text": "#1b1b19",
        "hover_bg": "#ffffff",
        "halo": "rgba(0,0,0,0.25)",
        "forecast_shade": "rgba(0,0,0,0.05)",
    },
}


def _chart_colors(theme):
    return _CHART_THEMES.get(theme, _CHART_THEMES["dark"])


_CHART_HEIGHT = 420
_VOLUME_COLOR = "rgba(137,135,129,0.45)"  # muted ink, translucent — a neutral
                                           # magnitude cue that doesn't compete
                                           # with the price line's up/down color


def empty_price_figure(message="Enter a ticker and click Generate to load a chart.", theme="dark"):
    colors = _chart_colors(theme)
    fig = go.Figure()
    fig.update_layout(
        height=_CHART_HEIGHT,
        paper_bgcolor=colors["surface"],
        plot_bgcolor=colors["surface"],
        margin=dict(l=20, r=20, t=20, b=20),
        xaxis=dict(visible=False),
        yaxis=dict(visible=False),
        annotations=[dict(text=message, showarrow=False,
                           font=dict(color=colors["muted_text"], size=14))],
    )
    return fig


# Same first/last/change/hi-lo math build_price_figure computes inline for
# its own title/colors (kept separate rather than shared, so this stays a
# trivial, easily-swappable-out function and never risks the chart itself).
# Feeds the real HTML price header (see update_price_chart's extra Outputs)
# now that the price/change text moved out of the Plotly title. Also read
# straight from the client, by name, for the price-scrub feature -- see
# the "Price" trace in build_price_figure and the scrub listeners in
# custom.js -- so this shape (first/last close) needs to keep matching.
def _price_change_stats(df):
    closes = df["Close"]
    first, last = float(closes.iloc[0]), float(closes.iloc[-1])
    change = last - first
    pct = (change / first * 100) if first else 0.0
    lo, hi = float(closes.min()), float(closes.max())
    return {"last": last, "change": change, "pct": pct, "up": change >= 0, "lo": lo, "hi": hi}


def _price_header_texts(stats):
    """(price, change-text, change-style, high, low) for update_price_chart's
    header Outputs, from a _price_change_stats() dict."""
    color = "var(--up)" if stats["up"] else "var(--down)"
    arrow = "▲" if stats["up"] else "▼"
    sign = "+" if stats["change"] >= 0 else ""
    price_text = f"${stats['last']:,.2f}"
    change_text = f"{arrow} {sign}{stats['change']:,.2f} ({sign}{stats['pct']:.2f}%)"
    return price_text, change_text, {"color": color}, f"${stats['hi']:,.2f}", f"${stats['lo']:,.2f}"


def build_price_figure(df, ticker, range_key, theme="dark", mobile=False):
    colors = _chart_colors(theme)
    closes = df["Close"]
    volume = df["Volume"]
    first, last = float(closes.iloc[0]), float(closes.iloc[-1])
    up = last >= first
    color = _PRICE_UP_COLOR if up else _PRICE_DOWN_COLOR
    # A flat translucent fill read as a barely-there tint on the old light
    # card; against the dark card it needs more opacity to actually look
    # "highlighted" rather than washing out to the background color.
    fill_color = "rgba(34,197,94,0.18)" if up else "rgba(239,68,68,0.18)"

    # Zoom the price axis to the period's actual range (with a little
    # headroom) instead of anchoring at zero, so price movement is legible.
    # The fill still targets zero — it just gets clipped to this visible
    # window, which reads as "filled to the bottom of the chart."
    lo, hi = float(closes.min()), float(closes.max())
    pad = (hi - lo) * 0.08 or max(hi * 0.01, 0.01)
    y_range = [lo - pad, hi + pad]

    # Price and volume share ONE plot area (one x-axis) rather than two
    # stacked subplots — a hover spike line only ever spans the subplot(s)
    # of the axis it's drawn on, so two separate row-panels each got their
    # own independent line instead of one shared crosshair. Volume gets its
    # own y-axis (yaxis2) overlaid on the same area, ranged tall enough that
    # its bars stay confined to roughly the bottom quarter, inset under the
    # price line — the same visual convention as most stock-chart apps.
    # Trace order controls z-order (Volume added first so Price's fill
    # draws on top of the bars); legendrank controls the unified-hover
    # tooltip's row order independently, so Price can still list first
    # there even though it's added second.
    has_volume = volume.notna().any()
    fig = go.Figure()
    if has_volume:
        vol_max = float(volume.max())
        fig.add_trace(go.Bar(
            x=volume.index, y=volume.values,
            name="Volume",
            marker=dict(color=_VOLUME_COLOR),
            yaxis="y2",
            legendrank=2,
            # Date as its own line above the volume figure -- "x unified"
            # mode doesn't reliably show its shared-x header when hover is
            # triggered programmatically (see the scrub listeners in
            # custom.js), so this bakes the date into the row itself
            # rather than depending on that.
            hovertemplate="%{x|%b %d, %Y}<br>Vol %{y:,.0f}<extra></extra>",
        ))

    fig.add_trace(go.Scatter(
        x=closes.index, y=closes.values,
        name="Price",
        mode="lines",
        line=dict(width=2, color=color, shape="linear"),
        fill="tozeroy",
        fillcolor=fill_color,
        legendrank=1,
        hovertemplate="%{y:$,.2f}<extra></extra>",
    ))
    # Live-price marker at the latest point: a solid dot plus a translucent
    # halo behind it. The halo's opacity is animated client-side (see the
    # price-pulse-interval clientside callback) to read as a pulsing "live"
    # indicator -- named traces so that callback can find them by name
    # regardless of whether the Volume trace shifts everyone's index.
    last_x = closes.index[-1]
    fig.add_trace(go.Scatter(
        x=[last_x], y=[last],
        mode="markers",
        name="_pulse_halo",
        marker=dict(size=14, color=colors["halo"]),
        hoverinfo="skip",
        showlegend=False,
    ))
    fig.add_trace(go.Scatter(
        x=[last_x], y=[last],
        mode="markers",
        name="_pulse_dot",
        marker=dict(size=8, color=colors["primary_text"], line=dict(width=1.5, color=color)),
        hoverinfo="skip",
        showlegend=False,
    ))

    # A plain date axis reserves calendar-time width for days with no data
    # (weekends; overnight hours on intraday ranges), which both stretches
    # gaps into the price line and visually separates otherwise-adjacent
    # volume bars. rangebreaks removes that dead space so trading
    # days/hours sit contiguously.
    rangebreaks = [dict(bounds=["sat", "mon"])]
    if range_key in ("1D", "5D"):
        rangebreaks.append(dict(bounds=[16, 9.5], pattern="hour"))  # market close -> next open, ET

    # Ticker/price/change now live in the real HTML header above the chart
    # (see update_price_chart's extra Outputs and _price_change_stats) --
    # no Plotly title here any more, both to avoid showing the same numbers
    # twice and for a quieter, less chart-junk-y look.
    fig.update_layout(
        # On mobile the price axis' tick labels are hidden below (the price
        # header above the chart already carries that number), so the left
        # margin that used to reserve room for them would just be dead
        # space -- shrinking it lets the plot itself use the full card
        # width instead of leaving a blank strip down the side.
        margin=dict(l=4, r=4, t=20, b=30) if mobile else dict(l=24, r=10, t=20, b=30),
        height=_CHART_HEIGHT,
        paper_bgcolor=colors["surface"],
        plot_bgcolor=colors["surface"],
        showlegend=False,
        hovermode="x unified",
        # On mobile, a touch-drag meant to scrub across the chart (hover
        # already shows price/volume at the cursor as it moves, via the
        # spikes below) kept triggering a click-drag zoom box instead --
        # False disables that drag-to-zoom interaction entirely. Plotly's
        # OWN hover system still only reacts to a plain mouse move though,
        # not a press-and-hold drag (the only gesture touch has), so
        # continuous hover/spikes during an actual finger-drag are driven
        # manually instead -- see the scrub listeners in custom.js, gated
        # on this same fixedrange (mobile-only) axis. Desktop keeps its
        # default (zoom) since a mouse drag there is deliberate, not an
        # incidental scroll/swipe gesture.
        dragmode=False if mobile else "zoom",
        # Without uirevision, Dash's Plotly.react treats every 15s refresh
        # (or 60s DCF price tick) as a brand-new figure and resets zoom/pan;
        # keeping it constant per ticker+range lets Plotly diff the traces
        # instead and animate between old/new points rather than popping.
        uirevision=f"{ticker}-{range_key}",
        # Plotly's hover box defaults to a light background with dark
        # text -- fine on the old light card, unreadable-low-contrast on
        # this dark one, so it needs its own explicit theme-matched styling.
        hoverlabel=dict(bgcolor=colors["hover_bg"], bordercolor=colors["axis_line"],
                         font=dict(color=colors["primary_text"], size=12)),
        bargap=0.2,
        xaxis=dict(
            showgrid=False, showline=True, linecolor=colors["axis_line"],
            tickfont=dict(color=colors["muted_text"], size=11),
            showspikes=True, spikemode="across",
            # "cursor" positions the spike at the mouse's actual pixel
            # position, which the custom.js scrub listeners' programmatic
            # Plotly.Fx.hover() call never supplies (only a curve+point
            # index) -- Plotly silently fell back to pixel 0 for it, which
            # is why the spike only ever showed up pinned to the left
            # edge while dragging on mobile. "data" positions it at the
            # hovered POINT's own pixel position instead, which Fx.hover
            # always knows regardless of how it was triggered. Desktop
            # keeps "cursor" (a real mouse position is always available
            # there) for its smoother continuous-tracking feel.
            spikesnap="data" if mobile else "cursor",
            spikedash="dot", spikethickness=1, spikecolor=colors["muted_text"],
            rangebreaks=rangebreaks,
            # dragmode=False (below) only turns off the drag-to-zoom
            # rectangle tool -- a raw touch-drag on mobile still panned the
            # axes underneath the hover crosshair, since nothing had told
            # Plotly the range itself can't move. fixedrange locks pan/zoom
            # on this axis via any interaction (drag, scroll, pinch),
            # leaving hover/spikes as the only thing a finger-drag does.
            fixedrange=mobile,
        ),
        yaxis=dict(
            # Quiet chart: no gridlines -- the price header above now
            # carries the numbers a busier axis used to help convey. On
            # mobile the tick labels themselves go too (hover still shows
            # the exact price), freeing the width they used to reserve for
            # the chart's own plot area -- see the mobile-only margin above.
            showgrid=False, zeroline=False,
            tickfont=dict(color=colors["muted_text"], size=11),
            tickprefix="$", side="right", range=y_range, autorange=False,
            showticklabels=not mobile,
            fixedrange=mobile,
        ),
        yaxis2=dict(
            overlaying="y", side="left", showticklabels=False,
            showgrid=False, zeroline=False,
            range=[0, vol_max * 4] if has_volume else None,
            fixedrange=mobile,
        ),
    )
    return fig


# Arbitrary category colors (not the single-stock chart's up/down green/red,
# which wouldn't distinguish two tickers moving the same direction) -- a
# cool blue and a warm amber read clearly against the dark card and against
# each other.
_COMPARE_COLOR_1 = "#4A90D9"
_COMPARE_COLOR_2 = "#F2A93B"


def build_compare_price_figure(df1, ticker1, df2, ticker2, range_key, theme="dark", mobile=False):
    """Overlay both tickers' price as cumulative % change from the first
    point in range, rather than raw price -- the two are almost never
    anywhere near the same price level, so plotting raw $ would just show
    two flat-looking lines at wildly different heights instead of a
    comparable growth trend. No volume bars (would be visually messy
    overlaid for two tickers, and the point of this view is the trend, not
    the volume)."""
    colors = _chart_colors(theme)
    fig = go.Figure()

    rangebreaks = [dict(bounds=["sat", "mon"])]
    if range_key in ("1D", "5D"):
        rangebreaks.append(dict(bounds=[16, 9.5], pattern="hour"))

    for df, ticker, color in ((df1, ticker1, _COMPARE_COLOR_1), (df2, ticker2, _COMPARE_COLOR_2)):
        closes = df["Close"]
        base = float(closes.iloc[0])
        pct_series = (closes / base - 1) * 100 if base else closes * 0
        last_pct = float(pct_series.iloc[-1])
        sign = "+" if last_pct >= 0 else ""
        arrow = "▲" if last_pct >= 0 else "▼"
        # The running %-change lives in the legend label itself (Plotly
        # colors each entry to match its trace automatically) rather than
        # a separate title -- a title positioned in the same top margin as
        # a top-anchored legend fights it for space and the two overlap.
        fig.add_trace(go.Scatter(
            x=closes.index, y=pct_series.values,
            name=f"{ticker}  {arrow} {sign}{last_pct:.2f}%",
            mode="lines",
            line=dict(width=2, color=color, shape="linear"),
            hovertemplate="%{y:+.2f}%<extra>" + ticker + "</extra>",
        ))
        last_x = closes.index[-1]
        fig.add_trace(go.Scatter(
            x=[last_x], y=[last_pct],
            mode="markers",
            name=f"_pulse_dot_{ticker}",
            marker=dict(size=8, color=color, line=dict(width=1.5, color=colors["primary_text"])),
            hoverinfo="skip",
            showlegend=False,
        ))

    fig.add_hline(y=0, line=dict(color=colors["muted_text"], dash="dot", width=1))

    fig.update_layout(
        # See build_price_figure's identical mobile-only margin shrink --
        # same reasoning (no reserved axis-label space needed once the
        # labels themselves are hidden below).
        margin=dict(l=4, r=4, t=50, b=30) if mobile else dict(l=24, r=10, t=50, b=30),
        height=_CHART_HEIGHT,
        paper_bgcolor=colors["surface"],
        plot_bgcolor=colors["surface"],
        showlegend=True,
        legend=dict(orientation="h", xref="paper", x=0, y=1.12, font=dict(color=colors["primary_text"], size=14)),
        hovermode="x unified",
        # See build_price_figure's identical dragmode -- same mobile
        # accidental-zoom fix (hover/spikes during a finger-drag are
        # driven manually instead, see custom.js), same desktop behavior
        # kept as-is.
        dragmode=False if mobile else "zoom",
        uirevision=f"{ticker1}-{ticker2}-{range_key}",
        hoverlabel=dict(bgcolor=colors["hover_bg"], bordercolor=colors["axis_line"],
                         font=dict(color=colors["primary_text"], size=12)),
        xaxis=dict(
            showgrid=False, showline=True, linecolor=colors["axis_line"],
            tickfont=dict(color=colors["muted_text"], size=11),
            showspikes=True, spikemode="across",
            # See build_price_figure's identical spikesnap -- "data" on
            # mobile so the manually-triggered hover still positions the
            # spike correctly (it only knows a point index, never a real
            # cursor pixel), "cursor" kept on desktop for its smoother feel.
            spikesnap="data" if mobile else "cursor",
            spikedash="dot", spikethickness=1, spikecolor=colors["muted_text"],
            rangebreaks=rangebreaks,
            # See build_price_figure's identical fixedrange -- locks
            # pan/zoom on mobile so a finger-drag only moves the hover
            # crosshair, never the axes underneath it.
            fixedrange=mobile,
        ),
        yaxis=dict(
            showgrid=True, gridcolor=colors["gridline"], zeroline=False,
            tickfont=dict(color=colors["muted_text"], size=11),
            ticksuffix="%", side="right",
            showticklabels=not mobile,
            fixedrange=mobile,
        ),
    )
    return fig


_DCF_REVENUE_BAR_COLOR = "#7a7f87"  # neutral grey -- Revenue, historical + forecast
_DCF_FCF_BAR_COLOR = "#2874a6"      # theme blue -- FCF, historical + forecast


def build_dcf_chart(historical, base_revenue, base_fcf, growth_rate, years, theme="dark", mobile=False):
    """Grouped Revenue/FCF bars: actuals from `historical` (see
    _dcf_defaults) followed by a projection grown at `growth_rate` for
    `years` years from base_revenue/base_fcf, with the forecast region
    shaded and labeled -- both series share the one growth-rate input,
    a simplification (the FCF projection compute_dcf actually discounts
    is identical to this chart's FCF bars; the Revenue bars are for
    context only and don't otherwise feed the valuation math).
    """
    colors = _chart_colors(theme)
    years = int(years) if years else 5
    g = (growth_rate or 0) / 100.0

    hist_labels = [h["year"] for h in historical]
    hist_revenue = [h["revenue_m"] for h in historical]
    hist_fcf = [h["fcf_m"] for h in historical]

    try:
        last_year = int(hist_labels[-1])
    except (IndexError, ValueError):
        last_year = date.today().year
    forecast_labels = [str(last_year + y) for y in range(1, years + 1)]

    forecast_revenue, forecast_fcf = [], []
    rev, fcf = base_revenue, base_fcf
    for _ in range(years):
        rev = (rev * (1 + g)) if rev is not None else None
        fcf = (fcf * (1 + g)) if fcf is not None else None
        forecast_revenue.append(rev)
        forecast_fcf.append(fcf)

    all_labels = hist_labels + forecast_labels
    # $M -> $B: Plotly's default tick formatting auto-abbreviates large
    # axis values with an SI suffix (e.g. "600k"), which combined with a
    # fixed ticksuffix="M" read as the nonsensical "600kM" -- converting
    # to billions up front keeps axis/hover labels ("$600B") sane without
    # fighting that auto-formatting.
    all_revenue = [(v / 1000 if v is not None else None) for v in hist_revenue + forecast_revenue]
    all_fcf = [(v / 1000 if v is not None else None) for v in hist_fcf + forecast_fcf]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=all_labels, y=all_revenue, name="Revenue",
        marker=dict(color=_DCF_REVENUE_BAR_COLOR),
        hovertemplate="Revenue: $%{y:,.1f}B<extra></extra>",
    ))
    fig.add_trace(go.Bar(
        x=all_labels, y=all_fcf, name="FCF",
        marker=dict(color=_DCF_FCF_BAR_COLOR),
        hovertemplate="FCF: $%{y:,.1f}B<extra></extra>",
    ))

    shapes, annotations = [], []
    if hist_labels and forecast_labels:
        split = len(hist_labels) - 0.5
        end = len(all_labels) - 0.5
        shapes.append(dict(
            type="rect", xref="x", yref="paper",
            x0=split, x1=end, y0=0, y1=1,
            fillcolor=colors["forecast_shade"], line_width=0, layer="below",
        ))
        annotations.append(dict(
            x=(split + end) / 2, y=1.04, yref="paper", xref="x",
            text="FORECAST", showarrow=False,
            font=dict(color=colors["muted_text"], size=10),
        ))

    fig.update_layout(
        barmode="group",
        height=_CHART_HEIGHT,
        paper_bgcolor=colors["surface"], plot_bgcolor=colors["surface"],
        margin=dict(l=10, r=10, t=40, b=30),
        legend=dict(orientation="h", y=1.1, x=0, font=dict(color=colors["muted_text"], size=11)),
        hoverlabel=dict(bgcolor=colors["hover_bg"], bordercolor=colors["axis_line"],
                         font=dict(color=colors["primary_text"], size=12)),
        # See build_price_figure's identical mobile dragmode/fixedrange --
        # a swipe meant to scroll the page past this chart (it's not tall
        # enough on its own to need scrubbing the way the price chart is)
        # kept triggering a drag-to-zoom box instead. Unlike the price
        # chart, nothing here drives hover manually during a press, so
        # there's no need to also strip pointer-events off the drag-catch
        # layer -- a plain tap still shows the native hover tooltip either
        # way; dragmode=False alone is enough to stop the zoom.
        dragmode=False if mobile else "zoom",
        xaxis=dict(showgrid=False, showline=True, linecolor=colors["axis_line"],
                   tickfont=dict(color=colors["muted_text"], size=11),
                   fixedrange=mobile),
        yaxis=dict(showgrid=True, gridcolor=colors["gridline"], zeroline=False,
                   tickfont=dict(color=colors["muted_text"], size=11),
                   tickprefix="$", ticksuffix="B",
                   fixedrange=mobile),
        shapes=shapes,
        annotations=annotations,
    )
    return fig


# Fixed (not var(--header)) like the Price/over-under-valuation segments'
# own colors below -- this bar's text is hardcoded white, and --header
# swings from near-black in dark mode to near-white in light mode post-
# redesign (it's now just the nav pill track color), which left "DCF
# Value" nearly illegible in light mode.
_DCF_VALUE_BAR_COLOR = "#2a2a27"


def _dcf_valuation_bar(label, value_pct, color, align="left"):
    justify = "flex-start" if align == "left" else "flex-end"
    pad_side = "paddingLeft" if align == "left" else "paddingRight"
    return html.Div(
        style={"width": f"{max(value_pct, 0):.2f}%", "backgroundColor": color,
               "display": "flex", "alignItems": "center", "justifyContent": justify,
               pad_side: "8px", "color": "#ffffff", "fontSize": "11px", "fontWeight": "700",
               "whiteSpace": "nowrap", "overflow": "hidden"},
        children=label,
    )


def build_dcf_banner(ticker, title, fair_value, current_price, growth_rate, discount_rate, terminal_growth):
    """The header card above the DCF assumptions/chart: a plain-English
    verdict sentence, the key rate assumptions, and a comparison bar
    (DCF fair value vs current price) styled after a typical broker-app
    DCF summary widget, in this app's dark theme."""
    if fair_value is None or current_price is None or not current_price:
        return [html.P("Enter the assumptions below and click Calculate to see the DCF summary.",
                        style={**_PARA_STYLE, "fontSize": "13px", "margin": "0"})]

    # Sign-safe regardless of fair_value's sign: a negative DCF fair value
    # (heavy net debt or weak FCF) means "worth less than nothing," which
    # is always maximally overvalued at any positive price -- dividing by
    # a negative fair_value in the ratio below would otherwise flip the
    # verdict to "undervalued." diff_pct itself only makes sense as a
    # percentage when fair_value is positive; None past that.
    overvalued = current_price > fair_value
    diff_pct = (current_price / fair_value - 1) * 100 if fair_value and fair_value > 0 else None
    verdict_word = "overvalued" if overvalued else "undervalued"
    verdict_color = _PRICE_DOWN_COLOR if overvalued else _PRICE_UP_COLOR
    diff_pct_label = f"{abs(diff_pct):.0f}%" if diff_pct is not None else "N/M"

    # Bar widths need a non-negative "fair value share" of the total --
    # clamp a negative fair_value to 0 just for this visualization; the $
    # figure shown elsewhere still displays the true, possibly-negative
    # DCF value.
    fair_value_for_bar = max(fair_value, 0.0)
    total = max(fair_value_for_bar, current_price, 0.01)
    dcf_pct = fair_value_for_bar / total * 100
    price_pct = current_price / total * 100

    if overvalued:
        dcf_row = html.Div(
            style={"display": "flex", "height": "28px", "borderRadius": "4px", "overflow": "hidden"},
            children=[
                _dcf_valuation_bar("DCF Value", dcf_pct, _DCF_VALUE_BAR_COLOR),
                # Just the percentage, not "OVERVALUATION 43%" -- the full
                # word doesn't reliably fit this segment's width (it's
                # sized to the valuation gap, which can be narrow), and
                # unlike "DCF Value" opposite it, a clipped label here
                # doesn't just lose context, it reads as a jumbled cut-off
                # word. Direction is already conveyed by color, and spelled
                # out in the narrative sentence above.
                _dcf_valuation_bar(diff_pct_label, 100 - dcf_pct, _PRICE_DOWN_COLOR, align="right"),
            ],
        )
        price_row = html.Div(
            style={"display": "flex", "height": "22px", "borderRadius": "4px", "overflow": "hidden",
                   "marginTop": "6px"},
            children=[_dcf_valuation_bar(f"Price  ${current_price:,.2f}", 100, "#555555")],
        )
    else:
        dcf_row = html.Div(
            style={"display": "flex", "height": "28px", "borderRadius": "4px", "overflow": "hidden"},
            children=[_dcf_valuation_bar("DCF Value", 100, _DCF_VALUE_BAR_COLOR)],
        )
        price_row = html.Div(
            style={"display": "flex", "height": "22px", "borderRadius": "4px", "overflow": "hidden",
                   "marginTop": "6px"},
            children=[
                _dcf_valuation_bar(f"Price  ${current_price:,.2f}", price_pct, "#555555"),
                _dcf_valuation_bar(diff_pct_label, 100 - price_pct, _PRICE_UP_COLOR, align="right"),
            ],
        )

    narrative = html.P(
        [
            "The DCF value for ", html.B(ticker), f" ({title}) is ",
            html.B(f"${fair_value:,.2f}"), ". Compared with the current market price of ",
            html.B(f"${current_price:,.2f}"), ", the stock appears ",
            html.B(verdict_word, style={"color": verdict_color}),
            (f" by {diff_pct_label}." if diff_pct is not None
             else " (the DCF model implies a negative fair value)."),
        ],
        style={**_PARA_STYLE, "fontSize": "14px", "lineHeight": "1.6", "margin": "0"},
    )
    assumptions = html.Ul(
        style={**_PARA_STYLE, "fontSize": "13px", "paddingLeft": "18px", "margin": "10px 0 0 0"},
        children=[
            html.Li(f"FCF Growth Rate: {growth_rate:.1f}%") if growth_rate is not None else None,
            html.Li(f"Discount Rate: {discount_rate:.1f}%") if discount_rate is not None else None,
            html.Li(f"Terminal Growth Rate: {terminal_growth:.1f}%") if terminal_growth is not None else None,
        ],
    )

    card = html.Div(
        style={"minWidth": "300px", "maxWidth": "320px", "backgroundColor": "var(--card-bg-2)",
               "borderRadius": "10px", "padding": "16px", "flex": "0 0 auto"},
        children=[
            html.Div(f"{ticker} DCF Value", style={"color": "var(--text)", "fontWeight": "700", "fontSize": "14px"}),
            html.Div("Base Case", style={"color": _BODY_TEXT_COLOR, "fontSize": "12px", "marginBottom": "8px"}),
            html.Div(f"${fair_value:,.2f}", style={"color": _HEADER_TEXT_COLOR, "fontWeight": "800",
                                                     "fontSize": "32px", "marginBottom": "16px"}),
            dcf_row,
            price_row,
        ],
    )

    return html.Div(
        style={"display": "flex", "gap": "24px", "flexWrap": "wrap", "alignItems": "flex-start"},
        children=[
            html.Div(style={"flex": "1", "minWidth": "280px"}, children=[narrative, assumptions]),
            card,
        ],
    )


# Overall page theme: dark-slate-blue-accented section headers, live
# light/dark switching via the CSS variables in assets/custom.css --
# these reference them by name rather than a literal hex so every
# element styled through them (nearly everything below) follows the
# current theme automatically, with no Python-side re-render needed.
# See the two clientside callbacks right after app.layout.
_HEADER_COLOR = "var(--header)"
# Section-heading TEXT (H3s, "DCF Assumptions"/"Filters" labels, ...) --
# deliberately NOT _HEADER_COLOR: that's a background (nav pills) that
# stays the same value in both themes, and that value as text is
# illegible against a near-black dark-mode page. See --header-text
# in assets/custom.css.
_HEADER_TEXT_COLOR = "var(--header-text)"
# The original theme-blue accent for the security/issuer identifier
# column every table highlights in its first column (ticker, issuer,
# member, line item...) -- kept blue on purpose even after the rest of
# the theme moved to grey, for a deliberate pop of color. See
# --security-text in assets/custom.css.
_SECURITY_TEXT_COLOR = "var(--security-text)"
_BODY_TEXT_COLOR = "var(--body-text)"
_HEADER_STYLE = {"color": _HEADER_TEXT_COLOR}
# Only matters once Compare mode stacks a second KPI row underneath the
# first (see stock-kpi-grid-2) -- otherwise it'd be redundant with the
# ticker/name already shown in the page's own header above the chart.
_KPI_SECTION_LABEL_STYLE = {"fontSize": "13px", "fontWeight": "700", "color": "var(--text)",
                             "marginBottom": "8px"}
# Compare mode's per-ticker label above each stacked Valuation panel --
# same look as the KPI row labels, a bit larger since it heads a much
# bigger section.
_DCF_COMPANY_LABEL_STYLE = {**_KPI_SECTION_LABEL_STYLE, "fontSize": "15px", "marginTop": "8px"}
_DCF_COMPANY_LABEL_HIDDEN_STYLE = {**_DCF_COMPANY_LABEL_STYLE, "display": "none"}
_PARA_STYLE = {"color": _BODY_TEXT_COLOR}
# Title of a scrollable summary card (Top Gainers/Losers, Recent Trades/
# Leaderboard) -- sticky so it stays put while the rows scroll beneath it.
# The card's own top padding lives here instead (the card itself has none
# on top), otherwise rows would show through that padding strip above
# the pinned title; the opaque background + zIndex keep them hidden
# underneath it, including a row's hover highlight. The negative side
# margin (matching the card's 20px side padding) stretches that
# background edge to edge, so rows scrolling up can't peek out beside it.
# Thin rule separating each tab's top summary cards from its "Look Up a
# ..." section below.
_SECTION_DIVIDER_STYLE = {"border": "none", "borderTop": "1px solid var(--border)", "margin": "48px 0 0"}
_LOOKUP_HEADING_STYLE = {**_HEADER_STYLE, "marginTop": "40px"}
_STICKY_CARD_TITLE_STYLE = {**_HEADER_STYLE, "position": "sticky", "top": "0", "zIndex": "1",
                            "backgroundColor": "var(--card-bg)", "margin": "0 -20px",
                            "padding": "16px 20px 8px"}
# DataTables keep their own light "card" background regardless of the dark
# page behind them, so their cell text needs an explicit dark color rather
# than inheriting the page's light default (which would wash out unreadable
# on the table's white cells).
_TABLE_CELL_STYLE = {"padding": "9px 12px", "fontSize": "13px", "textAlign": "right", "color": "#0b0b0b"}
# var(--table-header-bg): a near-white header row barely stood out from
# the table's white cells even in dark mode, and stood out even less
# once light mode made the page around the table light too -- darker in
# light mode specifically for that contrast (see assets/custom.css).
_TABLE_HEADER_STYLE = {"fontWeight": "500", "backgroundColor": "var(--table-header-bg)", "color": "#0b0b0b",
                       "fontSize": "11px", "textTransform": "uppercase", "letterSpacing": "0.05em"}

# Compact pill-style nav bar (fits its content instead of stretching full
# width) shared by the range picker and the statement-view tabs. Track
# color follows var(--header) -- a subtle near-bg tint in the current
# palette -- so both selected and unselected labels need to follow the
# page theme too rather than assuming a fixed dark backdrop.
_NAV_CONTAINER_STYLE = {
    "display": "inline-flex",
    "gap": "4px",
    "backgroundColor": _HEADER_COLOR,
    "padding": "3px",
    "borderRadius": "9px",
    "marginTop": "16px",
    "border": "1px solid var(--border)",
    # Harmless on desktop (nothing ever overflows there), but on a narrow
    # mobile screen a pill row with several tabs (see range-tabs' 9) can
    # still be wider than the viewport even with mobile_breakpoint=0
    # keeping it horizontal -- this lets it scroll sideways instead of
    # forcing every pill to shrink illegibly or overflowing the page.
    "maxWidth": "100%",
    "overflowX": "auto",
}
_NAV_TAB_STYLE = {
    "padding": "8px 16px",
    "border": "none",
    "borderBottom": "none",
    "borderRadius": "8px",
    "fontSize": "13px",
    "fontWeight": "500",
    "color": "var(--body-text)",
    "backgroundColor": "transparent",
    "flex": "initial",
    # Without this, a too-narrow container (e.g. range-tabs' 9 pills on a
    # phone screen) shrinks each tab below its own text+padding width
    # instead of relying on the container's overflow-x: auto to scroll --
    # the tab's own background (the selected-state highlight) shrinks to
    # match, so it ends up narrower than and misaligned with its label,
    # which is what actually overflows out past it uncontained.
    "flexShrink": "0",
}
_NAV_TAB_SELECTED_STYLE = {
    **_NAV_TAB_STYLE,
    "fontWeight": "600",
    "color": "var(--text)",
    "backgroundColor": "var(--pill-active)",
    "boxShadow": "0 1px 2px rgba(0,0,0,0.12)",
}
# Financials tabs (Growth Rates/Income/Balance/Cash Flow) don't apply to
# ETFs/funds (no 10-K data); Top Holdings only applies to funds. The
# Financials view-tabs callback toggles between these two per search.
_NAV_TAB_HIDDEN_STYLE = {"display": "none"}
# "price_only" mode (see _tab_visibility_styles): every individual tab
# above is hidden, but that alone left an empty tab bar (zero visible
# labels) with the still-selected Growth Rates tab's own empty table
# showing below it -- a blank table with no explanation. These hide the
# "Financials" header/Download-button row and the tab bar itself
# outright, in favor of a plain message in their place.
_FINANCIALS_HEADER_STYLE = {"display": "flex", "justifyContent": "space-between", "alignItems": "flex-end",
                             "marginTop": "40px", "flexWrap": "wrap", "gap": "12px"}
_FINANCIALS_HEADER_HIDDEN_STYLE = {**_FINANCIALS_HEADER_STYLE, "display": "none"}
_NO_FINANCIALS_MSG_STYLE = {"color": "var(--body-text)", "fontSize": "14px", "marginTop": "16px",
                              "display": "none"}
_NO_FINANCIALS_MSG_VISIBLE_STYLE = {**_NO_FINANCIALS_MSG_STYLE, "display": "block"}

# Live typeahead dropdown, connected visually to the search box it overlays.
# Positioning lives on the always-present outer container (shown/hidden via
# the :focus-within CSS rule in custom.css); the card look (background,
# border, shadow) lives on _SUGGESTIONS_CARD_STYLE instead, applied only to
# the inner Div the callback actually returns when there ARE matches --
# putting it on the outer container meant an empty result (no query yet,
# or no matches) still rendered as a bordered white box the instant the
# input gained focus, before any typing.
_SUGGESTIONS_CONTAINER_STYLE = {
    "position": "absolute",
    "top": "100%",
    "left": "0",
    "width": "380px",
    "zIndex": "20",
    "marginTop": "4px",
}
_SUGGESTIONS_CARD_STYLE = {
    "backgroundColor": "var(--card-bg)",
    "border": "1px solid var(--border)",
    "borderRadius": "8px",
    "boxShadow": "0 4px 12px rgba(11,11,11,0.12)",
    "overflow": "hidden",
}
_SUGGESTION_ROW_STYLE = {
    "display": "block",
    "width": "100%",
    "textAlign": "left",
    "padding": "8px 12px",
    "border": "none",
    "borderBottom": "1px solid var(--border)",
    "backgroundColor": "var(--card-bg)",
    "color": "var(--text)",
    "cursor": "pointer",
    "fontSize": "13px",
}


def _financials_valuation_blocks(suffix):
    """One ticker's Financials tabs and its Valuation/DCF panel, returned
    as two separate lists of children: (financials, valuation). Built once
    for the primary ticker (suffix="") and again for the Compare panel
    (suffix="-2"); every id in here gets that suffix so both copies can
    live in the DOM at once with independent callbacks. Split in two
    because Compare mode lays them out differently -- the two tickers'
    Financials sit side by side, but each DCF panel (assumptions + chart)
    needs the full page width, so the two Valuation panels stack instead
    (see _company_tracker_children)."""
    def _dcf_row(field, label):
        # id'd so the mobile media query can place it into a specific
        # grid cell independently of its sibling fields (see
        # [id^="dcf-row-..."] in custom.css) -- desktop never applies
        # that grid, so these ids are otherwise inert there.
        return html.Div(
            id=f"dcf-row-{field}{suffix}",
            style=_dcf_field_wrapper_style(field),
            children=[
                html.Label(label, style=_DCF_FIELD_LABEL_STYLE),
                _dcf_field_component(field, suffix),
            ],
        )

    # Two sub-groups (not one flat list of children) so mobile can lay
    # them out side by side instead of one narrow column with the rest of
    # the card's width sitting empty -- see [id^="dcf-assumptions-panel"]
    # in custom.css. On desktop these two divs carry no layout style of
    # their own, so they just stack in normal block flow exactly as the
    # flat list used to, pixel-identical to before. On mobile, both
    # dissolve via display:contents and every field/header inside gets
    # explicitly placed into one of two grid columns by id, independent
    # of this Python-level grouping -- e.g. Base FCF renders here (first,
    # in this main group) for desktop, but lands in the second mobile
    # column instead, to balance the two columns' heights there (the
    # sliders in this main group run taller than the market-data group's
    # plain number inputs).
    dcf_assumptions_panel = html.Div(
        id=f"dcf-assumptions-panel{suffix}",
        style=_FILTER_PANEL_STYLE,
        children=[
            html.Div(
                id=f"dcf-assumptions-main{suffix}",
                children=[
                    html.Div("DCF Assumptions", id=f"dcf-header-assumptions{suffix}",
                              style={"color": _HEADER_TEXT_COLOR, "fontWeight": "700",
                                     "marginBottom": "10px"}),
                    *[_dcf_row(field, label) for field, label in _DCF_INPUT_FIELDS[:3]],
                    html.Div("Model Settings", id=f"dcf-header-model-settings{suffix}",
                              style={"color": _HEADER_TEXT_COLOR, "fontWeight": "700",
                                     "marginTop": "14px", "marginBottom": "10px",
                                     "borderTop": "1px solid var(--border)", "paddingTop": "12px"}),
                    *[_dcf_row(field, label) for field, label in _DCF_INPUT_FIELDS[3:5]],
                ],
            ),
            html.Div(
                id=f"dcf-market-data{suffix}",
                children=[
                    # Hidden (not removed) on mobile -- see
                    # #dcf-header-market-data in custom.css -- once this
                    # group also holds Base FCF and Projection Years (see
                    # above), "Market Data" no longer describes everything
                    # in it there. Desktop keeps the header; that grouping
                    # is untouched for desktop.
                    html.Div("Market Data", id=f"dcf-header-market-data{suffix}",
                              style={"color": _HEADER_TEXT_COLOR, "fontWeight": "700",
                                     "marginTop": "14px", "marginBottom": "10px",
                                     "borderTop": "1px solid var(--border)", "paddingTop": "12px"}),
                    *[_dcf_row(field, label) for field, label in _DCF_INPUT_FIELDS[5:]],
                    html.Button("Calculate", id=f"calculate-dcf-btn{suffix}", n_clicks=0,
                                style={"width": "100%", "fontSize": "12px"}),
                ],
            ),
        ],
    )
    dcf_chart_column = html.Div(
        # minWidth 280px, not 0: a bare flex:1 + minWidth:0 lets the
        # browser satisfy the row by shrinking this down to near-nothing
        # instead of ever wrapping it below the (fixed-width) assumptions
        # panel -- on a narrow mobile screen that squeezed the chart into
        # an illegible sliver rather than the flexWrap on the parent row
        # (see _financials_valuation_blocks' caller) actually kicking in.
        style={"flex": "1", "minWidth": "280px"},
        children=[
            # The chart itself is only built where it's shown (see
            # _dcf_chart_view): always on desktop, but on mobile only once
            # the Valuation section is opened (valuation-toggle) -- a
            # Plotly chart costs real main-thread time on a phone even
            # while hidden. update_dcf writes the figure into
            # dcf-chart-store rather than straight into the Graph, so it
            # never has to target a component that may not exist yet.
            dcc.Store(id=f"dcf-chart-store{suffix}"),
            html.Div(id=f"dcf-chart-holder{suffix}"),
            html.Details(
                style={"marginTop": "20px"},
                children=[
                    html.Summary("View Calculation", style={"color": _HEADER_TEXT_COLOR, "cursor": "pointer",
                                                              "fontSize": "13px", "fontWeight": "600"}),
                    html.Div(
                        style={"marginTop": "12px"},
                        children=[
                            dash_table.DataTable(
                                id=f"dcf-table{suffix}",
                                columns=[{"name": "Line Item", "id": "line"}],
                                data=[],
                                cell_selectable=False,
                                style_table={"overflowX": "auto", "backgroundColor": _MANAGER_TABLE_BG},
                                style_cell=_MANAGER_TABLE_CELL_STYLE,
                                style_cell_conditional=[
                                    {"if": {"column_id": "line"}, "textAlign": "left",
                                     "fontWeight": "600", "color": _SECURITY_TEXT_COLOR},
                                ],
                                style_header=_MANAGER_TABLE_HEADER_STYLE,
                                style_data={"backgroundColor": _MANAGER_TABLE_BG},
                            ),
                            html.Div(
                                style={"marginTop": "16px", "maxWidth": "420px"},
                                children=dash_table.DataTable(
                                    id=f"dcf-summary-table{suffix}",
                                    columns=[
                                        {"name": "Metric", "id": "metric"},
                                        {"name": "Value", "id": "value"},
                                    ],
                                    data=[],
                                    cell_selectable=False,
                                    style_table={"overflowX": "auto", "backgroundColor": _MANAGER_TABLE_BG},
                                    style_cell=_MANAGER_TABLE_CELL_STYLE,
                                    style_cell_conditional=[
                                        {"if": {"column_id": "metric"}, "textAlign": "left",
                                         "fontWeight": "600", "color": _SECURITY_TEXT_COLOR},
                                    ],
                                    style_header=_MANAGER_TABLE_HEADER_STYLE,
                                    style_data={"backgroundColor": _MANAGER_TABLE_BG},
                                ),
                            ),
                        ],
                    ),
                ],
            ),
        ],
    )
    dcf_row_children = [dcf_assumptions_panel, dcf_chart_column]

    return [
        # Hidden until update_earnings_chart finds earnings history for the
        # loaded ticker (funds and recent IPOs have none).
        html.Div(
            id=f"earnings-wrap{suffix}",
            style={"display": "none"},
            children=[
                html.H3("Earnings", style={**_HEADER_STYLE, "marginBottom": "4px"}),
                html.Div(id=f"earnings-summary{suffix}", style={**_PARA_STYLE, "fontSize": "13px"}),
                # The dcc.Graph itself is only created once there's data
                # (see _earnings_outputs) -- an empty Plotly graph still
                # costs a full Plotly render, which adds up on a phone,
                # and Compare mode's copy is usually never shown at all.
                html.Div(id=f"earnings-chart-holder{suffix}"),
            ],
        ),
        html.Div(
            id=f"financials-header{suffix}",
            style=_FINANCIALS_HEADER_STYLE,
            children=[
                html.H3("Financials", style=_HEADER_STYLE),
                html.Button("Download Excel", id=f"download-btn{suffix}", n_clicks=0, disabled=True),
            ],
        ),
        # Shown instead of the Financials header/tabs above (see
        # _tab_visibility_styles' "price_only" mode) for a filer with
        # nothing to drive the financials tables or the DCF with -- a
        # company/politician-row click used to just leave the Growth
        # Rates tab's own table empty with no explanation.
        html.Div("No financials data available for this company.",
                 id=f"no-financials-msg{suffix}", style=_NO_FINANCIALS_MSG_STYLE),
        dcc.Download(id=f"download-csv{suffix}"),
        dcc.Tabs(
            id=f"view-tabs{suffix}",
            value="growth",
            # 0: never auto-collapse into Dash's own vertical/dropdown
            # mobile layout -- these pill navs have their own theming and
            # (via the shared #range-tabs mobile CSS rule / flexWrap) their
            # own wrapping behavior on narrow screens instead.
            mobile_breakpoint=0,
            style=_NAV_CONTAINER_STYLE,
            children=[
                dcc.Tab(
                    id=f"growth-tab{suffix}",
                    label="Growth Rates",
                    value="growth",
                    style=_NAV_TAB_STYLE,
                    selected_style=_NAV_TAB_SELECTED_STYLE,
                    children=html.Div(
                        dash_table.DataTable(
                            id=f"results-table{suffix}",
                            columns=DISPLAY_COLUMNS,
                            data=[],
                            cell_selectable=False,
                            style_cell_conditional=[
                                {"if": {"column_id": "end"}, "textAlign": "left", "color": _SECURITY_TEXT_COLOR},
                            ],
                            **_FINANCIALS_TABLE_STYLE,
                        ),
                        style={"marginTop": "16px"},
                    ),
                ),
                dcc.Tab(
                    id=f"income-tab{suffix}",
                    label="Income Statement",
                    value="income",
                    style=_NAV_TAB_STYLE,
                    selected_style=_NAV_TAB_SELECTED_STYLE,
                    children=html.Div(
                        dash_table.DataTable(
                            id=f"income-statement-table{suffix}",
                            columns=[{"name": "Breakdown", "id": "line"}],
                            data=[],
                            cell_selectable=False,
                            style_cell_conditional=[
                                {"if": {"column_id": "line"}, "textAlign": "left", "fontWeight": "600",
                                 "color": _SECURITY_TEXT_COLOR},
                            ],
                            **_FINANCIALS_TABLE_STYLE,
                        ),
                        style={"marginTop": "16px"},
                    ),
                ),
                dcc.Tab(
                    id=f"balance-tab{suffix}",
                    label="Balance Sheet",
                    value="balance",
                    style=_NAV_TAB_STYLE,
                    selected_style=_NAV_TAB_SELECTED_STYLE,
                    children=html.Div(
                        dash_table.DataTable(
                            id=f"balance-sheet-table{suffix}",
                            columns=[{"name": "Breakdown", "id": "line"}],
                            data=[],
                            cell_selectable=False,
                            style_cell_conditional=[
                                {"if": {"column_id": "line"}, "textAlign": "left", "fontWeight": "600",
                                 "color": _SECURITY_TEXT_COLOR},
                            ],
                            **_FINANCIALS_TABLE_STYLE,
                        ),
                        style={"marginTop": "16px"},
                    ),
                ),
                dcc.Tab(
                    id=f"cashflow-tab{suffix}",
                    label="Cash Flow Statement",
                    value="cashflow",
                    style=_NAV_TAB_STYLE,
                    selected_style=_NAV_TAB_SELECTED_STYLE,
                    children=html.Div(
                        dash_table.DataTable(
                            id=f"cash-flow-table{suffix}",
                            columns=[{"name": "Breakdown", "id": "line"}],
                            data=[],
                            cell_selectable=False,
                            style_cell_conditional=[
                                {"if": {"column_id": "line"}, "textAlign": "left", "fontWeight": "600",
                                 "color": _SECURITY_TEXT_COLOR},
                            ],
                            **_FINANCIALS_TABLE_STYLE,
                        ),
                        style={"marginTop": "16px"},
                    ),
                ),
                dcc.Tab(
                    id=f"holdings-tab{suffix}",
                    label="Top Holdings",
                    value="holdings",
                    style=_NAV_TAB_STYLE,
                    selected_style=_NAV_TAB_SELECTED_STYLE,
                    children=html.Div(
                        # No leading explanatory paragraph here (unlike an
                        # earlier version) -- every tab's content is just
                        # its DataTable now, so all 5 tabs' wrapper divs are
                        # the same height and the primary/compare panels'
                        # Valuation sections below stay aligned regardless
                        # of which tab happens to be active. The Symbol
                        # column's underline+pointer-cursor styling already
                        # signals it's clickable.
                        dash_table.DataTable(
                                id=f"holdings-table{suffix}",
                                columns=HOLDINGS_COLUMNS,
                                data=[],
                                cell_selectable=True,
                                style_cell_conditional=[
                                    {"if": {"column_id": "symbol"}, "textAlign": "left",
                                     "fontWeight": "600", "color": _SECURITY_TEXT_COLOR,
                                     "cursor": "pointer", "textDecoration": "underline"},
                                    {"if": {"column_id": "name"}, "textAlign": "left"},
                                ],
                                # DataTable auto-prefixes each selector here with the
                                # table's own id, so it's omitted below (adding it
                                # ourselves double-prefixes to e.g. "#holdings-table
                                # #holdings-table ...", which can never match since
                                # that id is only on one element).
                                css=[
                                    {"selector": 'td[data-dash-column="symbol"]:hover',
                                     "rule": "color: var(--text) !important; text-decoration: underline;"},
                                    # DataTable's built-in active/selected-cell
                                    # highlight (a hotpink border + reddish fill) is
                                    # meant for selecting/copying data, not a
                                    # click-to-navigate ticker link. Rather than
                                    # override just the cell--selected/focused
                                    # classes -- the selection rectangle also tints
                                    # individual border sides on neighboring cells --
                                    # force every cell's colors back to normal
                                    # unconditionally; harmless since that's already
                                    # their non-selected appearance.
                                    {"selector": "td.dash-cell",
                                     "rule": f"background-color: {_MANAGER_TABLE_BG} !important; "
                                             "border-color: var(--border) !important; "
                                             "outline-color: var(--border) !important;"},
                                    # style_cell_conditional only passes through a
                                    # fixed set of known style keys and silently drops
                                    # anything else, so this needs the raw-CSS route
                                    # instead -- without it the browser's default
                                    # skip-ink trimming makes the underline nearly
                                    # vanish under some tickers.
                                    {"selector": 'td[data-dash-column="symbol"]',
                                     "rule": "text-decoration-skip-ink: none !important;"},
                                ],
                                **_FINANCIALS_TABLE_STYLE,
                            ),
                        style={"marginTop": "16px"},
                    ),
                ),
            ],
        ),
    ], [
        # Wrapped (not just the section's own contents) so a fund/ETF ticker
        # -- no cash flows of its own to project, an operating-company DCF
        # is meaningless for it -- can hide the whole thing in one shot via
        # _tab_visibility_styles' dcf_wrap_style, the same is_fund switch
        # that already toggles the Financials/Holdings tabs above.
        html.Div(
            id=f"dcf-wrap{suffix}",
            children=[
                # Section heading + description only once, on the primary panel
                # -- in Compare mode the second panel stacks right below it
                # (see _company_tracker_children) under the same heading.
                # On mobile the whole section starts collapsed to just this
                # heading, opened by valuation-toggle (hidden on desktop, see
                # .valuation-toggle in custom.css) -- the clientside
                # callbacks after _dcf_chart_view show/hide dcf-body below.
                # dcf-wrap itself isn't toggled since generate already uses
                # its style to hide the whole section for funds.
                *([
                    html.Div(
                        style={"display": "flex", "alignItems": "center", "gap": "10px", "marginTop": "40px"},
                        children=[
                            html.H3("Valuation", style={**_HEADER_STYLE, "margin": "0"}),
                            html.Button("+", id="valuation-toggle", n_clicks=0, className="valuation-toggle",
                                        title="Show valuation"),
                        ],
                    ),
                ] if suffix == "" else []),
                # "TICKER — Company" label so the two stacked panels are
                # distinguishable in Compare mode -- filled in by
                # update_company_overview/_2 (same text as the KPI row
                # label), shown only in Compare mode (render_compare_mode).
                # The "-2" copy is only ever built once Compare mode is on
                # (see build_compare_panels), so it starts out visible.
                dcc.Interval(id=f"dcf-price-refresh{suffix}", interval=60000, n_intervals=0),
                html.Div(id=f"dcf-body{suffix}", children=[
                *([
                    html.P("A simple discounted cash flow model: projects Free Cash Flow forward at the "
                           "growth rate below, discounts each year back to present value, and adds a "
                           "discounted terminal value to estimate Enterprise and per-share fair value. "
                           "Inputs default from the company's own financials (where available) but are "
                           "yours to adjust — click Calculate to re-run with your changes.",
                           style={**_PARA_STYLE, "fontSize": "13px", "marginTop": "8px"}),
                ] if suffix == "" else []),
                html.Div(id=f"dcf-company-label{suffix}",
                         style=_DCF_COMPANY_LABEL_STYLE if suffix else _DCF_COMPANY_LABEL_HIDDEN_STYLE),
                # One shared Loading boundary around the banner, the assumptions
                # panel/chart, AND dcf-defaults-store itself (a non-visual Store,
                # but still a descendant here) -- so Dash's loading-state tracking
                # picks up generate's own in-flight status too (it Outputs
                # dcf-defaults-store.data), not just update_dcf's. Without that,
                # the spinner only ever covered update_dcf's own fast,
                # pure-computation run, never the slower live SEC fetches in
                # generate that precede it -- so on first load (or any fresh
                # ticker) the sliders would sit at their placeholder min-bound
                # value (0%, 6%, 0%) for however long those fetches took, reading
                # as "this didn't load" rather than "still loading."
                dcc.Loading(
                    custom_spinner=html.Div(className="spinner"),
                    children=[
                        html.Div(
                            id=f"dcf-banner{suffix}",
                            style={"backgroundColor": _MANAGER_TABLE_BG, "borderRadius": "10px",
                                   "padding": "20px", "marginTop": "8px"},
                        ),
                        html.Div(
                            style={"display": "flex", "flexWrap": "wrap", "gap": "16px", "alignItems": "flex-start",
                                   "marginTop": "16px"},
                            children=dcf_row_children,
                        ),
                        dcc.Store(id=f"dcf-defaults-store{suffix}", data=None),
                    ],
                ),
                ]),
            ],
        ),
    ]


def _movers_card(title, container_id):
    # Same scrollable-card shell as the Politicians tab's Recent Trades/
    # Leaderboard (see _politician_tracker_children) -- the
    # movers-scroll-poll clientside callbacks below rely on the container's
    # parentElement being this card's own overflowY:auto div.
    return html.Div(
        style={"flex": "1 1 420px", "minWidth": "0", "backgroundColor": "var(--card-bg)",
               "border": "1px solid var(--border)", "borderRadius": "14px",
               "padding": "0 20px 16px", "maxHeight": "480px", "overflowY": "auto"},
        children=[
            html.H3(title, style=_STICKY_CARD_TITLE_STYLE),
            html.Div(id=container_id),
        ],
    )


def _section_loading_overlay(overlay_id):
    """A spinner covering one section of the Companies tab while a newly
    picked stock's data for it loads -- shown/hidden by the
    section-spinner callbacks (_SECTION_LOADING_IDS). Starts visible: the panel is
    built with a lookup (the default ticker) already on its way."""
    return html.Div(id=overlay_id, className="section-loading", style={"display": "flex"},
                    children=html.Div(className="spinner"))


def _company_panel_skeleton():
    """What the Companies panel shows the instant its tab is opened, before
    build_company_panel's real content arrives: the tab's headings and its
    top sections' outlines, each with its own spinner -- rather than a
    full-page overlay hiding the whole tab. Replaced wholesale once the real
    panel lands (build_company_panel treats this as "not built yet")."""
    def card(title):
        return html.Div(
            style={"flex": "1 1 420px", "minWidth": "0", "backgroundColor": "var(--card-bg)",
                   "border": "1px solid var(--border)", "borderRadius": "14px", "padding": "0 20px 16px",
                   "minHeight": "220px", "display": "flex", "flexDirection": "column"},
            children=[html.H3(title, style={**_HEADER_STYLE, "padding": "16px 0 8px", "margin": "0"}),
                      html.Div(className="skeleton-spinner-wrap", children=html.Div(className="spinner"))],
        )
    return html.Div(
        id=_COMPANY_SKELETON_ID,
        style=_APP_CONTENT_STYLE,
        children=[
            html.H2("Stock Tracker", style=_HEADER_STYLE),
            html.P(_MOVERS_NOTE, style={**_PARA_STYLE, "fontSize": "13px"}),
            html.Div(style={"display": "flex", "gap": "48px", "flexWrap": "wrap"},
                     children=[card("Today's Top Gainers"), card("Today's Top Losers")]),
            html.Hr(style=_SECTION_DIVIDER_STYLE),
            html.H3("Look Up a Stock", style=_LOOKUP_HEADING_STYLE),
            html.Div(
                style={"backgroundColor": "var(--card-bg)", "border": "1px solid var(--border)",
                       "borderRadius": "14px", "marginTop": "24px", "minHeight": "420px",
                       "display": "flex", "flexDirection": "column"},
                children=html.Div(className="skeleton-spinner-wrap", children=html.Div(className="spinner")),
            ),
        ],
    )


_COMPANY_SKELETON_ID = "company-skeleton"


def _company_tracker_children():
    financials_1, valuation_1 = _financials_valuation_blocks("")
    return [
        # Kept here (not inside the per-ticker blocks) because callbacks
        # that are always on the page read them -- e.g. update_price_chart
        # takes rows-store-2 as an Input even outside Compare mode -- and
        # Compare mode's own blocks are only built on demand (see
        # build_compare_panels).
        dcc.Store(id="rows-store"),
        dcc.Store(id="suppress-next-suggestions", data=False),
        dcc.Store(id="rows-store-2"),
        dcc.Store(id="suppress-next-suggestions-2", data=False),
        # {"ticker", "title"} the moment a lookup's ticker is known -- set by
        # resolve_ticker_fast/_2 from the in-memory ticker map, in parallel
        # with (not after) the SEC lookup that fills rows-store. The price
        # chart, header/KPI tiles and earnings chart key off this, since
        # none of them need SEC data to start.
        dcc.Store(id="ticker-store"),
        dcc.Store(id="ticker-store-2"),
        html.H2("Stock Tracker", id="company-main-heading", style=_HEADER_STYLE),
        html.P(_MOVERS_NOTE, id="company-movers-note", style={**_PARA_STYLE, "fontSize": "13px"}),
        html.Div(
            id="company-movers-wrap",
            children=dcc.Loading(
                custom_spinner=html.Div(className="spinner"),
                # Same overlay_style override (and reasoning) as the
                # Politicians tab's Recent Trades/Leaderboard -- keeps
                # already-visible rows from blacking out on every
                # scroll-triggered "load more".
                overlay_style={"visibility": "visible"},
                children=html.Div(
                    style={"display": "flex", "gap": "48px", "flexWrap": "wrap"},
                    children=[
                        _movers_card("Today's Top Gainers", "top-gainers-container"),
                        _movers_card("Today's Top Losers", "top-losers-container"),
                    ],
                ),
            ),
        ),
        # Full, already-sorted lists live in the *-records stores; only a
        # growing prefix gets built into rows -- see load_day_movers/
        # render_mover_rows and the movers scroll-poll callbacks below.
        dcc.Store(id="top-gainers-records", data=[]),
        dcc.Store(id="top-gainers-visible-count", data=_MOVERS_PAGE_SIZE),
        dcc.Interval(id="top-gainers-scroll-poll", interval=1200, n_intervals=0),
        dcc.Store(id="top-losers-records", data=[]),
        dcc.Store(id="top-losers-visible-count", data=_MOVERS_PAGE_SIZE),
        dcc.Interval(id="top-losers-scroll-poll", interval=1200, n_intervals=0),
        html.Hr(id="lookup-stock-divider", style=_SECTION_DIVIDER_STYLE),
        html.H3("Look Up a Stock", id="lookup-stock-heading", style=_LOOKUP_HEADING_STYLE),
        # stock-header-{meta,name,price,change} start as placeholder/blank
        # text and are filled in by update_price_chart/update_company_overview
        # once a ticker's loaded (see those callbacks) -- price/change used
        # to be baked into the Plotly chart's own title; pulling them out
        # into real HTML lets the chart itself go quiet (no title, no
        # gridlines) while still surfacing the numbers prominently.
        html.Div(
            id="stock-header",
            style={"display": "flex", "justifyContent": "space-between", "alignItems": "flex-end",
                   "gap": "16px", "flexWrap": "wrap"},
            children=[
                html.Div(
                    id="stock-header-name-block",
                    style={"display": "flex", "flexDirection": "column", "gap": "6px"},
                    children=[
                        html.Div(id="stock-header-meta", style={"fontSize": "12px", "color": "var(--body-text)",
                                                                  "fontFamily": "'IBM Plex Mono', monospace"}),
                        html.Div(id="stock-header-name",
                                 style={"fontSize": "28px", "fontWeight": "600", "letterSpacing": "-0.02em",
                                        "color": "var(--text)"}),
                    ],
                ),
                # Two-segment toggle (same pill styling as the range picker
                # below) rather than a single button whose label swaps --
                # both modes stay visible so it reads as a toggle, not an
                # action. sync_compare_mode below turns its selected value
                # into the "compare-mode" store every other callback in
                # Compare mode actually keys off.
                dcc.Tabs(
                    id="compare-mode-tabs",
                    value="single",
                    mobile_breakpoint=0,
                    style={**_NAV_CONTAINER_STYLE, "marginTop": "0"},
                    children=[
                        dcc.Tab(label="Single Stock", value="single", style=_NAV_TAB_STYLE,
                                selected_style=_NAV_TAB_SELECTED_STYLE),
                        dcc.Tab(label="+ Compare", value="compare", style=_NAV_TAB_STYLE,
                                selected_style=_NAV_TAB_SELECTED_STYLE),
                    ],
                ),
            ],
        ),
        dcc.Store(id="compare-mode", data=False),
        # id'd (not just styled) so the mobile media query can hide it --
        # phone-width real estate is tight enough that this descriptive
        # line isn't worth the vertical space there; desktop keeps it.
        html.P("Sales, earnings, equity, cash, and ROIC growth from SEC 10-K XBRL data.",
               id="company-tagline", style={**_PARA_STYLE, "marginTop": "10px"}),
        html.Div(
            id="ticker-search-row",
            style={"display": "flex", "gap": "12px", "alignItems": "flex-end",
                   "flexWrap": "wrap"},
            children=[
                html.Div(
                    className="ticker-search-wrap",
                    style={"position": "relative"},
                    children=[
                        html.Label("Ticker or company name"),
                        dcc.Input(id="company-input", type="text", value="AAPL",
                                  placeholder="e.g. AAPL or Apple",
                                  autoComplete="off", n_submit=0,
                                  style={"width": "220px", "display": "block", "color": "var(--text)"}),
                        html.Div(id="company-suggestions", style=_SUGGESTIONS_CONTAINER_STYLE),
                    ],
                ),
                # Hidden until Compare mode is on (see render_compare_mode).
                html.Div(
                    id="compare-ticker-wrap",
                    className="ticker-search-wrap",
                    style={"position": "relative", "display": "none"},
                    children=[
                        html.Label("Compare to"),
                        dcc.Input(id="company-input-2", type="text", value="",
                                  placeholder="e.g. NVDA or Nvidia",
                                  autoComplete="off", n_submit=0,
                                  style={"width": "220px", "display": "block", "color": "var(--text)"}),
                        html.Div(id="company-suggestions-2", style=_SUGGESTIONS_CONTAINER_STYLE),
                    ],
                ),
            ],
        ),
        dcc.Loading(
            custom_spinner=html.Div(className="spinner"),
            children=html.Div(id="status-msg", className="collapse-when-empty", style={"marginTop": "16px", "whiteSpace": "pre-wrap"}),
        ),
        html.Div(id="company-candidates", className="collapse-when-empty", style={"marginTop": "8px"}),
        html.Div(
            id="compare-status-wrap",
            style={"display": "none"},
            children=[
                dcc.Loading(
                    custom_spinner=html.Div(className="spinner"),
                    children=html.Div(id="status-msg-2", className="collapse-when-empty", style={"marginTop": "8px", "whiteSpace": "pre-wrap"}),
                ),
                html.Div(id="company-candidates-2", className="collapse-when-empty", style={"marginTop": "8px"}),
            ],
        ),
        # Chart card: High/Low + range picker sit above the (now title-less,
        # gridline-less) chart itself, inside one bordered card. id'd (not
        # just styled) so the mobile media query can break it out to the
        # full viewport width instead of sitting inset like everything
        # else on the page (see #stock-chart-card in custom.css).
        html.Div(
            id="stock-chart-card",
            style={"backgroundColor": "var(--card-bg)", "border": "1px solid var(--border)",
                   "borderRadius": "14px", "padding": "18px 20px 12px", "marginTop": "24px",
                   "position": "relative"},
            children=[
                _section_loading_overlay("price-loading"),
                # Right above the chart itself (not up in the page header)
                # so the number you're looking at and the chart explaining
                # it sit together -- same on mobile and desktop, a real DOM
                # position rather than a CSS reorder.
                html.Div(
                    id="stock-header-price-inline",
                    style={"display": "flex", "alignItems": "baseline", "gap": "12px", "marginBottom": "12px"},
                    children=[
                        html.Span(id="stock-header-price",
                                  style={"fontFamily": "'IBM Plex Mono', monospace", "fontSize": "26px",
                                         "color": "var(--text)"}),
                        html.Span(id="stock-header-change",
                                  style={"fontFamily": "'IBM Plex Mono', monospace", "fontSize": "13px"}),
                    ],
                ),
                html.Div(
                    id="stock-chart-header-row",
                    style={"display": "flex", "justifyContent": "space-between", "alignItems": "center",
                           "gap": "12px", "flexWrap": "wrap"},
                    children=[
                        html.Div(
                            style={"display": "flex", "gap": "18px", "fontSize": "12px",
                                   "color": "var(--body-text)"},
                            children=[
                                html.Span(["High ", html.Span(
                                    id="stock-header-hi",
                                    style={"fontFamily": "'IBM Plex Mono', monospace", "color": "var(--text)"})]),
                                html.Span(["Low ", html.Span(
                                    id="stock-header-lo",
                                    style={"fontFamily": "'IBM Plex Mono', monospace", "color": "var(--text)"})]),
                            ],
                        ),
                        dcc.Tabs(
                            id="range-tabs",
                            value="6M",
                            mobile_breakpoint=0,
                            style={**_NAV_CONTAINER_STYLE, "marginTop": "0"},
                            children=[dcc.Tab(label=k, value=k, style=_NAV_TAB_STYLE,
                                               selected_style=_NAV_TAB_SELECTED_STYLE)
                                      for k in RANGE_KEYS],
                        ),
                    ],
                ),
                dcc.Interval(id="price-chart-refresh", interval=15000, n_intervals=0),
                # Drives the live-price dot's pulsing halo -- deliberately not
                # wired to update_price_chart; a clientside callback below
                # just tweaks the existing figure's marker opacity in place
                # (see build_price_figure) rather than round-tripping to the
                # server every ~1s.
                dcc.Interval(id="price-pulse-interval", interval=900, n_intervals=0),
                html.Div(id="price-pulse-sink", style={"display": "none"}),
                # No dcc.Loading here (unlike elsewhere in this app): its
                # spinner overlay would flash over the whole chart on every
                # 15s auto-refresh, which is the "blinks when it refreshes"
                # behavior -- the chart swap itself is fast enough not to
                # need loading feedback.
                html.Div(
                    dcc.Graph(
                        id="price-chart",
                        figure=empty_price_figure(),
                        config={"displayModeBar": False},
                    ),
                    style={"marginTop": "8px"},
                ),
            ],
        ),
        # Filled in by update_company_overview once a ticker's loaded.
        html.Div(
            id="stock-kpi-wrap",
            style={"marginTop": "20px", "position": "relative", "minHeight": "80px"},
            children=[
                _section_loading_overlay("kpi-loading"),
                html.Div(id="stock-kpi-label", style=_KPI_SECTION_LABEL_STYLE),
                html.Div(id="stock-kpi-grid",
                         style={"display": "grid", "gridTemplateColumns": "repeat(auto-fit, minmax(150px,1fr))",
                                "gap": "10px"}),
            ],
        ),
        # Compare mode's second ticker gets its own labeled KPI row right
        # below the first, rather than being squeezed beside it -- five
        # tiles per ticker is already a lot of horizontal room to share.
        # Hidden until Compare mode is on (see render_compare_mode), same
        # as compare-ticker-wrap/financials-col-2 above.
        html.Div(
            id="stock-kpi-wrap-2",
            style={"marginTop": "16px", "display": "none"},
            children=[
                html.Div(id="stock-kpi-label-2", style=_KPI_SECTION_LABEL_STYLE),
                html.Div(id="stock-kpi-grid-2",
                         style={"display": "grid", "gridTemplateColumns": "repeat(auto-fit, minmax(150px,1fr))",
                                "gap": "10px"}),
            ],
        ),
        html.Div(
            id="financials-columns",
            style={"display": "flex", "flexWrap": "wrap", "gap": "32px", "alignItems": "flex-start",
                   "marginTop": "24px"},
            children=[
                # financials_1[0] is the Earnings block, which loads off the
                # fast ticker store -- kept outside the Financials spinner so
                # it isn't hidden while the slower SEC lookup finishes.
                html.Div(style={"flex": "1", "minWidth": "0"}, children=[
                    financials_1[0],
                    html.Div(style={"position": "relative"},
                             children=[_section_loading_overlay("financials-loading"), *financials_1[1:]]),
                ]),
                # Hidden until Compare mode is on (see render_compare_mode).
                html.Div(id="financials-col-2", style={"flex": "1", "minWidth": "0", "display": "none"},
                         children=[]),
            ],
        ),
        # Valuation panels stack (full width each) rather than sitting side
        # by side like Financials above -- two DCF assumption panels + charts
        # squeezed into half-width columns each was unreadably cramped.
        html.Div(id="valuation-col-1", children=valuation_1),
        # Hidden until Compare mode is on (see render_compare_mode).
        # financials-col-2 and valuation-col-2 start empty and are filled
        # the first time Compare mode turns on (build_compare_panels) --
        # building both full copies up front (tables, tabs, DCF inputs,
        # charts) cost seconds of main-thread time on a phone for a panel
        # most visits never open.
        html.Div(id="valuation-col-2", style={"marginTop": "40px", "display": "none"}, children=[]),
    ]


EMPTY_COLS = [{"name": "Breakdown", "id": "line"}]

# All Equity Positions loads incrementally (see the scroll-poll-interval
# clientside callbacks below) rather than sending every holding to the
# table at once, since some managers file thousands of positions. 50
# rather than 100: every sort re-renders the whole visible slice, and
# at 100 rows a sort took ~0.7s on a phone-speed CPU (~0.45s at 50).
_POSITIONS_PAGE_SIZE = 50

# Same reasoning, for the Politicians tab's Recent Trades/Leaderboard
# cards (see recent-trades-scroll-poll/congress-leaderboard-scroll-poll
# below) -- each row is a richer hand-built card (member/chamber block,
# ticker, a colored BUY/SELL pill, a log-scale range bar) than a plain
# DataTable row, so a smaller page than _POSITIONS_PAGE_SIZE keeps the
# first render snappy.
_ACTIVITY_PAGE_SIZE = 30

# Companies tab's Top Gainers/Losers cards -- same incremental-load
# mechanism as _ACTIVITY_PAGE_SIZE above, starting at a top-10.
_MOVERS_PAGE_SIZE = 10
_MOVERS_FETCH_COUNT = 100
_MOVERS_NOTE = "Today's biggest moves among US-listed stocks with a market cap of roughly $2B+"

_FILTER_PANEL_STYLE = {
    "minWidth": "180px", "maxWidth": "180px",
    "backgroundColor": "var(--card-bg)", "borderRadius": "8px",
    "padding": "12px", "flex": "0 0 auto",
}
_FILTER_INPUT_STYLE = {"width": "100%", "fontSize": "12px", "color": "var(--text)", "boxSizing": "border-box"}

# Columns hold raw numeric values (not pre-formatted strings) so
# sort_action="native" sorts numerically instead of lexicographically —
# display formatting is applied separately via these Format specs. A
# missing ΔShares/ΔValue % (a brand-new position with no prior-quarter
# base) is stored as None and rendered via .nully("New"); Dash's native
# sort treats null as larger than any number, which conveniently puts new
# positions first when sorting that column descending, last when ascending.
_MONEY_FORMAT = Format(precision=2, scheme=Scheme.fixed, group=True)
_PORTFOLIO_PCT_FORMAT = Format(precision=4, scheme=Scheme.fixed)
_DELTA_PORTFOLIO_PCT_FORMAT = Format(precision=4, scheme=Scheme.fixed, sign=Sign.positive)
_DELTA_PCT_OR_NEW_FORMAT = Format(precision=2, scheme=Scheme.fixed, sign=Sign.positive).nully("New")
_DELTA_SHARES_VALUE_FORMAT = Format(precision=2, scheme=Scheme.fixed, sign=Sign.positive, group=True)
# share_price/prev_share_price are None for a position with no shares held
# that quarter (brand-new or fully-exited) -- "—" rather than "New" since
# there's no meaningful price to show either way.
_SHARE_PRICE_FORMAT = Format(precision=2, scheme=Scheme.fixed, group=True).nully("—")

# Ranked by delta_shares_value_m -- the change in share count priced at the
# last known per-share price -- rather than portfolio-% change: a
# position's value/weight can rise or fall purely from the stock's price
# moving with zero trading, so ranking by shares isolates actual
# buying/selling activity, but a raw share-count change means nothing
# without pricing it (10,000 shares of a $5 stock vs. a $500 one are very
# different trades). See build_holdings_comparison.


def _manager_row_to_record(r, companies):
    return {
        "issuer": r["issuer"],
        "shares_m": r["shares_m"],
        "prev_shares_m": r["prev_shares_m"],
        "delta_shares_m": r["delta_shares_m"],
        "delta_shares_value_m": r["delta_shares_value_m"],
        "value_m": r["value_m"],
        "portfolio_pct": r["portfolio_pct"],
        "delta_pct": r["delta_pct"],
        # Best-effort 13F-issuer-name -> ticker match (see
        # resolve_ticker_for_security) -- "" rather than None so the
        # DataTable's style_data_conditional filter_query below (which
        # can't test for None/falsy) has a plain string to compare
        # against. Makes the security name clickable through to the
        # Public Company Tracker when resolved (_build_delta_bar_row,
        # all-positions-table's conditional styling).
        "resolved_ticker": resolve_ticker_for_security(r["issuer"], companies) or "",
    }


def _top_buy_row_to_record(r, companies):
    return {
        "manager_name": r["manager_name"],
        "cik": int(r["cik"]),
        "total_portfolio_value_m": r["total_portfolio_value_m"],
        "issuer": r["issuer"],
        "delta_shares_value_pct_of_portfolio": r["delta_shares_value_pct_of_portfolio"],
        "delta_shares_value_m": r["delta_shares_value_m"],
        "delta_shares_pct": r["delta_shares_pct"],
        "value_m": r["value_m"],
        "portfolio_pct": r["portfolio_pct"],
        # See _manager_row_to_record's matching comment.
        "resolved_ticker": resolve_ticker_for_security(r["issuer"], companies) or "",
    }


# delta_shares_pct is None for a brand-new position (see
# _DELTA_PCT_OR_NEW_FORMAT elsewhere) -- the same signal used here to tag
# cards/bar-rows as New vs Added vs Trimmed, and by the position-filter
# chips' predicate below.
def _delta_tag(delta_shares_pct):
    if delta_shares_pct is None:
        return "New"
    if delta_shares_pct > 0:
        return "Added"
    if delta_shares_pct < 0:
        return "Trimmed"
    return "Unchanged"


_CARD_STYLE = {
    "backgroundColor": "var(--card-bg)", "border": "1px solid var(--border)", "borderRadius": "10px",
    "padding": "14px", "display": "flex", "flexDirection": "column", "gap": "10px",
    "width": "210px", "flexShrink": "0",
}
_CARD_BADGE_STYLE = {
    "fontFamily": "'IBM Plex Mono', monospace", "fontSize": "12px", "padding": "2px 7px",
    "borderRadius": "4px", "backgroundColor": "rgba(76,195,138,0.14)", "color": "var(--up)",
}


def _build_top_buy_card(r, idx):
    pct = r.get("delta_shares_value_pct_of_portfolio")
    pct_text = f"+{pct:.1f}%" if pct is not None else "—"
    value_m = r.get("delta_shares_value_m") or 0
    issuer_style = {"fontFamily": "'IBM Plex Mono', monospace", "fontSize": "15px",
                    "fontWeight": "500", "color": "var(--security-text)"}
    ticker = r.get("resolved_ticker")
    # top-buy-link, not manager-holding-link -- this list is cross-
    # manager (top_buys_across_managers) and always built eagerly, so the
    # same ticker could otherwise appear here AND in the currently-
    # searched manager's own Top Increases at once, giving two
    # components the identical pattern-matching id (the exact "two
    # children with the same key" React bug fixed earlier for Recent
    # Trades' rows -- see _build_trade_row). `idx` is the same fix here:
    # several different managers can each have their own top-buy row for
    # the same security (confirmed directly -- one real snapshot had 9
    # separate Booking Holdings cards), which without it would all share
    # one id/React key.
    issuer_span = (
        html.Span(r["issuer"], id={"type": "top-buy-link", "ticker": ticker, "idx": idx}, n_clicks=0,
                   style={**issuer_style, "textDecoration": "underline", "cursor": "pointer"})
        if ticker else html.Span(r["issuer"], style=issuer_style)
    )
    return html.Div(
        style=_CARD_STYLE,
        children=[
            html.Div(
                style={"display": "flex", "justifyContent": "space-between", "alignItems": "center"},
                children=[
                    issuer_span,
                    html.Span(pct_text, style=_CARD_BADGE_STYLE),
                ],
            ),
            html.Div(
                style={"display": "flex", "flexDirection": "column", "gap": "2px"},
                children=[
                    # Loads this manager in the Look Up a Manager section
                    # below (see select_manager_candidate).
                    html.Span(r["manager_name"],
                              id={"type": "top-buy-manager-link", "cik": r["cik"], "idx": idx}, n_clicks=0,
                              style={"fontSize": "13px", "color": "var(--body-text)", "lineHeight": "1.35",
                                     "textDecoration": "underline", "cursor": "pointer"}),
                    # The manager's total public-equity AUM -- what the
                    # "Fund Size" sort orders by, so it's visible why the
                    # cards land in the order they do.
                    html.Div(f"Equity AUM {_fmt_big_dollars(r['total_portfolio_value_m'] * 1e6)}",
                             style={"fontSize": "11px", "color": "var(--body-text)",
                                    "fontFamily": "'IBM Plex Mono', monospace"}),
                ],
            ),
            html.Div(
                style={"display": "flex", "justifyContent": "space-between", "fontSize": "12px",
                       "color": "var(--body-text)", "fontFamily": "'IBM Plex Mono', monospace"},
                children=[
                    html.Span(f"${value_m:,.0f}M"),
                    html.Span(_delta_tag(r.get("delta_shares_pct"))),
                ],
            ),
        ],
    )


def _quarter_end_label(iso):
    """"2026-06-30" -> "Jun 30, 2026" for the returns table's note."""
    try:
        d = date.fromisoformat(iso)
    except (TypeError, ValueError):
        return iso or "?"
    return f"{d:%b} {d.day}, {d.year}"


def _best_returns_note(rows):
    if not rows:
        return "Estimated returns will appear after the next weekly refresh of the top managers' data."
    return (f"Estimated return from {_quarter_end_label(rows[0].get('previous_period'))} to "
            f"{_quarter_end_label(rows[0].get('latest_period'))} on each manager's 13F portfolio as of the start "
            "of the quarter, as if held unchanged and ignoring trading during quarter. $1B+ equity "
            "portfolios with 10+ positions only.")


def _build_best_return_row(r, rank):
    ret = r["est_return_pct"]
    up = ret >= 0
    details = f"Equity AUM {_fmt_big_dollars(r['equity_aum_m'] * 1e6)} · {r['positions']} positions"
    return html.Div(
        style={"display": "grid", "gridTemplateColumns": "22px minmax(0,1fr) 78px", "gap": "12px",
               "alignItems": "center", "padding": "9px 0", "borderBottom": "1px solid var(--border)",
               "fontSize": "13px"},
        children=[
            html.Span(str(rank), style={"fontFamily": "'IBM Plex Mono', monospace", "fontSize": "12px",
                                          "color": "var(--body-text)"}),
            # Name and AUM/positions on one line; flexWrap lets the details
            # drop under the name only when the row is too narrow (phones)
            # rather than squeezing the name down to nothing.
            html.Div(
                style={"display": "flex", "flexWrap": "wrap", "alignItems": "baseline", "columnGap": "12px",
                       "rowGap": "2px", "minWidth": "0"},
                children=[
                    # Same pattern as a Top Buys card's manager name, so
                    # select_manager_candidate loads it in Look Up a Manager.
                    html.Span(r["manager_name"],
                              id={"type": "top-buy-manager-link", "cik": r["cik"], "idx": f"ret-{rank}"},
                              n_clicks=0,
                              style={"color": "var(--text)", "fontWeight": "500", "textDecoration": "underline",
                                     "cursor": "pointer", "maxWidth": "100%",
                                     "whiteSpace": "nowrap", "overflow": "hidden", "textOverflow": "ellipsis"}),
                    html.Span(details, style={"fontSize": "12px", "color": "var(--body-text)",
                                              "whiteSpace": "nowrap"}),
                ],
            ),
            html.Span(
                f"{ret:+.1f}%",
                style={"fontFamily": "'IBM Plex Mono', monospace", "fontSize": "12px", "fontWeight": "600",
                       "textAlign": "center", "padding": "3px 0", "borderRadius": "4px",
                       "backgroundColor": "rgba(76,195,138,0.14)" if up else "rgba(229,103,90,0.14)",
                       "color": "var(--up)" if up else "var(--down)"},
            ),
        ],
    )


def _build_delta_bar_row(r, max_abs_value, up):
    value_m = r.get("delta_shares_value_m") or 0
    width_pct = min(100.0, abs(value_m) / max_abs_value * 100) if max_abs_value else 0.0
    color = "var(--up)" if up else "var(--down)"
    sign = "+" if value_m >= 0 else "-"
    issuer_style = {"color": "var(--security-text)", "overflow": "hidden",
                    "textOverflow": "ellipsis", "whiteSpace": "nowrap"}
    ticker = r.get("resolved_ticker")
    # Clickable through to the Public Company Tracker only when
    # resolve_ticker_for_security found one -- most bond/preferred/
    # foreign-ADR holdings won't, and stay plain text rather than a
    # dead-looking link (see that function's docstring for why).
    issuer_span = (
        html.Span(r["issuer"], id={"type": "manager-holding-link", "ticker": ticker}, n_clicks=0,
                   style={**issuer_style, "textDecoration": "underline", "cursor": "pointer"})
        if ticker else html.Span(r["issuer"], style=issuer_style)
    )
    return html.Div(
        # 151px (72 * 1.5, then * 1.4 again): security names still got cut
        # off too early. The bar column is minmax(0,1fr) -- flexible -- so
        # widening this fixed column shrinks the bar to compensate; row
        # height, the delta-value column, and gaps are all unchanged.
        style={"display": "grid", "gridTemplateColumns": "151px minmax(0,1fr) 90px", "gap": "12px",
               "alignItems": "center", "fontSize": "13px", "padding": "4px 0"},
        children=[
            issuer_span,
            html.Div(
                style={"height": "6px", "backgroundColor": "var(--card-bg-2)", "borderRadius": "3px"},
                children=html.Div(style={"height": "6px", "borderRadius": "3px",
                                          "backgroundColor": color, "width": f"{width_pct}%"}),
            ),
            html.Span(f"{sign}${abs(value_m):,.0f}M", style={"fontFamily": "'IBM Plex Mono', monospace",
                                                                "textAlign": "right", "color": color}),
        ],
    )


def _build_delta_bar_list(records, up):
    if not records:
        return [html.Div("No data.", style={"fontSize": "13px", "color": "var(--body-text)"})]
    max_abs = max((abs(r.get("delta_shares_value_m") or 0) for r in records), default=0) or 1
    return [_build_delta_bar_row(r, max_abs, up) for r in records]


def _manager_summary_tiles(all_positions):
    if not all_positions:
        return []
    aum_m = sum(r.get("value_m") or 0 for r in all_positions)
    # all_positions is already sorted by portfolio_pct descending (see
    # thirteenf.build_holdings_comparison), so the first 5 are the top 5.
    top5_weight = sum(r.get("portfolio_pct") or 0 for r in all_positions[:5])
    new_count = sum(1 for r in all_positions if r.get("delta_shares_pct") is None)
    turnover = (sum(abs(r.get("delta_shares_value_m") or 0) for r in all_positions) / aum_m
                if aum_m else None)
    return [
        _kpi_tile("Equity AUM", _fmt_big_dollars(aum_m * 1e6) if aum_m else None),
        _kpi_tile("Top 5 Weight", f"{top5_weight:.1f}%" if all_positions else None),
        _kpi_tile("New Positions", str(new_count)),
        _kpi_tile("Turnover", f"{turnover * 100:.1f}%" if turnover is not None else None),
    ]


ALL_POSITIONS_COLUMNS = [
    {"name": "Security", "id": "issuer"},
    {"name": "Shares (MM)", "id": "shares_m", "type": "numeric", "format": _MONEY_FORMAT},
    {"name": "Prev Shares (MM)", "id": "prev_shares_m", "type": "numeric", "format": _MONEY_FORMAT},
    {"name": "ΔShares %", "id": "delta_shares_pct", "type": "numeric", "format": _DELTA_PCT_OR_NEW_FORMAT},
    {"name": "~ΔShares Value ($MM)", "id": "delta_shares_value_m", "type": "numeric",
     "format": _DELTA_SHARES_VALUE_FORMAT},
    {"name": "Avg Share Price ($)", "id": "share_price", "type": "numeric", "format": _SHARE_PRICE_FORMAT},
    {"name": "Avg Prev Share Price ($)", "id": "prev_share_price", "type": "numeric",
     "format": _SHARE_PRICE_FORMAT},
    {"name": "Value ($MM)", "id": "value_m", "type": "numeric", "format": _MONEY_FORMAT},
    {"name": "Prev Value ($MM)", "id": "prev_value_m", "type": "numeric", "format": _MONEY_FORMAT},
    {"name": "ΔValue %", "id": "delta_value_pct", "type": "numeric", "format": _DELTA_PCT_OR_NEW_FORMAT},
    {"name": "Portfolio %", "id": "portfolio_pct", "type": "numeric", "format": _PORTFOLIO_PCT_FORMAT},
    {"name": "Prev Portfolio %", "id": "prev_portfolio_pct", "type": "numeric", "format": _PORTFOLIO_PCT_FORMAT},
    {"name": "ΔPortfolio %", "id": "delta_pct", "type": "numeric", "format": _DELTA_PORTFOLIO_PCT_FORMAT},
]
# Investment Manager Tracker tables get their own dark-card theme (distinct
# from the light-card Public Company Tracker tables above): dark grey
# background, white text for numeric columns, theme blue for the Security
# column.
_MANAGER_TABLE_BG = "var(--card-bg)"
_MANAGER_TABLE_CELL_STYLE = {
    **_TABLE_CELL_STYLE,
    "backgroundColor": _MANAGER_TABLE_BG,
    "color": "var(--text)",
    "border": "none",
    "borderBottom": "1px solid var(--border)",
}
_MANAGER_TABLE_HEADER_STYLE = {
    "backgroundColor": _MANAGER_TABLE_BG,
    "color": "var(--body-text)",
    "fontWeight": "500",
    "fontSize": "11px",
    "textTransform": "uppercase",
    "letterSpacing": "0.05em",
    "border": "none",
    "borderBottom": "1px solid var(--border)",
    # Column names like "Prev Portfolio %" don't fit the numeric columns'
    # natural width — wrap onto a second line instead of truncating.
    "whiteSpace": "normal",
    "height": "auto",
    "lineHeight": "1.3",
}
# Explicit per-column min widths so headers have room to wrap cleanly and,
# with style_table's overflowX "auto" below, so the All Equity Positions
# table (10 columns) scrolls horizontally instead of squeezing every
# column down to fit the container.
_MANAGER_COLUMN_WIDTHS = {
    "issuer": "200px",
    "shares_m": "95px",
    "prev_shares_m": "100px",
    "delta_shares_pct": "90px",
    "delta_shares_m": "100px",
    "delta_shares_value_m": "130px",
    "share_price": "100px",
    "prev_share_price": "115px",
    "value_m": "100px",
    "prev_value_m": "105px",
    "delta_value_pct": "90px",
    "portfolio_pct": "95px",
    "prev_portfolio_pct": "105px",
    "delta_pct": "100px",
}
_DELTA_COLUMNS = ["delta_shares_m", "delta_shares_value_m", "delta_shares_pct",
                  "delta_value_pct", "delta_pct", "delta_shares_value_pct_of_portfolio"]
_DELTA_CONDITIONALS = [
    rule for c in _DELTA_COLUMNS for rule in (
        {"if": {"filter_query": f"{{{c}}} > 0", "column_id": c}, "color": "var(--up)"},
        {"if": {"filter_query": f"{{{c}}} < 0", "column_id": c}, "color": "var(--down)"},
    )
] + [
    # null ΔShares/ΔValue % renders as "New" (see _DELTA_PCT_OR_NEW_FORMAT)
    {"if": {"filter_query": f"{{{c}}} is blank", "column_id": c}, "color": "var(--up)"}
    for c in ("delta_shares_pct", "delta_value_pct")
]
_POL_CONDITIONALS = [
    {"if": {"filter_query": '{transaction_type} = "Purchase"', "column_id": "transaction_type"},
     "color": "var(--up)", "fontWeight": "600"},
    {"if": {"filter_query": '{transaction_type} contains "Sale"', "column_id": "transaction_type"},
     "color": "var(--down)", "fontWeight": "600"},
    {"if": {"filter_query": "{net_estimated_value} < 0", "column_id": "net_estimated_value"},
     "color": "var(--down)"},
]
_MANAGER_TABLE_STYLE = dict(
    style_table={"overflowX": "auto", "backgroundColor": _MANAGER_TABLE_BG},
    style_cell=_MANAGER_TABLE_CELL_STYLE,
    style_cell_conditional=[
        {"if": {"column_id": "issuer"}, "textAlign": "left", "color": _SECURITY_TEXT_COLOR},
    ] + [
        {"if": {"column_id": col_id}, "minWidth": width, "width": width}
        for col_id, width in _MANAGER_COLUMN_WIDTHS.items()
    ],
    style_header=_MANAGER_TABLE_HEADER_STYLE,
    style_data={"backgroundColor": _MANAGER_TABLE_BG},
    style_data_conditional=_DELTA_CONDITIONALS,
)
# Public Company Tracker's Financials tables (Growth Rates, Income Statement,
# Balance Sheet, Cash Flow Statement, Top Holdings) reuse the Investment
# Manager Tracker's dark-card theme so the two tabs look consistent. A
# fixed height (rather than sizing to content) keeps every tab -- and, in
# Compare mode, both the primary and compare panel's tables -- the same
# height regardless of row count (Income Statement typically has the most
# rows, ETF Top Holdings varies with fund size); anything taller scrolls.
_FINANCIALS_TABLE_STYLE = dict(
    style_table={"overflowX": "auto", "backgroundColor": _MANAGER_TABLE_BG,
                 "height": "450px", "overflowY": "auto"},
    style_cell=_MANAGER_TABLE_CELL_STYLE,
    style_header=_MANAGER_TABLE_HEADER_STYLE,
    style_data={"backgroundColor": _MANAGER_TABLE_BG},
    # Keeps the column headers (dates/labels) visible while the body
    # scrolls, instead of scrolling them out of view along with the rows.
    fixed_rows={"headers": True},
)
_MANAGER_NOTE = ("Ranked by ΔShares Value: the change in share count priced at the last known "
                  "per-share price")
# Pooled from every precomputed top-AUM manager's own all_positions (see
# thirteenf.top_buys_across_managers) rather than a live search across
# all of them -- so this is only as current as the weekly snapshot, and
# only covers the managers that snapshot successfully precomputed.
_TOP_BUYS_NOTE = ("The biggest buys this past quarter, ranked by purchase activity as percentage of "
                   "AUM signaling largest conviction")
_TOP_BUYS_NOTE_BY_AUM = "The largest buy this past quarter from each of the 50 largest managers, ranked by fund AUM"
# Sort By options for the Top Buys cards -- value -> (label, note text,
# which load_top_buys() list it shows). Two separately precomputed lists,
# not one list re-sorted: see thirteenf.top_buys_across_managers/
# largest_managers_top_buys.
_TOP_BUYS_SORTS = {
    "conviction": ("Highest Conviction", _TOP_BUYS_NOTE, "conviction"),
    "aum": ("Fund Size", _TOP_BUYS_NOTE_BY_AUM, "fund_size"),
}

# Politician Tracker: House and Senate members' STOCK Act disclosures
# (Periodic Transaction Reports). These report individual buy/sell events in a
# dollar RANGE rather than an exact share count, and there's no quarterly
# holdings snapshot or net-worth figure to compute a portfolio % from — so
# unlike the 13F-based manager tables, "position size" here is an estimate
# built by accumulating each disclosed transaction's range midpoint over
# time. See congress_trades.py's module docstring for the full caveat.
_POL_MONEY_FORMAT = Format(precision=0, scheme=Scheme.fixed, group=True)
_POL_COUNT_FORMAT = Format(precision=0, scheme=Scheme.fixed)

POLITICIAN_MOVE_COLUMNS = [
    {"name": "Ticker", "id": "ticker"},
    {"name": "Type", "id": "transaction_type"},
    {"name": "Date", "id": "transaction_date"},
    {"name": "Amount Low ($)", "id": "amount_low", "type": "numeric", "format": _POL_MONEY_FORMAT},
    {"name": "Amount High ($)", "id": "amount_high", "type": "numeric", "format": _POL_MONEY_FORMAT},
]
POLITICIAN_POSITIONS_COLUMNS = [
    {"name": "Ticker", "id": "ticker"},
    {"name": "Est. Net Value ($)", "id": "net_estimated_value", "type": "numeric", "format": _POL_MONEY_FORMAT},
    {"name": "Total Bought ($)", "id": "total_bought", "type": "numeric", "format": _POL_MONEY_FORMAT},
    {"name": "Total Sold ($)", "id": "total_sold", "type": "numeric", "format": _POL_MONEY_FORMAT},
    {"name": "Transactions", "id": "transaction_count", "type": "numeric", "format": _POL_COUNT_FORMAT},
    {"name": "Last Transaction", "id": "last_transaction_date"},
]
_POLITICIAN_COLUMN_WIDTHS = {
    "ticker": "90px",
    "transaction_type": "110px",
    "transaction_date": "100px",
    "amount_low": "120px",
    "amount_high": "120px",
    "net_estimated_value": "130px",
    "total_bought": "120px",
    "total_sold": "110px",
    "transaction_count": "100px",
    "last_transaction_date": "120px",
}
_POLITICIAN_TABLE_STYLE = dict(
    style_table={"overflowX": "auto", "backgroundColor": _MANAGER_TABLE_BG},
    style_cell=_MANAGER_TABLE_CELL_STYLE,
    style_cell_conditional=[
        {"if": {"column_id": "ticker"}, "textAlign": "left", "color": _SECURITY_TEXT_COLOR},
    ] + [
        {"if": {"column_id": col_id}, "minWidth": width, "width": width}
        for col_id, width in _POLITICIAN_COLUMN_WIDTHS.items()
    ],
    style_header=_MANAGER_TABLE_HEADER_STYLE,
    style_data={"backgroundColor": _MANAGER_TABLE_BG},
    style_data_conditional=_POL_CONDITIONALS,
)
_POLITICIAN_NOTE = ("Every ticker with disclosed activity, ranked by estimated net position, built "
                     "by accumulating the midpoint of each disclosed buy (+) and sell (-) over all "
                     "available filing history. Amounts are disclosed dollar RANGES (STOCK Act "
                     "filings don't require exact figures), so position sizes below are estimates "
                     "built from the midpoint of each disclosed transaction and are approximations.")
_POLITICIAN_POSITION_FILTERS = [
    ("net_estimated_value", "Est. Net Value ($)"),
    ("total_bought", "Total Bought ($)"),
    ("total_sold", "Total Sold ($)"),
    ("transaction_count", "Transactions"),
]
_POLITICIAN_FILTER_FIELDS = [field for field, _label in _POLITICIAN_POSITION_FILTERS]

# Cross-chamber summary tables (above the House/Senate nav): "member" is
# the blue left-aligned column here instead of "ticker"/"issuer".
_CHAMBER_LABELS = {"house": "House", "senate": "Senate"}
_ACTIVITY_WINDOW_NOTE = (
    "Covers filings from roughly the last 6 months across every current House member and "
    "senator. Does not include activity before elected to office therefore portfolio values "
    "may be negative."
)


_POSITION_CHIPS = [
    ("all", "All"),
    ("new", "New positions"),
    ("added", "Added"),
    ("trimmed", "Trimmed"),
    ("big", "> 1% of fund"),
]


def _position_chip_style(active):
    return {
        "fontSize": "12px", "padding": "5px 10px", "borderRadius": "999px", "cursor": "pointer",
        "whiteSpace": "nowrap",
        "border": f"1px solid {'var(--up)' if active else 'var(--border)'}",
        "backgroundColor": "rgba(76,195,138,0.12)" if active else "transparent",
        "color": "var(--up)" if active else "var(--body-text)",
    }


def _manager_tracker_children():
    top_buy_companies = load_ticker_map(_session)
    # Both card lists, keyed by Sort By value. idx is prefixed with the
    # list's own key so the two lists' cards never share a top-buy-link id
    # (and React key) with each other.
    top_buys = load_top_buys()
    top_buy_records = {
        value: [{**_top_buy_row_to_record(r, top_buy_companies), "idx": f"{value}-{i}"}
                for i, r in enumerate(top_buys[list_key])]
        for value, (_label, _note, list_key) in _TOP_BUYS_SORTS.items()
    }
    return [
        html.H2("Investment Manager Tracker", style=_HEADER_STYLE),
        html.H3("Top Buys From Largest Managers", style=_HEADER_STYLE),
        html.Div(
            style={"display": "flex", "justifyContent": "space-between", "alignItems": "center",
                   "gap": "8px 16px", "flexWrap": "wrap", "marginBottom": "12px"},
            children=[
                html.P(_TOP_BUYS_NOTE, id="top-buys-note",
                       style={**_PARA_STYLE, "fontSize": "13px", "margin": "0"}),
                html.Div(
                    style={"display": "flex", "alignItems": "center", "gap": "8px"},
                    children=[
                        html.Span("Sort by", style={"fontSize": "12px", "color": "var(--body-text)"}),
                        # Same pill toggle as the Companies tab's Single
                        # Stock/Compare switch. sort_top_buys (below)
                        # re-orders the cards from top-buys-records.
                        dcc.Tabs(
                            id="top-buys-sort",
                            value="conviction",
                            mobile_breakpoint=0,
                            style={**_NAV_CONTAINER_STYLE, "marginTop": "0"},
                            children=[dcc.Tab(label=label, value=value,
                                              style={**_NAV_TAB_STYLE, "padding": "6px 12px", "fontSize": "12px"},
                                              selected_style={**_NAV_TAB_SELECTED_STYLE, "padding": "6px 12px",
                                                              "fontSize": "12px"})
                                      for value, (label, _note, _list) in _TOP_BUYS_SORTS.items()],
                        ),
                    ],
                ),
            ],
        ),
        html.Div(
            id="top-buys-table-container",
            style={"display": "flex", "gap": "10px", "overflowX": "auto", "paddingBottom": "6px"},
            children=[_build_top_buy_card(r, r["idx"]) for r in top_buy_records["conviction"]],
        ),
        # Both lists' records, so sort_top_buys can swap between them
        # without another round trip for the data.
        dcc.Store(id="top-buys-records", data=top_buy_records),
        html.H3("Best Performing Funds Previous Quarter", style={**_HEADER_STYLE, "marginTop": "32px"}),
        html.P(_best_returns_note(top_buys.get("best_returns")),
               style={**_PARA_STYLE, "fontSize": "13px", "marginTop": "0", "maxWidth": "760px"}),
        html.Div(
            id="best-returns-card",
            style={"backgroundColor": "var(--card-bg)", "border": "1px solid var(--border)",
                   "borderRadius": "14px", "padding": "4px 20px 8px", "maxWidth": "760px"},
            children=[_build_best_return_row(r, i + 1) for i, r in enumerate(top_buys.get("best_returns") or [])]
            or html.P("No data yet.", style={"color": "var(--body-text)", "fontSize": "13px"}),
        ),
        html.Hr(style=_SECTION_DIVIDER_STYLE),
        html.H3("Look Up a Manager", id="lookup-manager-heading", style=_LOOKUP_HEADING_STYLE),
        html.Div(
            style={"display": "flex", "gap": "12px", "alignItems": "flex-end", "flexWrap": "wrap"},
            children=[
                html.Div(
                    className="manager-search-wrap",
                    style={"position": "relative"},
                    children=[
                        html.Label("Investment manager name"),
                        dcc.Input(id="manager-input", type="text", value="Berkshire Hathaway",
                                  placeholder="e.g. Berkshire Hathaway",
                                  autoComplete="off", n_submit=0,
                                  style={"width": "260px", "display": "block", "color": "var(--text)"}),
                        html.Div(id="manager-suggestions", style=_SUGGESTIONS_CONTAINER_STYLE),
                    ],
                ),
            ],
        ),
        dcc.Loading(
            custom_spinner=html.Div(className="spinner"),
            children=html.Div(id="manager-status-msg", style={"marginTop": "16px", "whiteSpace": "pre-wrap"}),
        ),
        html.Div(id="manager-candidates", style={"marginTop": "8px"}),
        # Filled in by generate_manager/select_manager_candidate once a
        # manager's loaded (empty/hidden until then, same as the KPI grid
        # on the Company Tracker).
        html.Div(id="manager-summary-tiles",
                 style={"display": "grid", "gridTemplateColumns": "repeat(auto-fit, minmax(150px,1fr))",
                        "gap": "10px", "marginTop": "16px"}),
        html.P("Top holding increases and decreases quarter-over-quarter, from SEC 13F-HR filings.",
               style={**_PARA_STYLE, "marginTop": "24px"}),
        html.Div(
            style={"display": "flex", "gap": "24px", "flexWrap": "wrap"},
            children=[
                html.Div(
                    style={"flex": "1 1 420px", "minWidth": "0"},
                    children=[
                        html.H3("Top Increases This Quarter", style=_HEADER_STYLE),
                        html.P(_MANAGER_NOTE, style={**_PARA_STYLE, "fontSize": "13px"}),
                        html.Div(id="increases-table-container"),
                    ],
                ),
                html.Div(
                    style={"flex": "1 1 420px", "minWidth": "0"},
                    children=[
                        html.H3("Top Decreases This Quarter", style=_HEADER_STYLE),
                        html.P(_MANAGER_NOTE, style={**_PARA_STYLE, "fontSize": "13px"}),
                        html.Div(id="decreases-table-container"),
                    ],
                ),
            ],
        ),
        html.H3("All Equity Positions", style={**_HEADER_STYLE, "marginTop": "40px"}),
        dcc.Download(id="download-positions-csv"),
        html.P("Every current or prior-quarter holding, ranked by portfolio weight, with "
               "quarter-over-quarter share/value/allocation changes.",
               style={**_PARA_STYLE, "fontSize": "13px"}),
        # Filter chips apply instantly (no Calculate step) -- "All" doubles
        # as the old "Clear Filters" button, since it's just another chip.
        dcc.Store(id="position-filter-chip", data="all"),
        html.Div(
            style={"display": "flex", "gap": "8px", "alignItems": "center", "flexWrap": "wrap",
                   "marginTop": "8px"},
            children=[
                html.Span("Show", style={"fontSize": "12px", "color": "var(--body-text)", "marginRight": "4px"}),
                *[
                    html.Div(label, id=f"chip-{key}", n_clicks=0, style=_position_chip_style(key == "all"))
                    for key, label in _POSITION_CHIPS
                ],
                html.Button("Export CSV", id="download-positions-btn", n_clicks=0, disabled=True,
                            style={"marginLeft": "auto", "fontSize": "12px"}),
            ],
        ),
        html.Div(
            style={"marginTop": "12px"},
            children=dash_table.DataTable(
                id="all-positions-table",
                columns=ALL_POSITIONS_COLUMNS,
                data=[],
                # True so a click on a resolved-ticker "issuer" cell (see
                # the active_cell handling alongside select_global_search_
                # result) registers at all -- see the css override below
                # for why this doesn't bring back DataTable's default
                # selection-box look.
                cell_selectable=True,
                page_action="none",
                fixed_rows={"headers": True},
                # fixed_columns (freezing Security) is switched on for
                # mobile only -- see its viewport-is-mobile callback.
                # "native" sort puts a null ΔShares/ΔValue % ("New"
                # position) last regardless of ascending/descending
                # — "custom" hands sorting to the clientside
                # callback below instead, which treats null as
                # larger than any number so New positions sort
                # first when descending, last when ascending.
                sort_action="custom",
                sort_by=[],
                css=[
                    {"selector": 'td[data-dash-column="issuer"]:hover',
                     "rule": "color: var(--text) !important;"},
                    # DataTable's built-in active/selected-cell highlight
                    # (a hotpink border + reddish fill) is meant for
                    # selecting/copying data, not a click-to-navigate
                    # ticker link -- see holdings-table's identical
                    # override above for why every cell's colors are
                    # force-reset unconditionally rather than just the
                    # selected one.
                    {"selector": "td.dash-cell",
                     "rule": f"background-color: {_MANAGER_TABLE_BG} !important; "
                             "border-color: var(--border) !important; "
                             "outline-color: var(--border) !important;"},
                    {"selector": 'td[data-dash-column="issuer"]',
                     "rule": "text-decoration-skip-ink: none !important;"},
                ],
                **{**_MANAGER_TABLE_STYLE,
                   "style_table": {**_MANAGER_TABLE_STYLE["style_table"],
                                    "maxHeight": "600px", "overflowY": "auto"},
                   # Only an "issuer" cell with a resolved_ticker (a
                   # hidden field, not in ALL_POSITIONS_COLUMNS -- see
                   # _all_positions_row_to_record) reads as a link; most
                   # bond/preferred/foreign-ADR holdings don't resolve
                   # one and stay plain, unclickable text.
                   "style_data_conditional": _MANAGER_TABLE_STYLE["style_data_conditional"] + [
                       {"if": {"column_id": "issuer", "filter_query": '{resolved_ticker} != ""'},
                        "textDecoration": "underline", "cursor": "pointer"},
                   ]},
            ),
        ),
        dcc.Store(id="suppress-next-manager-suggestions", data=False),
        dcc.Store(id="manager-search-debounced", data=""),
        # Infinite scroll for the positions table: only the first
        # _POSITIONS_PAGE_SIZE rows of the full result ever reach the
        # table's own data prop; scrolling near the bottom polls (see
        # scroll-poll-interval below) and grows the visible slice.
        dcc.Store(id="all-positions-full", data=[]),
        dcc.Store(id="all-positions-visible-count", data=_POSITIONS_PAGE_SIZE),
        # Set alongside all-positions-full so download_positions knows
        # whether that list is the real, complete one or the precomputed
        # snapshot's position-capped version (see
        # thirteenf.build_top_managers) -- {"cik": int, "truncated": bool}.
        dcc.Store(id="manager-positions-meta", data=None),
        # Every tick — even a no-op one — makes Dash briefly touch the
        # app's root DOM for its loading-state bookkeeping, which at 400ms
        # was fast enough to read as a visible flicker across the whole
        # page. 1200ms is still well under human "instant" scroll-loading
        # expectations but cuts that churn to a third.
        dcc.Interval(id="scroll-poll-interval", interval=1200, n_intervals=0),
    ]


_CHAMBER_ROSTER_FN = {"house": list_all_house_members, "senate": list_all_senators}
_CHAMBER_DEFAULT_KEY_PREFIX = {"house": "house|Pelosi|", "senate": ""}


def _politician_dropdown_key(c):
    return f"{c['chamber']}|{c['last']}|{c['first']}"


def _politician_dropdown_options(chamber):
    roster = _CHAMBER_ROSTER_FN[chamber](session=_session)
    options = [
        {
            "label": f"{c['display']} ({c['state_dst']})" if c["state_dst"] else c["display"],
            "value": _politician_dropdown_key(c),
        }
        for c in roster
    ]
    default_prefix = _CHAMBER_DEFAULT_KEY_PREFIX.get(chamber, "")
    default_value = next((o["value"] for o in options if o["value"].startswith(default_prefix)), None)
    if default_value is None and options:
        default_value = options[0]["value"]
    return options, default_value


def _search_all_members(query, limit=5):
    """Search both chambers' rosters by first/last name substring, for the
    sidebar's cross-tracker search box -- same matching/ranking logic as
    congress_trades.search_politicians, just across both chambers instead
    of House only (that function predates the Senate roster and isn't
    otherwise used)."""
    q = query.strip().lower()
    if not q:
        return []
    roster = list_all_house_members(session=_session) + list_all_senators(session=_session)
    candidates = [
        c for c in roster
        if q in c["last"].lower() or q in c["first"].lower() or q in f"{c['first']} {c['last']}".lower()
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

    candidates.sort(key=rank)
    return candidates[:limit]


def _politician_tracker_children(initial_pending=None):
    # initial_pending ("chamber|last|first", from pending-member-selection)
    # lets build_politician_panel seed the correct chamber/member in this
    # one synchronous build instead of defaulting to House and relying on
    # apply_pending_politician_selection to correct it afterward -- on a
    # first-ever visit (e.g. a cross-chamber pick like McConnell via
    # global search before Politicians has ever been opened), that
    # after-the-fact correction raced this panel's own build with no
    # ordering guarantee, so the hardcoded House default could win and
    # leave the chamber tab stuck on House even though the member/dropdown
    # ended up right (confirmed matches the reported symptom).
    initial_chamber = initial_pending.split("|", 1)[0] if initial_pending else "house"
    options, default_value = _politician_dropdown_options(initial_chamber)
    value = initial_pending if initial_pending else default_value
    return [
        html.H2("Politician Tracker", style=_HEADER_STYLE),
        html.P("Buy/sell activity disclosed by members of Congress under the STOCK Act "
               "(Periodic Transaction Reports).", style=_PARA_STYLE),
        html.P(_ACTIVITY_WINDOW_NOTE, style={**_PARA_STYLE, "fontSize": "13px"}),
        dcc.Loading(
            custom_spinner=html.Div(className="spinner"),
            # dcc.Loading's default overlay_style hides its children
            # (visibility:hidden) for as long as any callback outputting
            # into them is in flight -- fine for a component that only
            # loads once, but render_trade_rows/render_leaderboard_rows
            # also fire on every scroll-triggered "load more" increment
            # (see recent-trades-scroll-poll/congress-leaderboard-scroll-
            # poll below), so without this override the already-visible
            # rows would black out (revealing the page's own dark
            # background underneath) on every such reload, not just the
            # first one.
            overlay_style={"visibility": "visible"},
            children=html.Div(
                style={"display": "flex", "gap": "48px", "flexWrap": "wrap"},
                children=[
                    html.Div(
                        style={"flex": "1 1 420px", "minWidth": "0", "backgroundColor": "var(--card-bg)",
                               "border": "1px solid var(--border)", "borderRadius": "14px",
                               "padding": "0 20px 16px", "maxHeight": "480px", "overflowY": "auto"},
                        children=[
                            html.H3("Most Recent Trades (All Members)", style=_STICKY_CARD_TITLE_STYLE),
                            html.Div(id="recent-trades-container"),
                        ],
                    ),
                    html.Div(
                        style={"flex": "1 1 420px", "minWidth": "0", "backgroundColor": "var(--card-bg)",
                               "border": "1px solid var(--border)", "borderRadius": "14px",
                               "padding": "0 20px 16px", "maxHeight": "480px", "overflowY": "auto"},
                        children=[
                            html.H3("Largest Est. Portfolio Value (All Members)", style=_STICKY_CARD_TITLE_STYLE),
                            html.Div(id="congress-leaderboard-container"),
                        ],
                    ),
                ],
            ),
        ),
        # Recent Trades/Leaderboard load incrementally, same reasoning and
        # mechanism as All Equity Positions' scroll-poll-interval
        # (dash_app.py, _manager_tracker_children) -- see
        # load_activity_summary/render_trade_rows/render_leaderboard_rows
        # and the two scroll-poll clientside callbacks below. The *-records
        # stores hold the full, already-sorted dataset; only a growing
        # prefix of each ever gets built into actual row components.
        dcc.Store(id="recent-trades-records", data=[]),
        dcc.Store(id="recent-trades-visible-count", data=_ACTIVITY_PAGE_SIZE),
        dcc.Interval(id="recent-trades-scroll-poll", interval=1200, n_intervals=0),
        dcc.Store(id="congress-leaderboard-records", data=[]),
        dcc.Store(id="congress-leaderboard-visible-count", data=_ACTIVITY_PAGE_SIZE),
        dcc.Interval(id="congress-leaderboard-scroll-poll", interval=1200, n_intervals=0),
        # pending-member-selection lives in app.layout's global scope now,
        # not here -- see there for why. Set by select_member_from_summary
        # when a row click needs to switch chambers first;
        # update_politician_roster (triggered by that chamber switch)
        # consumes it instead of falling back to that chamber's default
        # member, then clears it.
        html.Hr(style=_SECTION_DIVIDER_STYLE),
        html.H3("Look Up a Member", id="lookup-member-heading", style=_LOOKUP_HEADING_STYLE),
        dcc.Tabs(
            id="politician-chamber-tabs",
            value=initial_chamber,
            mobile_breakpoint=0,
            style=_NAV_CONTAINER_STYLE,
            children=[
                dcc.Tab(label="House of Representatives", value="house",
                        style=_NAV_TAB_STYLE, selected_style=_NAV_TAB_SELECTED_STYLE),
                dcc.Tab(label="Senate", value="senate",
                        style=_NAV_TAB_STYLE, selected_style=_NAV_TAB_SELECTED_STYLE),
            ],
        ),
        html.Div(
            style={"display": "flex", "gap": "12px", "alignItems": "flex-end", "flexWrap": "wrap",
                   "marginTop": "16px"},
            children=[
                html.Div(
                    children=[
                        html.Label("Member of Congress"),
                        dcc.Dropdown(
                            id="politician-input",
                            options=options,
                            value=value,
                            clearable=False,
                            searchable=True,
                            style={"width": "320px", "color": "var(--text)"},
                        ),
                    ],
                ),
            ],
        ),
        dcc.Loading(
            custom_spinner=html.Div(className="spinner"),
            children=html.Div(id="politician-status-msg", style={"marginTop": "16px", "whiteSpace": "pre-wrap"}),
        ),
        html.P(_POLITICIAN_NOTE, style={**_PARA_STYLE, "fontSize": "13px", "marginTop": "24px"}),
        html.Div(
            style={"display": "flex", "gap": "24px", "flexWrap": "wrap", "marginTop": "8px"},
            children=[
                html.Div(
                    style={"flex": "1 1 420px", "minWidth": "0"},
                    children=[
                        html.H3("Top Increases (Last 12 Months)", style=_HEADER_STYLE),
                        dash_table.DataTable(id="politician-increases-table", columns=POLITICIAN_MOVE_COLUMNS,
                                              data=[], cell_selectable=False, **_POLITICIAN_TABLE_STYLE),
                    ],
                ),
                html.Div(
                    style={"flex": "1 1 420px", "minWidth": "0"},
                    children=[
                        html.H3("Top Decreases (Last 12 Months)", style=_HEADER_STYLE),
                        dash_table.DataTable(id="politician-decreases-table", columns=POLITICIAN_MOVE_COLUMNS,
                                              data=[], cell_selectable=False, **_POLITICIAN_TABLE_STYLE),
                    ],
                ),
            ],
        ),
        html.H3("All Equity Positions (Estimated)", style={**_HEADER_STYLE, "marginTop": "40px"}),
        html.Div(
            style={"display": "flex", "flexWrap": "wrap", "gap": "16px", "alignItems": "flex-start",
                   "marginTop": "8px"},
            children=[
                html.Div(
                    style=_FILTER_PANEL_STYLE,
                    children=[
                        html.Div("Filters", style={"color": _HEADER_TEXT_COLOR, "fontWeight": "700",
                                                     "marginBottom": "10px"}),
                        *[
                            html.Div(
                                style={"marginBottom": "10px"},
                                children=[
                                    html.Label(label, style={"color": _BODY_TEXT_COLOR, "fontSize": "12px",
                                                              "display": "block", "marginBottom": "3px"}),
                                    html.Div(
                                        style={"display": "flex", "gap": "4px"},
                                        children=[
                                            dcc.Input(id=f"pol-filter-{field}-min", type="number",
                                                      placeholder="Min", style=_FILTER_INPUT_STYLE),
                                            dcc.Input(id=f"pol-filter-{field}-max", type="number",
                                                      placeholder="Max", style=_FILTER_INPUT_STYLE),
                                        ],
                                    ),
                                ],
                            )
                            for field, label in _POLITICIAN_POSITION_FILTERS
                        ],
                        html.Div(
                            style={"display": "flex", "gap": "6px"},
                            children=[
                                html.Button("Calculate", id="calculate-politician-filters-btn", n_clicks=0,
                                            style={"flex": "1", "fontSize": "12px"}),
                                html.Button("Clear Filters", id="clear-politician-filters-btn", n_clicks=0,
                                            style={"flex": "1", "fontSize": "12px"}),
                            ],
                        ),
                    ],
                ),
                html.Div(
                    # minWidth 280px, not 0 -- see dcf_chart_column's same
                    # fix: paired with a fixed-width sibling (the filter
                    # panel) and flexWrap on the parent, minWidth:0 here
                    # just let the browser shrink this instead of ever
                    # wrapping it below the filter panel on mobile.
                    style={"flex": "1", "minWidth": "280px"},
                    children=dash_table.DataTable(
                        id="politician-positions-table",
                        columns=POLITICIAN_POSITIONS_COLUMNS,
                        data=[],
                        cell_selectable=False,
                        page_action="none",
                        fixed_rows={"headers": True},
                        sort_action="native",
                        **{**_POLITICIAN_TABLE_STYLE,
                           "style_table": {**_POLITICIAN_TABLE_STYLE["style_table"],
                                            "maxHeight": "600px", "overflowY": "auto"}},
                    ),
                ),
            ],
        ),
        dcc.Store(id="politician-positions-full", data=[]),
    ]


# update_title=None: Dash's default swaps the tab title to "Updating..."
# while any callback is in flight, then back -- with callbacks firing as
# often as they do here (15s price refresh, scroll-driven pagination,
# ...) that made the tab flicker between two titles constantly. Static
# for now, per the user's request.
# suppress_callback_exceptions=True: the Companies/Politicians panels
# are built lazily (see build_company_panel/build_politician_panel
# below) -- their ~80 combined component IDs don't exist in the layout
# Dash validates at startup, only appearing once each panel is first
# visited. Without this, Dash refuses to register any callback that
# references one of those IDs.
# assets_ignore: assets/vendor/plotly-basic-*.min.js is Plotly's own
# "basic" build (scatter/bar/pie -- every trace type this app draws), a
# quarter the size of the full 4.7MB plotly.min.js dcc.Graph would
# otherwise download and evaluate the first time a chart appears (the
# Companies tab) -- several seconds of frozen page on a phone. It's kept
# out of the page's auto-loaded scripts and instead fetched in the
# background once the page is idle (see the end of assets/custom.js),
# so it's usually ready before Companies is ever opened.
_PLOTLY_BASIC_VERSION = "4.1.1"
app = Dash(__name__, update_title=None, suppress_callback_exceptions=True,
           assets_ignore=r"^plotly-basic-.*\.min\.js$")
try:
    from plotly.offline import get_plotlyjs_version
    if get_plotlyjs_version() != _PLOTLY_BASIC_VERSION:
        print(f"WARNING: assets/vendor/plotly-basic-{_PLOTLY_BASIC_VERSION}.min.js doesn't match the installed "
              f"plotly.js {get_plotlyjs_version()} -- replace it with the matching basic bundle "
              f"(https://cdn.plot.ly/plotly-basic-<version>.min.js) and update custom.js's URL.")
except Exception:
    pass
app.title = "Stockpick"
# Plain HTML/CSS overlay, outside Dash's own React tree entirely -- it
# paints as soon as the browser has parsed this far into the page, not
# waiting on the ~3-4s it otherwise takes for the JS bundle to download,
# hydrate, and build all three trackers' worth of layout (during which
# the page LOOKS loaded but doesn't respond to clicks yet). No
# background color and pointer-events:none -- it's just a reassuring
# spinner on top, not a solid cover; the real (still-frozen) page
# underneath stays visible the whole time, and a click reaches it
# immediately once it's actually ready, without waiting on this
# overlay's own timer. A plain inline <script>, not a Dash callback,
# fades it out after a fixed 3s -- if hydration is still running past
# that on a slow connection, the per-panel spinner (search
# "panel-switch-spinner" below) still covers the remainder on whichever
# tab gets clicked first.
app.index_string = """<!DOCTYPE html>
<html>
    <head>
        {%metas%}
        <title>{%title%}</title>
        {%favicon%}
        {%css%}
    </head>
    <body>
        <div id="initial-load-overlay" style="position:fixed;inset:0;z-index:9999;
             display:flex;align-items:center;justify-content:center;
             pointer-events:none;">
            <div class="spinner"></div>
        </div>
        <script>
            setTimeout(function () {
                var el = document.getElementById("initial-load-overlay");
                if (!el) return;
                el.style.transition = "opacity 0.3s ease";
                el.style.opacity = "0";
                setTimeout(function () { el.remove(); }, 300);
            }, 3000);
        </script>
        {%app_entry%}
        <footer>
            {%config%}
            {%scripts%}
            {%renderer%}
        </footer>
    </body>
</html>"""
# gunicorn's entry point in production is "dash_app:server" -- it imports
# this module and serves this Flask app directly, never calling app.run()
# below, so debug mode (and the dev-tools UI it enables) only ever exist
# for local `python dash_app.py` runs, not the hosted deployment.
server = app.server

# Request timing, to see where production time goes (stockpick.io sits
# behind Cloudflare, which blocks automated browsers from measuring it):
#   - every response carries a Server-Timing header with the app's own
#     processing time -- browser DevTools shows it under a request's
#     Timing tab, so a slow request can be split into "in the app" vs.
#     "before it reached the app" (queued for a free worker thread,
#     Cloudflare/network, a worker still starting up);
#   - any request taking over _SLOW_REQUEST_MS is logged to stdout (Render's
#     logs) with which callback it was for, how many requests were in
#     flight at the time, and the worker's memory use.
_SLOW_REQUEST_MS = 1000
_in_flight = 0
_in_flight_lock = threading.Lock()


def _rss_mb():
    """Resident memory of this process in MB (Linux /proc only -- None
    elsewhere, e.g. local Windows runs)."""
    try:
        with open("/proc/self/statm") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 2**20
    except (OSError, ValueError, AttributeError):
        return None


@server.before_request
def _start_request_timer():
    global _in_flight
    flask.g.request_started = time.perf_counter()
    with _in_flight_lock:
        _in_flight += 1
        flask.g.in_flight_at_start = _in_flight


@server.after_request
def _log_slow_requests(response):
    global _in_flight
    started = getattr(flask.g, "request_started", None)
    if started is None:
        return response
    with _in_flight_lock:
        _in_flight -= 1
    elapsed_ms = (time.perf_counter() - started) * 1000
    response.headers["Server-Timing"] = f"app;dur={elapsed_ms:.0f}"
    if elapsed_ms >= _SLOW_REQUEST_MS:
        what = flask.request.path
        if what.endswith("_dash-update-component"):
            body = flask.request.get_json(silent=True) or {}
            what = f"callback {str(body.get('output', '?'))[:120]}"
        rss = _rss_mb()
        print(f"SLOW REQUEST {elapsed_ms:.0f}ms: {what} | in flight at start: "
              f"{flask.g.in_flight_at_start} | rss: {f'{rss:.0f}MB' if rss else 'n/a'}", flush=True)
    return response

# Sidebar replaces the old edge-to-edge top nav bar: a fixed 232px rail
# (logo, search stub, tracker nav, theme toggle) beside a scrolling main
# content column. Each panel's own content wrapper still carries its own
# maxWidth/centering (see _APP_CONTENT_STYLE) since the sidebar itself
# doesn't constrain it.
_APP_CONTENT_STYLE = {"maxWidth": "1400px", "margin": "24px auto 60px", "padding": "0 16px"}

_SIDEBAR_SECTION_LABEL_STYLE = {
    "fontSize": "11px", "letterSpacing": "0.08em", "textTransform": "uppercase",
    "color": "var(--body-text)", "padding": "0 10px 8px",
}
# view id -> (nav row id, label, meta badge). Nav order and initial view
# (_DEFAULT_VIEW) are independent choices; both happen to be Managers.
_SIDEBAR_NAV_ITEMS = [
    ("manager", "nav-managers", "Managers", "13F"),
    ("politician", "nav-congress", "Politicians", "PTR"),
    ("company", "nav-companies", "Companies", ""),
]
_DEFAULT_VIEW = "manager"


def _sidebar_nav_row_style(active):
    return {
        "display": "flex", "alignItems": "center", "gap": "10px",
        "padding": "9px 10px", "borderRadius": "7px", "cursor": "pointer",
        "fontSize": "14px", "fontWeight": "500",
        "backgroundColor": "var(--pill-active)" if active else "transparent",
        "color": "var(--text)" if active else "var(--body-text)",
    }


def _sidebar_nav_dot_style(active):
    return {
        "width": "6px", "height": "6px", "borderRadius": "50%", "flex": "0 0 auto",
        "backgroundColor": "var(--accent)" if active else "var(--border)",
    }


def _build_sidebar():
    nav_rows = [
        html.Div(
            id=row_id,
            n_clicks=0,
            style=_sidebar_nav_row_style(view == _DEFAULT_VIEW),
            children=[
                html.Span(id=f"{row_id}-dot", style=_sidebar_nav_dot_style(view == _DEFAULT_VIEW)),
                html.Span(label, style={"flex": "1"}),
                html.Span(meta, style={"fontFamily": "'IBM Plex Mono', monospace", "fontSize": "11px",
                                        "color": "var(--body-text)"}),
            ],
        )
        for view, row_id, label, meta in _SIDEBAR_NAV_ITEMS
    ]
    return html.Div(
        id="app-sidebar",
        children=[
            html.Div(
                id="sidebar-logo-row",
                style={"display": "flex", "alignItems": "center", "gap": "10px", "padding": "0 8px"},
                children=[
                    html.Div(style={"width": "22px", "height": "22px", "borderRadius": "6px",
                                     "backgroundColor": "var(--accent)"}),
                    html.Div("Stockpick", style={"fontWeight": "700", "fontSize": "17px",
                                                  "letterSpacing": "-0.02em", "color": "var(--text)"}),
                ],
            ),
            html.Div(
                className="global-search-wrap",
                style={"position": "relative"},
                children=[
                    html.Div(
                        style={"display": "flex", "alignItems": "center", "gap": "8px", "padding": "9px 10px",
                               "border": "1px solid var(--border)", "borderRadius": "8px",
                               "backgroundColor": "var(--card-bg)"},
                        children=[
                            dcc.Input(
                                id="global-search-input", type="text",
                                placeholder="Search ticker, fund, member",
                                autoComplete="off", n_submit=0,
                                # minWidth: flex items (this renders as a
                                # flex child via the wrapper div dcc.Input
                                # puts its style on) default to min-width:
                                # auto, which for a text input means it
                                # won't shrink below its own content's
                                # intrinsic width -- without this override
                                # it was blowing out the whole sidebar's
                                # width on narrow (mobile) viewports.
                                style={"flex": "1", "minWidth": "0", "border": "none",
                                       "backgroundColor": "transparent", "padding": "0", "fontSize": "13px",
                                       "color": "var(--text)", "outline": "none"},
                            ),
                            # id'd (not just styled) so the mobile media
                            # query can hide it -- there's no physical
                            # keyboard shortcut to advertise on a phone.
                            html.Span("⌘K", id="global-search-shortcut-hint",
                                      style={"fontFamily": "'IBM Plex Mono', monospace",
                                             "fontSize": "11px", "padding": "2px 5px",
                                             "border": "1px solid var(--border)", "borderRadius": "4px",
                                             "color": "var(--body-text)"}),
                        ],
                    ),
                    html.Div(id="global-search-suggestions",
                             style={"position": "absolute", "top": "100%", "left": "0", "width": "100%",
                                    "zIndex": "30", "marginTop": "4px"}),
                ],
            ),
            dcc.Store(id="global-search-debounced", data=""),
            html.Div(
                id="sidebar-trackers-section",
                style={"display": "flex", "flexDirection": "column", "gap": "2px"},
                children=[
                    html.Div("Trackers", style=_SIDEBAR_SECTION_LABEL_STYLE),
                    # Its own wrapper (not nav_rows flattened directly into
                    # the column above) so mobile can flip just this list
                    # to a horizontal row -- see #sidebar-trackers-list in
                    # custom.css -- without also dragging the "TRACKERS"
                    # label into that row.
                    html.Div(id="sidebar-trackers-list",
                             style={"display": "flex", "flexDirection": "column", "gap": "2px"},
                             children=nav_rows),
                ],
            ),
            html.Div(
                style={"marginTop": "auto", "display": "flex", "flexDirection": "column", "gap": "10px"},
                children=[
                    html.Button(
                        "🌙 Dark", id="theme-toggle-btn", n_clicks=0,
                        style={
                            "backgroundColor": "var(--card-bg)", "color": "var(--text)",
                            "border": "1px solid var(--border)", "borderRadius": "8px",
                            "padding": "8px 12px", "fontSize": "13px", "fontWeight": "600",
                            "cursor": "pointer", "width": "100%",
                        },
                    ),
                    # id'd (not just styled) so the mobile media query can
                    # hide it -- freeing up a bit more of the sidebar's
                    # tight vertical space there; desktop keeps it.
                    html.Div("Data: SEC EDGAR 13F · House & Senate PTRs.",
                             id="sidebar-data-source",
                             style={"fontSize": "11px", "color": "var(--body-text)", "padding": "0 4px",
                                    "lineHeight": "1.5"}),
                ],
            ),
        ],
    )


def _tracker_panel_style(view):
    return {"display": "block" if view == _DEFAULT_VIEW else "none"}


app.layout = html.Div(
    id="app-shell",
    children=[
        # storage_type="local" persists the choice in the browser's own
        # localStorage and re-hydrates it before any Python callback
        # runs -- see the two clientside callbacks right after this
        # layout for how a click updates it and how it's applied.
        dcc.Store(id="theme-store", storage_type="local", data="dark"),
        # storage_type="session": a refresh keeps whichever tracker was
        # open, but a brand-new tab/session lands back on _DEFAULT_VIEW.
        # Nothing reads this store server-side (see the clientside nav
        # callback below) -- it exists purely so other future callbacks
        # have a single source of truth for "which view is active".
        dcc.Store(id="active-view", storage_type="session", data=_DEFAULT_VIEW),
        # Kept in sync with the #app-shell mobile breakpoint (768px, see
        # custom.css) by the clientside callback below -- update_price_chart
        # reads it to turn off the price chart's click-drag zoom-box on
        # mobile, where a touch-drag meant to scrub across the chart (Plotly
        # hover already shows price/volume at the cursor as it moves) kept
        # triggering an accidental zoom instead.
        dcc.Store(id="viewport-is-mobile", data=False),
        # Exists purely as a guaranteed-no-other-writer trigger for the
        # viewport-is-mobile clientside callback below -- active-view
        # itself has an upstream producer (the sidebar nav clientside
        # callback), and chaining a second clientside callback off a prop
        # another one owns turned out not to fire reliably on initial load.
        dcc.Store(id="page-load-trigger", data=True),
        # Global (not inside a lazy panel -- see build_company_panel/
        # build_politician_panel below) so select_global_search_result can
        # always stash a pending pick regardless of whether its target
        # panel has been built yet. apply_pending_company_selection/
        # apply_pending_politician_selection (below) consume these once
        # the panel actually exists -- see those for why a direct write
        # from select_global_search_result alone isn't enough.
        dcc.Store(id="pending-company-selection", data=None),
        # Relocated here from inside _politician_tracker_children() for
        # the same reason -- select_member_from_summary/
        # update_politician_roster's use of this (set by a row click
        # inside an already-open Politicians panel) is unaffected, since
        # the panel necessarily already exists whenever those fire.
        dcc.Store(id="pending-member-selection", data=None),
        # Set by select_global_search_result (a top-search-bar pick) and
        # by select_member_from_summary (an in-panel Recent Trades/
        # Leaderboard row click) -- a plain sidebar nav click never
        # touches this, so the scroll-to-lookup clientside callback below
        # only fires for an actual member/company/manager pick, not every
        # navigation.
        dcc.Store(id="scroll-to-lookup-trigger", data=None),
        html.Div(id="scroll-to-lookup-sink", style={"display": "none"}),
        _build_sidebar(),
        html.Div(
            id="main-content",
            # position:relative anchors #panel-switch-spinner below (no
            # top/left offset of its own, so this doesn't otherwise change
            # main-content's own layout).
            style={"position": "relative"},
            children=[
                # Company/Politician panels start empty -- see
                # build_company_panel/build_politician_panel below, which
                # fill them in the first time each is actually visited,
                # instead of Dash having to mount and diff their entire
                # subtree (thousands of DOM nodes between the two) on
                # every single page load just to keep them ready unseen.
                # Manager is _DEFAULT_VIEW and stays eager, same as today.
                # Starts with a lightweight skeleton (headings + per-section
                # spinners) so opening the tab shows its outline immediately,
                # instead of a full-page overlay, while the real content
                # builds.
                html.Div(id="company-panel", style=_tracker_panel_style("company"),
                         children=_company_panel_skeleton()),
                html.Div(id="manager-panel", style=_tracker_panel_style("manager"),
                          children=html.Div(style=_APP_CONTENT_STYLE, children=_manager_tracker_children())),
                html.Div(id="politician-panel", style=_tracker_panel_style("politician"), children=None),
                # Hidden by default; shown briefly by the sidebar nav
                # clientside callback below the first time you switch to
                # Companies or Politicians in a session -- the very first
                # navigation there can be genuinely slow-to-respond (the
                # whole page's initial layout, all three trackers' worth,
                # is still being constructed in the background right
                # after page load), so this gives visible "something's
                # happening" feedback instead of the click just seeming to
                # do nothing for a moment. Every navigation after the
                # first is instant, same as Managers always is.
                html.Div(
                    id="panel-switch-spinner",
                    style={"display": "none", "position": "absolute", "inset": "0",
                           "backgroundColor": "var(--bg)", "zIndex": "50"},
                    children=html.Div(
                        # inset:0 alone centers within this box's OWN
                        # height, which (it covers the full scrollable
                        # panel, not just one viewport) can be several
                        # times taller than the screen -- landing the
                        # spinner itself well below the fold instead of
                        # visibly on screen. position:fixed centers this
                        # inner wrapper on the actual browser viewport
                        # instead, regardless of scroll position or how
                        # far down the page main-content starts (on
                        # mobile, below the sidebar's nav/search row).
                        # inset (not width:100vw/height:100vh) -- vw units
                        # include the scrollbar's width, which on a page
                        # without one yet can itself trigger a horizontal
                        # scrollbar and overflow.
                        style={"position": "fixed", "inset": "0",
                               "display": "flex", "alignItems": "center", "justifyContent": "center"},
                        children=html.Div(className="spinner"),
                    ),
                ),
            ],
        ),
    ],
)


# Builds the Company/Politician panels' real content the first time each
# is actually visited (see app.layout above) -- State on the panel's own
# current children is the "already built" guard, so this only ever runs
# once per panel per session and leaves whatever's there (including any
# ticker/DCF input the user's since changed) alone on every later visit,
# rather than re-fetching/rebuilding and wiping it out. The panels' own
# structure/ids are unchanged from before this was lazy -- everything
# that already reacted to them (price chart, DCF, search) keeps working
# exactly as it did when they were built eagerly, just later.
#
# Triggered off active-view rather than the sidebar nav button's
# n_clicks: active-view is the one signal every "switch to this view"
# path already sets -- the sidebar nav click, and also
# select_global_search_result jumping here from a global search result
# without ever clicking the nav button itself. Keying off the nav click
# specifically meant a global-search jump to a never-yet-visited panel
# switched the view's visibility but never actually built it, leaving a
# permanently blank panel.
@app.callback(
    Output("company-panel", "children"),
    Input("active-view", "data"),
    State("company-panel", "children"),
    prevent_initial_call=True,
)
def build_company_panel(active_view, existing_children):
    # The panel starts out holding _company_panel_skeleton, which counts as
    # "not built yet" -- only real content already there means skip.
    is_skeleton = isinstance(existing_children, dict) and \
        (existing_children.get("props") or {}).get("id") == _COMPANY_SKELETON_ID
    if active_view != "company" or (existing_children and not is_skeleton):
        raise PreventUpdate
    return html.Div(id="company-content", style=_APP_CONTENT_STYLE, children=_company_tracker_children())


@app.callback(
    Output("politician-panel", "children"),
    Input("active-view", "data"),
    State("politician-panel", "children"),
    State("pending-member-selection", "data"),
    prevent_initial_call=True,
)
def build_politician_panel(active_view, existing_children, pending):
    if active_view != "politician" or existing_children:
        raise PreventUpdate
    return html.Div(style=_APP_CONTENT_STYLE, children=_politician_tracker_children(pending))


# Drives the sidebar nav: which tracker panel is visible and each nav
# row's active styling. Clientside (not a server round-trip) for the same
# reason the theme toggle below is -- instant visual feedback on click.
# Guarded on all three n_clicks being falsy so the initial call Dash
# fires for every clientside callback on page load doesn't fight the
# styles already baked into the layout above (_DEFAULT_VIEW).
app.clientside_callback(
    """
    function(nManagers, nCongress, nCompanies) {
        if (!nManagers && !nCongress && !nCompanies) {
            return Array(10).fill(window.dash_clientside.no_update);
        }
        const trig = window.dash_clientside.callback_context.triggered_id;
        const view = trig === "nav-managers" ? "manager" : trig === "nav-congress" ? "politician" : "company";

        // Manager starts pre-visited (it's the default view, already
        // rendered on page load) -- Companies/Politicians only show this
        // once, the first time you actually switch to each, covering
        // the panel swap below rather than gating it: the swap itself
        // still happens immediately, on the same click, same as always;
        // this is a purely visual overlay for the moment right after
        // that, while build_company_panel/build_politician_panel
        // (dash_app.py) are still building that panel's content for the
        // first time. Hidden by the panel-content-arrived callback right
        // below this one, not a fixed timer -- that panel now starts
        // with no content at all rather than just being slow to paint,
        // so there's no fixed delay that's reliably "long enough."
        window.__visitedViews = window.__visitedViews || new Set(["manager"]);
        const spinner = document.getElementById("panel-switch-spinner");
        // Companies shows its own skeleton (see _company_panel_skeleton)
        // instead of this full-page overlay.
        if (spinner && !window.__visitedViews.has(view) && view !== "company") {
            // "block", not "flex" -- the centering flex properties live
            // on the inner fixed-position wrapper (see dash_app.py
            // layout), not this outer div; making this one a flex
            // container too would turn that wrapper into a shrink-to-fit
            // flex item instead of the full-width block it needs to be
            // to center correctly.
            spinner.style.display = "block";
            // Safety net only -- if build_company_panel/
            // build_politician_panel ever errors out server-side, that
            // panel's children never arrive, and without this the
            // spinner would otherwise sit there forever.
            setTimeout(function () {
                if (spinner.style.display !== "none") spinner.style.display = "none";
            }, 8000);
        }
        window.__visitedViews.add(view);

        const rowStyle = (active) => ({
            display: "flex", alignItems: "center", gap: "10px", padding: "9px 10px",
            borderRadius: "7px", cursor: "pointer", fontSize: "14px", fontWeight: "500",
            backgroundColor: active ? "var(--pill-active)" : "transparent",
            color: active ? "var(--text)" : "var(--body-text)",
        });
        const dotStyle = (active) => ({
            width: "6px", height: "6px", borderRadius: "50%", flex: "0 0 auto",
            backgroundColor: active ? "var(--accent)" : "var(--border)",
        });
        return [
            view,
            {display: view === "company" ? "block" : "none"},
            {display: view === "manager" ? "block" : "none"},
            {display: view === "politician" ? "block" : "none"},
            rowStyle(view === "manager"), dotStyle(view === "manager"),
            rowStyle(view === "politician"), dotStyle(view === "politician"),
            rowStyle(view === "company"), dotStyle(view === "company"),
        ];
    }
    """,
    Output("active-view", "data"),
    Output("company-panel", "style"),
    Output("manager-panel", "style"),
    Output("politician-panel", "style"),
    Output("nav-managers", "style"),
    Output("nav-managers-dot", "style"),
    Output("nav-congress", "style"),
    Output("nav-congress-dot", "style"),
    Output("nav-companies", "style"),
    Output("nav-companies-dot", "style"),
    Input("nav-managers", "n_clicks"),
    Input("nav-congress", "n_clicks"),
    Input("nav-companies", "n_clicks"),
)

# Hides panel-switch-spinner the moment a lazily-built panel's real
# content actually lands, rather than guessing at a fixed delay (see the
# nav callback above). Fires at most once per panel per session --
# build_company_panel/build_politician_panel (dash_app.py) never update
# "children" again after the first time, since their own State guard
# skips rebuilding on every later visit.
app.clientside_callback(
    """
    function(companyChildren, politicianChildren) {
        const spinner = document.getElementById("panel-switch-spinner");
        if (spinner) spinner.style.display = "none";
        return window.dash_clientside.no_update;
    }
    """,
    Output("panel-switch-spinner", "title"),
    Input("company-panel", "children"),
    Input("politician-panel", "children"),
    prevent_initial_call=True,
)

# Scrolls down to the relevant "Look Up a ..." section whenever a stock,
# manager or member is picked from elsewhere -- a top-search-bar result, a
# ticker or manager link on the Managers tab, a mover row, a Recent
# Trades/Leaderboard row -- on desktop and mobile alike (otherwise the page
# lands at the top of the tab, with what was just loaded out of view below
# the Top Buys/movers/Recent Trades cards). scroll-to-lookup-trigger (set by
# select_global_search_result and select_member_from_summary, never by a
# plain sidebar nav click) is also an Input here rather than just a State
# so the two panel-children Inputs' "catch-up" firings -- for a company/
# politician picked before its panel has ever been built -- see its
# current value too; no self-reference risk since this callback's Output
# is a dummy sink, never the trigger store itself (see the "nonexistent
# object" Output-validation notes above on why that pairing is avoided
# elsewhere in this file).
app.clientside_callback(
    """
    function(trigger, _companyChildren, _politicianChildren, isMobile) {
        if (!trigger) return window.dash_clientside.no_update;
        const targetId = {
            // Desktop lands on the section heading; mobile skips straight
            // to the search row below it to save a screen of scrolling.
            company: isMobile ? "ticker-search-row" : "lookup-stock-heading",
            politician: "lookup-member-heading",
            manager: "lookup-manager-heading",
        }[trigger];
        const el = targetId ? document.getElementById(targetId) : null;
        if (!el) return window.dash_clientside.no_update;
        el.scrollIntoView({behavior: "smooth", block: "start"});
        // Content above the target is often still loading at this point
        // (the Top Gainers/Losers lists grow from a spinner to ~480px), which
        // pushed the target back down out of view after the scroll. Keep it
        // aligned while the page settles -- for a few seconds at most, and
        // never once the user scrolls/taps/types themselves.
        let userMoved = false;
        const stop = () => { userMoved = true; };
        ["wheel", "touchstart", "keydown", "mousedown"].forEach(
            (evt) => window.addEventListener(evt, stop, {once: true, passive: true}));
        const watched = document.getElementById("main-content") || document.body;
        const realign = new ResizeObserver(() => {
            if (userMoved) return;
            if (Math.abs(el.getBoundingClientRect().top) > 4) el.scrollIntoView({block: "start"});
        });
        realign.observe(watched);
        setTimeout(() => {
            realign.disconnect();
            ["wheel", "touchstart", "keydown", "mousedown"].forEach((evt) => window.removeEventListener(evt, stop));
        }, 4000);
        return "";
    }
    """,
    Output("scroll-to-lookup-sink", "children"),
    Input("scroll-to-lookup-trigger", "data"),
    Input("company-panel", "children"),
    Input("politician-panel", "children"),
    State("viewport-is-mobile", "data"),
    prevent_initial_call=True,
)

# Keeps viewport-is-mobile in sync with the 768px mobile breakpoint (see
# #app-shell in custom.css) -- sets it once on load and again on every
# resize, so rotating a phone or resizing a browser window across the
# breakpoint is picked up without a refresh.
app.clientside_callback(
    """
    function() {
        function checkMobile() {
            window.dash_clientside.set_props("viewport-is-mobile", {data: window.innerWidth <= 768});
        }
        if (!window.__mobileResizeBound) {
            window.__mobileResizeBound = true;
            window.addEventListener("resize", checkMobile);
        }
        return window.innerWidth <= 768;
    }
    """,
    Output("viewport-is-mobile", "data"),
    Input("page-load-trigger", "data"),
)

# Debounces the sidebar search box the same way manager-search-debounced
# (further below) does for the manager tracker's own search -- waits for a
# pause in typing before hitting the three search functions below, rather
# than on every keystroke.
app.clientside_callback(
    """
    function(value) {
        if (window.__globalSearchTimer) {
            clearTimeout(window.__globalSearchTimer);
        }
        window.__globalSearchTimer = setTimeout(function() {
            window.dash_clientside.set_props("global-search-debounced", {data: value});
        }, 400);
        return window.dash_clientside.no_update;
    }
    """,
    Output("global-search-debounced", "data"),
    Input("global-search-input", "value"),
)

_GLOBAL_SEARCH_SECTION_LABEL_STYLE = {
    "fontSize": "11px", "letterSpacing": "0.06em", "textTransform": "uppercase",
    "color": "var(--body-text)", "padding": "8px 12px 4px",
}


def _global_search_section(label, buttons):
    return [html.Div(label, style=_GLOBAL_SEARCH_SECTION_LABEL_STYLE)] + buttons


@app.callback(
    Output("global-search-suggestions", "children"),
    Input("global-search-debounced", "data"),
    prevent_initial_call=True,
)
def update_global_search_suggestions(query):
    if not query or len(query.strip()) < 2:
        return None

    # Each source fails independently -- a transient SEC hiccup on one
    # shouldn't blank out matches from the other two.
    companies = []
    try:
        companies = search_companies(query, load_ticker_map(_session), limit=5)
    except requests.RequestException:
        pass
    managers = []
    try:
        managers = search_managers(query, _session, limit=5)
    except requests.RequestException:
        pass
    members = _search_all_members(query, limit=5)

    if not companies and not managers and not members:
        return html.Div("No matches.", style={**_SUGGESTIONS_CARD_STYLE, "padding": "10px 12px",
                                                 "fontSize": "13px", "color": "var(--body-text)"})

    sections = []
    if companies:
        _prefetch_fund_names(companies)
        sections += _global_search_section("Companies", [
            html.Button(
                [html.Span(c["ticker"], style={"fontWeight": "700", "marginRight": "8px"}),
                 html.Span(_display_title(c), style={"color": "var(--body-text)"})],
                id={"type": "global-search-company", "ticker": c["ticker"]},
                n_clicks=0, style=_SUGGESTION_ROW_STYLE,
            )
            for c in companies
        ])
    if managers:
        sections += _global_search_section("Managers", [
            html.Button(m["name"], id={"type": "global-search-manager", "name": m["name"]},
                        n_clicks=0, style=_SUGGESTION_ROW_STYLE)
            for m in managers
        ])
    if members:
        sections += _global_search_section("Members of Congress", [
            html.Button(
                f"{c['display']} ({c['state_dst']})" if c["state_dst"] else c["display"],
                id={"type": "global-search-member", "chamber": c["chamber"], "last": c["last"],
                    "first": c["first"]},
                n_clicks=0, style=_SUGGESTION_ROW_STYLE,
            )
            for c in members
        ])
    return html.Div(sections, style=_SUGGESTIONS_CARD_STYLE)


# Jumps to the right tracker and loads the picked result, from anywhere in
# the app. Rather than duplicating each tracker's own lookup/render logic
# here, this leans on the exact mechanism each already listens on:
# company/manager lookups trigger off n_submit (bumped here the same way a
# real Enter keypress would), and the politician dropdown's generate_
# politician already triggers directly off politician-input's value --
# Dash re-fires an Input-owning callback on any change to that prop
# regardless of whether a person or another callback's Output produced it.
@app.callback(
    Output("active-view", "data", allow_duplicate=True),
    Output("company-panel", "style", allow_duplicate=True),
    Output("manager-panel", "style", allow_duplicate=True),
    Output("politician-panel", "style", allow_duplicate=True),
    Output("nav-managers", "style", allow_duplicate=True),
    Output("nav-managers-dot", "style", allow_duplicate=True),
    Output("nav-congress", "style", allow_duplicate=True),
    Output("nav-congress-dot", "style", allow_duplicate=True),
    Output("nav-companies", "style", allow_duplicate=True),
    Output("nav-companies-dot", "style", allow_duplicate=True),
    Output("manager-input", "value", allow_duplicate=True),
    Output("manager-input", "n_submit"),
    Output("pending-member-selection", "data", allow_duplicate=True),
    Output("pending-company-selection", "data", allow_duplicate=True),
    Output("global-search-input", "value", allow_duplicate=True),
    Output("global-search-suggestions", "children", allow_duplicate=True),
    Output("scroll-to-lookup-trigger", "data", allow_duplicate=True),
    Output("all-positions-table", "active_cell", allow_duplicate=True),
    Output("all-positions-table", "selected_cells", allow_duplicate=True),
    Input({"type": "global-search-company", "ticker": ALL}, "n_clicks"),
    Input({"type": "global-search-manager", "name": ALL}, "n_clicks"),
    Input({"type": "global-search-member", "chamber": ALL, "last": ALL, "first": ALL}, "n_clicks"),
    # A clickable security name in Top Increases/Decreases (manager-
    # holding-link) or All Equity Positions (all-positions-table's
    # active_cell) also jumps to the Public Company Tracker -- same
    # "view" dispatch below, just two more ways to reach the "company"
    # branch, both entirely inside the always-eager Manager panel so
    # neither needs the pending-store indirection the other two do.
    Input({"type": "manager-holding-link", "ticker": ALL}, "n_clicks"),
    # Top Buys From Largest Managers' own cards -- a separate pattern
    # (top-buy-link, not manager-holding-link) since that list is cross-
    # manager and always built eagerly; see _build_top_buy_card for why
    # sharing one pattern with the per-manager tables risks a duplicate
    # id if the same ticker ever appears in both at once.
    Input({"type": "top-buy-link", "ticker": ALL, "idx": ALL}, "n_clicks"),
    Input("all-positions-table", "active_cell"),
    State("manager-input", "n_submit"),
    State("all-positions-table", "data"),
    prevent_initial_call=True,
)
def select_global_search_result(company_clicks, manager_clicks, member_clicks, holding_clicks,
                                 top_buy_clicks, active_cell, manager_n_submit, all_positions_data):
    # IMPORTANT: this callback must never declare company-input,
    # politician-chamber-tabs, or politician-input as Outputs, even
    # conditionally returning no_update for them -- all three live
    # inside a lazily-built panel that may not exist yet (see
    # build_company_panel/build_politician_panel), and merely
    # *declaring* an Output to a component that doesn't currently exist
    # silently discards this callback's ENTIRE response, including
    # every OTHER Output (confirmed: active-view/company-panel.style
    # never applied either, every time this was tried, even though
    # neither of those two is ever actually missing). Company/politician
    # selections are routed through the pending-* stores exclusively
    # instead -- apply_pending_company_selection/
    # apply_pending_politician_selection (below) are separate callbacks
    # whose own Inputs (the pending store itself, and the panel's
    # children) mean each of their invocations is evaluated
    # independently: one firing before the panel exists can fail
    # harmlessly without blocking the later one that fires once it does.
    triggered = ctx.triggered_id
    manager_value = manager_submit = no_update
    pending_selection = pending_company_selection = no_update

    if triggered == "all-positions-table":
        if not active_cell or active_cell.get("column_id") != "issuer":
            raise PreventUpdate
        row = (all_positions_data or [])[active_cell["row"]]
        ticker = row.get("resolved_ticker")
        if not ticker:
            raise PreventUpdate
        view = "company"
        pending_company_selection = ticker
    elif isinstance(triggered, dict) and triggered["type"] in ("manager-holding-link", "top-buy-link"):
        clicks = holding_clicks if triggered["type"] == "manager-holding-link" else top_buy_clicks
        if not any(clicks or []):
            raise PreventUpdate  # fires with all-zero clicks whenever either list re-renders
        view = "company"
        pending_company_selection = triggered["ticker"]
    else:
        if not any(company_clicks or []) and not any(manager_clicks or []) and not any(member_clicks or []):
            raise PreventUpdate  # fires with all-zero clicks whenever the suggestion list re-renders
        kind = triggered["type"]
        if kind == "global-search-company":
            view = "company"
            pending_company_selection = triggered["ticker"]
        elif kind == "global-search-manager":
            view = "manager"
            # Manager is _DEFAULT_VIEW and always built eagerly, so no
            # not-built-yet case to guard against -- safe to write directly.
            manager_value = triggered["name"]
            manager_submit = (manager_n_submit or 0) + 1
        else:
            view = "politician"
            chamber = triggered["chamber"]
            pending_selection = f"{chamber}|{triggered['last']}|{triggered['first']}"

    return (
        view,
        {"display": "block" if view == "company" else "none"},
        {"display": "block" if view == "manager" else "none"},
        {"display": "block" if view == "politician" else "none"},
        _sidebar_nav_row_style(view == "manager"), _sidebar_nav_dot_style(view == "manager"),
        _sidebar_nav_row_style(view == "politician"), _sidebar_nav_dot_style(view == "politician"),
        _sidebar_nav_row_style(view == "company"), _sidebar_nav_dot_style(view == "company"),
        manager_value, manager_submit, pending_selection, pending_company_selection, "", None,
        view, None, [],
    )


# Applies a pending company selection (see select_global_search_result
# above) to company-input. Two Inputs, each independently evaluated by
# Dash as its own invocation -- not one callback that has to get the
# timing right:
#   - pending-company-selection changing covers the "Companies panel
#     already existed" case: company-input already exists, this applies
#     immediately.
#   - company-panel.children changing covers the "panel didn't exist
#     yet" case: fires once build_company_panel (above) delivers the
#     panel for the first time, by which point company-input exists too.
# A firing that lands before company-input exists (pending-company-
# selection changing while the panel is still unbuilt) fails harmlessly
# on its own -- it does not block or poison the other, later invocation
# that succeeds once the panel actually exists.
@app.callback(
    Output("company-input", "value", allow_duplicate=True),
    Output("company-input", "n_submit", allow_duplicate=True),
    Input("pending-company-selection", "data"),
    Input("company-panel", "children"),
    State("company-input", "n_submit"),
    prevent_initial_call=True,
)
def apply_pending_company_selection(pending_ticker, _panel_children, current_n_submit):
    if not pending_ticker:
        raise PreventUpdate
    return pending_ticker, (current_n_submit or 0) + 1


# Same two-Input/independent-invocation reasoning as
# apply_pending_company_selection above, for politician-chamber-tabs/
# politician-input -- now the sole place that applies a pending
# politician selection (select_global_search_result no longer writes to
# either directly at all, for the same reason it no longer does for
# company-input).
#
# Does NOT also clear pending-member-selection itself in the same-chamber
# branch, unlike an earlier version of this callback -- Output and Input
# both on pending-member-selection.data, on the very same callback, was
# itself enough to trigger the "nonexistent object" failure on this
# callback's OTHER Outputs (politician-chamber-tabs/politician-input),
# confirmed by removing just that one Output and seeing the error
# disappear entirely. clear_pending_politician_selection below clears it
# instead, from a separate callback with no such self-reference.
@app.callback(
    Output("politician-chamber-tabs", "value", allow_duplicate=True),
    Output("politician-input", "value", allow_duplicate=True),
    Input("pending-member-selection", "data"),
    Input("politician-panel", "children"),
    State("politician-chamber-tabs", "value"),
    prevent_initial_call=True,
)
def apply_pending_politician_selection(pending, _panel_children, current_chamber):
    if not pending:
        raise PreventUpdate
    chamber = pending.split("|", 1)[0]
    if chamber == current_chamber:
        return no_update, pending
    return chamber, no_update


# Clears pending-member-selection once it's actually been consumed --
# split out from apply_pending_politician_selection above specifically
# to avoid that self-reference (see its comment). politician-input only
# gets set, from anywhere in the app, as a *result* of a pending
# selection being applied (by this callback's sibling above, or by
# update_politician_roster, both for the already-open-panel case and
# this one together covering every path) -- so reacting to it changing
# is a reliable "a selection was just consumed" signal, not just a
# proxy for it; a plain, direct user pick from the dropdown re-fires
# the same lookup regardless of whether this also clears an
# already-empty store.
@app.callback(
    Output("pending-member-selection", "data", allow_duplicate=True),
    Input("politician-input", "value"),
    prevent_initial_call=True,
)
def clear_pending_politician_selection(_value):
    return None


# Flips theme-store's persisted value on a click. Guarded on n_clicks so
# the initial call Dash fires for every clientside callback on page load
# doesn't itself toggle away from whatever was just read out of
# localStorage before the visitor has clicked anything.
app.clientside_callback(
    """
    function(n_clicks, current) {
        if (!n_clicks) {
            return window.dash_clientside.no_update;
        }
        return current === "light" ? "dark" : "light";
    }
    """,
    Output("theme-store", "data"),
    Input("theme-toggle-btn", "n_clicks"),
    State("theme-store", "data"),
)

# Applies theme-store's value -- data-theme on <html>, which every
# var(--...)-based Python style in this file follows live via the CSS
# variables in assets/custom.css -- and updates the toggle's own label.
# Fires on theme-store itself rather than the button, so a persisted
# "light" choice from a previous visit takes effect on page load too,
# not just after a click.
app.clientside_callback(
    """
    function(theme) {
        var t = theme || "dark";
        document.documentElement.setAttribute("data-theme", t);
        return t === "light" ? "☀️ Light" : "🌙 Dark";
    }
    """,
    Output("theme-toggle-btn", "children"),
    Input("theme-store", "data"),
)


def _tab_visibility_styles(mode):
    """(growth, income, balance, cashflow, holdings, dcf-wrap, financials-
    header, view-tabs parent, no-financials-msg) styles.
    "operating": financials tabs + DCF, for a normal 10-K filer.
    "fund": Top Holdings only -- a fund has no operating cash flows of
    its own to project, so the DCF valuation panel is hidden outright
    rather than just left showing meaningless (all-None) inputs.
    "price_only": neither -- a filer with zero us-gaap XBRL facts (e.g.
    a foreign private issuer on Form 20-F, see NoXbrlFactsError) has
    nothing to drive the financials tables or the DCF with, but its
    price history is completely independent of SEC XBRL and still
    shows (see _run_company_lookup). The Financials header/tab bar are
    hidden outright here too (not just each individual tab, which alone
    still left the Growth Rates tab's own empty table on screen with no
    explanation) in favor of no-financials-msg's plain text."""
    financials_style = _NAV_TAB_STYLE if mode == "operating" else _NAV_TAB_HIDDEN_STYLE
    holdings_style = _NAV_TAB_STYLE if mode == "fund" else _NAV_TAB_HIDDEN_STYLE
    dcf_wrap_style = {"display": "block"} if mode == "operating" else {"display": "none"}
    header_style = _FINANCIALS_HEADER_HIDDEN_STYLE if mode == "price_only" else _FINANCIALS_HEADER_STYLE
    # parent_style, not style: dcc.Tabs' own style only covers the tab bar,
    # while the selected tab's content (the Growth Rates table) renders in
    # a separate wrapper -- hiding just the bar left that empty table on
    # screen. parent_style wraps both.
    tabs_style = {"display": "none"} if mode == "price_only" else {}
    no_financials_style = _NO_FINANCIALS_MSG_VISIBLE_STYLE if mode == "price_only" else _NO_FINANCIALS_MSG_STYLE
    return (financials_style, financials_style, financials_style, financials_style,
            holdings_style, dcf_wrap_style, header_style, tabs_style, no_financials_style)


def _empty_company_outputs(status, view_tab="growth", mode="operating"):
    return ([], status, None, True, [], EMPTY_COLS, [], EMPTY_COLS, [], EMPTY_COLS,
            [], HOLDINGS_COLUMNS, view_tab, *_tab_visibility_styles(mode), None)


def _line_value(line_items, label, periods):
    """Most recent non-null value for `label` across `periods` (which are
    in chronological order, so scanning in reverse prefers a trailing
    MRQ/TTM column over the last full fiscal year when both exist)."""
    row = dict(line_items).get(label, {})
    for period in reversed(periods):
        v = row.get(period)
        if v is not None:
            return v
    return None


def _dcf_defaults(ticker, title, rows, is_periods, is_line_items, bs_periods, bs_line_items,
                   cf_periods, cf_line_items):
    """Best-effort starting point for the DCF inputs, derived from data
    already fetched for the financial statements above (no extra SEC
    calls) plus one lightweight price lookup. Any piece that isn't
    determinable is left None -- _run_dcf treats missing required inputs
    as "not ready yet" rather than guessing, but Net Debt/shares/price are
    fine to leave blank for the user to fill in.

    Also carries 'ticker'/'title' (for the summary banner) and
    'historical'/'base_revenue' (for the Revenue-vs-FCF chart) -- these
    aren't user-editable DCF inputs, so they don't correspond to any
    _DCF_INPUT_FIELDS entry, but ride along in the same defaults payload
    since they're derived from the exact same fetch.
    """
    cf_by_label = dict(cf_line_items)
    fcf_row = cf_by_label.get("Free Cash Flow", {})
    historical = [
        {
            "year": r["end"][:4],
            "revenue_m": r.get("revenue_m"),
            "fcf_m": (fcf_row[r["end"]] / 1e6) if fcf_row.get(r["end"]) is not None else None,
        }
        for r in rows
    ]
    base_revenue_m = rows[-1]["revenue_m"] if rows else None

    cash = _line_value(bs_line_items, "Cash & Equivalents", bs_periods)
    debt = _line_value(bs_line_items, "Total Debt", bs_periods)
    net_debt_m = None
    if cash is not None or debt is not None:
        net_debt_m = ((debt or 0) - (cash or 0)) / 1e6

    fcf = _line_value(cf_line_items, "Free Cash Flow", cf_periods)
    base_fcf_m = (fcf / 1e6) if fcf is not None else None

    # Net Income / Diluted EPS (same period for both, not independently
    # "most recent") backs out a diluted weighted-average share count --
    # no share-count XBRL tag is fetched anywhere else in this app.
    shares_out_m = None
    ni_row = dict(is_line_items).get("Net Income", {})
    eps_row = dict(is_line_items).get("Diluted EPS", {})
    for period in reversed(is_periods):
        ni, eps = ni_row.get(period), eps_row.get(period)
        if ni is not None and eps:
            shares_out_m = (ni / eps) / 1e6
            break

    # r["revenue_growth"] is a raw fraction (0.10 for 10%), like every
    # other growth figure in `rows` -- see _pct, which is what multiplies
    # by 100 for display in the Growth Rates table.
    growth_vals = [r["revenue_growth"] * 100 for r in rows if r.get("revenue_growth") is not None]
    growth_rate = round(sum(growth_vals) / len(growth_vals), 1) if growth_vals else 8.0
    growth_rate = max(0.0, min(30.0, growth_rate))

    current_price = None
    try:
        price_df = fetch_price_history(ticker, "5D")
        if not price_df.empty:
            current_price = float(price_df["Close"].iloc[-1])
    except Exception:
        pass

    return {
        "base_fcf": round(base_fcf_m, 1) if base_fcf_m is not None else None,
        "growth_rate": growth_rate,
        "terminal_growth": 2.5,
        "discount_rate": 9.0,
        "years": 5,
        "net_debt": round(net_debt_m, 1) if net_debt_m is not None else None,
        "shares_out": round(shares_out_m, 1) if shares_out_m is not None else None,
        "current_price": round(current_price, 2) if current_price is not None else None,
        "ticker": ticker,
        "title": title,
        "historical": historical,
        "base_revenue": base_revenue_m,
    }


def _run_company_lookup(query, years):
    """Fetch growth rows + all three statements for one company query.
    Returns (outputs, candidates): outputs matches the generate callback's
    Outputs (in order); candidates is None unless `query` was ambiguous, in
    which case it's the list of SEC ticker-map dicts to choose from."""
    if not query or not query.strip():
        return _empty_company_outputs("Enter a ticker or company name."), None

    # ETFs/mutual funds are registered investment companies, not operating
    # companies -- they don't file 10-Ks/XBRL facts, so fetch_growth_data
    # below would just fail company-by-company. Resolve the query once up
    # front so funds can be routed to the holdings lookup instead.
    try:
        company = resolve_company(query, load_ticker_map(_session))
    except CompanyLookupError as e:
        return _empty_company_outputs(str(e)), (e.candidates or None)
    except requests.RequestException as e:
        return _empty_company_outputs(f"Network error talking to SEC EDGAR: {e}"), None
    if company.get("is_fund"):
        return _run_etf_lookup(company["ticker"]), None

    try:
        rows, title, ticker, debt_warning = fetch_growth_data(
            query, years=years, session=_session,
        )
    except CompanyLookupError as e:
        return _empty_company_outputs(str(e)), (e.candidates or None)
    except CompanyDataError as e:
        # No XBRL facts at all can also mean an exchange-traded trust that
        # SEC's ticker map lists as an ordinary company (SPY, DIA, ...)
        # rather than a fund, so it never got is_fund above -- if Yahoo has
        # a holdings breakdown for it, show the same fund view QQQ/VOO get.
        # A commodity trust like GLD, or a foreign filer like TSM, has none
        # and falls through to price-only below.
        if isinstance(e, NoXbrlFactsError):
            try:
                holdings_df = fetch_top_holdings(company["ticker"])
            except HoldingsDataError:
                holdings_df = None
            if holdings_df is not None:
                return _run_etf_lookup(company["ticker"], title=company["title"], holdings_df=holdings_df), None
        # Whatever the specific reason (no us-gaap facts at all --
        # NoXbrlFactsError, typically a foreign private issuer filing
        # Form 20-F under IFRS like TSM/ASML -- or some us-gaap facts
        # but not enough to build financials from, e.g. a recent IPO
        # without a full fiscal year filed yet), `company` was already
        # resolved above, so its ticker/title are good regardless --
        # price history (stock_price.fetch_price_history) has nothing
        # to do with SEC XBRL, so it still shows even though the
        # financials tables and DCF (both XBRL-driven) can't.
        ticker, title = company["ticker"], company["title"]
        # No status text -- the page header already names the company,
        # and no-financials-msg (see _tab_visibility_styles' "price_only"
        # mode) already explains the missing financials.
        status = ""
        store = {"rows": [], "title": title, "ticker": ticker}
        return ([], status, store, True, [], EMPTY_COLS, [], EMPTY_COLS, [], EMPTY_COLS,
                [], HOLDINGS_COLUMNS, "growth", *_tab_visibility_styles("price_only"), None), None
    except requests.RequestException as e:
        return _empty_company_outputs(f"Network error talking to SEC EDGAR: {e}"), None

    # No "Found: ..." line -- the page header already names the company,
    # so status-msg only carries the debt warning (if any).
    status = debt_warning or ""

    # Resolved to a single exact ticker above, so reuse it here (skips
    # re-doing name resolution and guarantees all three statements match
    # the same company that produced `rows`). Raw periods/line_items are
    # kept (not just the display-formatted records) so _dcf_defaults can
    # read actual numbers out of them below.
    is_data, is_columns = [], EMPTY_COLS
    is_periods, is_line_items = [], []
    try:
        is_periods, is_line_items, _, _ = fetch_income_statement_data(
            ticker, years=years, session=_session,
        )
        is_data = financial_table_records(is_periods, is_line_items)
        is_columns = financial_table_columns(is_periods)
    except (CompanyLookupError, CompanyDataError, requests.RequestException) as e:
        status += f"\nIncome statement unavailable: {e}"

    bs_data, bs_columns = [], EMPTY_COLS
    bs_periods, bs_line_items = [], []
    try:
        bs_periods, bs_line_items, mrq_period, _, _ = fetch_balance_sheet_data(
            ticker, years=years, session=_session,
        )
        bs_data = financial_table_records(bs_periods, bs_line_items)
        bs_columns = financial_table_columns(bs_periods, mrq_period)
    except (CompanyLookupError, CompanyDataError, requests.RequestException) as e:
        status += f"\nBalance sheet unavailable: {e}"

    cf_data, cf_columns = [], EMPTY_COLS
    cf_periods, cf_line_items = [], []
    try:
        cf_periods, cf_line_items, _, _ = fetch_cash_flow_data(
            ticker, years=years, session=_session,
        )
        cf_data = financial_table_records(cf_periods, cf_line_items)
        cf_columns = financial_table_columns(cf_periods)
    except (CompanyLookupError, CompanyDataError, requests.RequestException) as e:
        status += f"\nCash flow statement unavailable: {e}"

    dcf_defaults = _dcf_defaults(ticker, title, rows, is_periods, is_line_items,
                                  bs_periods, bs_line_items, cf_periods, cf_line_items)

    store = {"rows": rows, "title": title, "ticker": ticker}
    outputs = (rows_to_display_records(rows), status, store, False,
               is_data, is_columns, bs_data, bs_columns, cf_data, cf_columns,
               [], HOLDINGS_COLUMNS, "growth", *_tab_visibility_styles("operating"), dcf_defaults)
    return outputs, None


def _run_etf_lookup(ticker, title=None, holdings_df=None):
    """ETFs/mutual funds don't have 10-K financials or a DCF to compute, so
    this skips the SEC pipeline entirely and shows top holdings instead.
    `title` (the page header name) defaults to the ticker -- SEC's fund
    map has no names; `holdings_df` skips the fetch if the caller already
    has it."""
    if holdings_df is None:
        try:
            holdings_df = fetch_top_holdings(ticker)
        except HoldingsDataError as e:
            return _empty_company_outputs(f"{ticker}: {e}", mode="fund")

    status = ("ETF/Fund: financials and DCF valuation aren't available for funds (no 10-K/XBRL data); "
              "showing top holdings instead.")
    holdings_data = [
        {"symbol": r.symbol, "name": r.name, "holding_pct": round(r.holding_pct, 2)}
        for r in holdings_df.itertuples()
    ]
    store = {"rows": [], "title": title or ticker, "ticker": ticker}
    return (
        # download-btn.disabled=False: the holdings table alone is enough
        # for the Excel download (see the download callback below).
        [], status, store, False,
        [], EMPTY_COLS, [], EMPTY_COLS, [], EMPTY_COLS,
        holdings_data, HOLDINGS_COLUMNS, "holdings", *_tab_visibility_styles("fund"), None,
    )


# Process-lifetime, not TTL'd like _ticker_cache -- a fund's name doesn't
# change day to day the way its price/holdings do, so there's no need to
# ever refetch one once we have it.
_fund_name_cache = {}


def _prefetch_fund_names(companies):
    """Warms _fund_name_cache for any not-yet-cached funds in `companies`,
    in parallel. A search full of funds calling fetch_fund_name one at a
    time inside the results list comprehension (see _display_title) was
    visibly slow -- several seconds -- since each is its own yfinance
    round trip; concurrent lookups cut that down to roughly the slowest
    single one instead of their sum. Called once up front, before building
    each suggestion/candidate list, so _display_title's own per-ticker
    calls below are then all cache hits."""
    missing = [c["ticker"] for c in companies if c.get("is_fund") and c["ticker"] not in _fund_name_cache]
    if not missing:
        return
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(missing))) as pool:
        for ticker, name in zip(missing, pool.map(fetch_fund_name, missing)):
            _fund_name_cache[ticker] = name


def _display_title(c):
    """A ticker-map entry's real display title. For an operating company
    that's just its own "title" field; for an ETF/mutual fund, "title" is
    only ever the ticker symbol again (SEC's fund ticker map carries no
    name at all -- see load_ticker_map), so this fetches the real fund
    name via yfinance instead, on first use per ticker, falling back to a
    generic label if yfinance doesn't have one either."""
    if not c.get("is_fund"):
        return c["title"]
    ticker = c["ticker"]
    if ticker not in _fund_name_cache:
        _fund_name_cache[ticker] = fetch_fund_name(ticker)
    return _fund_name_cache[ticker] or "ETF / Fund"


def _render_company_candidates(candidates, query, candidate_type="company-candidate"):
    if not candidates:
        return None
    shown = candidates[:_MAX_CANDIDATES_SHOWN]
    _prefetch_fund_names(shown)
    buttons = [
        html.Button(
            f"{c['ticker']}  {_display_title(c)}",
            id={"type": candidate_type, "ticker": c["ticker"]},
            n_clicks=0,
            style=_CANDIDATE_BTN_STYLE,
        )
        for c in shown
    ]
    note = []
    if len(candidates) > _MAX_CANDIDATES_SHOWN:
        note = [html.P(f"Showing the first {_MAX_CANDIDATES_SHOWN} of {len(candidates)} matches — "
                        "refine your search to narrow it down.",
                        style={"color": "var(--body-text)", "fontSize": "12px"})]
    return html.Div(style={"maxWidth": "520px", "marginTop": "8px"}, children=buttons + note)


@app.callback(
    Output("compare-mode", "data"),
    Input("compare-mode-tabs", "value"),
)
def sync_compare_mode(tab_value):
    # Skips the no-op initial call when the Companies panel mounts (store
    # already False) -- every callback response costs a full dash-renderer
    # pass over the page, which on a phone CPU is a few hundred ms each.
    if ctx.triggered_id is None and tab_value != "compare":
        raise PreventUpdate
    return tab_value == "compare"


@app.callback(
    Output("compare-ticker-wrap", "style"),
    Output("compare-status-wrap", "style"),
    Output("stock-kpi-wrap-2", "style"),
    Output("financials-col-2", "style"),
    Output("valuation-col-2", "style"),
    Output("dcf-company-label", "style"),
    Input("compare-mode", "data"),
)
def render_compare_mode(is_compare):
    if ctx.triggered_id is None and not is_compare:
        raise PreventUpdate  # layout already starts with every compare piece hidden
    ticker_wrap_style = {"position": "relative"} if is_compare else {"position": "relative", "display": "none"}
    status_wrap_style = {} if is_compare else {"display": "none"}
    kpi_wrap_2_style = {"marginTop": "16px"} if is_compare else {"marginTop": "16px", "display": "none"}
    col2_style = {"flex": "1", "minWidth": "0"} if is_compare else {"flex": "1", "minWidth": "0", "display": "none"}
    valuation_2_style = {"marginTop": "40px"} if is_compare else {"marginTop": "40px", "display": "none"}
    # Per-ticker labels over the stacked Valuation panels only matter once
    # there are two of them.
    label_style = _DCF_COMPANY_LABEL_STYLE if is_compare else _DCF_COMPANY_LABEL_HIDDEN_STYLE
    return ticker_wrap_style, status_wrap_style, kpi_wrap_2_style, col2_style, valuation_2_style, label_style


# Builds Compare mode's second Financials column and Valuation panel the
# first time Compare mode turns on, then leaves them in place (hidden by
# render_compare_mode when it's off) so a second ticker's data survives
# toggling back and forth.
@app.callback(
    Output("financials-col-2", "children"),
    Output("valuation-col-2", "children"),
    Input("compare-mode", "data"),
    State("financials-col-2", "children"),
)
def build_compare_panels(is_compare, existing_children):
    if not is_compare or existing_children:
        raise PreventUpdate
    return _financials_valuation_blocks("-2")


# Section spinners (see _section_loading_overlay): shown the moment a new
# stock is picked (ticker-store changes), hidden when that section's own
# data lands. Driven by the lookup itself -- not by Dash's generic loading
# state -- so the price chart's 15-second refresh never flashes them.
#
# Show and hide are separate callbacks on purpose: Dash holds back a
# callback while any of its Inputs is still being produced by an in-flight
# callback, so one callback listening to both ticker-store and, say,
# price-chart.figure (which ticker-store itself triggers) would only ever
# run once the chart had already arrived -- never showing the spinner.
_SECTION_LOADING_IDS = ("price-loading", "kpi-loading", "financials-loading")
app.clientside_callback(
    """
    function(store, rowsStore) {
        if (!store) return Array(3).fill(window.dash_clientside.no_update);
        const show = {display: "flex"}, hide = {display: "none"};
        // A 20s fallback hides them regardless, so an error can't leave
        // one stuck.
        clearTimeout(window.__sectionLoadingTimer);
        window.__sectionLoadingTimer = setTimeout(() => {
            ["price-loading", "kpi-loading", "financials-loading"].forEach(
                (id) => window.dash_clientside.set_props(id, {style: hide}));
        }, 20000);
        // The SEC lookup can finish before this fast ticker resolve does --
        // if the Financials data is already in for this ticker, leave it.
        const financialsLoaded = rowsStore && rowsStore.ticker === store.ticker;
        return [show, show, financialsLoaded ? hide : show];
    }
    """,
    *[Output(_id, "style") for _id in _SECTION_LOADING_IDS],
    Input("ticker-store", "data"),
    State("rows-store", "data"),
    prevent_initial_call=True,
)
for _overlay_id, _done_input in (("price-loading", Input("price-chart", "figure")),
                                 ("kpi-loading", Input("stock-kpi-grid", "children")),
                                 ("financials-loading", Input("results-table", "data"))):
    app.clientside_callback(
        """function(_done) { return {display: "none"}; }""",
        Output(_overlay_id, "style", allow_duplicate=True),
        _done_input,
        prevent_initial_call=True,
    )


def _fast_ticker_store(query):
    """{"ticker", "title"} for `query` from the in-memory ticker map alone
    (no SEC/Yahoo round trip), or None if it doesn't resolve to exactly
    one company -- the SEC lookup alongside it handles the not-found and
    ambiguous (candidate list) cases."""
    if not query or not str(query).strip():
        return None
    try:
        company = resolve_company(query, load_ticker_map(_session))
    except (CompanyLookupError, requests.RequestException):
        return None
    return {"ticker": company["ticker"], "title": company["title"]}


def _fast_lookup_query(triggered, n_submit_query, candidate_clicks, suggestion_clicks, active_cell,
                       holdings_data, holdings_table_id):
    """The query a lookup was just started with, from whichever of the SEC
    lookup callbacks' own triggers fired -- mirrors generate/
    select_company_candidate (and their _2 twins) exactly, so this runs in
    parallel with them on the very same click/Enter."""
    if triggered == holdings_table_id:
        if not active_cell or active_cell.get("column_id") != "symbol" or not holdings_data:
            return None
        return holdings_data[active_cell["row"]]["symbol"]
    if isinstance(triggered, dict):
        if not any(candidate_clicks or []) and not any(suggestion_clicks or []):
            return None
        return triggered["ticker"]
    return n_submit_query


# Fast half of a company lookup: resolves the ticker from the in-memory
# ticker map and sets ticker-store immediately, so the price chart, KPI
# tiles and earnings start right away -- in parallel with generate/
# select_company_candidate's SEC lookup (which fills rows-store, the
# financials and the DCF) rather than waiting for it to finish first.
@app.callback(
    Output("ticker-store", "data"),
    Input("company-input", "n_submit"),
    Input({"type": "company-candidate", "ticker": ALL}, "n_clicks"),
    Input({"type": "company-suggestion", "ticker": ALL}, "n_clicks"),
    Input("holdings-table", "active_cell"),
    State("company-input", "value"),
    State("holdings-table", "data"),
)
def resolve_ticker_fast(_n_submit, candidate_clicks, suggestion_clicks, active_cell, query, holdings_data):
    query = _fast_lookup_query(ctx.triggered_id, query, candidate_clicks, suggestion_clicks, active_cell,
                               holdings_data, "holdings-table")
    store = _fast_ticker_store(query)
    if store is None:
        raise PreventUpdate
    return store


@app.callback(
    Output("ticker-store-2", "data"),
    Input("company-input-2", "n_submit"),
    Input({"type": "company-candidate-2", "ticker": ALL}, "n_clicks"),
    Input({"type": "company-suggestion-2", "ticker": ALL}, "n_clicks"),
    Input("holdings-table-2", "active_cell"),
    State("company-input-2", "value"),
    State("holdings-table-2", "data"),
    prevent_initial_call=True,
)
def resolve_ticker_fast_2(_n_submit, candidate_clicks, suggestion_clicks, active_cell, query, holdings_data):
    query = _fast_lookup_query(ctx.triggered_id, query, candidate_clicks, suggestion_clicks, active_cell,
                               holdings_data, "holdings-table-2")
    store = _fast_ticker_store(query)
    if store is None:
        raise PreventUpdate
    return store


@app.callback(
    Output("results-table", "data"),
    Output("status-msg", "children"),
    Output("rows-store", "data"),
    Output("download-btn", "disabled"),
    Output("income-statement-table", "data"),
    Output("income-statement-table", "columns"),
    Output("balance-sheet-table", "data"),
    Output("balance-sheet-table", "columns"),
    Output("cash-flow-table", "data"),
    Output("cash-flow-table", "columns"),
    Output("holdings-table", "data"),
    Output("holdings-table", "columns"),
    Output("view-tabs", "value"),
    Output("growth-tab", "style"),
    Output("income-tab", "style"),
    Output("balance-tab", "style"),
    Output("cashflow-tab", "style"),
    Output("holdings-tab", "style"),
    Output("dcf-wrap", "style"),
    Output("financials-header", "style"),
    Output("view-tabs", "parent_style"),
    Output("no-financials-msg", "style"),
    Output("company-candidates", "children"),
    Output("company-suggestions", "children"),
    Output("dcf-defaults-store", "data"),
    Input("company-input", "n_submit"),
    State("company-input", "value"),
)
def generate(_n_submit, company):
    outputs, candidates = _run_company_lookup(company, _FINANCIALS_YEARS)
    *main_outputs, dcf_defaults = outputs
    return (*main_outputs, _render_company_candidates(candidates, company), None, dcf_defaults)


@app.callback(
    Output("results-table-2", "data"),
    Output("status-msg-2", "children"),
    Output("rows-store-2", "data"),
    Output("download-btn-2", "disabled"),
    Output("income-statement-table-2", "data"),
    Output("income-statement-table-2", "columns"),
    Output("balance-sheet-table-2", "data"),
    Output("balance-sheet-table-2", "columns"),
    Output("cash-flow-table-2", "data"),
    Output("cash-flow-table-2", "columns"),
    Output("holdings-table-2", "data"),
    Output("holdings-table-2", "columns"),
    Output("view-tabs-2", "value"),
    Output("growth-tab-2", "style"),
    Output("income-tab-2", "style"),
    Output("balance-tab-2", "style"),
    Output("cashflow-tab-2", "style"),
    Output("holdings-tab-2", "style"),
    Output("dcf-wrap-2", "style"),
    Output("financials-header-2", "style"),
    Output("view-tabs-2", "parent_style"),
    Output("no-financials-msg-2", "style"),
    Output("company-candidates-2", "children"),
    Output("company-suggestions-2", "children", allow_duplicate=True),
    Output("dcf-defaults-store-2", "data"),
    Input("company-input-2", "n_submit"),
    State("company-input-2", "value"),
    prevent_initial_call=True,
)
def generate_2(_n_submit, company):
    # Compare panel's ticker starts empty (no default), unlike the primary
    # one -- nothing to load until the user actually searches.
    outputs, candidates = _run_company_lookup(company, _FINANCIALS_YEARS)
    *main_outputs, dcf_defaults = outputs
    return (*main_outputs, _render_company_candidates(candidates, company, "company-candidate-2"),
            None, dcf_defaults)


@app.callback(
    Output("results-table", "data", allow_duplicate=True),
    Output("status-msg", "children", allow_duplicate=True),
    Output("rows-store", "data", allow_duplicate=True),
    Output("download-btn", "disabled", allow_duplicate=True),
    Output("income-statement-table", "data", allow_duplicate=True),
    Output("income-statement-table", "columns", allow_duplicate=True),
    Output("balance-sheet-table", "data", allow_duplicate=True),
    Output("balance-sheet-table", "columns", allow_duplicate=True),
    Output("cash-flow-table", "data", allow_duplicate=True),
    Output("cash-flow-table", "columns", allow_duplicate=True),
    Output("holdings-table", "data", allow_duplicate=True),
    Output("holdings-table", "columns", allow_duplicate=True),
    Output("view-tabs", "value", allow_duplicate=True),
    Output("growth-tab", "style", allow_duplicate=True),
    Output("income-tab", "style", allow_duplicate=True),
    Output("balance-tab", "style", allow_duplicate=True),
    Output("cashflow-tab", "style", allow_duplicate=True),
    Output("holdings-tab", "style", allow_duplicate=True),
    Output("dcf-wrap", "style", allow_duplicate=True),
    Output("financials-header", "style", allow_duplicate=True),
    Output("view-tabs", "parent_style", allow_duplicate=True),
    Output("no-financials-msg", "style", allow_duplicate=True),
    Output("company-candidates", "children", allow_duplicate=True),
    Output("company-suggestions", "children", allow_duplicate=True),
    Output("company-input", "value", allow_duplicate=True),
    Output("suppress-next-suggestions", "data", allow_duplicate=True),
    Output("dcf-defaults-store", "data", allow_duplicate=True),
    Output("holdings-table", "active_cell", allow_duplicate=True),
    Output("holdings-table", "selected_cells", allow_duplicate=True),
    Input({"type": "company-candidate", "ticker": ALL}, "n_clicks"),
    Input({"type": "company-suggestion", "ticker": ALL}, "n_clicks"),
    Input("holdings-table", "active_cell"),
    State("holdings-table", "data"),
    prevent_initial_call=True,
)
def select_company_candidate(candidate_clicks, suggestion_clicks, active_cell, holdings_data):
    triggered = ctx.triggered_id
    if triggered == "holdings-table":
        # Only a click on the Symbol cell (not Name/% of Fund) jumps to that
        # holding's ticker.
        if not active_cell or active_cell.get("column_id") != "symbol":
            raise PreventUpdate
        ticker = holdings_data[active_cell["row"]]["symbol"]
    else:
        if not any(candidate_clicks) and not any(suggestion_clicks):
            raise PreventUpdate  # fires with all-zero clicks whenever the button list re-renders
        ticker = triggered["ticker"]
    outputs, _candidates = _run_company_lookup(ticker, _FINANCIALS_YEARS)
    *main_outputs, dcf_defaults = outputs
    # Setting company-input's value below re-triggers update_company_suggestions
    # (it watches that same value) — this flag tells that callback to skip
    # showing a dropdown for this one programmatic change, not real typing.
    # active_cell/selected_cells are cleared too, so a stale highlight from
    # this click (or a holdings-table click) doesn't stick around on
    # whatever loads next.
    return (*main_outputs, None, None, ticker, True, dcf_defaults, None, [])


@app.callback(
    Output("results-table-2", "data", allow_duplicate=True),
    Output("status-msg-2", "children", allow_duplicate=True),
    Output("rows-store-2", "data", allow_duplicate=True),
    Output("download-btn-2", "disabled", allow_duplicate=True),
    Output("income-statement-table-2", "data", allow_duplicate=True),
    Output("income-statement-table-2", "columns", allow_duplicate=True),
    Output("balance-sheet-table-2", "data", allow_duplicate=True),
    Output("balance-sheet-table-2", "columns", allow_duplicate=True),
    Output("cash-flow-table-2", "data", allow_duplicate=True),
    Output("cash-flow-table-2", "columns", allow_duplicate=True),
    Output("holdings-table-2", "data", allow_duplicate=True),
    Output("holdings-table-2", "columns", allow_duplicate=True),
    Output("view-tabs-2", "value", allow_duplicate=True),
    Output("growth-tab-2", "style", allow_duplicate=True),
    Output("income-tab-2", "style", allow_duplicate=True),
    Output("balance-tab-2", "style", allow_duplicate=True),
    Output("cashflow-tab-2", "style", allow_duplicate=True),
    Output("holdings-tab-2", "style", allow_duplicate=True),
    Output("dcf-wrap-2", "style", allow_duplicate=True),
    Output("financials-header-2", "style", allow_duplicate=True),
    Output("view-tabs-2", "parent_style", allow_duplicate=True),
    Output("no-financials-msg-2", "style", allow_duplicate=True),
    Output("company-candidates-2", "children", allow_duplicate=True),
    Output("company-suggestions-2", "children", allow_duplicate=True),
    Output("company-input-2", "value", allow_duplicate=True),
    Output("suppress-next-suggestions-2", "data", allow_duplicate=True),
    Output("dcf-defaults-store-2", "data", allow_duplicate=True),
    Output("holdings-table-2", "active_cell", allow_duplicate=True),
    Output("holdings-table-2", "selected_cells", allow_duplicate=True),
    Input({"type": "company-candidate-2", "ticker": ALL}, "n_clicks"),
    Input({"type": "company-suggestion-2", "ticker": ALL}, "n_clicks"),
    Input("holdings-table-2", "active_cell"),
    State("holdings-table-2", "data"),
    prevent_initial_call=True,
)
def select_company_candidate_2(candidate_clicks, suggestion_clicks, active_cell, holdings_data):
    triggered = ctx.triggered_id
    if triggered == "holdings-table-2":
        if not active_cell or active_cell.get("column_id") != "symbol":
            raise PreventUpdate
        ticker = holdings_data[active_cell["row"]]["symbol"]
    else:
        if not any(candidate_clicks) and not any(suggestion_clicks):
            raise PreventUpdate
        ticker = triggered["ticker"]
    outputs, _candidates = _run_company_lookup(ticker, _FINANCIALS_YEARS)
    *main_outputs, dcf_defaults = outputs
    return (*main_outputs, None, None, ticker, True, dcf_defaults, None, [])


def _build_suggestions_dropdown(matches, suggestion_type="company-suggestion"):
    _prefetch_fund_names(matches)
    return html.Div(
        [
            html.Button(
                [html.Span(c["ticker"], style={"fontWeight": "700", "marginRight": "8px"}),
                 html.Span(_display_title(c), style={"color": "var(--body-text)"})],
                id={"type": suggestion_type, "ticker": c["ticker"]},
                n_clicks=0,
                style=_SUGGESTION_ROW_STYLE,
            )
            for c in matches
        ],
        style=_SUGGESTIONS_CARD_STYLE,
    )


@app.callback(
    Output("company-suggestions", "children", allow_duplicate=True),
    Output("suppress-next-suggestions", "data"),
    Input("company-input", "value"),
    State("suppress-next-suggestions", "data"),
    prevent_initial_call=True,
)
def update_company_suggestions(query, suppress):
    if suppress:
        return None, False
    if not query or len(query.strip()) < 2:
        return None, False
    try:
        companies = load_ticker_map(_session)
    except requests.RequestException:
        return None, False  # a transient SEC hiccup shouldn't break typing
    matches = search_companies(query, companies, limit=8)
    if not matches:
        return None, False
    return _build_suggestions_dropdown(matches), False


@app.callback(
    Output("company-suggestions-2", "children", allow_duplicate=True),
    Output("suppress-next-suggestions-2", "data"),
    Input("company-input-2", "value"),
    State("suppress-next-suggestions-2", "data"),
    prevent_initial_call=True,
)
def update_company_suggestions_2(query, suppress):
    if suppress:
        return None, False
    if not query or len(query.strip()) < 2:
        return None, False
    try:
        companies = load_ticker_map(_session)
    except requests.RequestException:
        return None, False  # a transient SEC hiccup shouldn't break typing
    matches = search_companies(query, companies, limit=8)
    if not matches:
        return None, False
    return _build_suggestions_dropdown(matches, "company-suggestion-2"), False


def _build_mover_row(r, rank, kind):
    # Clickable the same way as the Politicians tab's Recent Trades rows
    # (.pol-row hover/tap highlight, underlined name) -- select_mover below
    # reads the ticker straight off the clicked row's id.
    up = (r.get("change_pct") or 0) >= 0
    color = "var(--up)" if up else "var(--down)"
    price = r.get("price")
    return html.Div(
        id={"type": "mover-row", "kind": kind, "ticker": r["symbol"]},
        n_clicks=0,
        className="pol-row",
        style={"display": "grid", "gridTemplateColumns": "22px minmax(0,1fr) auto 78px", "gap": "12px",
               "alignItems": "center", "padding": "11px 0", "borderBottom": "1px solid var(--border)",
               "fontSize": "13px", "cursor": "pointer"},
        children=[
            html.Span(str(rank), style={"fontFamily": "'IBM Plex Mono', monospace", "fontSize": "12px",
                                          "color": "var(--body-text)"}),
            html.Div(
                style={"display": "flex", "flexDirection": "column", "gap": "2px", "minWidth": "0"},
                children=[
                    html.Span(r["symbol"], style={**_CLICKABLE_NAME_STYLE,
                                                   "fontFamily": "'IBM Plex Mono', monospace",
                                                   "color": "var(--security-text)"}),
                    html.Span(r["name"], style={"fontSize": "12px", "color": "var(--body-text)",
                                                "whiteSpace": "nowrap", "overflow": "hidden",
                                                "textOverflow": "ellipsis"}),
                ],
            ),
            html.Span(f"${price:,.2f}" if price is not None else "—",
                      style={"fontFamily": "'IBM Plex Mono', monospace", "color": "var(--text)",
                             "textAlign": "right"}),
            html.Span(
                f"{r['change_pct']:+.2f}%",
                style={"fontFamily": "'IBM Plex Mono', monospace", "fontSize": "12px", "fontWeight": "600",
                       "textAlign": "center", "padding": "3px 0", "borderRadius": "4px",
                       "backgroundColor": "rgba(76,195,138,0.14)" if up else "rgba(229,103,90,0.14)",
                       "color": color},
            ),
        ],
    )


_MOVERS_EMPTY_MSG_STYLE = {"color": "var(--body-text)", "fontSize": "13px", "margin": "8px 0"}


@app.callback(
    Output("top-gainers-records", "data"),
    Output("top-losers-records", "data"),
    Output("top-gainers-container", "children"),
    Output("top-losers-container", "children"),
    Input("company-panel", "children"),
    prevent_initial_call=True,
)
def load_day_movers(_panel_children):
    # Fires once the Companies panel itself exists (build_company_panel),
    # same as load_activity_summary does for the Politicians panel. Both
    # screens are fetched in parallel; either failing just leaves that
    # card showing an empty-state message (see _render_mover_rows) rather
    # than taking the other down with it.
    #
    # Returns both cards' first page of rows directly, rather than just
    # the records and leaving render_gainer_rows/render_loser_rows to
    # build them (they only handle scroll-triggered "load more" now).
    # Every callback response costs the Dash renderer ~0.15-0.2s of
    # main-thread work on this (large) panel regardless of payload size,
    # and those two extra round trips landed right as the price chart's
    # own response did -- measured delaying the chart by ~0.6s on open.
    def fetch(kind):
        try:
            return fetch_day_movers(kind, count=_MOVERS_FETCH_COUNT)
        except PriceDataError:
            return []

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        gainers, losers = pool.map(fetch, ["gainers", "losers"])
    return (gainers, losers, _render_mover_rows(_MOVERS_PAGE_SIZE, gainers, "gainers"),
            _render_mover_rows(_MOVERS_PAGE_SIZE, losers, "losers"))


def _render_mover_rows(visible_count, records, kind):
    if not records:
        return html.P("Couldn't load today's movers right now -- try again shortly.", style=_MOVERS_EMPTY_MSG_STYLE)
    return [_build_mover_row(r, i + 1, kind) for i, r in enumerate(records[:visible_count])]


@app.callback(
    Output("top-gainers-container", "children", allow_duplicate=True),
    Input("top-gainers-visible-count", "data"),
    State("top-gainers-records", "data"),
    prevent_initial_call=True,
)
def render_gainer_rows(visible_count, records):
    return _render_mover_rows(visible_count, records, "gainers")


@app.callback(
    Output("top-losers-container", "children", allow_duplicate=True),
    Input("top-losers-visible-count", "data"),
    State("top-losers-records", "data"),
    prevent_initial_call=True,
)
def render_loser_rows(visible_count, records):
    return _render_mover_rows(visible_count, records, "losers")


# Infinite scroll for the movers cards -- identical to the Politicians
# tab's recent-trades-scroll-poll (see there for the poll-not-listener
# and hidden-tab clientHeight guard reasoning).
for _movers_kind in ("top-gainers", "top-losers"):
    app.clientside_callback(
        """
        function(_n_intervals, visibleCount, records) {
            if (!records || !records.length || visibleCount >= records.length) {
                return window.dash_clientside.no_update;
            }
            var container = document.getElementById("%(container)s");
            // Rows not rendered up to visibleCount yet (records just
            // arrived, render_*_rows still in flight) -- the near-empty
            // card would otherwise read as "scrolled to the bottom" and
            // grow the list before anyone's scrolled at all.
            if (!container || container.children.length < visibleCount) {
                return window.dash_clientside.no_update;
            }
            var scroller = container.parentElement;
            if (scroller.clientHeight === 0) return window.dash_clientside.no_update;
            var nearBottom = (scroller.scrollTop + scroller.clientHeight) >= (scroller.scrollHeight - 80);
            if (nearBottom) return Math.min(visibleCount + %(page_size)d, records.length);
            return window.dash_clientside.no_update;
        }
        """ % {"container": f"{_movers_kind}-container", "page_size": _MOVERS_PAGE_SIZE},
        Output(f"{_movers_kind}-visible-count", "data", allow_duplicate=True),
        Input(f"{_movers_kind}-scroll-poll", "n_intervals"),
        State(f"{_movers_kind}-visible-count", "data"),
        State(f"{_movers_kind}-records", "data"),
        prevent_initial_call=True,
    )


# A mover row click loads that ticker the same way typing it and pressing
# Enter would. Safe to write to company-input directly (unlike
# select_global_search_result) since these rows only exist inside the
# already-built Companies panel. suppress-next-suggestions keeps the
# programmatic value change from popping the suggestions dropdown open
# (see update_company_suggestions); scroll-to-lookup-trigger brings the
# chart into view on mobile, where it sits below both movers cards.
@app.callback(
    Output("company-input", "value", allow_duplicate=True),
    Output("company-input", "n_submit", allow_duplicate=True),
    Output("suppress-next-suggestions", "data", allow_duplicate=True),
    Output("scroll-to-lookup-trigger", "data", allow_duplicate=True),
    Input({"type": "mover-row", "kind": ALL, "ticker": ALL}, "n_clicks"),
    State("company-input", "n_submit"),
    State("company-input", "value"),
    prevent_initial_call=True,
)
def select_mover(clicks, current_n_submit, current_value):
    if not any(clicks or []):
        raise PreventUpdate  # fires with all-zero clicks whenever either list re-renders
    ticker = ctx.triggered_id["ticker"]
    # Only suppress when the value actually changes -- re-clicking the
    # already-loaded ticker never fires update_company_suggestions, so a
    # leftover True would swallow the user's next real keystroke instead.
    return ticker, (current_n_submit or 0) + 1, ticker != current_value, "company"


_EMPTY_PRICE_HEADER = ("", "", {}, "", "")


@app.callback(
    Output("price-chart", "figure"),
    Output("stock-header-price", "children"),
    Output("stock-header-change", "children"),
    Output("stock-header-change", "style"),
    Output("stock-header-hi", "children"),
    Output("stock-header-lo", "children"),
    Input("ticker-store", "data"),
    Input("ticker-store-2", "data"),
    Input("compare-mode", "data"),
    Input("range-tabs", "value"),
    Input("price-chart-refresh", "n_intervals"),
    Input("theme-store", "data"),
    Input("viewport-is-mobile", "data"),
)
def update_price_chart(store, store2, is_compare, range_key, _n_intervals, theme, mobile):
    if not store:
        return empty_price_figure(theme=theme), *_EMPTY_PRICE_HEADER
    ticker = store["ticker"]

    if is_compare and store2:
        ticker2 = store2["ticker"]
        try:
            df1 = fetch_price_history(ticker, range_key)
            df2 = fetch_price_history(ticker2, range_key)
        except PriceDataError as e:
            return empty_price_figure(str(e), theme=theme), *_EMPTY_PRICE_HEADER
        except Exception as e:
            return empty_price_figure(f"Price data unavailable: {e}", theme=theme), *_EMPTY_PRICE_HEADER
        figure = build_compare_price_figure(df1, ticker, df2, ticker2, range_key, theme=theme, mobile=mobile)
        return (figure, *_price_header_texts(_price_change_stats(df1)))

    try:
        df = fetch_price_history(ticker, range_key)
    except PriceDataError as e:
        return empty_price_figure(str(e), theme=theme), *_EMPTY_PRICE_HEADER
    except Exception as e:
        return empty_price_figure(f"Price data unavailable: {e}", theme=theme), *_EMPTY_PRICE_HEADER
    figure = build_price_figure(df, ticker, range_key, theme=theme, mobile=mobile)
    return (figure, *_price_header_texts(_price_change_stats(df)))


_KPI_TILE_STYLE = {
    "backgroundColor": "var(--card-bg)", "border": "1px solid var(--border)", "borderRadius": "10px",
    "padding": "14px", "display": "flex", "flexDirection": "column", "gap": "6px",
}
_KPI_TILE_LABEL_STYLE = {"fontSize": "11px", "letterSpacing": "0.06em", "textTransform": "uppercase",
                          "color": "var(--body-text)"}
_KPI_TILE_VALUE_STYLE = {"fontFamily": "'IBM Plex Mono', monospace", "fontSize": "19px", "color": "var(--text)"}


_KPI_TILE_SUB_STYLE = {"fontSize": "12px", "color": "var(--body-text)"}


def _kpi_tile(label, value, sub=None, value_id=None):
    value_span = html.Span(value if value is not None else "—", style=_KPI_TILE_VALUE_STYLE)
    if value_id:
        value_span.id = value_id
    children = [
        html.Span(label, style=_KPI_TILE_LABEL_STYLE),
        value_span,
    ]
    if sub is not None:
        children.append(html.Span(sub, style=_KPI_TILE_SUB_STYLE))
    return html.Div(style=_KPI_TILE_STYLE, children=children)


def _analyst_tiles(overview):
    """Analyst Rating (% buy, with hold/sell underneath) and Avg Price
    Target (with upside vs. the current price) tiles for the KPI grid."""
    pct_buy, pct_hold, pct_sell = (overview.get(k) for k in ("pct_buy", "pct_hold", "pct_sell"))
    count = overview.get("rating_count")
    rating_value = rating_sub = None
    if pct_buy is not None:
        rating_value = f"{pct_buy * 100:.0f}% Buy"
        rating_sub = [f"{pct_hold * 100:.0f}% Hold · {pct_sell * 100:.0f}% Sell"]
        if count:
            # Hidden on mobile (see .kpi-analyst-count in custom.css).
            rating_sub.append(html.Span(f" · {count} analysts", className="kpi-analyst-count"))

    target = overview.get("target_mean_price")
    price = overview.get("current_price")
    target_value = target_sub = None
    if target is not None:
        target_value = f"${target:,.2f}"
        if price:
            upside = target / price - 1
            target_sub = html.Span(
                f"{upside * 100:+.1f}% vs. price",
                style={"color": "var(--up)" if upside >= 0 else "var(--down)"},
            )
    return [
        _kpi_tile("Analyst Rating", rating_value, rating_sub),
        _kpi_tile("Avg Price Target", target_value, target_sub),
    ]


def _roic_text(store, rows_store):
    """ROIC for the ticker in `store` (ticker-store) from the SEC lookup's
    rows-store -- None until that lookup has finished for this same
    ticker (or if it has no ROIC, e.g. a fund)."""
    if not store or not rows_store or rows_store.get("ticker") != store.get("ticker"):
        return None
    rows = rows_store.get("rows") or []
    roic = rows[-1].get("roic") if rows else None
    return f"{roic * 100:.1f}%" if roic is not None else None


def _company_overview_data(store, rows_store=None, suffix=""):
    """(meta_text, kpi_label, tiles) for a ticker's header meta line, KPI
    section label, and KPI tiles -- shared between update_company_overview
    (which also uses meta_text for the page's own header) and Compare
    mode's update_company_overview_2 (which only needs a label + tiles for
    its second, no-header KPI row). `store` is ticker-store; `rows_store`
    (the SEC lookup's rows-store, passed as State) only supplies ROIC if
    it's already in for this same ticker -- otherwise update_kpi_roic/_2
    fills that one tile in when it arrives."""
    if not store:
        return "", "", []
    ticker = store["ticker"]
    title = store.get("title") or ticker

    # Best-effort: yfinance not having overview data for this ticker (funds,
    # thinly-traded names) shouldn't break the rest of the page -- the KPI
    # tiles just show "—" for whatever's missing.
    try:
        overview = fetch_ticker_overview(ticker)
    except PriceDataError:
        overview = {}

    meta_text = " · ".join(p for p in (overview.get("exchange"), ticker, overview.get("sector")) if p)
    trailing_pe = overview.get("trailing_pe")
    net_margin = overview.get("net_margin")

    tiles = [
        _kpi_tile("Market Cap", _fmt_big_dollars(overview.get("market_cap"))),
        _kpi_tile("P/E (TTM)", f"{trailing_pe:.1f}×" if trailing_pe is not None else None),
        _kpi_tile("Revenue TTM", _fmt_big_dollars(overview.get("revenue_ttm"))),
        _kpi_tile("Net Margin", f"{net_margin * 100:.1f}%" if net_margin is not None else None),
        _kpi_tile("ROIC", _roic_text(store, rows_store), value_id=f"kpi-roic{suffix}"),
        *_analyst_tiles(overview),
    ]
    return meta_text, f"{ticker} — {title}", tiles


# Header meta/name and the KPI tile grid only depend on which ticker is
# loaded, not the selected chart range -- kept as a separate callback from
# update_price_chart above (which does re-run on every range-tabs click)
# so switching ranges doesn't re-hit yfinance for an overview fetch that
# hasn't changed.
@app.callback(
    Output("stock-header-meta", "children"),
    Output("stock-header-name", "children"),
    Output("stock-kpi-label", "children"),
    Output("stock-kpi-grid", "children"),
    Output("dcf-company-label", "children"),
    Input("ticker-store", "data"),
    # State, not Input: Dash holds a callback back while any of its Inputs
    # is still being produced by an in-flight callback, so an Input here
    # made the KPI tiles wait for the whole SEC lookup. ROIC catches up
    # via update_kpi_roic instead.
    State("rows-store", "data"),
)
def update_company_overview(store, rows_store):
    meta_text, kpi_label, tiles = _company_overview_data(store, rows_store)
    title = (store or {}).get("title") or (store or {}).get("ticker") or ""
    return meta_text, title, kpi_label, tiles, kpi_label


# Names the company in no-financials-msg rather than a generic "this
# company" -- in Compare mode the two side-by-side Financials columns
# aren't labeled, so the generic text didn't say which ticker it meant.
# Its own callback (not folded into update_company_overview) so the text
# lands as fast as rows-store does, without waiting on that callback's
# yfinance overview fetch.
def _no_financials_text(store):
    ticker = (store or {}).get("ticker")
    title = (store or {}).get("title")
    if not ticker:
        return "No financials data available for this company."
    return f"No financials data available for {ticker} ({title})." if title else         f"No financials data available for {ticker}."


@app.callback(Output("no-financials-msg", "children"), Input("rows-store", "data"))
def update_no_financials_msg(store):
    return _no_financials_text(store)


@app.callback(Output("no-financials-msg-2", "children"), Input("rows-store-2", "data"))
def update_no_financials_msg_2(store):
    if ctx.triggered_id is None and not store:
        raise PreventUpdate  # layout default already says the same thing
    return _no_financials_text(store)


def _fiscal_quarter_label(report_date, fy_end_month):
    """"Q3 FY26"-style label for the fiscal quarter an earnings report on
    `report_date` covers: the latest fiscal-quarter-end month before the
    report, given the company's fiscal year-end month (12 for calendar)."""
    year, month = report_date.year, report_date.month - 1  # a quarter ends before it's reported
    while (month - fy_end_month) % 3:
        month -= 1
    if month < 1:
        year, month = year - 1, month + 12
    quarter = (month - fy_end_month - 1) % 12 // 3 + 1
    fiscal_year = year if month <= fy_end_month else year + 1
    return f"Q{quarter} FY{fiscal_year % 100:02d}"


def _fy_end_month(fiscal_year_end):
    """Fiscal year-end month from an ISO date (Yahoo's lastFiscalYearEnd, see
    fetch_ticker_overview), falling back to December. A 52/53-week year
    ending in a month's first week (a retailer's "Feb 1") counts as the
    prior month's year-end. Not taken from the SEC rows, so the earnings
    chart doesn't have to wait for the SEC lookup."""
    try:
        end = date.fromisoformat(fiscal_year_end[:10])
    except (TypeError, ValueError):
        return 12
    return (end - timedelta(days=7)).month


_EARNINGS_CHART_HEIGHT = 300


def build_earnings_figure(events, fy_end_month, theme="dark", mobile=False):
    """Estimate (hollow) vs. actual (filled, green beat / red miss) EPS per
    quarter, with the next scheduled report's estimate alone at the right
    edge. Beat/Miss and the $ surprise sit under each quarter's label."""
    colors = _chart_colors(theme)
    if mobile:
        # Last four reported quarters plus the upcoming report (when
        # scheduled), to keep the chart compact on a phone.
        reported = [e for e in events if e["actual"] is not None][-4:]
        events = reported + [e for e in events if e["actual"] is None]
    labels, ticktext = [], []
    est_y, act_y, act_colors, est_hover, act_hover = [], [], [], [], []
    for e in events:
        label = _fiscal_quarter_label(e["date"], fy_end_month)
        labels.append(label)
        est, act = e["estimate"], e["actual"]
        if act is None:
            ticktext.append(f"{label}<br><span style='color:{colors['muted_text']}'>Reports</span>"
                            f"<br>{e['date']:%b} {e['date'].day}")
        elif est is None:
            ticktext.append(label)
        else:
            diff = act - est
            if abs(diff) < 0.005:  # rounds to $0.00 -- neither a beat nor a miss
                ticktext.append(f"{label}<br><span style='color:{colors['muted_text']}'>Met<br>$0.00</span>")
            else:
                color = _PRICE_UP_COLOR if diff > 0 else _PRICE_DOWN_COLOR
                ticktext.append(f"{label}<br><span style='color:{color}'>{'Beat' if diff > 0 else 'Miss'}<br>"
                                f"{'+' if diff > 0 else '−'}${abs(diff):.2f}</span>")
        est_y.append(est)
        act_y.append(act)
        act_colors.append(_PRICE_UP_COLOR if act is None or est is None or act >= est else _PRICE_DOWN_COLOR)
        est_hover.append(f"{label}<br>Estimate: ${est:.2f}" if est is not None else "")
        act_hover.append(f"{label}<br>Actual: ${act:.2f}" if act is not None else "")

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=labels, y=est_y, name="Estimate", mode="markers",
        marker=dict(size=16, color="rgba(0,0,0,0)", line=dict(color=colors["muted_text"], width=2)),
        hovertext=est_hover, hovertemplate="%{hovertext}<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=labels, y=act_y, name="Actual", mode="markers",
        marker=dict(size=16, color=act_colors),
        hovertext=act_hover, hovertemplate="%{hovertext}<extra></extra>",
    ))
    fig.update_layout(
        height=_EARNINGS_CHART_HEIGHT,
        paper_bgcolor=colors["surface"], plot_bgcolor=colors["surface"],
        margin=dict(l=10, r=10, t=10, b=10),
        showlegend=False,
        hoverlabel=dict(bgcolor=colors["hover_bg"], bordercolor=colors["axis_line"],
                         font=dict(color=colors["primary_text"], size=12)),
        # Same mobile reasoning as build_dcf_figure's dragmode/fixedrange.
        dragmode=False if mobile else "zoom",
        xaxis=dict(showgrid=False, showline=True, linecolor=colors["axis_line"],
                   tickmode="array", tickvals=labels, ticktext=ticktext,
                   tickfont=dict(color=colors["primary_text"], size=11), fixedrange=True),
        yaxis=dict(showgrid=True, gridcolor=colors["gridline"], griddash="dash", zeroline=False,
                   tickprefix="$", tickformat=".2f",
                   tickfont=dict(color=colors["muted_text"], size=11), fixedrange=mobile),
    )
    return fig


def _earnings_summary(events, fy_end_month):
    """Latest reported quarter's estimate vs. actual, above the chart."""
    last = next((e for e in reversed(events) if e["actual"] is not None), None)
    if not last:
        return ""
    parts = [html.B(_fiscal_quarter_label(last["date"], fy_end_month)), "  ○ Estimate "]
    parts.append(f"${last['estimate']:.2f}" if last["estimate"] is not None else "—")
    beat = last["estimate"] is None or last["actual"] >= last["estimate"]
    parts += ["  ", html.Span("●", style={"color": "var(--up)" if beat else "var(--down)"}), " Actual ",
              html.Span(f"${last['actual']:.2f}", style={"color": "var(--up)" if beat else "var(--down)"})]
    return parts


def _earnings_outputs(store, theme, mobile, suffix=""):
    hidden = {"display": "none"}
    if not store:
        return None, "", hidden
    try:
        events = fetch_earnings_history(store["ticker"])
    except PriceDataError:
        return None, "", hidden
    try:
        fy_end = _fy_end_month(fetch_ticker_overview(store["ticker"]).get("fiscal_year_end"))
    except PriceDataError:
        fy_end = 12
    graph = dcc.Graph(id=f"earnings-chart{suffix}", config={"displayModeBar": False},
                      figure=build_earnings_figure(events, fy_end, theme=theme, mobile=mobile))
    return graph, _earnings_summary(events, fy_end), {"marginBottom": "24px"}


@app.callback(
    Output("earnings-chart-holder", "children"),
    Output("earnings-summary", "children"),
    Output("earnings-wrap", "style"),
    Input("ticker-store", "data"),
    Input("theme-store", "data"),
    Input("viewport-is-mobile", "data"),
)
def update_earnings_chart(store, theme, mobile):
    return _earnings_outputs(store, theme, mobile)


@app.callback(
    Output("earnings-chart-holder-2", "children"),
    Output("earnings-summary-2", "children"),
    Output("earnings-wrap-2", "style"),
    Input("ticker-store-2", "data"),
    Input("theme-store", "data"),
    Input("viewport-is-mobile", "data"),
)
def update_earnings_chart_2(store, theme, mobile):
    # Nothing to show (or re-theme) until Compare mode has a second ticker.
    if not store and ctx.triggered_id != "ticker-store-2":
        raise PreventUpdate
    return _earnings_outputs(store, theme, mobile, suffix="-2")


# Compare mode's second ticker: same KPI tiles, but no second page header
# to also populate (the chart already overlays both tickers on one shared
# header/chart) -- just its own labeled row underneath the primary one.
@app.callback(
    Output("stock-kpi-label-2", "children"),
    Output("stock-kpi-grid-2", "children"),
    Input("ticker-store-2", "data"),
    State("rows-store-2", "data"),  # see update_company_overview
)
def update_company_overview_2(store, rows_store):
    if ctx.triggered_id is None and not store:
        raise PreventUpdate  # empty KPI row, already empty in the layout
    _meta_text, kpi_label, tiles = _company_overview_data(store, rows_store, suffix="-2")
    return kpi_label, tiles


# Fills in the ROIC tile once the SEC lookup (rows-store) lands -- the rest
# of the KPI tiles render as soon as the ticker is known (see
# update_company_overview), without waiting for it. Also re-runs whenever
# the tile grid itself is (re)drawn: the grid's own request can be sent
# before the SEC lookup finishes but answered after it, so neither
# arrival order on its own reliably leaves ROIC filled in.
@app.callback(Output("kpi-roic", "children"), Input("rows-store", "data"), Input("stock-kpi-grid", "children"),
              State("ticker-store", "data"), prevent_initial_call=True)
def update_kpi_roic(rows_store, _tiles, store):
    text = _roic_text(store, rows_store)
    if text is None:
        raise PreventUpdate
    return text


@app.callback(Output("kpi-roic-2", "children"), Input("rows-store-2", "data"), Input("stock-kpi-grid-2", "children"),
              State("ticker-store-2", "data"), prevent_initial_call=True)
def update_kpi_roic_2(rows_store, _tiles, store):
    text = _roic_text(store, rows_store)
    if text is None:
        raise PreventUpdate
    return text


# Its own callback (not another Output of update_company_overview_2):
# dcf-company-label-2 only exists once build_compare_panels has run, and
# Dash rejects a callback whose Outputs are only partly on the page.
@app.callback(Output("dcf-company-label-2", "children"), Input("rows-store-2", "data"))
def update_dcf_company_label_2(store):
    if not store:
        raise PreventUpdate
    return f"{store['ticker']} — {store.get('title') or store['ticker']}"


# Pulses the live-price dot's halo (see build_price_figure) by directly
# restyling the existing Plotly figure's marker opacity in place, rather
# than round-tripping to the server on a ~1s cadence like the 15s price
# refresh does. Traces are found by name since Volume's presence shifts
# everyone else's index. Silently no-ops on the empty/placeholder figure,
# which has no "_pulse_halo" trace.
app.clientside_callback(
    """
    function(n_intervals) {
        var container = document.getElementById('price-chart');
        // dcc.Graph's own id lands on the wrapper div; the actual Plotly
        // graph div (the one with .data/.layout) is the .js-plotly-plot
        // child inside it.
        var gd = container ? container.querySelector('.js-plotly-plot') : null;
        if (!gd || !gd.data) {
            return window.dash_clientside.no_update;
        }
        // Skip this tick during an active mobile scrub (see custom.js,
        // which sets this flag for the gesture's whole duration and also
        // hides both pulse traces itself) -- restyling mid-gesture would
        // undo that hide, flashing the latest-price dot/halo back to
        // visible for a frame at the far right of the chart while a
        // second, scrub-following dot is showing elsewhere, reading as
        // two dots at once. window.__chartScrubbing is the authoritative
        // signal; the hoverlayer check below is kept as a fallback for
        // desktop's native hover (no flag involved there) but is too
        // easily satisfied to rely on alone during a scrub -- Plotly
        // briefly empties the hoverlayer between successive Fx.hover()
        // calls, and that gap can land on the same tick as this interval.
        if (window.__chartScrubbing) {
            return window.dash_clientside.no_update;
        }
        // Not scrubbing -- _pulse_dot should always be fully opaque here
        // (only a scrub gesture ever hides it). Self-heals the rare case
        // where a server-driven figure refresh (see custom.js's
        // afterplot hook) landed in the small window between a drag's
        // release and its own restyle-back-to-1 actually taking effect,
        // rather than leaving the live dot invisible until the next
        // scrub. Unlike the halo pulse below, not gated on the
        // hoverlayer check -- a genuine native hover never touches this
        // trace's opacity, so there's nothing here for it to clash with.
        var dotIdx = -1;
        for (var j = 0; j < gd.data.length; j++) {
            if (gd.data[j].name === '_pulse_dot') { dotIdx = j; break; }
        }
        if (dotIdx !== -1 && gd._fullData[dotIdx].marker.opacity !== 1) {
            Plotly.restyle(gd, {'marker.opacity': [1]}, [dotIdx]);
        }
        var hoverLayer = gd.querySelector('.hoverlayer');
        if (hoverLayer && hoverLayer.childNodes.length > 0) {
            return window.dash_clientside.no_update;
        }
        var idx = -1;
        for (var i = 0; i < gd.data.length; i++) {
            if (gd.data[i].name === '_pulse_halo') { idx = i; break; }
        }
        if (idx === -1) {
            return window.dash_clientside.no_update;
        }
        var opacity = (n_intervals % 2 === 0) ? 0.55 : 0.12;
        Plotly.restyle(gd, {'marker.opacity': [[opacity]]}, [idx]);
        return '';
    }
    """,
    Output("price-pulse-sink", "children"),
    Input("price-pulse-interval", "n_intervals"),
)


def _table_dataframe(columns, data):
    """Build a pandas DataFrame from a dash_table columns/data pair (the
    same shape the Financials/Top Holdings tables already hold), for one
    sheet of the Excel download below."""
    col_ids = [c["id"] for c in columns]
    col_names = [c["name"] for c in columns]
    return pd.DataFrame([[r.get(cid, "") for cid in col_ids] for r in data], columns=col_names)


_EXCEL_SHEET_NAME_RE = re.compile(r'[\[\]:*?/\\]')


def build_financials_excel(sheets):
    """sheets: ordered [(sheet_name, columns, data), ...] -- one per tab
    currently shown in the Financials nav (or just Top Holdings for a
    fund). Sheets with no rows are skipped. Returns the .xlsx file's raw
    bytes."""
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        wrote_any = False
        for name, columns, data in sheets:
            if not data:
                continue
            df = _table_dataframe(columns, data)
            # Excel sheet names: 31-char limit, and a handful of
            # characters ([]:*?/\) aren't allowed at all.
            safe_name = _EXCEL_SHEET_NAME_RE.sub("", name)[:31]
            df.to_excel(writer, sheet_name=safe_name, index=False)
            wrote_any = True
        if not wrote_any:
            pd.DataFrame().to_excel(writer, sheet_name="Data", index=False)
    return buf.getvalue()


@app.callback(
    Output("download-csv", "data"),
    Input("download-btn", "n_clicks"),
    State("rows-store", "data"),
    State("results-table", "data"),
    State("income-statement-table", "data"),
    State("income-statement-table", "columns"),
    State("balance-sheet-table", "data"),
    State("balance-sheet-table", "columns"),
    State("cash-flow-table", "data"),
    State("cash-flow-table", "columns"),
    State("holdings-table", "data"),
    State("holdings-table", "columns"),
    prevent_initial_call=True,
)
def download(_n_clicks, store, growth_data, is_data, is_columns, bs_data, bs_columns,
              cf_data, cf_columns, holdings_data, holdings_columns):
    if not store:
        return None
    # ETFs/funds only ever populate the holdings table (see
    # _run_etf_lookup), never the financials tables, so that alone tells
    # us which workbook shape to build.
    if holdings_data:
        sheets = [("Top Holdings", holdings_columns, holdings_data)]
    else:
        sheets = [
            ("Growth Rates", DISPLAY_COLUMNS, growth_data),
            ("Income Statement", is_columns, is_data),
            ("Balance Sheet", bs_columns, bs_data),
            ("Cash Flow Statement", cf_columns, cf_data),
        ]
    xlsx_bytes = build_financials_excel(sheets)
    filename = f"{store['ticker']}_financials.xlsx"
    return dcc.send_bytes(xlsx_bytes, filename)


@app.callback(
    Output("download-csv-2", "data"),
    Input("download-btn-2", "n_clicks"),
    State("rows-store-2", "data"),
    State("results-table-2", "data"),
    State("income-statement-table-2", "data"),
    State("income-statement-table-2", "columns"),
    State("balance-sheet-table-2", "data"),
    State("balance-sheet-table-2", "columns"),
    State("cash-flow-table-2", "data"),
    State("cash-flow-table-2", "columns"),
    State("holdings-table-2", "data"),
    State("holdings-table-2", "columns"),
    prevent_initial_call=True,
)
def download_2(_n_clicks, store, growth_data, is_data, is_columns, bs_data, bs_columns,
                cf_data, cf_columns, holdings_data, holdings_columns):
    if not store:
        return None
    if holdings_data:
        sheets = [("Top Holdings", holdings_columns, holdings_data)]
    else:
        sheets = [
            ("Growth Rates", DISPLAY_COLUMNS, growth_data),
            ("Income Statement", is_columns, is_data),
            ("Balance Sheet", bs_columns, bs_data),
            ("Cash Flow Statement", cf_columns, cf_data),
        ]
    xlsx_bytes = build_financials_excel(sheets)
    filename = f"{store['ticker']}_financials.xlsx"
    return dcc.send_bytes(xlsx_bytes, filename)


def _dcf_money(v):
    return f"${v:,.1f}M" if v is not None else "—"


def _dcf_per_share(v):
    return f"${v:,.2f}" if v is not None else "—"


def _run_dcf(base_fcf, growth_rate, terminal_growth, discount_rate, years, net_debt, shares_out, current_price):
    """Shared by update_dcf's two triggers (auto-populated defaults, and
    the Calculate button) so both go through identical math/formatting.
    Returns (data, columns, summary, fair_value_per_share) -- the last one
    unformatted (float or None) for build_dcf_banner to use directly."""
    if any(v is None for v in (base_fcf, growth_rate, terminal_growth, discount_rate, years)):
        return [], [{"name": "Line Item", "id": "line"}], [
            {"metric": "Status", "value": "Enter the inputs on the left and click Calculate."},
        ], None
    try:
        result = compute_dcf(base_fcf, growth_rate, terminal_growth, discount_rate, years,
                              net_debt or 0, shares_out or 0)
    except (ValueError, ZeroDivisionError) as e:
        return [], [{"name": "Line Item", "id": "line"}], [{"metric": "Error", "value": str(e)}], None

    columns = [{"name": "Line Item", "id": "line"}] + [
        {"name": label, "id": col_id} for col_id, label in result["year_columns"]
    ]
    data = [
        {"line": "Projected FCF ($M)", **{k: f"{v:,.1f}" for k, v in result["fcf_row"].items()}},
        {"line": "Discount Factor", **{k: f"{v:.3f}" for k, v in result["discount_row"].items()}},
        {"line": "Present Value ($M)", **{k: f"{v:,.1f}" for k, v in result["pv_row"].items()}},
    ]

    fair_value = result["fair_value_per_share"]
    upside = (fair_value / current_price - 1) * 100 if fair_value and current_price else None

    summary = [
        {"metric": "Terminal Value ($M)", "value": _dcf_money(result["terminal_value"])},
        {"metric": "PV of Terminal Value ($M)", "value": _dcf_money(result["pv_terminal"])},
        {"metric": "Enterprise Value ($M)", "value": _dcf_money(result["enterprise_value"])},
        {"metric": "Less: Net Debt ($M)", "value": _dcf_money(net_debt)},
        {"metric": "Equity Value ($M)", "value": _dcf_money(result["equity_value"])},
        {"metric": "Diluted Shares Out. (M)", "value": f"{shares_out:,.1f}" if shares_out else "—"},
        {"metric": "Fair Value / Share", "value": _dcf_per_share(fair_value)},
        {"metric": "Current Price", "value": _dcf_per_share(current_price)},
        {"metric": "Upside / (Downside)", "value": f"{upside:+.1f}%" if upside is not None else "—"},
    ]
    return data, columns, summary, fair_value


_DCF_FIELD_NAMES = [field for field, _label in _DCF_INPUT_FIELDS]


# One callback handles both ways the DCF table gets (re)computed --
# defaults auto-populating after a new company loads, and the Calculate
# button -- rather than two callbacks that would otherwise race: an
# auto-populate callback writing dcf-*.value and a separate calculate
# callback reading those same fields as State have no guaranteed order
# when both fire off the same trigger (see clear_position_filters above,
# which hit exactly this). Here there's only one trigger's values in play
# at a time -- the freshly-fetched defaults dict, or the current input
# State -- so there's nothing to race.
@app.callback(
    [Output(f"dcf-{field}", "value") for field in _DCF_FIELD_NAMES]
    + [Output("dcf-table", "data"), Output("dcf-table", "columns"), Output("dcf-summary-table", "data"),
       Output("dcf-banner", "children"), Output("dcf-chart-store", "data")],
    Input("dcf-defaults-store", "data"),
    Input("calculate-dcf-btn", "n_clicks"),
    Input("dcf-price-refresh", "n_intervals"),
    Input("theme-store", "data"),
    Input("viewport-is-mobile", "data"),
    [State(f"dcf-{field}", "value") for field in _DCF_FIELD_NAMES],
    prevent_initial_call=True,
)
def update_dcf(defaults, _n_clicks, _n_intervals, theme, mobile, *current_values):
    if ctx.triggered_id == "dcf-defaults-store":
        if not defaults:
            raise PreventUpdate
        values = tuple(defaults.get(field) for field in _DCF_FIELD_NAMES)
        input_outputs = values
    else:
        values = current_values
        input_outputs = (no_update,) * len(_DCF_FIELD_NAMES)

    field_values = dict(zip(_DCF_FIELD_NAMES, values))

    if ctx.triggered_id == "dcf-price-refresh":
        ticker = (defaults or {}).get("ticker")
        if not ticker:
            raise PreventUpdate
        try:
            price_df = fetch_price_history(ticker, "1D")
            field_values["current_price"] = round(float(price_df["Close"].iloc[-1]), 2)
        except Exception:
            raise PreventUpdate
        input_outputs = tuple(
            field_values["current_price"] if field == "current_price" else no_update
            for field in _DCF_FIELD_NAMES
        )

    data, columns, summary, fair_value = _run_dcf(
        field_values["base_fcf"], field_values["growth_rate"], field_values["terminal_growth"],
        field_values["discount_rate"], field_values["years"], field_values["net_debt"],
        field_values["shares_out"], field_values["current_price"],
    )

    # ticker/title/historical/base_revenue aren't editable DCF inputs, so
    # they don't have their own dcf-*.value fields -- read straight from
    # the defaults dict, which Dash always passes the current value of
    # regardless of which Input actually triggered this call.
    defaults = defaults or {}
    banner = build_dcf_banner(
        defaults.get("ticker") or "—", defaults.get("title") or "", fair_value,
        field_values["current_price"], field_values["growth_rate"],
        field_values["discount_rate"], field_values["terminal_growth"],
    )
    chart = build_dcf_chart(
        defaults.get("historical") or [], defaults.get("base_revenue"),
        field_values["base_fcf"], field_values["growth_rate"], field_values["years"],
        theme=theme, mobile=mobile,
    )

    return (*input_outputs, data, columns, summary, banner, chart)


@app.callback(
    [Output(f"dcf-{field}-2", "value") for field in _DCF_FIELD_NAMES]
    + [Output("dcf-table-2", "data"), Output("dcf-table-2", "columns"), Output("dcf-summary-table-2", "data"),
       Output("dcf-banner-2", "children"), Output("dcf-chart-store-2", "data")],
    Input("dcf-defaults-store-2", "data"),
    Input("calculate-dcf-btn-2", "n_clicks"),
    Input("dcf-price-refresh-2", "n_intervals"),
    Input("theme-store", "data"),
    Input("viewport-is-mobile", "data"),
    [State(f"dcf-{field}-2", "value") for field in _DCF_FIELD_NAMES],
    prevent_initial_call=True,
)
def update_dcf_2(defaults, _n_clicks, _n_intervals, theme, mobile, *current_values):
    # No second ticker loaded: nothing to value. This also fired on the
    # Companies panel's first render despite prevent_initial_call (theme/
    # viewport stores), building a hidden DCF chart from empty inputs.
    if not defaults:
        raise PreventUpdate
    if ctx.triggered_id == "dcf-defaults-store-2":
        values = tuple(defaults.get(field) for field in _DCF_FIELD_NAMES)
        input_outputs = values
    else:
        values = current_values
        input_outputs = (no_update,) * len(_DCF_FIELD_NAMES)

    field_values = dict(zip(_DCF_FIELD_NAMES, values))

    if ctx.triggered_id == "dcf-price-refresh-2":
        ticker = (defaults or {}).get("ticker")
        if not ticker:
            raise PreventUpdate
        try:
            price_df = fetch_price_history(ticker, "1D")
            field_values["current_price"] = round(float(price_df["Close"].iloc[-1]), 2)
        except Exception:
            raise PreventUpdate
        input_outputs = tuple(
            field_values["current_price"] if field == "current_price" else no_update
            for field in _DCF_FIELD_NAMES
        )

    data, columns, summary, fair_value = _run_dcf(
        field_values["base_fcf"], field_values["growth_rate"], field_values["terminal_growth"],
        field_values["discount_rate"], field_values["years"], field_values["net_debt"],
        field_values["shares_out"], field_values["current_price"],
    )

    defaults = defaults or {}
    banner = build_dcf_banner(
        defaults.get("ticker") or "—", defaults.get("title") or "", fair_value,
        field_values["current_price"], field_values["growth_rate"],
        field_values["discount_rate"], field_values["terminal_growth"],
    )
    chart = build_dcf_chart(
        defaults.get("historical") or [], defaults.get("base_revenue"),
        field_values["base_fcf"], field_values["growth_rate"], field_values["years"],
        theme=theme, mobile=mobile,
    )

    return (*input_outputs, data, columns, summary, banner, chart)


def _valuation_open(n_clicks, mobile):
    """Valuation is always open on desktop; on mobile it starts collapsed
    and each tap of valuation-toggle flips it."""
    return not mobile or (n_clicks or 0) % 2 == 1


def _dcf_chart_view(figure, n_clicks, mobile, suffix):
    """The DCF chart, built only while the Valuation section is open --
    see dcf_chart_column in _financials_valuation_blocks."""
    if not _valuation_open(n_clicks, mobile) or not figure:
        return None
    return dcc.Graph(id=f"dcf-chart{suffix}", figure=figure, config={"displayModeBar": False})


@app.callback(
    Output("dcf-chart-holder", "children"),
    Input("dcf-chart-store", "data"),
    Input("valuation-toggle", "n_clicks"),
    Input("viewport-is-mobile", "data"),
)
def update_dcf_chart_view(figure, n_clicks, mobile):
    return _dcf_chart_view(figure, n_clicks, mobile, "")


@app.callback(
    Output("dcf-chart-holder-2", "children"),
    Input("dcf-chart-store-2", "data"),
    Input("valuation-toggle", "n_clicks"),
    Input("viewport-is-mobile", "data"),
)
def update_dcf_chart_view_2(figure, n_clicks, mobile):
    return _dcf_chart_view(figure, n_clicks, mobile, "-2")


# Shows/hides the Valuation section's body (everything under its heading)
# -- see valuation-toggle. Clientside since it's only a style flip, with
# the same open/closed rule as _valuation_open. Compare mode's second
# panel gets its own callback since it's built lazily (see
# build_compare_panels), and Dash rejects a callback whose Outputs are
# only partly on the page.
app.clientside_callback(
    """
    function(nClicks, isMobile) {
        const open = !isMobile || (nClicks || 0) % 2 === 1;
        return [open ? {} : {display: "none"}, open ? "\u2212" : "+",
                open ? "Hide valuation" : "Show valuation"];
    }
    """,
    Output("dcf-body", "style"),
    Output("valuation-toggle", "children"),
    Output("valuation-toggle", "title"),
    Input("valuation-toggle", "n_clicks"),
    Input("viewport-is-mobile", "data"),
)
app.clientside_callback(
    """
    function(nClicks, isMobile) {
        const open = !isMobile || (nClicks || 0) % 2 === 1;
        return open ? {} : {display: "none"};
    }
    """,
    Output("dcf-body-2", "style"),
    Input("valuation-toggle", "n_clicks"),
    Input("viewport-is-mobile", "data"),
)


def _all_positions_row_to_record(r, companies):
    return {
        "issuer": r["issuer"],
        "shares_m": r["shares_m"],
        "prev_shares_m": r["prev_shares_m"],
        "delta_shares_pct": r["delta_shares_pct"],
        "delta_shares_value_m": r["delta_shares_value_m"],
        "share_price": r["share_price"],
        "prev_share_price": r["prev_share_price"],
        "value_m": r["value_m"],
        "prev_value_m": r["prev_value_m"],
        "delta_value_pct": r["delta_value_pct"],
        "portfolio_pct": r["portfolio_pct"],
        "prev_portfolio_pct": r["prev_portfolio_pct"],
        "delta_pct": r["delta_pct"],
        # See _manager_row_to_record's matching comment.
        "resolved_ticker": resolve_ticker_for_security(r["issuer"], companies) or "",
    }


_MAX_CANDIDATES_SHOWN = 30
_CANDIDATE_BTN_STYLE = {
    "display": "block",
    "width": "100%",
    "textAlign": "left",
    "padding": "8px 12px",
    "border": "1px solid var(--border)",
    "borderRadius": "6px",
    "backgroundColor": "var(--card-bg)",
    "color": "var(--text)",
    "marginBottom": "4px",
    "cursor": "pointer",
    "fontSize": "13px",
}


def _render_candidates(candidates, query):
    if not candidates:
        return None
    shown = candidates[:_MAX_CANDIDATES_SHOWN]
    buttons = [
        html.Button(
            f"{c['name']}  (CIK {c['cik']})",
            id={"type": "manager-candidate", "cik": c["cik"]},
            n_clicks=0,
            style=_CANDIDATE_BTN_STYLE,
        )
        for c in shown
    ]
    note = []
    if len(candidates) > _MAX_CANDIDATES_SHOWN:
        note = [html.P(f"Showing the first {_MAX_CANDIDATES_SHOWN} of {len(candidates)} matches — "
                        "refine your search to narrow it down.",
                        style={"color": "var(--body-text)", "fontSize": "12px"})]
    return html.Div(
        style={"maxWidth": "520px", "marginTop": "8px"},
        children=buttons + note,
    )


@app.callback(
    Output("top-buys-table-container", "children"),
    Output("top-buys-note", "children"),
    Input("top-buys-sort", "value"),
    State("top-buys-records", "data"),
    prevent_initial_call=True,
)
def sort_top_buys(sort_key, records):
    if sort_key not in _TOP_BUYS_SORTS:
        sort_key = "conviction"
    _label, note, _list_key = _TOP_BUYS_SORTS[sort_key]
    return [_build_top_buy_card(r, r["idx"]) for r in (records or {}).get(sort_key, [])], note


_EMPTY_BAR_LIST = _build_delta_bar_list([], True)


@app.callback(
    Output("manager-status-msg", "children"),
    Output("increases-table-container", "children"),
    Output("decreases-table-container", "children"),
    Output("all-positions-table", "data"),
    Output("manager-candidates", "children"),
    Output("manager-suggestions", "children", allow_duplicate=True),
    Output("all-positions-full", "data"),
    Output("all-positions-visible-count", "data"),
    Output("download-positions-btn", "disabled"),
    Output("manager-positions-meta", "data"),
    Output("manager-summary-tiles", "children"),
    Input("manager-input", "n_submit"),
    State("manager-input", "value"),
    # This callback's own "manager-suggestions" output is itself declared
    # allow_duplicate=True (update_manager_suggestions owns it primarily),
    # so plain prevent_initial_call=False isn't allowed here -- Dash can't
    # guarantee firing order between duplicate-output callbacks on the
    # initial page load otherwise. "initial_duplicate" is its documented
    # opt-in for "fire on load anyway."
    prevent_initial_call="initial_duplicate",
)
def generate_manager(_n_submit, query):
    if not query or not query.strip():
        return ("Enter an investment manager name.", _EMPTY_BAR_LIST, _EMPTY_BAR_LIST, [], None, None, [],
                _POSITIONS_PAGE_SIZE, True, None, [])

    try:
        result = fetch_manager_comparison(query, session=_session)
    except ManagerLookupError as e:
        if e.candidates:
            return (str(e), _EMPTY_BAR_LIST, _EMPTY_BAR_LIST, [], _render_candidates(e.candidates, query), None,
                    [], _POSITIONS_PAGE_SIZE, True, None, [])
        return (str(e), _EMPTY_BAR_LIST, _EMPTY_BAR_LIST, [], None, None, [],
                _POSITIONS_PAGE_SIZE, True, None, [])
    except FilingDataError as e:
        return (f"{query}: {e}", _EMPTY_BAR_LIST, _EMPTY_BAR_LIST, [], None, None, [],
                _POSITIONS_PAGE_SIZE, True, None, [])
    except requests.RequestException as e:
        return (f"Network error talking to SEC EDGAR: {e}", _EMPTY_BAR_LIST, _EMPTY_BAR_LIST, [], None, None,
                [], _POSITIONS_PAGE_SIZE, True, None, [])

    status = (f"Found: {result['resolved_name']} (CIK {result['cik']}) — "
              f"{result['latest_period']} vs {result['previous_period']}")
    companies = load_ticker_map(_session)
    increases = [_manager_row_to_record(r, companies) for r in result["top_increases"]]
    decreases = [_manager_row_to_record(r, companies) for r in result["top_decreases"]]
    all_positions = [_all_positions_row_to_record(r, companies) for r in result["all_positions"]]
    meta = {"cik": result["cik"], "truncated": result.get("positions_truncated", False)}
    return (status, _build_delta_bar_list(increases, True), _build_delta_bar_list(decreases, False),
            all_positions[:_POSITIONS_PAGE_SIZE], None, None,
            all_positions, min(_POSITIONS_PAGE_SIZE, len(all_positions)), not all_positions, meta,
            _manager_summary_tiles(all_positions))


@app.callback(
    Output("manager-status-msg", "children", allow_duplicate=True),
    Output("increases-table-container", "children", allow_duplicate=True),
    Output("decreases-table-container", "children", allow_duplicate=True),
    Output("all-positions-table", "data", allow_duplicate=True),
    Output("manager-candidates", "children", allow_duplicate=True),
    Output("manager-suggestions", "children", allow_duplicate=True),
    Output("manager-input", "value", allow_duplicate=True),
    Output("suppress-next-manager-suggestions", "data", allow_duplicate=True),
    Output("all-positions-full", "data", allow_duplicate=True),
    Output("all-positions-visible-count", "data", allow_duplicate=True),
    Output("download-positions-btn", "disabled", allow_duplicate=True),
    Output("manager-positions-meta", "data", allow_duplicate=True),
    Output("manager-summary-tiles", "children", allow_duplicate=True),
    Output("scroll-to-lookup-trigger", "data", allow_duplicate=True),
    Input({"type": "manager-candidate", "cik": ALL}, "n_clicks"),
    Input({"type": "manager-suggestion", "cik": ALL}, "n_clicks"),
    Input({"type": "top-buy-manager-link", "cik": ALL, "idx": ALL}, "n_clicks"),
    prevent_initial_call=True,
)
def select_manager_candidate(candidate_clicks, suggestion_clicks, top_buy_manager_clicks):
    if not any(candidate_clicks) and not any(suggestion_clicks) and not any(top_buy_manager_clicks):
        raise PreventUpdate  # fires with all-zero clicks whenever a button/card list re-renders
    cik = ctx.triggered_id["cik"]
    # A Top Buys card's manager name sits above the lookup section, so on
    # mobile scroll down to where the result lands (same as a top-search pick).
    scroll = "manager" if ctx.triggered_id["type"] == "top-buy-manager-link" else no_update

    try:
        result = fetch_manager_comparison_by_cik(cik, session=_session)
    except FilingDataError as e:
        return (f"CIK {cik}: {e}", _EMPTY_BAR_LIST, _EMPTY_BAR_LIST, [], None, None, no_update, no_update,
                [], _POSITIONS_PAGE_SIZE, True, None, [], scroll)
    except requests.RequestException as e:
        return (f"Network error talking to SEC EDGAR: {e}", _EMPTY_BAR_LIST, _EMPTY_BAR_LIST, [], None, None,
                no_update, no_update, [], _POSITIONS_PAGE_SIZE, True, None, [], scroll)

    status = (f"Found: {result['resolved_name']} (CIK {result['cik']}) — "
              f"{result['latest_period']} vs {result['previous_period']}")
    companies = load_ticker_map(_session)
    increases = [_manager_row_to_record(r, companies) for r in result["top_increases"]]
    decreases = [_manager_row_to_record(r, companies) for r in result["top_decreases"]]
    all_positions = [_all_positions_row_to_record(r, companies) for r in result["all_positions"]]
    meta = {"cik": result["cik"], "truncated": result.get("positions_truncated", False)}
    # Setting manager-input's value below re-triggers update_manager_suggestions
    # (it watches that same value) — this flag tells that callback to skip
    # showing a dropdown for this one programmatic change, not real typing.
    return (status, _build_delta_bar_list(increases, True), _build_delta_bar_list(decreases, False),
            all_positions[:_POSITIONS_PAGE_SIZE], None, None,
            result["resolved_name"], True,
            all_positions, min(_POSITIONS_PAGE_SIZE, len(all_positions)), not all_positions, meta,
            _manager_summary_tiles(all_positions), scroll)


@app.callback(
    Output("download-positions-csv", "data"),
    Input("download-positions-btn", "n_clicks"),
    State("all-positions-full", "data"),
    State("manager-input", "value"),
    State("manager-positions-meta", "data"),
    prevent_initial_call=True,
)
def download_positions(_n_clicks, all_positions, manager_name, positions_meta):
    # Always the complete unfiltered position list (all-positions-full),
    # not whatever's currently scrolled into the table or narrowed by the
    # active filter chip -- a predictable "export everything" rather than
    # needing to reconcile against filter/scroll state.
    #
    # For a mega-manager the precomputed snapshot only kept the top
    # _MAX_SNAPSHOT_POSITIONS positions (see thirteenf.build_top_managers)
    # -- all_positions here would silently be an incomplete export, so a
    # truncated manager gets one real live fetch for the actual complete
    # list instead. Every other manager still exports the already-loaded
    # data with no extra fetch.
    if positions_meta and positions_meta.get("truncated"):
        try:
            result = fetch_manager_comparison_by_cik(
                positions_meta["cik"], session=_session, skip_snapshot=True)
        except (FilingDataError, requests.RequestException):
            pass  # fall back to the capped list below rather than failing the download
        else:
            all_positions = [_all_positions_row_to_record(r, load_ticker_map(_session))
                              for r in result["all_positions"]]
    if not all_positions:
        return None
    csv_bytes = _table_dataframe(ALL_POSITIONS_COLUMNS, all_positions).to_csv(index=False).encode("utf-8")
    safe_name = re.sub(r"[^A-Za-z0-9]+", "_", manager_name or "manager").strip("_")
    return dcc.send_bytes(csv_bytes, f"{safe_name}_all_positions.csv")


# Manager search hits SEC's live endpoint per query (unlike the company
# search's instant local ticker-list scan), so firing on every keystroke
# would queue a request per character and let stale ones pile up ahead of
# whatever the user actually meant to search. This clientside callback
# debounces: it waits 400ms after typing stops before writing to the store
# that actually drives the search, using dash_clientside.set_props for the
# delayed part since a clientside callback must return synchronously.
app.clientside_callback(
    """
    function(value) {
        if (window.__managerSearchTimer) {
            clearTimeout(window.__managerSearchTimer);
        }
        window.__managerSearchTimer = setTimeout(function() {
            window.dash_clientside.set_props("manager-search-debounced", {data: value});
        }, 400);
        return window.dash_clientside.no_update;
    }
    """,
    Output("manager-search-debounced", "data"),
    Input("manager-input", "value"),
)


@app.callback(
    Output("manager-suggestions", "children"),
    Output("suppress-next-manager-suggestions", "data"),
    Input("manager-search-debounced", "data"),
    State("suppress-next-manager-suggestions", "data"),
    prevent_initial_call=True,
)
def update_manager_suggestions(query, suppress):
    if suppress:
        return None, False
    if not query or len(query.strip()) < 2:
        return None, False
    try:
        matches = search_managers(query, _session, limit=8)
    except requests.RequestException:
        return None, False  # a transient SEC hiccup shouldn't break typing
    if not matches:
        return None, False

    dropdown = html.Div(
        [
            html.Button(
                m["name"],
                id={"type": "manager-suggestion", "cik": m["cik"]},
                n_clicks=0,
                style=_SUGGESTION_ROW_STYLE,
            )
            for m in matches
        ],
        style=_SUGGESTIONS_CARD_STYLE,
    )
    return dropdown, False


# Infinite scroll for the All Equity Positions table: rather than send
# every holding to the table at once (slow to render/scroll for managers
# with hundreds or thousands of positions), only a growing prefix of the
# full result is ever in the table's own data prop. This clientside
# callback polls the table's scroll container every 400ms (a poll, rather
# than a scroll-event listener, is used deliberately — a listener attached
# directly to dash_table's internal div can go stale if React re-renders
# and replaces that div) and grows the visible count once the user nears
# the bottom.
app.clientside_callback(
    """
    function(_n_intervals, visibleCount, fullData) {
        if (!fullData || !fullData.length || visibleCount >= fullData.length) {
            return window.dash_clientside.no_update;
        }
        // fixed_rows renders a separate (non-scrolling) container for the
        // pinned header alongside the actual scrollable body, both
        // matching this class prefix — pick whichever one truly overflows
        // rather than assuming which index is which.
        var candidates = document.querySelectorAll(
            '#all-positions-table [class*="dt-table-container__row"]'
        );
        var container = null;
        for (var i = 0; i < candidates.length; i++) {
            if (candidates[i].scrollHeight > candidates[i].clientHeight) {
                container = candidates[i];
                break;
            }
        }
        if (!container) {
            return window.dash_clientside.no_update;
        }
        var nearBottom = (container.scrollTop + container.clientHeight) >= (container.scrollHeight - 80);
        if (nearBottom) {
            return Math.min(visibleCount + %(page_size)d, fullData.length);
        }
        return window.dash_clientside.no_update;
    }
    """
    % {"page_size": _POSITIONS_PAGE_SIZE},
    Output("all-positions-visible-count", "data", allow_duplicate=True),
    Input("scroll-poll-interval", "n_intervals"),
    State("all-positions-visible-count", "data"),
    State("all-positions-full", "data"),
    prevent_initial_call=True,
)
# Chip click: sets the active filter key, resets the visible-row window
# back to one page (so "showing 100 of N" stays meaningful for a fresh
# filter instead of continuing from whatever scroll position it was at),
# and restyles all five chips. Clientside for instant feedback, same as
# the sidebar nav above.
app.clientside_callback(
    """
    function(nAll, nNew, nAdded, nTrimmed, nBig) {
        if (!nAll && !nNew && !nAdded && !nTrimmed && !nBig) {
            return Array(7).fill(window.dash_clientside.no_update);
        }
        var key = window.dash_clientside.callback_context.triggered_id.replace("chip-", "");
        var chipStyle = function(active) {
            return {
                fontSize: "12px", padding: "5px 10px", borderRadius: "999px", cursor: "pointer",
                whiteSpace: "nowrap",
                border: "1px solid " + (active ? "var(--up)" : "var(--border)"),
                backgroundColor: active ? "rgba(76,195,138,0.12)" : "transparent",
                color: active ? "var(--up)" : "var(--body-text)",
            };
        };
        return [
            key, %(page_size)d,
            chipStyle(key === "all"), chipStyle(key === "new"), chipStyle(key === "added"),
            chipStyle(key === "trimmed"), chipStyle(key === "big"),
        ];
    }
    """ % {"page_size": _POSITIONS_PAGE_SIZE},
    Output("position-filter-chip", "data"),
    Output("all-positions-visible-count", "data", allow_duplicate=True),
    Output("chip-all", "style"),
    Output("chip-new", "style"),
    Output("chip-added", "style"),
    Output("chip-trimmed", "style"),
    Output("chip-big", "style"),
    Input("chip-all", "n_clicks"),
    Input("chip-new", "n_clicks"),
    Input("chip-added", "n_clicks"),
    Input("chip-trimmed", "n_clicks"),
    Input("chip-big", "n_clicks"),
    prevent_initial_call=True,
)

# Mobile only: freeze the Security column so names stay in view while
# swiping across the numbers (custom.css also narrows it to half the
# screen there). Not on desktop -- every column already fits, and
# DataTable's fixed_columns squeezes the table into a 500px box unless
# its widths are all pinned.
app.clientside_callback(
    """
    function(isMobile) {
        return isMobile ? {headers: true, data: 1} : {headers: false, data: 0};
    }
    """,
    Output("all-positions-table", "fixed_columns"),
    Input("viewport-is-mobile", "data"),
)


# Filters by the active chip, sorts (custom — see below), and slices the
# full result down to the currently-visible prefix, in that order. Chip and
# sort are read as State rather than Input here: a chip click reaches this
# callback indirectly, through the chip-click callback above resetting
# all-positions-visible-count, so a scroll or a sort click always re-applies
# whatever chip is currently active without this callback needing its own
# separate trigger for every chip click. Pure client-side array work — no
# need to round-trip to the server.
app.clientside_callback(
    """
    function(sortBy, visibleCount, chip, fullData) {
        if (!fullData || !fullData.length) {
            return window.dash_clientside.no_update;
        }
        var rows = fullData.filter(function(row) {
            var deltaPct = row["delta_shares_pct"];
            if (chip === "new") return deltaPct === null || deltaPct === undefined;
            if (chip === "added") return deltaPct !== null && deltaPct !== undefined && deltaPct > 0;
            if (chip === "trimmed") return deltaPct !== null && deltaPct !== undefined && deltaPct < 0;
            if (chip === "big") {
                var pw = row["portfolio_pct"];
                return pw !== null && pw !== undefined && pw > 1;
            }
            return true;  // "all"
        });
        if (sortBy && sortBy.length) {
            var columnId = sortBy[0].column_id;
            var desc = sortBy[0].direction === "desc";
            rows = rows.slice().sort(function(a, b) {
                var av = a[columnId], bv = b[columnId];
                var aNull = (av === null || av === undefined);
                var bNull = (bv === null || bv === undefined);
                if (aNull && bNull) return 0;
                if (aNull) return desc ? -1 : 1;
                if (bNull) return desc ? 1 : -1;
                if (av < bv) return desc ? 1 : -1;
                if (av > bv) return desc ? -1 : 1;
                return 0;
            });
        }
        return rows.slice(0, visibleCount);
    }
    """,
    Output("all-positions-table", "data", allow_duplicate=True),
    Input("all-positions-table", "sort_by"),
    Input("all-positions-visible-count", "data"),
    State("position-filter-chip", "data"),
    State("all-positions-full", "data"),
    prevent_initial_call=True,
)


def _politician_move_row_to_record(t):
    return {
        "ticker": t["ticker"],
        "transaction_type": t["transaction_type"],
        "transaction_date": t["transaction_date"],
        "amount_low": t["amount_low"],
        "amount_high": t["amount_high"],
    }


def _politician_position_row_to_record(p):
    return {
        "ticker": p["ticker"],
        "net_estimated_value": p["net_estimated_value"],
        "total_bought": p["total_bought"],
        "total_sold": p["total_sold"],
        "transaction_count": p["transaction_count"],
        "last_transaction_date": p["last_transaction_date"],
    }


@app.callback(
    Output("politician-status-msg", "children"),
    Output("politician-increases-table", "data"),
    Output("politician-decreases-table", "data"),
    Output("politician-positions-table", "data"),
    Output("politician-positions-full", "data"),
    Input("politician-input", "value"),
    # "politician-positions-table" has other callbacks (filter_politician_
    # positions, clear_politician_filters) writing to it with
    # allow_duplicate=True; Dash requires this callback -- the non-duplicate
    # owner of that output -- to opt in with "initial_duplicate" rather
    # than a plain initial call to keep firing order well-defined. This is
    # also what makes the default dropdown selection (Nancy Pelosi) load
    # automatically on first visiting the tab, same as the other two
    # trackers, instead of showing an empty screen until a click.
    prevent_initial_call="initial_duplicate",
)
def generate_politician(dropdown_value):
    if not dropdown_value:
        return "Select a member of Congress.", [], [], [], []

    # dropdown_value is already "chamber|last|first" -- the same key
    # load_member_positions_snapshot's dict uses (see
    # congress_trades._member_positions_key) -- so no live fetch is
    # needed to look this member up.
    _, last, first = dropdown_value.split("|", 2)
    display = f"{first} {last}".strip()
    try:
        snapshot = load_member_positions_snapshot()
    except (OSError, json.JSONDecodeError):
        return f"{display}: no data available right now.", [], [], [], []

    result = snapshot.get(dropdown_value)
    if result is None:
        return (f"{display}: no parseable stock transactions found in the latest snapshot.",
                [], [], [], [])

    resolved = result["candidate"]
    chamber_label = "Senator" if resolved["chamber"] == "senate" else "Representative"
    status = (f"Found: {resolved['display']} ({resolved['state_dst']})" if resolved["state_dst"]
              else f"Found: {resolved['display']} ({chamber_label})")
    increases = [_politician_move_row_to_record(t) for t in result["top_increases"]]
    decreases = [_politician_move_row_to_record(t) for t in result["top_decreases"]]
    positions = [_politician_position_row_to_record(p) for p in result["all_positions"]]
    return status, increases, decreases, positions, positions


@app.callback(
    Output("politician-input", "options"),
    Output("politician-input", "value"),
    Output("pending-member-selection", "data", allow_duplicate=True),
    Input("politician-chamber-tabs", "value"),
    State("pending-member-selection", "data"),
    prevent_initial_call=True,
)
def update_politician_roster(chamber, pending_selection):
    try:
        options, default_value = _politician_dropdown_options(chamber)
    except (PoliticianDataError, requests.RequestException):
        # e.g. the Senate eFD disclaimer page is unreachable or its markup
        # changed -- an empty dropdown beats crashing the chamber-tab switch.
        options, default_value = [], None
    # A row click on one of the summary tables above sets this when it
    # also had to switch chambers first (see select_member_from_summary) --
    # use it instead of that chamber's default member, then clear it so it
    # doesn't linger and hijack a later, ordinary chamber-tab click.
    value = pending_selection if pending_selection else default_value
    return options, value, None


def _recent_trade_row_to_record(t):
    return {
        "member": t["member"],
        "chamber": _CHAMBER_LABELS.get(t["chamber"], t["chamber"]),
        "ticker": t["ticker"],
        "transaction_type": t["transaction_type"],
        "transaction_date": t["transaction_date"],
        "amount_low": t["amount_low"],
        "amount_high": t["amount_high"],
        # Not shown as columns -- carried along so a row click can resolve
        # exactly which dropdown entry this member is (see
        # select_member_from_summary below).
        "chamber_raw": t["chamber"],
        "first": t["first"],
        "last": t["last"],
    }


def _leaderboard_row_to_record(e):
    return {
        "member": e["member"],
        "chamber": _CHAMBER_LABELS.get(e["chamber"], e["chamber"]),
        "net_estimated_value": e["net_estimated_value"],
        "transaction_count": e["transaction_count"],
        "chamber_raw": e["chamber"],
        "first": e["first"],
        "last": e["last"],
    }


# Trade amounts are disclosed as a $1K-$5M-ish dollar RANGE (STOCK Act
# filings don't require exact figures) -- a log scale is what makes both a
# "$1,001-$15,000" and a "$1M-$5M" trade's bar readable on the same axis
# without the small ones vanishing to a sliver.
_RANGE_BAR_L0 = math.log10(1000)
_RANGE_BAR_L1 = math.log10(5_000_000)


def _range_bar_metrics(amount_low, amount_high):
    lo = max(1000, min(amount_low or 1000, 5_000_000))
    hi = max(1000, min(amount_high or lo, 5_000_000))
    left_pct = (math.log10(lo) - _RANGE_BAR_L0) / (_RANGE_BAR_L1 - _RANGE_BAR_L0) * 100
    width_pct = max(4.0, (math.log10(hi) - math.log10(lo)) / (_RANGE_BAR_L1 - _RANGE_BAR_L0) * 100)
    return left_pct, width_pct


def _fmt_amount(v):
    if v is None:
        return "—"
    if v >= 1e6:
        return f"${v / 1e6:.1f}M"
    if v >= 1e3:
        return f"${v / 1e3:.0f}K"
    return f"${v:,.0f}"


# Shared by _build_trade_row/_build_leaderboard_row's member-name span --
# an underline reads as "this is a link" on its own, unlike the row's
# hover-only background (.pol-row:hover in custom.css), which a touch
# device never shows at all. Plain --text (not an accent color) per
# feedback -- keeps the row's usual look, just underlined.
_CLICKABLE_NAME_STYLE = {"fontWeight": "500", "color": "var(--text)", "textDecoration": "underline"}


# id carries the (chamber, last, first) selection payload directly, rather
# than indexing into a separate data Store -- select_member_from_summary
# below reads it straight off ctx.triggered_id. `idx` is purely a
# uniqueness discriminator (a member can have many rows in this list,
# e.g. several trades in the same window) -- without it, two of that
# member's rows share one id/React key, which both throws "two children
# with the same key" and lets React's reconciliation hand a row's DOM
# node to the wrong record on re-render (confirmed via stress testing).
def _build_trade_row(r, idx):
    is_buy = r["transaction_type"] == "Purchase"
    pill_color = "var(--up)" if is_buy else "var(--down)"
    left_pct, width_pct = _range_bar_metrics(r["amount_low"], r["amount_high"])
    return html.Div(
        id={"type": "trade-row", "chamber": r["chamber_raw"], "last": r["last"], "first": r["first"],
            "idx": idx},
        n_clicks=0,
        className="pol-row",
        style={"display": "grid",
               "gridTemplateColumns": "minmax(150px,1.6fr) 52px 48px minmax(100px,1.2fr) 52px",
               "gap": "12px", "alignItems": "center", "padding": "12px 0",
               "borderBottom": "1px solid var(--border)", "fontSize": "13px", "cursor": "pointer"},
        children=[
            html.Div(
                style={"display": "flex", "flexDirection": "column", "gap": "2px", "minWidth": "0"},
                children=[
                    # Underlined + accent-colored (not just a hover
                    # background on the whole row, see .pol-row:hover in
                    # custom.css) so it reads as a link on its own --
                    # relying only on hover wasn't discoverable on touch,
                    # where there's no hover state at all.
                    html.Span(r["member"], style=_CLICKABLE_NAME_STYLE),
                    html.Span(r["chamber"], style={"fontSize": "12px", "color": "var(--body-text)"}),
                ],
            ),
            html.Span(r["ticker"], style={"fontFamily": "'IBM Plex Mono', monospace", "fontWeight": "500",
                                            "color": "var(--security-text)"}),
            html.Span(
                "BUY" if is_buy else "SELL",
                style={"fontSize": "11px", "fontWeight": "600", "letterSpacing": "0.04em",
                       "textAlign": "center", "padding": "3px 0", "borderRadius": "4px",
                       "backgroundColor": "rgba(76,195,138,0.14)" if is_buy else "rgba(229,103,90,0.14)",
                       "color": pill_color},
            ),
            html.Div(
                style={"display": "flex", "flexDirection": "column", "gap": "4px"},
                children=[
                    html.Div(
                        style={"position": "relative", "height": "4px", "backgroundColor": "var(--card-bg-2)",
                               "borderRadius": "2px"},
                        children=html.Div(style={"position": "absolute", "top": "0", "height": "4px",
                                                   "borderRadius": "2px", "backgroundColor": pill_color,
                                                   "left": f"{left_pct}%", "width": f"{width_pct}%"}),
                    ),
                    html.Span(f"{_fmt_amount(r['amount_low'])} – {_fmt_amount(r['amount_high'])}",
                               style={"fontFamily": "'IBM Plex Mono', monospace", "fontSize": "11px",
                                      "color": "var(--body-text)"}),
                ],
            ),
            html.Span(r["transaction_date"], style={"fontFamily": "'IBM Plex Mono', monospace",
                                                       "fontSize": "12px", "color": "var(--body-text)",
                                                       "textAlign": "right"}),
        ],
    )


def _build_leaderboard_row(r, rank):
    return html.Div(
        id={"type": "leaderboard-row", "chamber": r["chamber_raw"], "last": r["last"], "first": r["first"]},
        n_clicks=0,
        className="pol-row",
        style={"display": "grid", "gridTemplateColumns": "22px minmax(0,1fr) auto", "gap": "10px",
               "alignItems": "center", "padding": "11px 0", "borderBottom": "1px solid var(--border)",
               "fontSize": "13px", "cursor": "pointer"},
        children=[
            html.Span(str(rank), style={"fontFamily": "'IBM Plex Mono', monospace", "fontSize": "12px",
                                          "color": "var(--body-text)"}),
            html.Div(
                style={"display": "flex", "flexDirection": "column", "gap": "2px", "minWidth": "0"},
                children=[
                    html.Span(r["member"], style=_CLICKABLE_NAME_STYLE),
                    html.Span(r["chamber"], style={"fontSize": "12px", "color": "var(--body-text)"}),
                ],
            ),
            html.Span(_fmt_amount(r["net_estimated_value"]),
                      style={"fontFamily": "'IBM Plex Mono', monospace", "color": "var(--text)"}),
        ],
    )


@app.callback(
    Output("recent-trades-records", "data"),
    Output("congress-leaderboard-records", "data"),
    Output("recent-trades-visible-count", "data"),
    Output("congress-leaderboard-visible-count", "data"),
    Input("politician-panel", "children"),
    prevent_initial_call=True,
)
def load_activity_summary(_panel_children):
    # Fires as soon as the Politicians panel itself exists (build_politician_panel
    # above) rather than on a fixed delay -- there used to be an 800ms
    # dcc.Interval here, there to let the page settle before this kicked
    # off its own fetch during the old eager page-load. That rationale's
    # gone now that this panel only gets built on first visit in the
    # first place; a fixed delay on top of that would just be pure added
    # latency. Reads the repo-committed snapshot rather than scraping/
    # parsing PTR filings live -- that live build was OOM-killing the
    # production instance even serialized to one at a time. See
    # congress_trades.load_activity_summary_snapshot and
    # build_congress_snapshot.py for how the snapshot gets refreshed.
    #
    # Only stores the raw record lists here -- building all ~230 of them
    # into actual row components (render_trade_rows/render_leaderboard_rows
    # below) in one shot was itself the real cost (~20s of Dash-renderer
    # work confirmed via CPU profiling, not the data load), so that's
    # deferred to a growing prefix instead. See the scroll-poll clientside
    # callbacks below for how that prefix grows.
    try:
        summary = load_activity_summary_snapshot()
    except (OSError, json.JSONDecodeError):
        return [], [], _ACTIVITY_PAGE_SIZE, _ACTIVITY_PAGE_SIZE
    recent = [_recent_trade_row_to_record(t) for t in summary["recent_trades"][:150]]
    leaderboard = [_leaderboard_row_to_record(e) for e in summary["leaderboard"][:100]]
    return recent, leaderboard, _ACTIVITY_PAGE_SIZE, _ACTIVITY_PAGE_SIZE


@app.callback(
    Output("recent-trades-container", "children"),
    Input("recent-trades-visible-count", "data"),
    State("recent-trades-records", "data"),
    prevent_initial_call=True,
)
def render_trade_rows(visible_count, records):
    return [_build_trade_row(r, i) for i, r in enumerate((records or [])[:visible_count])]


@app.callback(
    Output("congress-leaderboard-container", "children"),
    Input("congress-leaderboard-visible-count", "data"),
    State("congress-leaderboard-records", "data"),
    prevent_initial_call=True,
)
def render_leaderboard_rows(visible_count, records):
    # Rank stays correct as visible_count grows since the slice always
    # starts at 0 -- enumerate's index is always the true rank.
    return [_build_leaderboard_row(r, i + 1) for i, r in enumerate((records or [])[:visible_count])]


# Infinite scroll for Recent Trades/Leaderboard -- same poll-not-listener
# reasoning as the All Equity Positions table's scroll-poll-interval
# (search that id above): a listener attached directly to these cards'
# contents would go stale once render_trade_rows/render_leaderboard_rows
# replace them. Simpler selector than that table's version needs, though
# -- the scrollable element here is just this card's own plain
# overflowY:auto div (_politician_tracker_children above), not a
# DataTable's internal, unpredictably-classed wrapper.
app.clientside_callback(
    """
    function(_n_intervals, visibleCount, records) {
        if (!records || !records.length || visibleCount >= records.length) {
            return window.dash_clientside.no_update;
        }
        var container = document.getElementById("recent-trades-container");
        if (!container) return window.dash_clientside.no_update;
        var scroller = container.parentElement;
        // A hidden ancestor (switched away to another tab) collapses
        // scrollTop/clientHeight/scrollHeight all to 0, which satisfies
        // the nearBottom inequality trivially (0 >= -80) -- without this
        // guard the poll silently grows to the full row count every time
        // the tab is hidden, defeating the whole point of windowed
        // rendering for whoever switches away and back (confirmed via
        // CPU/row-count investigation: 30 -> 150 rows in ~7.5s unseen).
        if (scroller.clientHeight === 0) return window.dash_clientside.no_update;
        var nearBottom = (scroller.scrollTop + scroller.clientHeight) >= (scroller.scrollHeight - 80);
        if (nearBottom) return Math.min(visibleCount + %(page_size)d, records.length);
        return window.dash_clientside.no_update;
    }
    """ % {"page_size": _ACTIVITY_PAGE_SIZE},
    Output("recent-trades-visible-count", "data", allow_duplicate=True),
    Input("recent-trades-scroll-poll", "n_intervals"),
    State("recent-trades-visible-count", "data"),
    State("recent-trades-records", "data"),
    prevent_initial_call=True,
)
app.clientside_callback(
    """
    function(_n_intervals, visibleCount, records) {
        if (!records || !records.length || visibleCount >= records.length) {
            return window.dash_clientside.no_update;
        }
        var container = document.getElementById("congress-leaderboard-container");
        if (!container) return window.dash_clientside.no_update;
        var scroller = container.parentElement;
        // See the matching guard in the recent-trades-scroll-poll callback
        // above -- same hidden-tab false positive.
        if (scroller.clientHeight === 0) return window.dash_clientside.no_update;
        var nearBottom = (scroller.scrollTop + scroller.clientHeight) >= (scroller.scrollHeight - 80);
        if (nearBottom) return Math.min(visibleCount + %(page_size)d, records.length);
        return window.dash_clientside.no_update;
    }
    """ % {"page_size": _ACTIVITY_PAGE_SIZE},
    Output("congress-leaderboard-visible-count", "data", allow_duplicate=True),
    Input("congress-leaderboard-scroll-poll", "n_intervals"),
    State("congress-leaderboard-visible-count", "data"),
    State("congress-leaderboard-records", "data"),
    prevent_initial_call=True,
)


@app.callback(
    Output("pending-member-selection", "data", allow_duplicate=True),
    Output("scroll-to-lookup-trigger", "data", allow_duplicate=True),
    Input({"type": "trade-row", "chamber": ALL, "last": ALL, "first": ALL, "idx": ALL}, "n_clicks"),
    Input({"type": "leaderboard-row", "chamber": ALL, "last": ALL, "first": ALL}, "n_clicks"),
    prevent_initial_call=True,
)
def select_member_from_summary(trade_clicks, leaderboard_clicks):
    # Routes through pending-member-selection unconditionally -- same vs.
    # cross chamber is entirely apply_pending_politician_selection's call
    # now (it reads politician-chamber-tabs' current value itself, safely,
    # since it only acts once the Politicians panel exists). This
    # callback previously wrote directly to politician-chamber-tabs/
    # politician-input for the same-chamber case, which was a latent bug
    # predating today's fixes: trade-row/leaderboard-row (this callback's
    # own Inputs) live entirely inside the lazily-built Politicians
    # panel, so the same-chamber branch's direct writes -- reached
    # whenever this fired at all, including the harmless all-zero-clicks
    # case the guard below exists for -- would target not-yet-existing
    # components before the panel's first visit, silently discarding
    # this callback's entire response (same failure mode documented on
    # select_global_search_result above).
    if not any(trade_clicks or []) and not any(leaderboard_clicks or []):
        raise PreventUpdate  # fires with all-zero clicks whenever either row list re-renders
    target = ctx.triggered_id
    chamber = target["chamber"]
    return f"{chamber}|{target['last']}|{target['first']}", "politician"


# Politician position counts are small enough (tens of tickers, not
# thousands like a large 13F filer) that there's no need for the manager
# tracker's incremental-load/virtualization machinery — just filter the
# already-fully-loaded set server-side and let the table's native sort
# handle column sorting. Filters only take effect on "Calculate" -- filter
# fields are State here, not Input. "Clear Filters" is handled entirely by
# clear_politician_filters below rather than also being an Input here:
# that callback and this one would be two independent requests fired from
# the same click with no ordering guarantee between them, and this one
# reads the filter fields' values as State -- if it happened to run before
# the other callback's cleared values reached the browser, it would filter
# by the stale (pre-clear) values instead of showing everything.
@app.callback(
    Output("politician-positions-table", "data", allow_duplicate=True),
    Input("calculate-politician-filters-btn", "n_clicks"),
    *[State(f"pol-filter-{field}-min", "value") for field in _POLITICIAN_FILTER_FIELDS],
    *[State(f"pol-filter-{field}-max", "value") for field in _POLITICIAN_FILTER_FIELDS],
    State("politician-positions-full", "data"),
    prevent_initial_call=True,
)
def filter_politician_positions(_calculate_clicks, *args):
    n = len(_POLITICIAN_FILTER_FIELDS)
    mins = args[:n]
    maxs = args[n:2 * n]
    full_data = args[2 * n]
    if not full_data:
        return no_update
    rows = []
    for row in full_data:
        keep = True
        for field, lo, hi in zip(_POLITICIAN_FILTER_FIELDS, mins, maxs):
            v = row.get(field)
            if lo is not None and not (v >= lo):
                keep = False
                break
            if hi is not None and not (v <= hi):
                keep = False
                break
        if keep:
            rows.append(row)
    return rows


@app.callback(
    [Output(f"pol-filter-{field}-min", "value") for field in _POLITICIAN_FILTER_FIELDS]
    + [Output(f"pol-filter-{field}-max", "value") for field in _POLITICIAN_FILTER_FIELDS]
    + [Output("politician-positions-table", "data", allow_duplicate=True)],
    Input("clear-politician-filters-btn", "n_clicks"),
    State("politician-positions-full", "data"),
    prevent_initial_call=True,
)
def clear_politician_filters(_n_clicks, full_data):
    # sort_action="native" on this table means it re-applies whatever
    # column sort is active to new data on its own -- no need to
    # replicate that here the way clear_position_filters (manager tracker,
    # which uses sort_action="custom") has to.
    return [None] * (len(_POLITICIAN_FILTER_FIELDS) * 2) + [full_data or []]


_rss_at_ready = _rss_mb()
print(f"Worker ready: dash_app imported in {time.perf_counter() - _IMPORT_STARTED:.1f}s"
      f" | rss: {f'{_rss_at_ready:.0f}MB' if _rss_at_ready else 'n/a'}", flush=True)

if __name__ == "__main__":
    # threaded=True matters here specifically: building the cross-chamber
    # activity summary (see congress_trades.build_activity_summary) is a
    # one-time ~1-2 minute call, and without threading it would block the
    # single-threaded dev server -- freezing every other tab/user -- for
    # that whole window instead of just showing a loading state on the
    # two tables that need it.
    app.run(debug=True, threaded=True)
