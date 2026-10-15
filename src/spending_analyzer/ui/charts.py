"""Altair charts shared by the Results and Chatbot pages.

Colors are the validated dataviz reference palette (slot 1 blue, slot 2 orange; both
pass the lightness, chroma, colorblind-separation and contrast checks on the light and
dark surfaces), chosen per Streamlit theme. Amounts are converted to float here only
for plotting; every number shown as text stays a formatted Decimal.
"""

from decimal import Decimal

import altair as alt
import pandas as pd
import streamlit as st

LIGHT = {"series_1": "#2a78d6", "series_2": "#eb6834", "grid": "#e1e0d9", "axis": "#c3c2b7"}
DARK = {"series_1": "#3987e5", "series_2": "#d95926", "grid": "#2c2c2a", "axis": "#383835"}


def _palette() -> dict:
    try:
        return DARK if st.context.theme.type == "dark" else LIGHT
    except AttributeError:  # older Streamlit without st.context.theme
        return LIGHT


def _style(chart: alt.Chart) -> alt.Chart:
    p = _palette()
    return chart.configure_axis(gridColor=p["grid"], domainColor=p["axis"],
                                tickColor=p["axis"], labelLimit=220) \
        .configure_view(strokeWidth=0)


def ranked_bars(rows: list[tuple[str, Decimal]], value_title: str,
                euros: bool = True) -> alt.Chart:
    """One series of magnitudes, largest first: horizontal bars with a hover tooltip.
    The title above the chart names the series, so no legend. euros=False for counts."""
    data = pd.DataFrame({"label": [label for label, _ in rows],
                         "value": [float(abs(value)) for _, value in rows],
                         "shown": [f"{abs(value):,.2f} €".replace(",", " ") if euros
                                   else f"{int(value)}" for _, value in rows]})
    chart = alt.Chart(data).mark_bar(
        color=_palette()["series_1"], cornerRadiusEnd=4, height={"band": 0.7},
    ).encode(
        x=alt.X("value:Q", title=value_title, axis=alt.Axis(format=",.0f", tickCount=5)),
        # labelOverlap off: Vega would otherwise silently drop every other label.
        # labelLimit: one long name must not squeeze the bars; the tooltip has it in full
        y=alt.Y("label:N", sort="-x", title=None,
                axis=alt.Axis(labelOverlap=False, labelLimit=150)),
        tooltip=[alt.Tooltip("label:N", title=" "), alt.Tooltip("shown:N", title=value_title)],
    ).properties(height=max(120, 34 * len(rows)))
    return _style(chart)


def signed_month_bars(rows: list[tuple[str, Decimal]], value_title: str) -> alt.Chart:
    """One value per month that may be negative (e.g. net cash flow), bars from zero."""
    data = pd.DataFrame({"Month": [m for m, _ in rows], "value": [float(v) for _, v in rows],
                         "shown": [f"{v:,.2f} €".replace(",", " ") for _, v in rows]})
    chart = alt.Chart(data).mark_bar(color=_palette()["series_1"], cornerRadiusEnd=4).encode(
        x=alt.X("Month:N", title=None, axis=alt.Axis(labelAngle=0)),
        y=alt.Y("value:Q", title=value_title, axis=alt.Axis(format=",.0f", tickCount=5)),
        tooltip=[alt.Tooltip("Month:N"), alt.Tooltip("shown:N", title=value_title)],
    ).properties(height=240)
    return _style(chart)


def monthly_bars(months: list[str], spending: list[Decimal], income: list[Decimal]
                 ) -> alt.Chart:
    """Spending and income side by side per month: two series on one shared euro
    axis, so a legend; colors follow the series, never the order."""
    p = _palette()
    data = pd.DataFrame(
        [{"Month": m, "Series": "Spending", "value": float(s),
          "shown": f"{s:,.2f} €".replace(",", " ")} for m, s in zip(months, spending)]
        + [{"Month": m, "Series": "Income", "value": float(i),
            "shown": f"{i:,.2f} €".replace(",", " ")} for m, i in zip(months, income)])
    chart = alt.Chart(data).mark_bar(cornerRadiusEnd=4).encode(
        x=alt.X("Month:N", title=None, axis=alt.Axis(labelAngle=0)),
        xOffset=alt.XOffset("Series:N", sort=["Spending", "Income"],
                            scale=alt.Scale(paddingInner=0.08)),  # gap between the pair
        y=alt.Y("value:Q", title="€", axis=alt.Axis(format=",.0f", tickCount=5)),
        color=alt.Color("Series:N", sort=["Spending", "Income"],
                        scale=alt.Scale(domain=["Spending", "Income"],
                                        range=[p["series_1"], p["series_2"]]),
                        legend=alt.Legend(orient="top", title=None)),
        tooltip=[alt.Tooltip("Month:N"), alt.Tooltip("Series:N"),
                 alt.Tooltip("shown:N", title="Amount")],
    ).properties(height=260)
    return _style(chart)
