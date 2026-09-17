"""
Fixed (non-zoomable) charts for the app.

Streamlit's built-in st.line_chart / st.bar_chart are interactive: a scroll
wheel over them zooms and drags pan, so scrolling the page could leave a
chart zoomed to nothing. These helpers build the same charts with Altair
WITHOUT interactivity - axes are fixed to the data, nothing zooms or pans -
and in the Dezignatek palette.

Every helper takes a DataFrame whose index is the x axis (hour / day /
station name) and whose columns are the series to plot, same as the
Streamlit built-ins they replace.
"""

from __future__ import annotations

import altair as alt
import pandas as pd
import streamlit as st

from plant_sim.theme import AMBER, GREEN, LINE, MUTED, RED, TEAL, TEXT

PALETTE = [TEAL, AMBER, "#7C9CFF", GREEN, RED, "#C77DFF", "#FF8FA3", "#8ED6FF", "#F5D67B", "#9AA5B8", "#5EE0C9"]


def _base(df: pd.DataFrame, x_title: str, value_title: str) -> tuple[pd.DataFrame, str]:
    """Long-form the frame: index -> x column, columns -> 'series'."""
    x = df.index.name or "x"
    long = df.reset_index().rename(columns={df.index.name or "index": x})
    long = long.melt(id_vars=[x], var_name="series", value_name="value")
    return long, x


def _style(chart: alt.Chart, height: int) -> alt.Chart:
    return (chart.properties(height=height, width="container")
            .configure_axis(labelColor=MUTED, titleColor=MUTED, gridColor=LINE, domainColor=LINE, tickColor=LINE)
            .configure_legend(labelColor=TEXT, titleColor=MUTED, orient="top")
            .configure_view(strokeWidth=0)
            .configure(background="transparent"))


def line_chart(df: pd.DataFrame, x_title: str = "", y_title: str = "", height: int = 300) -> None:
    """Fixed multi-series line chart (index = x)."""
    if df.empty:
        st.caption("Nothing to chart.")
        return
    long, x = _base(df, x_title, y_title)
    chart = alt.Chart(long).mark_line(strokeWidth=1.8).encode(
        x=alt.X(f"{x}:Q", title=x_title or x, scale=alt.Scale(domain=[float(df.index.min()), float(df.index.max())], nice=False)),
        y=alt.Y("value:Q", title=y_title, scale=alt.Scale(zero=True)),
        color=alt.Color("series:N", title=None, scale=alt.Scale(range=PALETTE)),
        tooltip=[alt.Tooltip(f"{x}:Q", title=x_title or x), alt.Tooltip("series:N"), alt.Tooltip("value:Q", format=",.1f")],
    )
    st.altair_chart(_style(chart, height), use_container_width=True)


def bar_chart(df: pd.DataFrame, x_title: str = "", y_title: str = "", height: int = 300,
              stacked: bool = True) -> None:
    """Fixed bar chart (index = category or day; several columns = stacked bars)."""
    if df.empty:
        st.caption("Nothing to chart.")
        return
    long, x = _base(df, x_title, y_title)
    numeric_x = pd.api.types.is_numeric_dtype(df.index)
    x_enc = alt.X(f"{x}:O", title=x_title or x, sort=None if numeric_x else list(df.index),
                  axis=alt.Axis(labelAngle=0 if numeric_x or len(df.index) <= 6 else -30))
    single = df.shape[1] == 1
    chart = alt.Chart(long).mark_bar().encode(
        x=x_enc,
        y=alt.Y("value:Q", title=y_title, scale=alt.Scale(zero=True), stack=True if stacked else None),
        color=alt.value(TEAL) if single else alt.Color("series:N", title=None, scale=alt.Scale(range=PALETTE)),
        tooltip=[alt.Tooltip(f"{x}:O", title=x_title or x), alt.Tooltip("series:N"), alt.Tooltip("value:Q", format=",.1f")],
    )
    st.altair_chart(_style(chart, height), use_container_width=True)
