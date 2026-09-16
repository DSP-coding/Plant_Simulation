"""
Dezignatek brand styling for the Streamlit app.

Design tokens are lifted straight from dezignatek.co.nz: teal #00C3B1 accents,
navy #171F32 text and footer, white pages with light-grey panels, thin light
headings (Sansation Light) and Avenir body text, uppercase letter-spaced
labels, and teal pill buttons.

Sansation and Avenir are commercial fonts. They're used when installed on
the viewer's machine; otherwise we fall back to the closest freely-available
faces (Quicksand Light for headings, Nunito Sans for body), loaded from
Google Fonts when there's an internet connection and plain sans-serif when
there isn't - the page never depends on them to work.

Nothing in here touches simulation logic - it's CSS and two small HTML
fragments (header wordmark, footer strip).
"""

from __future__ import annotations

import streamlit as st

TEAL = "#00C3B1"
TEAL_DARK = "#00A897"
NAVY = "#171F32"
INK_MUTED = "#6B7280"
PANEL = "#F4F6F8"
LINE = "#E3E7EC"
WHITE = "#FFFFFF"
AMBER = "#E0A83E"
RED = "#E5484D"
GREEN = "#2FB37A"

HEADING_FONT = '"Sansation Light", "Sansation", "Quicksand", "Avenir LT Std Book", "Nunito Sans", sans-serif'
BODY_FONT = '"Avenir LT Std Book", "Avenir", "Nunito Sans", "Segoe UI", sans-serif'
BODY_FONT_BLACK = '"Avenir LT Std Black", "Avenir LT Std Heavy", "Avenir", "Nunito Sans", "Segoe UI", sans-serif'

# Shared by the two HTML components (floor editor / floor playback) so the
# iframes match the page around them.
COMPONENT_CSS_VARS = f"""
  :root {{ --teal:{TEAL}; --teal-dark:{TEAL_DARK}; --navy:{NAVY}; --muted:{INK_MUTED}; --panel:{PANEL};
          --line:{LINE}; --white:{WHITE}; --amber:{AMBER}; --red:{RED}; --green:{GREEN};
          --heading-font:{HEADING_FONT}; --body-font:{BODY_FONT}; --label-font:{BODY_FONT_BLACK}; }}
"""

GOOGLE_FONTS_IMPORT = ("@import url('https://fonts.googleapis.com/css2?family=Quicksand:wght@300;400;500"
                       "&family=Nunito+Sans:wght@400;600;700;800&display=swap');")

_PAGE_CSS = f"""
<style>
{GOOGLE_FONTS_IMPORT}

/* ---- base typography ---------------------------------------------- */
html, body, .stApp, [data-testid="stAppViewContainer"], [data-testid="stSidebar"] {{
  font-family: {BODY_FONT};
  color: {NAVY};
}}
.stApp {{ background: {WHITE}; }}
[data-testid="stHeader"] {{ background: transparent; }}

h1, h2, h3, [data-testid="stHeadingWithActionElements"] h1,
[data-testid="stHeadingWithActionElements"] h2, [data-testid="stHeadingWithActionElements"] h3 {{
  font-family: {HEADING_FONT};
  font-weight: 300;
  letter-spacing: -0.3px;
  color: {NAVY};
}}
h1, [data-testid="stHeadingWithActionElements"] h1 {{ color: {TEAL}; font-size: 2.4rem; line-height: 1.15; }}
h2 {{ font-size: 1.7rem; }}
h3 {{ font-size: 1.25rem; }}
[data-testid="stCaptionContainer"], .stCaption, small {{ color: {INK_MUTED}; }}
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
.dzt-wordmark span {{ color: {NAVY}; letter-spacing: 3px; font-size: 13px; margin-left: 14px; }}
.dzt-nav {{ font-family: {BODY_FONT_BLACK}; font-size: 12px; letter-spacing: 1.9px; text-transform: uppercase; color: {NAVY}; }}
.dzt-nav b {{ color: {TEAL}; }}
.dzt-label {{
  font-family: {BODY_FONT_BLACK}; font-size: 12px; letter-spacing: 1.9px; text-transform: uppercase;
  color: {NAVY}; margin: 6px 0 2px;
}}
.dzt-label.teal {{ color: {TEAL}; }}
.dzt-footer {{
  background: {NAVY}; color: {WHITE}; padding: 26px 34px; margin: 40px -1rem -1rem; border-radius: 0;
  display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px;
}}
.dzt-footer .dzt-wordmark {{ color: {WHITE}; font-size: 20px; }}
.dzt-footer small {{ color: rgba(255,255,255,.7); font-size: 12px; letter-spacing: .5px; }}

/* ---- tabs as the site's uppercase nav ------------------------------ */
[data-baseweb="tab-list"] {{ gap: 26px; border-bottom: 1px solid {LINE}; }}
button[data-baseweb="tab"] {{ background: transparent !important; padding: 10px 0; }}
button[data-baseweb="tab"] p {{
  font-family: {BODY_FONT_BLACK}; font-size: 13px; letter-spacing: 1.9px; text-transform: uppercase;
  color: {NAVY};
}}
button[data-baseweb="tab"][aria-selected="true"] p {{ color: {TEAL}; }}
[data-baseweb="tab-highlight"] {{ background-color: {TEAL}; height: 3px; }}
[data-baseweb="tab-border"] {{ display: none; }}

/* ---- buttons as teal pills ----------------------------------------- */
.stButton > button, [data-testid="stBaseButton-primary"], [data-testid="stBaseButton-secondary"] {{
  font-family: {BODY_FONT_BLACK}; font-size: 12px; letter-spacing: 1.9px; text-transform: uppercase;
  border-radius: 24px; padding: 10px 22px; border: 2px solid {TEAL}; transition: all .15s;
}}
[data-testid="stBaseButton-primary"] {{ background: {TEAL}; color: {WHITE}; }}
[data-testid="stBaseButton-primary"]:hover {{ background: {TEAL_DARK}; border-color: {TEAL_DARK}; color: {WHITE}; }}
[data-testid="stBaseButton-secondary"] {{ background: {WHITE}; color: {TEAL}; }}
[data-testid="stBaseButton-secondary"]:hover {{ background: {TEAL}; color: {WHITE}; }}
[data-testid="stBaseButton-secondary"]:disabled {{ border-color: {LINE}; color: {INK_MUTED}; background: {WHITE}; }}

/* ---- metrics as clean tiles ---------------------------------------- */
[data-testid="stMetric"] {{
  background: {PANEL}; border-radius: 6px; padding: 14px 16px 12px; border-left: 3px solid {TEAL};
}}
[data-testid="stMetricLabel"] p {{
  font-family: {BODY_FONT_BLACK}; font-size: 11px; letter-spacing: 1.6px; text-transform: uppercase; color: {NAVY};
}}
[data-testid="stMetricValue"] {{ font-family: {HEADING_FONT}; font-weight: 300; color: {NAVY}; }}
[data-testid="stMetricDelta"] {{ font-size: 12px; }}

/* ---- hide Streamlit's own chrome (Deploy menu, footer) -------------- */
[data-testid="stToolbar"], [data-testid="stDecoration"], #MainMenu, footer {{ display: none !important; }}

/* ---- sidebar ------------------------------------------------------- */
[data-testid="stSidebar"] {{ background: {WHITE}; border-right: 1px solid {LINE}; }}
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
  border-color: {LINE} !important; border-radius: 4px;
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
