"""
dashboard.py - Crop Insurance Sales Dashboard (Streamlit).

Run it with the one-click launcher ("Open Dashboard.bat" / ".command"), or directly:
    streamlit run dashboard.py

On open it checks RMA for new data (data_pipeline.run_pipeline, ~1s when nothing changed),
then shows: stat cards -> premium/indemnity/loss ratio by crop year -> top commodities | top plans
-> map by state | coverage level -> top-products table. Settings are just below the imports.
"""
import re
import time
from datetime import datetime

import pandas as pd
import plotly.graph_objects as go
import plotly.io as pio
import streamlit as st
from plotly.subplots import make_subplots

import data_pipeline as dp

# --- Settings ----------------------------------------------------------------------------
PIPELINE_YEARS = None      # e.g. [2025, 2026]; None = newest crop year + HISTORY_YEARS prior
HISTORY_YEARS = 3          # prior years to load alongside the newest (partial) crop year
CHECK_PRIOR_YEARS = False  # True = also re-check prior years on RMA (revised as late losses come in)
RECHECK_MINUTES = 60       # while the page stays open, re-check RMA this often
TOP_N = 15                 # bars in the ranking charts

# --- Design tokens -----------------------------------------------------------------------
FONT = 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif'
INK, INK_2, MUTED = "#0b0b0b", "#52514e", "#898781"      # primary text, secondary text, captions
GRID, AXIS, SURFACE = "#e1e0d9", "#c3c2b7", "#fcfcfb"    # gridlines, baselines, chart background
NEUTRAL = MUTED

# One color per MEASURE, used everywhere that measure appears (card accent, bars, map ramp,
# table header). Premium vs indemnity was checked for colorblind separation; the year chart
# also uses a legend and bar gaps so color is never the only cue.
METRICS = {  # column -> (label, color, single-hue ramp for the map)
    "total_prem":       ("Total Premium ($)",        "#008300", "Greens"),
    "indem_amt":        ("Indemnity ($)",            "#e34948", "Reds"),
    "liability_amt":    ("Liability ($)",            "#2a78d6", "Blues"),
    "pol_sold_cnt":     ("Policies Sold",            "#4a3aa7", "Purples"),
    "net_acre_qty":     ("Net Acres",                "#eb6834", "Oranges"),
    "subsidy":          ("Subsidy ($)",              NEUTRAL,   "Greys"),
    "farmer_paid_prem": ("Farmer-paid Premium ($)",  NEUTRAL,   "Greys"),
    "pol_prem_cnt":     ("Policies Earning Premium", NEUTRAL,   "Greys"),
}
LABEL = {k: v[0] for k, v in METRICS.items()}
COLOR = {k: v[1] for k, v in METRICS.items()}
RAMP = {k: v[2] for k, v in METRICS.items()}
DOLLAR_METRICS = {"total_prem", "liability_amt", "subsidy", "farmer_paid_prem", "indem_amt"}
CHART_METRICS = ["total_prem", "indem_amt", "liability_amt", "pol_sold_cnt", "net_acre_qty"]
FILTERS = {"crop_yr": "Crop year", "commodity": "Commodity", "insurance_plan": "Insurance plan", "state_abbr": "State"}
ALL = "(All)"

# Plotly theme: quiet chrome so the data carries the color.
_axis = dict(gridcolor=GRID, linecolor=AXIS, zerolinecolor=AXIS, showline=False, ticks="",
             tickfont=dict(color=MUTED, size=11), title=dict(font=dict(color=MUTED, size=11)))
pio.templates["sob"] = go.layout.Template(layout=dict(
    font=dict(family=FONT, size=12, color=INK_2),
    title=dict(font=dict(size=15, color=INK), x=0, xanchor="left"),
    paper_bgcolor=SURFACE, plot_bgcolor=SURFACE, xaxis=_axis, yaxis=_axis,
    hoverlabel=dict(bgcolor="white", bordercolor=GRID, font=dict(family=FONT, size=12, color=INK)),
    legend=dict(orientation="h", yanchor="bottom", y=1.0, xanchor="right", x=1, title=dict(text=""),
                font=dict(color=INK_2)),
    margin=dict(l=8, r=8, t=56, b=8), bargap=0.28, bargroupgap=0.06,
))
try:
    pio.templates["sob"].layout.barcornerradius = 3   # rounded bar ends (plotly >= 5.19)
except (ValueError, AttributeError):
    pass
pio.templates.default = "plotly_white+sob"
PLOT_CONFIG = {"displaylogo": False, "modeBarButtonsToRemove": ["lasso2d", "select2d", "autoScale2d"]}

# Very brief plan summaries, shown when one insurance plan is selected. Keyed by RMA plan
# abbreviation (letters only); prefixes also match (SCO-RP -> SCO).
PLAN_DESCRIPTIONS = {
    "YP":     "Yield Protection: pays when the farm's yield falls below its guaranteed yield.",
    "RP":     "Revenue Protection: pays when farm revenue drops below the guarantee; guarantee rises if the harvest price goes up.",
    "RPHPE":  "Revenue Protection with Harvest Price Exclusion: like RP, but the guarantee doesn't rise with the harvest price (cheaper).",
    "APH":    "Actual Production History: yield coverage based on the farm's own yield history (common for specialty and perennial crops).",
    "ARH":    "Actual Revenue History: revenue coverage based on the farm's own revenue history (specialty crops).",
    "AYP":    "Area Yield Protection: pays when the county's average yield drops, not the individual farm's.",
    "ARP":    "Area Revenue Protection: pays when county-level revenue drops below the guarantee.",
    "ARPHPE": "Area Revenue Protection with Harvest Price Exclusion: county revenue coverage without the harvest-price increase.",
    "SCO":    "Supplemental Coverage Option: county-based add-on that covers part of the underlying policy's deductible.",
    "ECO":    "Enhanced Coverage Option: county-based add-on covering a shallow loss band near the top of coverage.",
    "STAX":   "Stacked Income Protection: county-based revenue coverage for upland cotton.",
    "MP":     "Margin Protection: county-based coverage against a drop in revenue minus input costs.",
    "RI":     "Rainfall Index: pays when rainfall in a grid area falls below normal (pasture, rangeland, forage).",
    "PRF":    "Pasture, Rangeland, Forage (Rainfall Index): pays when grid-area rainfall falls below normal.",
    "VI":     "Vegetation Index: pays when a satellite greenness index for the area falls below normal.",
    "WFRP":   "Whole-Farm Revenue Protection: covers the revenue of the entire farm under one policy.",
    "MFRP":   "Micro Farm Revenue Protection: simplified whole-farm revenue coverage for small farms.",
    "DO":     "Dollar plan: covers a fixed dollar amount per acre when crop value falls below it.",
    "DOL":    "Dollar plan: covers a fixed dollar amount per acre when crop value falls below it.",
    "PACE":   "Post-Application Coverage Endorsement: covers yield loss when planned in-season nitrogen can't be applied.",
    "HIPWI":  "Hurricane Insurance Protection - Wind Index: pays when a named hurricane's winds hit the county.",
    "LRP":    "Livestock Risk Protection: protects against a decline in livestock prices.",
    "LGM":    "Livestock Gross Margin: protects the margin between livestock prices and feed costs.",
    "DRP":    "Dairy Revenue Protection: protects against a drop in quarterly milk revenue.",
    "GRP":    "Group Risk Plan (legacy): county-yield-based coverage, replaced by Area Yield Protection.",
    "GRIP":   "Group Risk Income Protection (legacy): county-revenue-based coverage, replaced by Area Revenue Protection.",
}


def plan_description(plan_label: str) -> str | None:
    abbr = re.sub(r"[^A-Z]", "", plan_label.split("(")[0].upper())
    if abbr in PLAN_DESCRIPTIONS:
        return PLAN_DESCRIPTIONS[abbr]
    return next((PLAN_DESCRIPTIONS[k] for k in sorted(PLAN_DESCRIPTIONS, key=len, reverse=True)
                 if len(k) >= 2 and abbr.startswith(k)), None)


# --- Data --------------------------------------------------------------------------------
@st.cache_resource(ttl=RECHECK_MINUTES * 60, show_spinner="Checking RMA for new data...")
def load_data():
    """Refresh from RMA (falls back to the last data if RMA can't be reached), then load.
    cache_resource keeps one shared copy in memory instead of copying ~600k rows per click."""
    error, started = None, time.perf_counter()
    try:
        dp.run_pipeline(years=PIPELINE_YEARS, history=HISTORY_YEARS, check_all=CHECK_PRIOR_YEARS)
    except Exception as exc:
        error = type(exc).__name__
    df, meta = dp.load_processed()
    for col in ("commodity", "insurance_plan", "state_abbr"):   # categories: much faster filtering/grouping
        df[col] = df[col].astype("category")
    return df, meta, error, time.perf_counter() - started


def summarize(frame: pd.DataFrame, by=None) -> pd.DataFrame:
    """Sum the metrics (optionally grouped) and add ratios computed AFTER aggregation."""
    cols = list(METRICS)
    agg = frame[cols].sum().to_frame().T if by is None else frame.groupby(by, observed=True)[cols].sum().reset_index()
    prem = agg["total_prem"].where(agg["total_prem"] != 0)
    agg["loss_ratio"] = agg["indem_amt"] / prem
    agg["subsidy_pct"] = agg["subsidy"] / prem
    agg["prem_rate"] = agg["total_prem"] / agg["liability_amt"].where(agg["liability_amt"] != 0)
    return agg


def apply_filters(df: pd.DataFrame, selected: dict, exclude: str | None = None) -> pd.DataFrame:
    mask = pd.Series(True, index=df.index)
    for col, value in selected.items():
        if col != exclude and value != ALL:
            mask &= df[col] == value
    return df[mask]


# --- Formatting --------------------------------------------------------------------------
def fmt_money(v) -> str:
    """1 decimal with B/M/K suffix, e.g. $12.3M."""
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            return f"${v / div:,.1f}{suf}"
    return f"${v:,.1f}"


def axis_values(series: pd.Series, metric: str):
    """Chart values: dollars in $ millions (1 decimal), counts as-is."""
    if metric in DOLLAR_METRICS:
        return (series / 1e6).round(1), LABEL[metric].replace("($)", "($M)")
    return series, LABEL[metric]


def short(label: str, n: int = 26) -> str:
    return label if len(label) <= n else label[: n - 1] + "…"


def kpi_cards(frame: pd.DataFrame) -> str:
    s = summarize(frame).iloc[0]
    cards = [  # (label, value, caption, accent color or None)
        ("Total Premium", fmt_money(s.total_prem), f"{fmt_money(s.farmer_paid_prem)} farmer-paid", COLOR["total_prem"]),
        ("Indemnity", fmt_money(s.indem_amt), "losses paid", COLOR["indem_amt"]),
        ("Loss Ratio", f"{s.loss_ratio:.2f}" if pd.notna(s.loss_ratio) else "–", "indemnity ÷ premium", None),
        ("Liability", fmt_money(s.liability_amt), "coverage in force", COLOR["liability_amt"]),
        ("Policies Sold", f"{s.pol_sold_cnt:,.0f}", f"{s.pol_prem_cnt:,.0f} earning premium", COLOR["pol_sold_cnt"]),
        ("Net Acres", f"{s.net_acre_qty:,.0f}", "reported × share", COLOR["net_acre_qty"]),
        ("Subsidy", fmt_money(s.subsidy), f"{s.subsidy_pct:.1%} of premium" if pd.notna(s.subsidy_pct) else "", None),
    ]
    tiles = "".join(
        f'<div style="flex:1 1 150px;background:{SURFACE};border:1px solid rgba(11,11,11,.10);border-radius:10px;'
        f'padding:12px 14px;border-top:3px solid {c or "rgba(11,11,11,.10)"}">'
        f'<div style="font-size:12px;color:{INK_2}">{k}</div>'
        f'<div style="font-size:22px;font-weight:600;color:{INK};margin-top:2px">{v}</div>'
        f'<div style="font-size:11px;color:{MUTED};margin-top:2px">{cap}</div></div>'
        for k, v, cap, c in cards)
    return f'<div style="display:flex;flex-wrap:wrap;gap:10px;margin:4px 0 8px;font-family:{FONT}">{tiles}</div>'


# --- Charts ------------------------------------------------------------------------------
def _left_titles(fig, widths, spacing):
    """Left-align subplot titles over their panels (plotly centers them by default)."""
    x, usable = 0.0, 1 - spacing * (len(widths) - 1)
    for ann, w in zip(fig.layout.annotations, widths):
        ann.update(x=x, xanchor="left", font=dict(size=15, color=INK))
        x += usable * w / sum(widths) + spacing


def year_chart(frame, year_label):
    """Premium and indemnity side by side per crop year ($M, left axis) with loss ratio as a line (right axis)."""
    agg = summarize(frame, "crop_yr").sort_values("crop_yr")
    years = [year_label(y) for y in agg["crop_yr"]]
    fig = go.Figure()
    for m in ("total_prem", "indem_amt"):
        name = LABEL[m].replace(" ($)", "")
        fig.add_bar(x=years, y=axis_values(agg[m], m)[0], name=name, marker_color=COLOR[m],
                    hovertemplate="%{x}<br>" + name + ": $%{y:,.1f}M<extra></extra>")
    lr = agg["loss_ratio"]
    fig.add_scatter(x=years, y=lr, name="Loss Ratio", yaxis="y2", mode="lines+markers+text",
                    line=dict(color=INK_2, width=2), marker=dict(size=8, color=INK_2, line=dict(color=SURFACE, width=2)),
                    text=[f"{v:.2f}" if pd.notna(v) else "" for v in lr], textposition="top center",
                    textfont=dict(size=11, color=INK), hovertemplate="%{x}<br>Loss ratio: %{y:.2f}<extra></extra>")
    lr_max = float(lr.max()) if lr.notna().any() else 0
    lr_top = 1.0 if lr_max <= 0.92 else round(lr_max * 1.12, 1)   # 0-1 unless a year runs above it
    fig.update_layout(
        title="Premium, Indemnity & Loss Ratio by Crop Year", height=400, barmode="group",
        xaxis=dict(type="category", showgrid=False, tickfont=dict(color=INK_2, size=12)),  # years are labels, not numbers
        yaxis=dict(title=dict(text="$ millions"), rangemode="tozero", tickformat=",.0f"),
        yaxis2=dict(title=dict(text="Loss ratio", font=dict(color=MUTED, size=11)), overlaying="y", side="right",
                    range=[0, lr_top], dtick=0.2 if lr_top <= 1.2 else None, tickformat=".1f", showgrid=False,
                    zeroline=False, tickfont=dict(color=MUTED, size=11)),
        margin=dict(l=8, r=8, t=64, b=8),
    )
    return fig


def ranking_pair(frame, metric):
    """Top commodities and top insurance plans, side by side, in the metric's color."""
    name = LABEL[metric].replace(" ($)", "")
    fig = make_subplots(rows=1, cols=2, horizontal_spacing=0.2,
                        subplot_titles=(f"Top {TOP_N} Commodities by {name}", f"Top {TOP_N} Insurance Plans by {name}"))
    fmt = "$%{x:,.1f}M" if metric in DOLLAR_METRICS else "%{x:,.0f}"
    n_max = 1
    for col, by in enumerate(("commodity", "insurance_plan"), start=1):
        agg = summarize(frame, by).nlargest(TOP_N, metric).sort_values(metric)
        vals, axis_title = axis_values(agg[metric], metric)
        n_max = max(n_max, len(agg))
        fig.add_trace(go.Bar(
            x=vals, y=[short(str(v)) for v in agg[by]], orientation="h", marker_color=COLOR[metric], showlegend=False,
            customdata=list(zip(agg[by].astype(str), agg["loss_ratio"].fillna(0))),
            hovertemplate="<b>%{customdata[0]}</b><br>" + fmt + "<br>Loss ratio %{customdata[1]:.2f}<extra></extra>"),
            row=1, col=col)
        fig.update_xaxes(title_text=axis_title, row=1, col=col)
        fig.update_yaxes(showgrid=False, tickfont=dict(color=INK_2, size=11), row=1, col=col)
    _left_titles(fig, [1, 1], 0.2)
    fig.update_layout(height=max(380, 24 * n_max + 110), bargap=0.3)
    return fig


def map_and_coverage(frame, metric):
    """State map (single-hue ramp of the metric's color) beside the coverage-level distribution."""
    widths, spacing = [0.58, 0.42], 0.06
    name = LABEL[metric].replace(" ($)", "")
    fig = make_subplots(rows=1, cols=2, column_widths=widths, horizontal_spacing=spacing,
                        specs=[[{"type": "choropleth"}, {"type": "xy"}]],
                        subplot_titles=(f"{name} by State", f"{name} by Coverage Level"))
    states = summarize(frame, "state_abbr")
    z, axis_title = axis_values(states[metric], metric)
    zfmt = "$%{z:,.1f}M" if metric in DOLLAR_METRICS else "%{z:,.0f}"
    fig.add_trace(go.Choropleth(
        locations=states["state_abbr"].astype(str), z=z, locationmode="USA-states", colorscale=RAMP[metric],
        marker_line_color="white", marker_line_width=0.6, customdata=states["loss_ratio"].fillna(0),
        colorbar=dict(title=dict(text=axis_title, side="top", font=dict(size=11, color=MUTED)),
                      orientation="h", x=widths[0] * (1 - spacing) / 2, xanchor="center", y=-0.02, yanchor="top",
                      len=0.4, thickness=10, outlinewidth=0, tickfont=dict(size=10, color=MUTED)),
        hovertemplate="<b>%{location}</b><br>" + zfmt + "<br>Loss ratio %{customdata:.2f}<extra></extra>"),
        row=1, col=1)
    fig.update_geos(scope="usa", bgcolor=SURFACE, lakecolor=SURFACE, showlakes=False,
                    landcolor="#f0efec", subunitcolor="white")

    cov = summarize(frame, "cov_lvl_pct")
    cov = cov[(cov[metric] > 0) & (cov["cov_lvl_pct"] > 0)].sort_values("cov_lvl_pct")
    yfmt = "$%{y:,.1f}M" if metric in DOLLAR_METRICS else "%{y:,.0f}"
    fig.add_trace(go.Bar(x=[f"{int(c)}%" for c in cov["cov_lvl_pct"]], y=axis_values(cov[metric], metric)[0],
                         marker_color=COLOR[metric], showlegend=False,
                         hovertemplate="Coverage %{x}<br>" + yfmt + "<extra></extra>"), row=1, col=2)
    fig.update_xaxes(title_text="Coverage level", type="category", showgrid=False, row=1, col=2)
    fig.update_yaxes(title_text=axis_title, row=1, col=2)
    _left_titles(fig, widths, spacing)
    fig.update_layout(height=450, margin=dict(l=8, r=8, t=56, b=70))
    return fig


def summary_table_html(frame) -> str:
    """Top products (commodity x plan) by premium. $ columns in millions, 1 decimal."""
    t = summarize(frame, ["commodity", "insurance_plan"]).sort_values("total_prem", ascending=False).head(25)
    names = {}
    for col in METRICS:
        names[col] = LABEL[col].replace("($)", "($M)") if col in DOLLAR_METRICS else LABEL[col]
        if col in DOLLAR_METRICS:
            t[col] = t[col] / 1e6
    t = t.rename(columns={**names, "commodity": "Commodity", "insurance_plan": "Plan",
                          "loss_ratio": "Loss Ratio", "subsidy_pct": "Subsidy %", "prem_rate": "Premium Rate"})
    accent = [{"selector": f"th.col_heading.col{list(t.columns).index(names[c])}",
               "props": [("border-top", f"3px solid {COLOR[c]}")]} for c in CHART_METRICS]
    return (t.style
            .format({names[c]: ("{:,.1f}" if c in DOLLAR_METRICS else "{:,.0f}") for c in METRICS})
            .format({"Loss Ratio": "{:.2f}", "Subsidy %": "{:.1%}", "Premium Rate": "{:.1%}"}, na_rep="–")
            .hide(axis="index")
            .set_table_styles([
                {"selector": "", "props": [("border-collapse", "collapse"), ("font-family", FONT),
                                           ("font-size", "12px"), ("width", "100%")]},
                {"selector": "th", "props": [("background", "#f6f5f2"), ("color", INK), ("font-weight", "600"),
                                             ("padding", "6px 10px"), ("text-align", "right"),
                                             ("border-bottom", f"1px solid {AXIS}")]},
                {"selector": "td", "props": [("padding", "5px 10px"), ("text-align", "right"), ("color", INK),
                                             ("border-bottom", f"1px solid {GRID}"),
                                             ("font-variant-numeric", "tabular-nums")]},
                {"selector": "th.col_heading.col0, th.col_heading.col1, td.col0, td.col1",
                 "props": [("text-align", "left")]},
                {"selector": "tbody tr:hover", "props": [("background", "#f6f5f2")]},
                *accent])
            .to_html())


# --- Page --------------------------------------------------------------------------------
def reset_filters():
    for col in FILTERS:
        st.session_state[f"f_{col}"] = ALL


def main():
    st.set_page_config(page_title="Crop Insurance Sales Dashboard", page_icon="📊", layout="wide")
    try:
        df, meta, refresh_error, refresh_secs = load_data()
    except FileNotFoundError:
        st.error("No data yet and RMA couldn't be reached. Check the internet connection and reload the page.")
        st.stop()

    partial = set(meta.get("partial_years", []))
    def year_label(y):
        return y if y == ALL else (f"{int(y)} (partial)" if int(y) in partial else str(int(y)))
    checked = datetime.fromisoformat(meta.get("checked_at", meta["built_at"])).astimezone().strftime("%b %d, %Y %I:%M %p")

    # Header + data status
    head, refresh = st.columns([5, 1])
    with head:
        st.markdown(f'<div style="font:600 24px {FONT};color:{INK}">Crop Insurance Sales Dashboard</div>'
                    f'<div style="font:13px {FONT};color:{INK_2}">USDA RMA Summary of Business · '
                    + (f'<span style="color:#a15c00">couldn&#39;t reach RMA just now ({refresh_error}); '
                       f'showing data last checked {checked}</span>' if refresh_error
                       else f'up to date with RMA as of {checked}') + '</div>', unsafe_allow_html=True)
    with refresh:
        if st.button("Check RMA now"):
            load_data.clear()
            st.rerun()

    # Filters (cascading: each list only offers values that exist under the other filters)
    selected = {col: st.session_state.get(f"f_{col}", ALL) for col in FILTERS}
    filter_cols = st.columns([1, 1.5, 1.2, 0.8, 0.6])
    for (col, label), slot in zip(FILTERS.items(), filter_cols):
        pool = apply_filters(df, selected, exclude=col)[col].dropna().unique()
        values = sorted(pool, reverse=True) if col == "crop_yr" else sorted(map(str, pool))
        options = [ALL] + list(values)
        if st.session_state.get(f"f_{col}", ALL) not in options:
            st.session_state[f"f_{col}"] = ALL
        with slot:
            st.selectbox(label, options, key=f"f_{col}", format_func=year_label if col == "crop_yr" else str)
        selected[col] = st.session_state[f"f_{col}"]
    with filter_cols[-1]:
        st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
        st.button("Reset", on_click=reset_filters)
    metric = st.radio("Chart metric", CHART_METRICS, format_func=lambda m: LABEL[m].replace(" ($)", ""),
                      horizontal=True, key="metric")

    sub = apply_filters(df, selected)
    started = time.perf_counter()
    active = [f"{FILTERS[c]}: <b>{year_label(v) if c == 'crop_yr' else v}</b>" for c, v in selected.items() if v != ALL]
    years = ", ".join(year_label(y) for y in sorted(sub["crop_yr"].unique()))
    st.markdown(f'<div style="font-family:{FONT};margin:4px 0 0">'
                f'<div style="font-size:18px;font-weight:600;color:{INK}">{" · ".join(active) or "All sales data combined"}</div>'
                f'<div style="font-size:12px;color:{MUTED}">Crop years {years or "–"} · {len(sub):,} records</div></div>',
                unsafe_allow_html=True)
    if sub.empty:
        st.info("No records match these filters.")
        return
    if selected["insurance_plan"] != ALL:
        st.caption(f"*{plan_description(selected['insurance_plan']) or 'No description on file for this plan.'}*")

    st.markdown(kpi_cards(sub), unsafe_allow_html=True)
    st.plotly_chart(year_chart(sub, year_label), theme=None, config=PLOT_CONFIG)        # row 1
    st.plotly_chart(ranking_pair(sub, metric), theme=None, config=PLOT_CONFIG)          # row 2
    st.plotly_chart(map_and_coverage(sub, metric), theme=None, config=PLOT_CONFIG)      # row 3
    st.markdown(f'<div style="font:600 15px {FONT};color:{INK};margin:8px 0 6px">'
                f'Top Products (Commodity × Plan) by Premium</div>', unsafe_allow_html=True)
    st.markdown(summary_table_html(sub), unsafe_allow_html=True)                        # row 4
    st.download_button("Download this view as CSV",
                       summarize(sub, ["crop_yr", "commodity", "insurance_plan"]).to_csv(index=False),
                       file_name=f"crop_insurance_view_{datetime.now():%Y%m%d}.csv", mime="text/csv")
    st.markdown(f'<div style="font:11px {FONT};color:{MUTED};margin-top:6px">'
                f'RMA check {refresh_secs:.1f}s · page built in {time.perf_counter() - started:.1f}s</div>',
                unsafe_allow_html=True)


main()
