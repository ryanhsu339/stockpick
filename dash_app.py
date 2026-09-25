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

import io
import json
import re
from datetime import date

import pandas as pd
import plotly.graph_objects as go
import requests
from dash import ALL, Dash, Input, Output, State, ctx, dash_table, dcc, html, no_update
from dash.dash_table.Format import Format, Scheme, Sign
from dash.exceptions import PreventUpdate

from company_growth_calc import (
    CompanyDataError,
    CompanyLookupError,
    compute_dcf,
    fetch_balance_sheet_data,
    fetch_cash_flow_data,
    fetch_growth_data,
    fetch_income_statement_data,
    load_ticker_map,
    resolve_company,
    search_companies,
)
from stock_price import HoldingsDataError, PriceDataError, RANGE_KEYS, fetch_price_history, fetch_top_holdings
from thirteenf import (
    FilingDataError,
    ManagerLookupError,
    fetch_manager_comparison,
    fetch_manager_comparison_by_cik,
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
_PRICE_UP_COLOR = "#22c55e"
_PRICE_DOWN_COLOR = "#ef4444"
_CHART_SURFACE = "#1c1c1c"
_CHART_GRIDLINE = "#333333"
_CHART_AXIS_LINE = "#444444"
_CHART_MUTED_TEXT = "#9a9a9a"
_CHART_PRIMARY_TEXT = "#ffffff"


_CHART_HEIGHT = 420
_VOLUME_COLOR = "rgba(137,135,129,0.45)"  # muted ink, translucent — a neutral
                                           # magnitude cue that doesn't compete
                                           # with the price line's up/down color


def empty_price_figure(message="Enter a ticker and click Generate to load a chart."):
    fig = go.Figure()
    fig.update_layout(
        height=_CHART_HEIGHT,
        paper_bgcolor=_CHART_SURFACE,
        plot_bgcolor=_CHART_SURFACE,
        margin=dict(l=20, r=20, t=20, b=20),
        xaxis=dict(visible=False),
        yaxis=dict(visible=False),
        annotations=[dict(text=message, showarrow=False,
                           font=dict(color=_CHART_MUTED_TEXT, size=14))],
    )
    return fig


def build_price_figure(df, ticker, range_key):
    closes = df["Close"]
    volume = df["Volume"]
    first, last = float(closes.iloc[0]), float(closes.iloc[-1])
    change = last - first
    pct = (change / first * 100) if first else 0.0
    up = change >= 0
    color = _PRICE_UP_COLOR if up else _PRICE_DOWN_COLOR
    # A flat translucent fill read as a barely-there tint on the old light
    # card; against the dark card it needs more opacity to actually look
    # "highlighted" rather than washing out to the background color.
    fill_color = "rgba(34,197,94,0.18)" if up else "rgba(239,68,68,0.18)"
    sign = "+" if change >= 0 else ""

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
            hovertemplate="Vol %{y:,.0f}<extra></extra>",
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
        marker=dict(size=14, color="rgba(255,255,255,0.35)"),
        hoverinfo="skip",
        showlegend=False,
    ))
    fig.add_trace(go.Scatter(
        x=[last_x], y=[last],
        mode="markers",
        name="_pulse_dot",
        marker=dict(size=8, color="#ffffff", line=dict(width=1.5, color=color)),
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

    # Ticker + current price large and bold on their own line; the
    # change and timeframe (smaller, change colored by up/down) on a
    # second line below, matching a typical broker-app price header.
    title_text = (
        f"<b>{ticker}  ${last:,.2f}</b><br>"
        f"<span style='font-size:14px'>"
        f"<span style='color:{color}'>{sign}{change:,.2f} ({sign}{pct:.2f}%)</span>"
        f" · {range_key}"
        f"</span>"
    )
    fig.update_layout(
        title=dict(
            text=title_text,
            font=dict(size=24, color=_CHART_PRIMARY_TEXT),
            x=0, xanchor="left",
        ),
        margin=dict(l=10, r=10, t=75, b=30),
        height=_CHART_HEIGHT,
        paper_bgcolor=_CHART_SURFACE,
        plot_bgcolor=_CHART_SURFACE,
        showlegend=False,
        hovermode="x unified",
        # Without uirevision, Dash's Plotly.react treats every 15s refresh
        # (or 60s DCF price tick) as a brand-new figure and resets zoom/pan;
        # keeping it constant per ticker+range lets Plotly diff the traces
        # instead and animate between old/new points rather than popping.
        uirevision=f"{ticker}-{range_key}",
        # Plotly's hover box defaults to a light background with dark
        # text -- fine on the old light card, unreadable-low-contrast on
        # this dark one, so it needs its own explicit dark/white styling.
        hoverlabel=dict(bgcolor="#2a2a2a", bordercolor=_CHART_AXIS_LINE,
                         font=dict(color="#ffffff", size=12)),
        bargap=0.2,
        xaxis=dict(
            showgrid=False, showline=True, linecolor=_CHART_AXIS_LINE,
            tickfont=dict(color=_CHART_MUTED_TEXT, size=11),
            showspikes=True, spikemode="across", spikesnap="cursor",
            spikedash="dot", spikethickness=1, spikecolor=_CHART_MUTED_TEXT,
            rangebreaks=rangebreaks,
        ),
        yaxis=dict(
            showgrid=True, gridcolor=_CHART_GRIDLINE, zeroline=False,
            tickfont=dict(color=_CHART_MUTED_TEXT, size=11),
            tickprefix="$", side="right", range=y_range, autorange=False,
        ),
        yaxis2=dict(
            overlaying="y", side="left", showticklabels=False,
            showgrid=False, zeroline=False,
            range=[0, vol_max * 4] if has_volume else None,
        ),
    )
    return fig


# Arbitrary category colors (not the single-stock chart's up/down green/red,
# which wouldn't distinguish two tickers moving the same direction) -- a
# cool blue and a warm amber read clearly against the dark card and against
# each other.
_COMPARE_COLOR_1 = "#4A90D9"
_COMPARE_COLOR_2 = "#F2A93B"


def build_compare_price_figure(df1, ticker1, df2, ticker2, range_key):
    """Overlay both tickers' price as cumulative % change from the first
    point in range, rather than raw price -- the two are almost never
    anywhere near the same price level, so plotting raw $ would just show
    two flat-looking lines at wildly different heights instead of a
    comparable growth trend. No volume bars (would be visually messy
    overlaid for two tickers, and the point of this view is the trend, not
    the volume)."""
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
        # The running %-change lives in the legend label itself (Plotly
        # colors each entry to match its trace automatically) rather than
        # a separate title -- a title positioned in the same top margin as
        # a top-anchored legend fights it for space and the two overlap.
        fig.add_trace(go.Scatter(
            x=closes.index, y=pct_series.values,
            name=f"{ticker}  {sign}{last_pct:.2f}%",
            mode="lines",
            line=dict(width=2, color=color, shape="linear"),
            hovertemplate="%{y:+.2f}%<extra>" + ticker + "</extra>",
        ))
        last_x = closes.index[-1]
        fig.add_trace(go.Scatter(
            x=[last_x], y=[last_pct],
            mode="markers",
            name=f"_pulse_dot_{ticker}",
            marker=dict(size=8, color=color, line=dict(width=1.5, color="#ffffff")),
            hoverinfo="skip",
            showlegend=False,
        ))

    fig.add_hline(y=0, line=dict(color=_CHART_MUTED_TEXT, dash="dot", width=1))

    fig.update_layout(
        margin=dict(l=10, r=10, t=50, b=30),
        height=_CHART_HEIGHT,
        paper_bgcolor=_CHART_SURFACE,
        plot_bgcolor=_CHART_SURFACE,
        showlegend=True,
        legend=dict(orientation="h", x=0, y=1.12, font=dict(color=_CHART_PRIMARY_TEXT, size=14)),
        hovermode="x unified",
        uirevision=f"{ticker1}-{ticker2}-{range_key}",
        hoverlabel=dict(bgcolor="#2a2a2a", bordercolor=_CHART_AXIS_LINE,
                         font=dict(color="#ffffff", size=12)),
        xaxis=dict(
            showgrid=False, showline=True, linecolor=_CHART_AXIS_LINE,
            tickfont=dict(color=_CHART_MUTED_TEXT, size=11),
            showspikes=True, spikemode="across", spikesnap="cursor",
            spikedash="dot", spikethickness=1, spikecolor=_CHART_MUTED_TEXT,
            rangebreaks=rangebreaks,
        ),
        yaxis=dict(
            showgrid=True, gridcolor=_CHART_GRIDLINE, zeroline=False,
            tickfont=dict(color=_CHART_MUTED_TEXT, size=11),
            ticksuffix="%", side="right",
        ),
    )
    return fig


_DCF_REVENUE_BAR_COLOR = "#7a7f87"  # neutral grey -- Revenue, historical + forecast
_DCF_FCF_BAR_COLOR = "#2874a6"      # theme blue -- FCF, historical + forecast


def build_dcf_chart(historical, base_revenue, base_fcf, growth_rate, years):
    """Grouped Revenue/FCF bars: actuals from `historical` (see
    _dcf_defaults) followed by a projection grown at `growth_rate` for
    `years` years from base_revenue/base_fcf, with the forecast region
    shaded and labeled -- both series share the one growth-rate input,
    a simplification (the FCF projection compute_dcf actually discounts
    is identical to this chart's FCF bars; the Revenue bars are for
    context only and don't otherwise feed the valuation math).
    """
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
            fillcolor="rgba(255,255,255,0.05)", line_width=0, layer="below",
        ))
        annotations.append(dict(
            x=(split + end) / 2, y=1.04, yref="paper", xref="x",
            text="FORECAST", showarrow=False,
            font=dict(color=_CHART_MUTED_TEXT, size=10),
        ))

    fig.update_layout(
        barmode="group",
        height=_CHART_HEIGHT,
        paper_bgcolor=_CHART_SURFACE, plot_bgcolor=_CHART_SURFACE,
        margin=dict(l=10, r=10, t=40, b=30),
        legend=dict(orientation="h", y=1.1, x=0, font=dict(color=_CHART_MUTED_TEXT, size=11)),
        hoverlabel=dict(bgcolor="#2a2a2a", bordercolor=_CHART_AXIS_LINE,
                         font=dict(color="#ffffff", size=12)),
        xaxis=dict(showgrid=False, showline=True, linecolor=_CHART_AXIS_LINE,
                   tickfont=dict(color=_CHART_MUTED_TEXT, size=11)),
        yaxis=dict(showgrid=True, gridcolor=_CHART_GRIDLINE, zeroline=False,
                   tickfont=dict(color=_CHART_MUTED_TEXT, size=11),
                   tickprefix="$", ticksuffix="B"),
        shapes=shapes,
        annotations=annotations,
    )
    return fig


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
                _dcf_valuation_bar("DCF Value", dcf_pct, _HEADER_COLOR),
                _dcf_valuation_bar(f"OVERVALUATION {diff_pct_label}", 100 - dcf_pct,
                                    _PRICE_DOWN_COLOR, align="right"),
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
            children=[_dcf_valuation_bar("DCF Value", 100, _HEADER_COLOR)],
        )
        price_row = html.Div(
            style={"display": "flex", "height": "22px", "borderRadius": "4px", "overflow": "hidden",
                   "marginTop": "6px"},
            children=[
                _dcf_valuation_bar(f"Price  ${current_price:,.2f}", price_pct, "#555555"),
                _dcf_valuation_bar(f"UNDERVALUATION {diff_pct_label}", 100 - price_pct,
                                    _PRICE_UP_COLOR, align="right"),
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
        style={"minWidth": "300px", "maxWidth": "320px", "backgroundColor": "#161616",
               "borderRadius": "10px", "padding": "16px", "flex": "0 0 auto"},
        children=[
            html.Div(f"{ticker} DCF Value", style={"color": "#ffffff", "fontWeight": "700", "fontSize": "14px"}),
            html.Div("Base Case", style={"color": _CHART_MUTED_TEXT, "fontSize": "12px", "marginBottom": "8px"}),
            html.Div(f"${fair_value:,.2f}", style={"color": _HEADER_COLOR, "fontWeight": "800",
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


# Overall page theme: dark grey background, accent-colored section headers.
_PAGE_BG = "#000000"
_HEADER_COLOR = "#2874a6"
# A brighter tint of _HEADER_COLOR for the security/issuer identifier column
# every table highlights in its first column (ticker, issuer, member, line
# item...) -- at _HEADER_COLOR's darker blue that text read as low-contrast
# against the dark card background.
_SECURITY_TEXT_COLOR = "#5DADE2"
_BODY_TEXT_COLOR = "#cfcfcf"
_HEADER_STYLE = {"color": _HEADER_COLOR}
_PARA_STYLE = {"color": _BODY_TEXT_COLOR}
# DataTables keep their own light "card" background regardless of the dark
# page behind them, so their cell text needs an explicit dark color rather
# than inheriting the page's light default (which would wash out unreadable
# on the table's white cells).
_TABLE_CELL_STYLE = {"padding": "6px 10px", "fontSize": "14px", "textAlign": "right", "color": "#0b0b0b"}
_TABLE_HEADER_STYLE = {"fontWeight": "bold", "backgroundColor": "#f4f4f4", "color": "#0b0b0b"}

# Compact pill-style nav bar (fits its content instead of stretching full
# width) shared by the range picker and the statement-view tabs. Track is
# the same blue as the section headers; the selected tab gets a translucent
# white overlay rather than swapping to a different text color, so both
# selected and unselected labels stay white.
_NAV_CONTAINER_STYLE = {
    "display": "inline-flex",
    "gap": "4px",
    "backgroundColor": _HEADER_COLOR,
    "padding": "4px",
    "borderRadius": "10px",
    "marginTop": "16px",
    "border": "none",
}
_NAV_TAB_STYLE = {
    "padding": "8px 16px",
    "border": "none",
    "borderBottom": "none",
    "borderRadius": "8px",
    "fontSize": "13px",
    "fontWeight": "600",
    "color": "#ffffff",
    "backgroundColor": "transparent",
    "flex": "initial",
}
_NAV_TAB_SELECTED_STYLE = {
    **_NAV_TAB_STYLE,
    "fontWeight": "700",
    "color": "#ffffff",
    "backgroundColor": "rgba(255,255,255,0.22)",
    "boxShadow": "0 1px 2px rgba(0,0,0,0.15)",
}
# Financials tabs (Growth Rates/Income/Balance/Cash Flow) don't apply to
# ETFs/funds (no 10-K data); Top Holdings only applies to funds. The
# Financials view-tabs callback toggles between these two per search.
_NAV_TAB_HIDDEN_STYLE = {"display": "none"}
# Dark-grey variant of the pill container -- used for the Single
# Stock/Compare toggle, which sits next to the page's own blue accent
# header and would otherwise blend into every other blue pill on the page.
_NAV_CONTAINER_STYLE_GREY = {**_NAV_CONTAINER_STYLE, "backgroundColor": "#3a3a3a"}

# Top-level app nav (Public Company Tracker vs Investment Manager Tracker):
# same pill treatment, just a little larger since it's the primary nav.
_APP_TAB_STYLE = {**_NAV_TAB_STYLE, "padding": "10px 20px", "fontSize": "14px"}
_APP_TAB_SELECTED_STYLE = {**_NAV_TAB_SELECTED_STYLE, "padding": "10px 20px", "fontSize": "14px"}

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
    "backgroundColor": "#ffffff",
    "border": "1px solid #e1e0d9",
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
    "borderBottom": "1px solid #f0efec",
    "backgroundColor": "#ffffff",
    "color": "#0b0b0b",
    "cursor": "pointer",
    "fontSize": "13px",
}


def _financials_valuation_block(suffix, mirror=False):
    """One ticker's Financials tabs + Valuation/DCF panel. Built once for
    the primary ticker (suffix="") and again for the Compare panel
    (suffix="-2"); every id in here gets that suffix so both copies can
    live in the DOM at once with independent callbacks. mirror=True (the
    Compare side) puts the DCF Assumptions panel after the chart instead
    of before it, so the two side-by-side panels mirror each other
    (assumptions hug the middle seam on both sides) rather than both
    having assumptions on the left."""
    dcf_assumptions_panel = html.Div(
        style=_FILTER_PANEL_STYLE,
        children=[
            html.Div("DCF Assumptions", style={"color": _HEADER_COLOR, "fontWeight": "700",
                                                 "marginBottom": "10px"}),
            *[
                html.Div(
                    style={"marginBottom": "10px"},
                    children=[
                        html.Label(label, style={"color": _BODY_TEXT_COLOR, "fontSize": "12px",
                                                  "display": "block", "marginBottom": "3px"}),
                        dcc.Input(id=f"dcf-{field}{suffix}", type="number",
                                  style=_FILTER_INPUT_STYLE),
                    ],
                )
                for field, label in _DCF_INPUT_FIELDS[:3]
            ],
            html.Div("Model Settings", style={"color": _HEADER_COLOR, "fontWeight": "700",
                                                "marginTop": "14px", "marginBottom": "10px",
                                                "borderTop": "1px solid #333333", "paddingTop": "12px"}),
            *[
                html.Div(
                    style={"marginBottom": "10px"},
                    children=[
                        html.Label(label, style={"color": _BODY_TEXT_COLOR, "fontSize": "12px",
                                                  "display": "block", "marginBottom": "3px"}),
                        dcc.Input(id=f"dcf-{field}{suffix}", type="number",
                                  style=_FILTER_INPUT_STYLE),
                    ],
                )
                for field, label in _DCF_INPUT_FIELDS[3:5]
            ],
            html.Div("Market Data", style={"color": _HEADER_COLOR, "fontWeight": "700",
                                             "marginTop": "14px", "marginBottom": "10px",
                                             "borderTop": "1px solid #333333", "paddingTop": "12px"}),
            *[
                html.Div(
                    style={"marginBottom": "10px"},
                    children=[
                        html.Label(label, style={"color": _BODY_TEXT_COLOR, "fontSize": "12px",
                                                  "display": "block", "marginBottom": "3px"}),
                        dcc.Input(id=f"dcf-{field}{suffix}", type="number",
                                  style=_FILTER_INPUT_STYLE),
                    ],
                )
                for field, label in _DCF_INPUT_FIELDS[5:]
            ],
            html.Button("Calculate", id=f"calculate-dcf-btn{suffix}", n_clicks=0,
                        style={"width": "100%", "fontSize": "12px"}),
        ],
    )
    dcf_chart_column = html.Div(
        style={"flex": "1", "minWidth": "0"},
        children=[
            dcc.Graph(
                id=f"dcf-chart{suffix}",
                figure=empty_price_figure("Enter a ticker and click Generate to load a DCF."),
                config={"displayModeBar": False},
            ),
            html.Details(
                style={"marginTop": "20px"},
                children=[
                    html.Summary("View Calculation", style={"color": _HEADER_COLOR, "cursor": "pointer",
                                                              "fontSize": "13px", "fontWeight": "600"}),
                    html.Div(
                        style={"marginTop": "12px"},
                        children=[
                            dash_table.DataTable(
                                id=f"dcf-table{suffix}",
                                columns=[{"name": "Line Item", "id": "line"}],
                                data=[],
                                cell_selectable=False,
                                style_table={"overflowX": "auto"},
                                style_cell=_TABLE_CELL_STYLE,
                                style_cell_conditional=[
                                    {"if": {"column_id": "line"}, "textAlign": "left",
                                     "fontWeight": "600"},
                                ],
                                style_header=_TABLE_HEADER_STYLE,
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
                                    style_table={"overflowX": "auto"},
                                    style_cell=_TABLE_CELL_STYLE,
                                    style_cell_conditional=[
                                        {"if": {"column_id": "metric"}, "textAlign": "left",
                                         "fontWeight": "600"},
                                    ],
                                    style_header=_TABLE_HEADER_STYLE,
                                ),
                            ),
                        ],
                    ),
                ],
            ),
        ],
    )
    dcf_row_children = ([dcf_chart_column, dcf_assumptions_panel] if mirror
                         else [dcf_assumptions_panel, dcf_chart_column])

    return [
        html.Div(
            style={"display": "flex", "justifyContent": "space-between", "alignItems": "flex-end",
                   "marginTop": "40px", "flexWrap": "wrap", "gap": "12px"},
            children=[
                html.H3("Financials", style=_HEADER_STYLE),
                html.Button("Download Excel", id=f"download-btn{suffix}", n_clicks=0, disabled=True),
            ],
        ),
        dcc.Download(id=f"download-csv{suffix}"),
        dcc.Tabs(
            id=f"view-tabs{suffix}",
            value="growth",
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
                                     "rule": "color: #ffffff !important;"},
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
                                             "border-color: #333333 !important; "
                                             "outline-color: #333333 !important;"},
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
        html.H3("Valuation", style={**_HEADER_STYLE, "marginTop": "40px"}),
        html.P("A simple discounted cash flow model: projects Free Cash Flow forward at the "
               "growth rate below, discounts each year back to present value, and adds a "
               "discounted terminal value to estimate Enterprise and per-share fair value. "
               "Inputs default from the company's own financials (where available) but are "
               "yours to adjust — click Calculate to re-run with your changes.",
               style={**_PARA_STYLE, "fontSize": "13px"}),
        dcc.Interval(id=f"dcf-price-refresh{suffix}", interval=60000, n_intervals=0),
        dcc.Loading(
            type="default",
            children=html.Div(
                id=f"dcf-banner{suffix}",
                style={"backgroundColor": _MANAGER_TABLE_BG, "borderRadius": "10px",
                       "padding": "20px", "marginTop": "8px"},
            ),
        ),
        html.Div(
            style={"display": "flex", "gap": "16px", "alignItems": "flex-start", "marginTop": "16px"},
            children=dcf_row_children,
        ),
        dcc.Store(id=f"rows-store{suffix}"),
        dcc.Store(id=f"suppress-next-suggestions{suffix}", data=False),
        dcc.Store(id=f"dcf-defaults-store{suffix}", data=None),
    ]


def _company_tracker_children():
    return [
        html.Div(
            style={"display": "flex", "justifyContent": "space-between", "alignItems": "center",
                   "flexWrap": "wrap", "gap": "12px"},
            children=[
                html.H2("Public Company Tracker", style=_HEADER_STYLE),
                # Two-segment toggle (same pill styling as the range picker
                # and Financials tabs elsewhere) rather than a single button
                # whose label swaps -- both modes stay visible so it reads
                # as a toggle, not an action. sync_compare_mode below turns
                # its selected value into the "compare-mode" store every
                # other callback in Compare mode actually keys off.
                dcc.Tabs(
                    id="compare-mode-tabs",
                    value="single",
                    style=_NAV_CONTAINER_STYLE_GREY,
                    children=[
                        dcc.Tab(label="Single Stock", value="single", style=_NAV_TAB_STYLE,
                                selected_style=_NAV_TAB_SELECTED_STYLE),
                        dcc.Tab(label="Compare", value="compare", style=_NAV_TAB_STYLE,
                                selected_style=_NAV_TAB_SELECTED_STYLE),
                    ],
                ),
            ],
        ),
        dcc.Store(id="compare-mode", data=False),
        html.P("Sales, earnings, equity, cash, and ROIC growth from SEC 10-K XBRL data.",
               style=_PARA_STYLE),
        html.Div(
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
                                  style={"width": "220px", "display": "block", "color": "#0b0b0b"}),
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
                                  style={"width": "220px", "display": "block", "color": "#0b0b0b"}),
                        html.Div(id="company-suggestions-2", style=_SUGGESTIONS_CONTAINER_STYLE),
                    ],
                ),
            ],
        ),
        dcc.Loading(
            type="default",
            children=html.Div(id="status-msg", style={"marginTop": "16px", "whiteSpace": "pre-wrap"}),
        ),
        html.Div(id="company-candidates", style={"marginTop": "8px"}),
        html.Div(
            id="compare-status-wrap",
            style={"display": "none"},
            children=[
                dcc.Loading(
                    type="default",
                    children=html.Div(id="status-msg-2", style={"marginTop": "8px", "whiteSpace": "pre-wrap"}),
                ),
                html.Div(id="company-candidates-2", style={"marginTop": "8px"}),
            ],
        ),
        html.H3("Stock Price", style={**_HEADER_STYLE, "marginTop": "40px"}),
        dcc.Tabs(
            id="range-tabs",
            value="6M",
            style=_NAV_CONTAINER_STYLE,
            children=[dcc.Tab(label=k, value=k, style=_NAV_TAB_STYLE,
                               selected_style=_NAV_TAB_SELECTED_STYLE)
                      for k in RANGE_KEYS],
        ),
        dcc.Interval(id="price-chart-refresh", interval=15000, n_intervals=0),
        # Drives the live-price dot's pulsing halo -- deliberately not wired
        # to update_price_chart; a clientside callback below just tweaks the
        # existing figure's marker opacity in place (see build_price_figure)
        # rather than round-tripping to the server every ~1s.
        dcc.Interval(id="price-pulse-interval", interval=900, n_intervals=0),
        html.Div(id="price-pulse-sink", style={"display": "none"}),
        # No dcc.Loading here (unlike elsewhere in this app): its spinner
        # overlay would flash over the whole chart on every 15s auto-refresh,
        # which is the "blinks when it refreshes" behavior -- the chart swap
        # itself is fast enough not to need loading feedback.
        html.Div(
            dcc.Graph(
                id="price-chart",
                figure=empty_price_figure(),
                config={"displayModeBar": False},
            ),
            style={"marginTop": "16px"},
        ),
        html.Div(
            id="financials-columns",
            style={"display": "flex", "gap": "32px", "alignItems": "flex-start"},
            children=[
                html.Div(style={"flex": "1", "minWidth": "0"},
                         children=_financials_valuation_block("", mirror=False)),
                # Hidden until Compare mode is on (see render_compare_mode).
                html.Div(id="financials-col-2", style={"flex": "1", "minWidth": "0", "display": "none"},
                         children=_financials_valuation_block("-2", mirror=True)),
            ],
        ),
    ]


EMPTY_COLS = [{"name": "Breakdown", "id": "line"}]

# All Equity Positions loads incrementally (see the scroll-poll-interval
# clientside callbacks below) rather than sending every holding to the
# table at once, since some managers file thousands of positions.
_POSITIONS_PAGE_SIZE = 100

# Between-filters for the All Equity Positions table: (field, label) pairs,
# each rendered as a Min/Max number-input pair to the table's left.
_POSITION_FILTERS = [
    ("shares_m", "Shares (MM)"),
    ("prev_shares_m", "Prev Shares (MM)"),
    ("value_m", "Value ($MM)"),
    ("prev_value_m", "Prev Value ($MM)"),
    ("portfolio_pct", "Portfolio %"),
    ("prev_portfolio_pct", "Prev Portfolio %"),
]
_FILTER_PANEL_STYLE = {
    "minWidth": "180px", "maxWidth": "180px",
    "backgroundColor": "#1a1a1a", "borderRadius": "8px",
    "padding": "12px", "flex": "0 0 auto",
}
_FILTER_INPUT_STYLE = {"width": "100%", "fontSize": "12px", "color": "#0b0b0b", "boxSizing": "border-box"}

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
_DELTA_SHARES_FORMAT = Format(precision=2, scheme=Scheme.fixed, sign=Sign.positive, group=True)
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
MANAGER_COLUMNS = [
    {"name": "Security", "id": "issuer"},
    {"name": "Shares (MM)", "id": "shares_m", "type": "numeric", "format": _MONEY_FORMAT},
    {"name": "Prev Shares (MM)", "id": "prev_shares_m", "type": "numeric", "format": _MONEY_FORMAT},
    {"name": "ΔShares (MM)", "id": "delta_shares_m", "type": "numeric", "format": _DELTA_SHARES_FORMAT},
    {"name": "~ΔShares Value ($MM)", "id": "delta_shares_value_m", "type": "numeric",
     "format": _DELTA_SHARES_VALUE_FORMAT},
    {"name": "Value (MM$)", "id": "value_m", "type": "numeric", "format": _MONEY_FORMAT},
    {"name": "Portfolio %", "id": "portfolio_pct", "type": "numeric", "format": _PORTFOLIO_PCT_FORMAT},
    {"name": "ΔPortfolio %", "id": "delta_pct", "type": "numeric", "format": _DELTA_PORTFOLIO_PCT_FORMAT},
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
_MANAGER_TABLE_BG = "#1c1c1c"
_MANAGER_TABLE_CELL_STYLE = {
    **_TABLE_CELL_STYLE,
    "backgroundColor": _MANAGER_TABLE_BG,
    "color": "#ffffff",
    "border": "none",
    "borderBottom": "1px solid #333333",
}
_MANAGER_TABLE_HEADER_STYLE = {
    "backgroundColor": _MANAGER_TABLE_BG,
    "color": "#ffffff",
    "fontWeight": "bold",
    "border": "none",
    "borderBottom": "1px solid #444444",
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
)
# Top Increases/Decreases sit side by side, but each is independently capped
# at top_n=10 rows and one side often has fewer positive/negative movers
# than the other -- a fixed height (rather than _MANAGER_TABLE_STYLE's
# content-sized default) keeps both cards the same height regardless of how
# many rows either one actually has.
_TOP_MOVES_TABLE_STYLE = {
    **_MANAGER_TABLE_STYLE,
    "style_table": {**_MANAGER_TABLE_STYLE["style_table"], "height": "420px", "overflowY": "auto"},
}
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
RECENT_TRADES_COLUMNS = [
    {"name": "Member", "id": "member"},
    {"name": "Chamber", "id": "chamber"},
    {"name": "Ticker", "id": "ticker"},
    {"name": "Type", "id": "transaction_type"},
    {"name": "Date", "id": "transaction_date"},
    {"name": "Amount Low ($)", "id": "amount_low", "type": "numeric", "format": _POL_MONEY_FORMAT},
    {"name": "Amount High ($)", "id": "amount_high", "type": "numeric", "format": _POL_MONEY_FORMAT},
]
CONGRESS_LEADERBOARD_COLUMNS = [
    {"name": "Member", "id": "member"},
    {"name": "Chamber", "id": "chamber"},
    {"name": "Est. Net Value ($)", "id": "net_estimated_value", "type": "numeric", "format": _POL_MONEY_FORMAT},
    {"name": "Transactions", "id": "transaction_count", "type": "numeric", "format": _POL_COUNT_FORMAT},
]
_ACTIVITY_COLUMN_WIDTHS = {
    "member": "170px",
    "chamber": "80px",
    "ticker": "80px",
    "transaction_type": "110px",
    "transaction_date": "100px",
    "amount_low": "120px",
    "amount_high": "120px",
    "net_estimated_value": "140px",
    "transaction_count": "110px",
}
_ACTIVITY_TABLE_STYLE = dict(
    style_table={"overflowX": "auto", "backgroundColor": _MANAGER_TABLE_BG,
                 "maxHeight": "420px", "overflowY": "auto"},
    style_cell=_MANAGER_TABLE_CELL_STYLE,
    style_cell_conditional=[
        {"if": {"column_id": "member"}, "textAlign": "left", "color": _SECURITY_TEXT_COLOR},
    ] + [
        {"if": {"column_id": col_id}, "minWidth": width, "width": width}
        for col_id, width in _ACTIVITY_COLUMN_WIDTHS.items()
    ],
    style_header=_MANAGER_TABLE_HEADER_STYLE,
    style_data={"backgroundColor": _MANAGER_TABLE_BG},
)
_ACTIVITY_WINDOW_NOTE = (
    "Covers filings from roughly the last 6 months across every current House member and "
    "senator. Does not include activity before elected to office therefore portfolio values "
    "may be negative."
)


def _manager_tracker_children():
    return [
        html.H2("Investment Manager Tracker", style=_HEADER_STYLE),
        html.P("Top holding increases and decreases quarter-over-quarter, from SEC 13F-HR filings.",
               style=_PARA_STYLE),
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
                                  style={"width": "260px", "display": "block", "color": "#0b0b0b"}),
                        html.Div(id="manager-suggestions", style=_SUGGESTIONS_CONTAINER_STYLE),
                    ],
                ),
            ],
        ),
        dcc.Loading(
            type="default",
            children=html.Div(id="manager-status-msg", style={"marginTop": "16px", "whiteSpace": "pre-wrap"}),
        ),
        html.Div(id="manager-candidates", style={"marginTop": "8px"}),
        html.Div(
            style={"display": "flex", "gap": "24px", "flexWrap": "wrap", "marginTop": "24px"},
            children=[
                html.Div(
                    style={"flex": "1 1 420px", "minWidth": "0"},
                    children=[
                        html.H3("Top Increases This Quarter", style=_HEADER_STYLE),
                        html.P(_MANAGER_NOTE, style={**_PARA_STYLE, "fontSize": "13px"}),
                        dash_table.DataTable(id="increases-table", columns=MANAGER_COLUMNS,
                                              data=[], cell_selectable=False,
                                              **_TOP_MOVES_TABLE_STYLE),
                    ],
                ),
                html.Div(
                    style={"flex": "1 1 420px", "minWidth": "0"},
                    children=[
                        html.H3("Top Decreases This Quarter", style=_HEADER_STYLE),
                        html.P(_MANAGER_NOTE, style={**_PARA_STYLE, "fontSize": "13px"}),
                        dash_table.DataTable(id="decreases-table", columns=MANAGER_COLUMNS,
                                              data=[], cell_selectable=False,
                                              **_TOP_MOVES_TABLE_STYLE),
                    ],
                ),
            ],
        ),
        html.Div(
            style={"display": "flex", "justifyContent": "space-between", "alignItems": "flex-end",
                   "marginTop": "40px", "flexWrap": "wrap", "gap": "12px"},
            children=[
                html.H3("All Equity Positions", style=_HEADER_STYLE),
                html.Button("Download CSV", id="download-positions-btn", n_clicks=0, disabled=True),
            ],
        ),
        dcc.Download(id="download-positions-csv"),
        html.P("Every current or prior-quarter holding, ranked by portfolio weight, with "
               "quarter-over-quarter share/value/allocation changes.",
               style={**_PARA_STYLE, "fontSize": "13px"}),
        html.Div(
            style={"display": "flex", "gap": "16px", "alignItems": "flex-start", "marginTop": "8px"},
            children=[
                html.Div(
                    style=_FILTER_PANEL_STYLE,
                    children=[
                        html.Div("Filters", style={"color": _HEADER_COLOR, "fontWeight": "700",
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
                                            dcc.Input(id=f"filter-{field}-min", type="number",
                                                      placeholder="Min", style=_FILTER_INPUT_STYLE),
                                            dcc.Input(id=f"filter-{field}-max", type="number",
                                                      placeholder="Max", style=_FILTER_INPUT_STYLE),
                                        ],
                                    ),
                                ],
                            )
                            for field, label in _POSITION_FILTERS
                        ],
                        html.Div(
                            style={"display": "flex", "gap": "6px"},
                            children=[
                                html.Button("Calculate", id="calculate-position-filters-btn", n_clicks=0,
                                            style={"flex": "1", "fontSize": "12px"}),
                                html.Button("Clear Filters", id="clear-position-filters-btn", n_clicks=0,
                                            style={"flex": "1", "fontSize": "12px"}),
                            ],
                        ),
                    ],
                ),
                html.Div(
                    style={"flex": "1", "minWidth": "0"},
                    children=dash_table.DataTable(
                        id="all-positions-table",
                        columns=ALL_POSITIONS_COLUMNS,
                        data=[],
                        cell_selectable=False,
                        page_action="none",
                        fixed_rows={"headers": True},
                        # "native" sort puts a null ΔShares/ΔValue % ("New"
                        # position) last regardless of ascending/descending
                        # — "custom" hands sorting to the clientside
                        # callback below instead, which treats null as
                        # larger than any number so New positions sort
                        # first when descending, last when ascending.
                        sort_action="custom",
                        sort_by=[],
                        **{**_MANAGER_TABLE_STYLE,
                           "style_table": {**_MANAGER_TABLE_STYLE["style_table"],
                                            "maxHeight": "600px", "overflowY": "auto"}},
                    ),
                ),
            ],
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


def _politician_tracker_children():
    options, default_value = _politician_dropdown_options("house")
    return [
        html.H2("Politician Tracker", style=_HEADER_STYLE),
        html.P("Buy/sell activity disclosed by members of Congress under the STOCK Act "
               "(Periodic Transaction Reports).", style=_PARA_STYLE),
        html.P(_ACTIVITY_WINDOW_NOTE, style={**_PARA_STYLE, "fontSize": "13px"}),
        dcc.Loading(
            type="default",
            children=html.Div(
                style={"display": "flex", "gap": "24px", "flexWrap": "wrap"},
                children=[
                    html.Div(
                        style={"flex": "1 1 420px", "minWidth": "0"},
                        children=[
                            html.H3("Most Recent Trades (All Members)", style=_HEADER_STYLE),
                            dash_table.DataTable(
                                id="recent-trades-table", columns=RECENT_TRADES_COLUMNS,
                                data=[], page_action="none",
                                fixed_rows={"headers": True}, sort_action="native",
                                **_ACTIVITY_TABLE_STYLE,
                            ),
                        ],
                    ),
                    html.Div(
                        style={"flex": "1 1 420px", "minWidth": "0"},
                        children=[
                            html.H3("Largest Est. Portfolio Value (All Members)", style=_HEADER_STYLE),
                            dash_table.DataTable(
                                id="congress-leaderboard-table", columns=CONGRESS_LEADERBOARD_COLUMNS,
                                data=[], page_action="none",
                                fixed_rows={"headers": True}, sort_action="native",
                                **_ACTIVITY_TABLE_STYLE,
                            ),
                        ],
                    ),
                ],
            ),
        ),
        dcc.Interval(id="activity-summary-trigger", interval=800, n_intervals=0, max_intervals=1),
        # Set by select_member_from_summary when a row click needs to
        # switch chambers first; update_politician_roster (triggered by
        # that chamber switch) consumes it instead of falling back to that
        # chamber's default member, then clears it.
        dcc.Store(id="pending-member-selection", data=None),
        html.H3("Look Up a Member", style={**_HEADER_STYLE, "marginTop": "40px"}),
        dcc.Tabs(
            id="politician-chamber-tabs",
            value="house",
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
                            value=default_value,
                            clearable=False,
                            searchable=True,
                            style={"width": "320px", "color": "#0b0b0b"},
                        ),
                    ],
                ),
            ],
        ),
        dcc.Loading(
            type="default",
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
            style={"display": "flex", "gap": "16px", "alignItems": "flex-start", "marginTop": "8px"},
            children=[
                html.Div(
                    style=_FILTER_PANEL_STYLE,
                    children=[
                        html.Div("Filters", style={"color": _HEADER_COLOR, "fontWeight": "700",
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
                    style={"flex": "1", "minWidth": "0"},
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


app = Dash(__name__)
app.title = "Public Company Tracker"
# gunicorn's entry point in production is "dash_app:server" -- it imports
# this module and serves this Flask app directly, never calling app.run()
# below, so debug mode (and the dev-tools UI it enables) only ever exist
# for local `python dash_app.py` runs, not the hosted deployment.
server = app.server

app.layout = html.Div(
    style={"maxWidth": "1400px", "margin": "40px auto", "fontFamily": "sans-serif",
           "padding": "0 16px"},
    children=[
        dcc.Tabs(
            id="app-tabs",
            value="company",
            style=_NAV_CONTAINER_STYLE,
            children=[
                dcc.Tab(
                    label="Public Company Tracker",
                    value="company",
                    style=_APP_TAB_STYLE,
                    selected_style=_APP_TAB_SELECTED_STYLE,
                    children=html.Div(style={"marginTop": "8px"}, children=_company_tracker_children()),
                ),
                dcc.Tab(
                    label="Investment Manager Tracker",
                    value="manager",
                    style=_APP_TAB_STYLE,
                    selected_style=_APP_TAB_SELECTED_STYLE,
                    children=html.Div(style={"marginTop": "8px"}, children=_manager_tracker_children()),
                ),
                dcc.Tab(
                    label="Politician Tracker",
                    value="politician",
                    style=_APP_TAB_STYLE,
                    selected_style=_APP_TAB_SELECTED_STYLE,
                    children=html.Div(style={"marginTop": "8px"}, children=_politician_tracker_children()),
                ),
            ],
        ),
    ],
)


def _tab_visibility_styles(is_fund):
    """(growth, income, balance, cashflow, holdings) tab header styles --
    Financials tabs for regular companies, Top Holdings for funds."""
    financials_style = _NAV_TAB_HIDDEN_STYLE if is_fund else _NAV_TAB_STYLE
    holdings_style = _NAV_TAB_STYLE if is_fund else _NAV_TAB_HIDDEN_STYLE
    return (financials_style, financials_style, financials_style, financials_style, holdings_style)


def _empty_company_outputs(status, view_tab="growth", is_fund=False):
    return ([], status, None, True, [], EMPTY_COLS, [], EMPTY_COLS, [], EMPTY_COLS,
            [], HOLDINGS_COLUMNS, view_tab, *_tab_visibility_styles(is_fund), None)


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
        return _empty_company_outputs(f"{query}: {e}"), None
    except requests.RequestException as e:
        return _empty_company_outputs(f"Network error talking to SEC EDGAR: {e}"), None

    status = f"Found: {title} ({ticker})"
    if debt_warning:
        status += f"\n{debt_warning}"

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
               [], HOLDINGS_COLUMNS, "growth", *_tab_visibility_styles(False), dcf_defaults)
    return outputs, None


def _run_etf_lookup(ticker):
    """ETFs/mutual funds don't have 10-K financials or a DCF to compute, so
    this skips the SEC pipeline entirely and shows top holdings instead."""
    try:
        holdings_df = fetch_top_holdings(ticker)
    except HoldingsDataError as e:
        return _empty_company_outputs(f"{ticker}: {e}", is_fund=True)

    status = (f"Found: {ticker} — ETF/Fund. Financials and DCF valuation aren't available "
              "for funds (no 10-K/XBRL data); showing top holdings instead.")
    holdings_data = [
        {"symbol": r.symbol, "name": r.name, "holding_pct": round(r.holding_pct, 2)}
        for r in holdings_df.itertuples()
    ]
    store = {"rows": [], "title": ticker, "ticker": ticker}
    return (
        # download-btn.disabled=False: the holdings table alone is enough
        # for the Excel download (see the download callback below).
        [], status, store, False,
        [], EMPTY_COLS, [], EMPTY_COLS, [], EMPTY_COLS,
        holdings_data, HOLDINGS_COLUMNS, "holdings", *_tab_visibility_styles(True), None,
    )


def _render_company_candidates(candidates, query, candidate_type="company-candidate"):
    if not candidates:
        return None
    shown = candidates[:_MAX_CANDIDATES_SHOWN]
    buttons = [
        html.Button(
            f"{c['ticker']}  {'ETF / Fund' if c.get('is_fund') else c['title']}",
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
                        style={"color": "#898781", "fontSize": "12px"})]
    return html.Div(style={"maxWidth": "520px", "marginTop": "8px"}, children=buttons + note)


@app.callback(
    Output("compare-mode", "data"),
    Input("compare-mode-tabs", "value"),
)
def sync_compare_mode(tab_value):
    return tab_value == "compare"


@app.callback(
    Output("compare-ticker-wrap", "style"),
    Output("compare-status-wrap", "style"),
    Output("financials-col-2", "style"),
    Input("compare-mode", "data"),
)
def render_compare_mode(is_compare):
    ticker_wrap_style = {"position": "relative"} if is_compare else {"position": "relative", "display": "none"}
    status_wrap_style = {} if is_compare else {"display": "none"}
    col2_style = {"flex": "1", "minWidth": "0"} if is_compare else {"flex": "1", "minWidth": "0", "display": "none"}
    return ticker_wrap_style, status_wrap_style, col2_style


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
    return html.Div(
        [
            html.Button(
                [html.Span(c["ticker"], style={"fontWeight": "700", "marginRight": "8px"}),
                 html.Span("ETF / Fund" if c.get("is_fund") else c["title"],
                           style={"color": "#52514e"})],
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


@app.callback(
    Output("price-chart", "figure"),
    Input("rows-store", "data"),
    Input("rows-store-2", "data"),
    Input("compare-mode", "data"),
    Input("range-tabs", "value"),
    Input("price-chart-refresh", "n_intervals"),
)
def update_price_chart(store, store2, is_compare, range_key, _n_intervals):
    if not store:
        return empty_price_figure()
    ticker = store["ticker"]

    if is_compare and store2:
        ticker2 = store2["ticker"]
        try:
            df1 = fetch_price_history(ticker, range_key)
            df2 = fetch_price_history(ticker2, range_key)
        except PriceDataError as e:
            return empty_price_figure(str(e))
        except Exception as e:
            return empty_price_figure(f"Price data unavailable: {e}")
        return build_compare_price_figure(df1, ticker, df2, ticker2, range_key)

    try:
        df = fetch_price_history(ticker, range_key)
    except PriceDataError as e:
        return empty_price_figure(str(e))
    except Exception as e:
        return empty_price_figure(f"Price data unavailable: {e}")
    return build_price_figure(df, ticker, range_key)


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
       Output("dcf-banner", "children"), Output("dcf-chart", "figure")],
    Input("dcf-defaults-store", "data"),
    Input("calculate-dcf-btn", "n_clicks"),
    Input("dcf-price-refresh", "n_intervals"),
    [State(f"dcf-{field}", "value") for field in _DCF_FIELD_NAMES],
    prevent_initial_call=True,
)
def update_dcf(defaults, _n_clicks, _n_intervals, *current_values):
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
    )

    return (*input_outputs, data, columns, summary, banner, chart)


@app.callback(
    [Output(f"dcf-{field}-2", "value") for field in _DCF_FIELD_NAMES]
    + [Output("dcf-table-2", "data"), Output("dcf-table-2", "columns"), Output("dcf-summary-table-2", "data"),
       Output("dcf-banner-2", "children"), Output("dcf-chart-2", "figure")],
    Input("dcf-defaults-store-2", "data"),
    Input("calculate-dcf-btn-2", "n_clicks"),
    Input("dcf-price-refresh-2", "n_intervals"),
    [State(f"dcf-{field}-2", "value") for field in _DCF_FIELD_NAMES],
    prevent_initial_call=True,
)
def update_dcf_2(defaults, _n_clicks, _n_intervals, *current_values):
    if ctx.triggered_id == "dcf-defaults-store-2":
        if not defaults:
            raise PreventUpdate
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
    )

    return (*input_outputs, data, columns, summary, banner, chart)


def _manager_row_to_record(r):
    return {
        "issuer": r["issuer"],
        "shares_m": r["shares_m"],
        "prev_shares_m": r["prev_shares_m"],
        "delta_shares_m": r["delta_shares_m"],
        "delta_shares_value_m": r["delta_shares_value_m"],
        "value_m": r["value_m"],
        "portfolio_pct": r["portfolio_pct"],
        "delta_pct": r["delta_pct"],
    }


def _all_positions_row_to_record(r):
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
    }


_MAX_CANDIDATES_SHOWN = 30
_CANDIDATE_BTN_STYLE = {
    "display": "block",
    "width": "100%",
    "textAlign": "left",
    "padding": "8px 12px",
    "border": "1px solid #e1e0d9",
    "borderRadius": "6px",
    "backgroundColor": "#ffffff",
    "color": "#0b0b0b",
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
                        style={"color": "#898781", "fontSize": "12px"})]
    return html.Div(
        style={"maxWidth": "520px", "marginTop": "8px"},
        children=buttons + note,
    )


@app.callback(
    Output("manager-status-msg", "children"),
    Output("increases-table", "data"),
    Output("decreases-table", "data"),
    Output("all-positions-table", "data"),
    Output("manager-candidates", "children"),
    Output("manager-suggestions", "children", allow_duplicate=True),
    Output("all-positions-full", "data"),
    Output("all-positions-visible-count", "data"),
    Output("download-positions-btn", "disabled"),
    Output("manager-positions-meta", "data"),
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
        return ("Enter an investment manager name.", [], [], [], None, None, [],
                _POSITIONS_PAGE_SIZE, True, None)

    try:
        result = fetch_manager_comparison(query, session=_session)
    except ManagerLookupError as e:
        if e.candidates:
            return (str(e), [], [], [], _render_candidates(e.candidates, query), None,
                    [], _POSITIONS_PAGE_SIZE, True, None)
        return str(e), [], [], [], None, None, [], _POSITIONS_PAGE_SIZE, True, None
    except FilingDataError as e:
        return f"{query}: {e}", [], [], [], None, None, [], _POSITIONS_PAGE_SIZE, True, None
    except requests.RequestException as e:
        return (f"Network error talking to SEC EDGAR: {e}", [], [], [], None, None,
                [], _POSITIONS_PAGE_SIZE, True, None)

    status = (f"Found: {result['resolved_name']} (CIK {result['cik']}) — "
              f"{result['latest_period']} vs {result['previous_period']}")
    increases = [_manager_row_to_record(r) for r in result["top_increases"]]
    decreases = [_manager_row_to_record(r) for r in result["top_decreases"]]
    all_positions = [_all_positions_row_to_record(r) for r in result["all_positions"]]
    meta = {"cik": result["cik"], "truncated": result.get("positions_truncated", False)}
    return (status, increases, decreases, all_positions[:_POSITIONS_PAGE_SIZE], None, None,
            all_positions, min(_POSITIONS_PAGE_SIZE, len(all_positions)), not all_positions, meta)


@app.callback(
    Output("manager-status-msg", "children", allow_duplicate=True),
    Output("increases-table", "data", allow_duplicate=True),
    Output("decreases-table", "data", allow_duplicate=True),
    Output("all-positions-table", "data", allow_duplicate=True),
    Output("manager-candidates", "children", allow_duplicate=True),
    Output("manager-suggestions", "children", allow_duplicate=True),
    Output("manager-input", "value", allow_duplicate=True),
    Output("suppress-next-manager-suggestions", "data", allow_duplicate=True),
    Output("all-positions-full", "data", allow_duplicate=True),
    Output("all-positions-visible-count", "data", allow_duplicate=True),
    Output("download-positions-btn", "disabled", allow_duplicate=True),
    Output("manager-positions-meta", "data", allow_duplicate=True),
    Input({"type": "manager-candidate", "cik": ALL}, "n_clicks"),
    Input({"type": "manager-suggestion", "cik": ALL}, "n_clicks"),
    prevent_initial_call=True,
)
def select_manager_candidate(candidate_clicks, suggestion_clicks):
    if not any(candidate_clicks) and not any(suggestion_clicks):
        raise PreventUpdate  # fires with all-zero clicks whenever the button list re-renders
    cik = ctx.triggered_id["cik"]

    try:
        result = fetch_manager_comparison_by_cik(cik, session=_session)
    except FilingDataError as e:
        return (f"CIK {cik}: {e}", [], [], [], None, None, no_update, no_update,
                [], _POSITIONS_PAGE_SIZE, True, None)
    except requests.RequestException as e:
        return (f"Network error talking to SEC EDGAR: {e}", [], [], [], None, None, no_update, no_update,
                [], _POSITIONS_PAGE_SIZE, True, None)

    status = (f"Found: {result['resolved_name']} (CIK {result['cik']}) — "
              f"{result['latest_period']} vs {result['previous_period']}")
    increases = [_manager_row_to_record(r) for r in result["top_increases"]]
    decreases = [_manager_row_to_record(r) for r in result["top_decreases"]]
    all_positions = [_all_positions_row_to_record(r) for r in result["all_positions"]]
    meta = {"cik": result["cik"], "truncated": result.get("positions_truncated", False)}
    # Setting manager-input's value below re-triggers update_manager_suggestions
    # (it watches that same value) — this flag tells that callback to skip
    # showing a dropdown for this one programmatic change, not real typing.
    return (status, increases, decreases, all_positions[:_POSITIONS_PAGE_SIZE], None, None,
            result["resolved_name"], True,
            all_positions, min(_POSITIONS_PAGE_SIZE, len(all_positions)), not all_positions, meta)


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
    # Filters panel -- a predictable "export everything" rather than
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
            all_positions = [_all_positions_row_to_record(r) for r in result["all_positions"]]
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
_POSITION_FILTER_FIELDS = [field for field, _label in _POSITION_FILTERS]

# Filters (min/max per field), sorts (custom — see below), and slices the
# full result down to the currently-visible prefix, in that order. Filter
# and sort inputs are read as State rather than Input here: a filter edit
# reaches this callback indirectly, through reset_visible_count_on_filter_
# change resetting all-positions-visible-count below, so a scroll or a sort
# click always re-applies whatever filters are currently set without this
# callback needing its own separate trigger for every filter keystroke.
# Pure client-side array work — no need to round-trip to the server.
app.clientside_callback(
    """
    function() {
        // Dash's clientside callbacks flatten a Python-side grouped list
        // of dependencies into individual positional arguments (unlike
        // server-side callbacks, where the same grouping arrives as one
        // list) — so the 12 filter States land as 12 separate arguments,
        // not one array. Index into `arguments` directly rather than
        // naming all 15 positional parameters.
        var fields = %(fields)s;
        var n = fields.length;
        var sortBy = arguments[0];
        var visibleCount = arguments[1];
        var mins = Array.prototype.slice.call(arguments, 2, 2 + n);
        var maxs = Array.prototype.slice.call(arguments, 2 + n, 2 + 2 * n);
        var fullData = arguments[2 + 2 * n];
        if (!fullData || !fullData.length) {
            return window.dash_clientside.no_update;
        }
        var rows = fullData.filter(function(row) {
            for (var i = 0; i < n; i++) {
                var v = row[fields[i]];
                if (mins[i] !== null && mins[i] !== undefined && !(v >= mins[i])) {
                    return false;
                }
                if (maxs[i] !== null && maxs[i] !== undefined && !(v <= maxs[i])) {
                    return false;
                }
            }
            return true;
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
    """
    % {"fields": json.dumps(_POSITION_FILTER_FIELDS)},
    Output("all-positions-table", "data", allow_duplicate=True),
    Input("all-positions-table", "sort_by"),
    Input("all-positions-visible-count", "data"),
    *[State(f"filter-{field}-min", "value") for field in _POSITION_FILTER_FIELDS],
    *[State(f"filter-{field}-max", "value") for field in _POSITION_FILTER_FIELDS],
    State("all-positions-full", "data"),
    prevent_initial_call=True,
)

# Filters only take effect on "Calculate" -- resetting the visible-row
# window back to one page is what actually triggers the callback above to
# re-filter (see the comment there), and also keeps "showing 100 of N"
# meaningful for a fresh filter rather than continuing from whatever the
# scroll position happened to be. "Clear Filters" is handled entirely by
# clear_position_filters below instead of also being an Input here: that
# callback and this one are two independent requests fired from the same
# click with no ordering guarantee between them, and this one reads the
# filter fields' values as State -- if it happened to run before the other
# callback's cleared values reached the browser, it would filter by the
# stale (pre-clear) values instead of showing everything.
app.clientside_callback(
    "function() { return %d; }" % _POSITIONS_PAGE_SIZE,
    Output("all-positions-visible-count", "data", allow_duplicate=True),
    Input("calculate-position-filters-btn", "n_clicks"),
    prevent_initial_call=True,
)


def _sort_rows_for_clear(rows, sort_by):
    """Mirror the clientside sort's null-handling (see the filter/sort/
    slice clientside callback above) so clearing filters doesn't also
    silently drop whatever column sort was active."""
    if not sort_by:
        return rows
    column_id = sort_by[0]["column_id"]
    desc = sort_by[0]["direction"] == "desc"
    return sorted(
        rows,
        key=lambda r: (r.get(column_id) if r.get(column_id) is not None else float("inf")),
        reverse=desc,
    )


@app.callback(
    [Output(f"filter-{field}-min", "value") for field in _POSITION_FILTER_FIELDS]
    + [Output(f"filter-{field}-max", "value") for field in _POSITION_FILTER_FIELDS]
    + [Output("all-positions-table", "data", allow_duplicate=True),
       Output("all-positions-visible-count", "data", allow_duplicate=True)],
    Input("clear-position-filters-btn", "n_clicks"),
    State("all-positions-full", "data"),
    State("all-positions-table", "sort_by"),
    prevent_initial_call=True,
)
def clear_position_filters(_n_clicks, full_data, sort_by):
    full_data = full_data or []
    rows = _sort_rows_for_clear(full_data, sort_by)
    cleared_filters = [None] * (len(_POSITION_FILTER_FIELDS) * 2)
    return cleared_filters + [rows[:_POSITIONS_PAGE_SIZE], min(_POSITIONS_PAGE_SIZE, len(rows))]


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


@app.callback(
    Output("recent-trades-table", "data"),
    Output("congress-leaderboard-table", "data"),
    Input("activity-summary-trigger", "n_intervals"),
    prevent_initial_call=True,
)
def load_activity_summary(_n_intervals):
    # Reads the repo-committed snapshot rather than scraping/parsing PTR
    # filings live -- that live build was OOM-killing the production
    # instance even serialized to one at a time. See
    # congress_trades.load_activity_summary_snapshot and
    # build_congress_snapshot.py for how the snapshot gets refreshed.
    try:
        summary = load_activity_summary_snapshot()
    except (OSError, json.JSONDecodeError):
        return [], []
    recent = [_recent_trade_row_to_record(t) for t in summary["recent_trades"][:150]]
    leaderboard = [_leaderboard_row_to_record(e) for e in summary["leaderboard"][:100]]
    return recent, leaderboard


@app.callback(
    Output("politician-chamber-tabs", "value", allow_duplicate=True),
    Output("politician-input", "value", allow_duplicate=True),
    Output("pending-member-selection", "data", allow_duplicate=True),
    Output("recent-trades-table", "active_cell", allow_duplicate=True),
    Output("congress-leaderboard-table", "active_cell", allow_duplicate=True),
    Output("recent-trades-table", "selected_cells", allow_duplicate=True),
    Output("congress-leaderboard-table", "selected_cells", allow_duplicate=True),
    Input("recent-trades-table", "active_cell"),
    Input("congress-leaderboard-table", "active_cell"),
    State("recent-trades-table", "data"),
    State("congress-leaderboard-table", "data"),
    State("politician-chamber-tabs", "value"),
    prevent_initial_call=True,
)
def select_member_from_summary(recent_cell, leaderboard_cell, recent_data, leaderboard_data, current_chamber):
    if ctx.triggered_id == "recent-trades-table":
        cell, data = recent_cell, recent_data
    else:
        cell, data = leaderboard_cell, leaderboard_data
    if not cell or not data:
        raise PreventUpdate
    row = data[cell["row"]]
    chamber = row["chamber_raw"]
    dropdown_key = f"{chamber}|{row['last']}|{row['first']}"
    # A click sets both active_cell (the focus outline) and selected_cells
    # (the reddish "cell--selected" fill) -- clearing active_cell alone
    # leaves the fill in place, so both need to be reset here to actually
    # restore the cell to its unclicked look. A bold-on-hover CSS rule
    # (custom.css) is the only lasting visual affordance for "clickable".
    if chamber == current_chamber:
        # Same chamber already showing -- no tab switch, so
        # update_politician_roster won't fire (its Input wouldn't change).
        # Set the dropdown directly instead of going through the pending-
        # selection store.
        return no_update, dropdown_key, no_update, None, None, [], []
    # Switching chambers: update_politician_roster is about to fire
    # (its chamber-tabs Input is changing) and would otherwise overwrite
    # the dropdown with that chamber's default member -- stash the real
    # target here for it to pick up instead.
    return chamber, no_update, dropdown_key, None, None, [], []


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


if __name__ == "__main__":
    # threaded=True matters here specifically: building the cross-chamber
    # activity summary (see congress_trades.build_activity_summary) is a
    # one-time ~1-2 minute call, and without threading it would block the
    # single-threaded dev server -- freezing every other tab/user -- for
    # that whole window instead of just showing a loading state on the
    # two tables that need it.
    app.run(debug=True, threaded=True)
