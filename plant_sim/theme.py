"""
Dezignatek brand styling for the Streamlit app - dark edition.

Design tokens come from dezignatek.co.nz: teal #00C3B1 accents, the brand
navy #171F32, thin light headings (Sansation Light) and Avenir body text,
uppercase letter-spaced labels, teal pill buttons. Here they sit on a dark
ground: a deep navy page, brand-navy panels, light text.

Sansation and Avenir are commercial fonts. They're used when installed on
the viewer's machine; otherwise we fall back to the closest freely-available
faces (Quicksand Light for headings, Nunito Sans for body), loaded from
Google Fonts when there's an internet connection and plain sans-serif when
there isn't - the page never depends on them to work.

Nothing in here touches simulation logic - it's CSS and two small HTML
fragments (header wordmark, footer strip). The two HTML components
(floor_editor/index.html and floor_view.py) carry a copy of the same
palette in their own :root block - keep them in step.
"""

from __future__ import annotations

import streamlit as st

# Brand
TEAL = "#00C3B1"
TEAL_DARK = "#00A897"
NAVY = "#171F32"
# Dark ground
BG = "#0E1320"          # page
PANEL = NAVY            # cards, sidebar, tiles
PANEL_RAISED = "#1F2940"  # chips, inputs
LINE = "#2A3550"
TEXT = "#EEF1F6"
MUTED = "#9AA5B8"
# Status
AMBER = "#E0A83E"
RED = "#E5484D"
GREEN = "#2FB37A"

HEADING_FONT = '"Sansation Light", "Sansation", "Quicksand", "Avenir LT Std Book", "Nunito Sans", sans-serif'
BODY_FONT = '"Avenir LT Std Book", "Avenir", "Nunito Sans", "Segoe UI", sans-serif'
BODY_FONT_BLACK = '"Avenir LT Std Black", "Avenir LT Std Heavy", "Avenir", "Nunito Sans", "Segoe UI", sans-serif'

GOOGLE_FONTS_IMPORT = ("@import url('https://fonts.googleapis.com/css2?family=Quicksand:wght@300;400;500"
                       "&family=Nunito+Sans:wght@400;600;700;800&display=swap');")

_PAGE_CSS = f"""
<style>
{GOOGLE_FONTS_IMPORT}

/* ---- base typography ---------------------------------------------- */
html, body, .stApp, [data-testid="stAppViewContainer"], [data-testid="stSidebar"] {{
  font-family: {BODY_FONT};
  color: {TEXT};
}}
.stApp {{ background: {BG}; }}
[data-testid="stHeader"] {{ background: transparent; }}

h1, h2, h3, [data-testid="stHeadingWithActionElements"] h1,
[data-testid="stHeadingWithActionElements"] h2, [data-testid="stHeadingWithActionElements"] h3 {{
  font-family: {HEADING_FONT};
  font-weight: 300;
  letter-spacing: -0.3px;
  color: {TEXT};
}}
h1, [data-testid="stHeadingWithActionElements"] h1 {{ color: {TEAL}; font-size: 2.4rem; line-height: 1.15; }}
h2 {{ font-size: 1.7rem; }}
h3 {{ font-size: 1.25rem; }}
[data-testid="stCaptionContainer"], .stCaption, small {{ color: {MUTED}; }}
a {{ color: {TEAL}; }}

/* ---- brand header + footer ---------------------------------------- */
.dzt-header {{
  display: flex; align-items: center; justify-content: space-between;
  padding: 6px 0 14px; border-bottom: 1px solid {LINE}; margin-bottom: 14px;
}}
.dzt-wordmark {{
  font-family: {HEADING_FONT}; font-weight: 300; font-size: 26px; letter-spacing: 6px;
  text-transform: uppercase; color: {TEAL}; text-decoration: none;
}}
.dzt-wordmark span {{ color: {TEXT}; letter-spacing: 3px; font-size: 13px; margin-left: 14px; }}
.dzt-nav {{ font-family: {BODY_FONT_BLACK}; font-size: 12px; letter-spacing: 1.9px; text-transform: uppercase; color: {TEXT}; }}
.dzt-nav b {{ color: {TEAL}; }}
.dzt-label {{
  font-family: {BODY_FONT_BLACK}; font-size: 12px; letter-spacing: 1.9px; text-transform: uppercase;
  color: {MUTED}; margin: 6px 0 2px;
}}
.dzt-label.teal {{ color: {TEAL}; }}
.dzt-footer {{
  background: {NAVY}; color: {TEXT}; padding: 26px 34px; margin: 40px -1rem -1rem; border-top: 1px solid {LINE};
  display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px;
}}
.dzt-footer .dzt-wordmark {{ color: {TEXT}; font-size: 20px; }}
.dzt-footer small {{ color: {MUTED}; font-size: 12px; letter-spacing: .5px; }}

/* ---- tabs as the site's uppercase nav ------------------------------ */
[data-baseweb="tab-list"] {{ gap: 26px; border-bottom: 1px solid {LINE}; }}
button[data-baseweb="tab"] {{ background: transparent !important; padding: 10px 0; }}
button[data-baseweb="tab"] p {{
  font-family: {BODY_FONT_BLACK}; font-size: 13px; letter-spacing: 1.9px; text-transform: uppercase;
  color: {MUTED};
}}
button[data-baseweb="tab"][aria-selected="true"] p {{ color: {TEAL}; }}
[data-baseweb="tab-highlight"] {{ background-color: {TEAL}; height: 3px; }}
[data-baseweb="tab-border"] {{ display: none; }}

/* ---- buttons as teal pills ----------------------------------------- */
.stButton > button, [data-testid="stBaseButton-primary"], [data-testid="stBaseButton-secondary"] {{
  font-family: {BODY_FONT_BLACK}; font-size: 12px; letter-spacing: 1.9px; text-transform: uppercase;
  border-radius: 24px; padding: 10px 22px; border: 2px solid {TEAL}; transition: all .15s;
}}
[data-testid="stBaseButton-primary"] {{ background: {TEAL}; color: {BG}; }}
[data-testid="stBaseButton-primary"]:hover {{ background: {TEAL_DARK}; border-color: {TEAL_DARK}; color: {BG}; }}
[data-testid="stBaseButton-secondary"] {{ background: transparent; color: {TEAL}; }}
[data-testid="stBaseButton-secondary"]:hover {{ background: {TEAL}; color: {BG}; }}
[data-testid="stBaseButton-secondary"]:disabled {{ border-color: {LINE}; color: {MUTED}; background: transparent; }}

/* ---- metrics as tiles ---------------------------------------------- */
[data-testid="stMetric"] {{
  background: {PANEL}; border-radius: 6px; padding: 14px 16px 12px; border-left: 3px solid {TEAL};
}}
[data-testid="stMetricLabel"] p {{
  font-family: {BODY_FONT_BLACK}; font-size: 11px; letter-spacing: 1.6px; text-transform: uppercase; color: {MUTED};
}}
[data-testid="stMetricValue"] {{ font-family: {HEADING_FONT}; font-weight: 300; color: {TEXT}; }}
[data-testid="stMetricDelta"] {{ font-size: 12px; }}

/* ---- hide Streamlit's own chrome (Deploy menu, footer) -------------- */
[data-testid="stToolbar"], [data-testid="stDecoration"], #MainMenu, footer {{ display: none !important; }}

/* ---- sidebar ------------------------------------------------------- */
[data-testid="stSidebar"] {{ background: {PANEL}; border-right: 1px solid {LINE}; }}
[data-testid="stSidebar"] .stButton > button {{
  text-transform: none; letter-spacing: 0; font-family: {BODY_FONT}; font-size: 12.5px;
  padding: 6px 10px; border-width: 1px; border-radius: 16px; width: 100%;
}}
[data-testid="stSidebar"] h2 {{
  font-family: {BODY_FONT_BLACK}; font-size: 12px; letter-spacing: 1.9px; text-transform: uppercase;
  color: {TEAL}; font-weight: 400; margin-top: 4px;
}}
[data-testid="stSidebar"] [data-testid="stExpander"] details {{ border-color: {LINE}; border-radius: 6px; }}
[data-testid="stSidebar"] hr {{ border-color: {LINE}; }}

/* ---- inputs, tables, alerts --------------------------------------- */
[data-baseweb="input"] > div, [data-baseweb="select"] > div, [data-baseweb="base-input"] {{
  background: {PANEL_RAISED} !important; border-color: {LINE} !important; border-radius: 4px;
}}
[data-testid="stDataFrame"] {{ border: 1px solid {LINE}; border-radius: 6px; }}
[data-testid="stAlert"] {{ border-radius: 6px; }}
hr {{ border-color: {LINE}; }}
[data-testid="stExpander"] details {{ border-color: {LINE}; border-radius: 6px; }}
</style>
"""


def apply_theme() -> None:
    """Inject the brand CSS. Call once, right after st.set_page_config()."""
    st.markdown(_PAGE_CSS, unsafe_allow_html=True)


def brand_header(subtitle: str = "Plant Simulator") -> None:
    st.markdown(
        f"""<div class="dzt-header">
              <a class="dzt-wordmark" href="#">Dezignatek<span>{subtitle}</span></a>
              <div class="dzt-nav">Thermoform <b>·</b> Cut &amp; Clash <b>·</b> Staffing</div>
            </div>""",
        unsafe_allow_html=True,
    )


def label(text: str, teal: bool = False) -> None:
    """The site's small uppercase letter-spaced section label."""
    cls = "dzt-label teal" if teal else "dzt-label"
    st.markdown(f'<div class="{cls}">{text}</div>', unsafe_allow_html=True)


def brand_footer(note: str = "") -> None:
    st.markdown(
        f"""<div class="dzt-footer">
              <div class="dzt-wordmark">Dezignatek</div>
              <small>Internal planning tool · {note}</small>
            </div>""",
        unsafe_allow_html=True,
    )
