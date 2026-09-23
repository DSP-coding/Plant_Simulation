"""
Streamlit front-end for the thermoform / cut-and-clash plant simulator.

Run with:
    streamlit run app.py

Layout:
    Sidebar   - order intake, target lead time, area capacities, product mix,
                shift schedules, remake and sick-leave settings.
    Tab 1     - Staff & Skills: view every operator, click one to edit their
                skills / home station / shift / personal absence rate.
    Tab 2     - Simulation Results: run the sim and see the outcome (KPIs,
                reality check against real plant figures, live floor view,
                buffer levels, DIFOT, lead time, absenteeism, per-class mix).

Everything here just calls into plant_sim/*.py - if you want to change how the
factory *behaves*, edit that package. This file only wires up the UI.
"""

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from plant_sim import config as cfg
from plant_sim.charts import bar_chart, line_chart
from plant_sim.constraints import analyse as analyse_constraints
from plant_sim.floor_editor import apply_move, floor_editor, restore, snapshot
from plant_sim.floor_view import build_floor_html
from plant_sim import persist
from plant_sim.simulation import Simulator, SimulationSettings
from plant_sim.staff import Operator, Roster, RosterError
from plant_sim.theme import apply_theme, brand_footer, label

st.set_page_config(page_title="Dezignatek | Plant Simulator", page_icon="🏭", layout="wide",
                   menu_items={"About": "Dezignatek thermoform + cut & clash plant simulator."})
apply_theme()

ROSTER_CSV_PATH = "data/staff_roster.csv"
STATION_OPTIONS = list(cfg.STATIONS.keys())
STATION_LABELS = {sid: st_.label for sid, st_ in cfg.STATIONS.items()}
INTAKE_SOURCE = {
    "day": "product_2026.xlsx, Jan-Sep 2026 (per working day)",
    "week": "product_2026.xlsx, Jan-Sep 2026 (full calendar weeks)",
    "month": "13-month production tracker, Jun-25 to Jun-26",
}


def station_label(sid: str) -> str:
    return STATION_LABELS.get(sid, sid)


def short_station_label(sid: str) -> str:
    """'CNC 1536 (Cut & Clash)' -> 'CNC 1536' - for metric tiles, which truncate."""
    return station_label(sid).split(" (")[0]


def day_unit_for(route: str) -> str:
    return "working days" if cfg.LEAD_TIME_EXCLUDES_WEEKENDS.get(route, False) else "calendar days"


# ---------------------------------------------------------------------------
# Session state bootstrap
# ---------------------------------------------------------------------------

def load_roster() -> Roster:
    try:
        return Roster.from_csv(ROSTER_CSV_PATH)
    except RosterError as e:
        st.error(f"Could not load the staff roster:\n\n```\n{e}\n```\n\n"
                 f"Fix `{ROSTER_CSV_PATH}` and reload the page.")
        st.stop()


if "roster" not in st.session_state:
    st.session_state.roster = load_roster()
if "sim_result" not in st.session_state:
    st.session_state.sim_result = None
if "roster_history" not in st.session_state:
    st.session_state.roster_history = []      # undo stack for floor-editor moves
if "floor_last_seq" not in st.session_state:
    st.session_state.floor_last_seq = None    # last drop event already applied

roster: Roster = st.session_state.roster

if "roster_backed_up" not in st.session_state:
    st.session_state.roster_backed_up = False
if "saved_settings" not in st.session_state:
    st.session_state.saved_settings = persist.load_settings()


def persist_roster() -> None:
    """Write the roster straight back to its CSV (first change of the
    session takes a timestamped backup first)."""
    if roster.problems():
        return                                   # never persist a roster the app can't reload
    if not st.session_state.roster_backed_up:
        persist.backup_roster(ROSTER_CSV_PATH)
        st.session_state.roster_backed_up = True
    try:
        roster.to_csv(ROSTER_CSV_PATH)
    except OSError as e:
        st.error(f"Could not save the roster to {ROSTER_CSV_PATH}: {e}")


SETTING_DEFAULTS: dict[str, object] = {}


def seed(key: str, default):
    """Give a sidebar widget its starting value: what was saved last time
    (if the user had changed it), else the current config default. Widgets
    are created with key= only - the value lives in session_state."""
    SETTING_DEFAULTS[key] = default
    if key not in st.session_state:
        st.session_state[key] = persist.seeded_value(key, default, st.session_state.saved_settings)
    return st.session_state[key]


st.markdown("# Dezignatek Factory Simulation")
st.caption("Plant Planning")

# ---------------------------------------------------------------------------
# Sidebar: intake, mix, target, shifts, remake, sick leave
# ---------------------------------------------------------------------------

settings_problems: list[str] = []

with st.sidebar:
    st.header("Order intake & target")
    seed("horizon", "month")
    horizon = st.selectbox("Horizon", list(cfg.HORIZON_DAYS.keys()), key="horizon",
                           help="day = 1 Monday, week = 7 days from Monday, month = 30 days from Monday")

    # The intake number means "total m2 FOR THE SELECTED HORIZON" - i.e. if
    # you pick Week, this is m2 for the whole week, not m2/day. Each horizon
    # remembers its own last-entered value (separate session_state keys) so
    # switching horizons doesn't silently reinterpret your number.
    # One intake box PER PRODUCT RANGE: the two lines take orders
    # independently, so Thermo can be pushed to 13,000 m2 without inventing
    # Cut & Clash volume to match. Each horizon keeps its own value.
    #
    # Settings saved before the split hold one combined "intake_m2_<horizon>"
    # per horizon. Carry those over by splitting them with the product mix, so
    # upgrading doesn't quietly throw away the number someone was working to.
    saved_values = st.session_state.saved_settings.get("values", {})
    for h in cfg.HORIZON_DAYS:
        old_total = saved_values.get(f"intake_m2_{h}")
        for route in cfg.ROUTE_SEQUENCE:
            default = cfg.REAL_INTAKE_M2[h][route]["median"]
            if old_total and not saved_values.get(f"intake_m2_{route}_{h}"):
                route_share = sum(cfg.DEFAULT_MIX_PCT[c] for c, pc in cfg.PRODUCT_CLASSES.items()
                                   if pc.route == route)
                default = round(old_total * route_share / 100.0, 1)
            seed(f"intake_m2_{route}_{h}", default)

    st.caption(f"Presets from retrived Tracker data ({INTAKE_SOURCE[horizon]}) for a typical **{horizon}**. "
               f"Orders arrive on weekdays between hour {cfg.INTAKE_WINDOW_HOURS[0]:.0f} and "
               f"{cfg.INTAKE_WINDOW_HOURS[1]:.0f} of the day shift. The product mix below splits each "
               "range internally (Series 1/2/3 within Thermo, Melamine/Acrylic within Cut & Clash).")
    # NOTE: no `value=` on any keyed widget below - each value lives in
    # st.session_state (seeded from the saved settings file), which is also
    # what lets the preset buttons write to the intake boxes.
    intake_m2_by_route = {}
    for route, route_label in ((cfg.Route.THERMO, "Thermo"), (cfg.Route.CUT_AND_CLASH, "Cut & Clash")):
        key = f"intake_m2_{route}_{horizon}"
        stats = cfg.REAL_INTAKE_M2[horizon][route]
        preset_cols = st.columns(2)
        if preset_cols[0].button(f"Median ({stats['median']:,.0f} m²)", key=f"preset_med_{route}"):
            st.session_state[key] = stats["median"]
        if preset_cols[1].button(f"Busiest ({stats['max']:,.0f} m²)", key=f"preset_max_{route}"):
            st.session_state[key] = stats["max"]
        intake_m2_by_route[route] = st.number_input(f"{route_label} intake (m² / {horizon})",
                                                     min_value=0.0, step=10.0, key=key)
    intake_m2_for_horizon = sum(intake_m2_by_route.values())
    st.caption(f"**Total {horizon}: {intake_m2_for_horizon:,.0f} m²** "
               f"(Cut & Clash "
               f"{100 * intake_m2_by_route[cfg.Route.CUT_AND_CLASH] / intake_m2_for_horizon:.1f}%)"
               if intake_m2_for_horizon > 0 else "**Total: 0 m²** - nothing will be simulated.")

    st.caption("Cut & Clash (1536) quotes its own lead time, separate from Thermo. Both targets and "
               "DIFOT are measured in WORKING days (Mon-Fri, weekends excluded), matching the real "
               "lead-time dashboard.")
    seed("target_thermo", cfg.DEFAULT_TARGET_LEAD_DAYS[cfg.Route.THERMO])
    seed("target_cutclash", cfg.DEFAULT_TARGET_LEAD_DAYS[cfg.Route.CUT_AND_CLASH])
    target_lead_thermo = st.number_input("Target lead time - Thermo (working days)", min_value=0.0, step=0.5,
                                          key="target_thermo")
    target_lead_cutclash = st.number_input("Target lead time - Cut & Clash (working days)", min_value=0.0,
                                            step=0.5, key="target_cutclash")
    target_lead_days = {cfg.Route.THERMO: target_lead_thermo, cfg.Route.CUT_AND_CLASH: target_lead_cutclash}

    st.divider()
    st.header("Area capacities (m² / month)")
    st.caption("Known capacities of machines. Note: capacities change depending on staffing and absenteeism.")
    people_gated = ", ".join(station_label(sid) for sid in cfg.PEOPLE_GATED_STATIONS)
    st.caption(f"**{people_gated}** are people-gated: the figure is what ONE person gets through, "
               "and with nobody on the station its capacity is zero - a second person there covers "
               "breaks or the afternoon rather than making the machine run faster.")
    station_capacity_m2_per_month = {}
    # Flow order matching how the factory actually runs, CNC/Sanding/MB Sander/
    # Press/Cefla first (the ones you're most likely to have real numbers for).
    capacity_order = ["cnc_thermo", "sanding", "mb_sander", "press_1", "press_2", "edging",
                       "despatch", "optimising", "cnc_1536", "edge_bander", "drilling"]
    for sid in capacity_order:
        seed(f"cap_{sid}", float(round(cfg.default_station_capacity_m2_per_month(sid), 0)))
        gated = " (per person; 0 when unstaffed)" if sid in cfg.PEOPLE_GATED_STATIONS else ""
        station_capacity_m2_per_month[sid] = st.number_input(
            f"{station_label(sid)}{gated}", min_value=0.0, step=100.0, key=f"cap_{sid}")

    st.divider()
    st.header("Product mix (%)")
    st.caption("The split WITHIN each range: Series 1/2/3 share of Thermo, Melamine/Acrylic share of "
               "Cut & Clash. How much each range gets in total comes from the two intake boxes above. "
               "The S1/S2/S3 mix can change depending on market trend.")
    mix_pct = {}
    for cls, pc in cfg.PRODUCT_CLASSES.items():
        seed(f"mix_{cls}", float(round(cfg.DEFAULT_MIX_PCT[cls], 2)))
        mix_pct[cls] = st.number_input(f"{pc.label}", min_value=0.0, max_value=100.0, step=0.1, key=f"mix_{cls}")
    mix_sum = sum(mix_pct.values())
    if mix_sum <= 0:
        settings_problems.append("Product mix: at least one class needs a share above 0%.")
    elif abs(mix_sum - 100.0) > 0.5:
        st.warning(f"Mix sums to {mix_sum:.1f}%, not 100% - it will be scaled to 100% so the "
                   "intake total you entered is honoured.")

    st.divider()
    st.header("Shift schedules (per crew)")
    st.caption("Hour 0 of each simulated day is 6am, the plant's reference day-shift start; the afternoon "
               "shift follows straight on. A crew on N days/week works Monday to day N. Each crew has its own "
               "start time: set the CNC crew to -2 and it runs from 4am, building WIP in front of the "
               "sanders and the Cefla before those crews arrive at 6am.")
    shift_schedules = {}

    def _clock(offset: float) -> str:
        mins = int(round((6.0 + offset) * 60)) % (24 * 60)
        return f"{mins // 60:02d}:{mins % 60:02d}"

    for crew_name, default_sched in cfg.DEFAULT_SHIFT_SCHEDULES.items():
        with st.expander(cfg.CREW_LABELS.get(crew_name, crew_name), expanded=False):
            seed(f"crew_{crew_name}_days", int(default_sched.days_per_week))
            seed(f"crew_{crew_name}_dayhrs", float(default_sched.day_hrs))
            seed(f"crew_{crew_name}_aftenabled", bool(default_sched.aft_enabled))
            seed(f"crew_{crew_name}_afthrs", float(default_sched.aft_hrs))
            seed(f"crew_{crew_name}_start", float(default_sched.start_hour))
            days = st.slider("Days/week", 0, 7, key=f"crew_{crew_name}_days")
            start = st.number_input("Day shift starts (hours before/after 6am; -2 = 4am, +1 = 7am)",
                                    float(cfg.MIN_CREW_START_HOUR), float(cfg.MAX_CREW_START_HOUR), step=0.5,
                                    key=f"crew_{crew_name}_start")
            day_hrs = st.number_input("Day shift hours", 0.0, 24.0, key=f"crew_{crew_name}_dayhrs")
            aft_enabled = st.checkbox("Afternoon shift enabled", key=f"crew_{crew_name}_aftenabled")
            aft_hrs = st.number_input("Afternoon shift hours", 0.0, 24.0, key=f"crew_{crew_name}_afthrs")
            try:
                shift_schedules[crew_name] = cfg.ShiftSchedule(days, day_hrs, aft_enabled, aft_hrs, start)
                end_day = start + day_hrs
                clock = f"day {_clock(start)}-{_clock(end_day)}"
                if aft_enabled and aft_hrs > 0:
                    clock += f", afternoon {_clock(end_day)}-{_clock(end_day + aft_hrs)}"
                st.caption(f"{days} day(s)/week: {clock}")
            except ValueError as e:
                st.error(str(e))
                settings_problems.append(f"{crew_name} crew schedule: {e}")

    st.divider()
    st.header("Remakes & special orders")
    seed("special_pct", float(cfg.DEFAULT_SPECIAL_ORDER_PCT))
    seed("remake_enabled", True)
    seed("remake_rate", float(cfg.DEFAULT_REMAKE_RATE_PCT))
    seed("remake_days", float(cfg.DEFAULT_REMAKE_DAYS))
    special_order_pct = st.number_input("Special orders (% of Thermo intake)", 0.0, 100.0, step=1.0,
                                        key="special_pct",
                                        help=f"Non-standard Thermo work. Specials and remakes are cut on "
                                             f"{cfg.CNC_THERMO_SPECIAL_MACHINE} first; the other CNCs cut regular "
                                             "work first. The real share isn't known yet - this default is a guess.")
    remake_enabled = st.checkbox("Enable remake loop", key="remake_enabled")
    remake_rate_pct = st.number_input("Remake rate (% of packing output)", 0.0, 100.0, step=0.1, key="remake_rate")
    remake_days = st.number_input("Remake hold before re-entering the line (days)", 0.0, 30.0, step=0.1,
                                   key="remake_days",
                                   help="Remakes keep their original order date, so they jump the "
                                        "queue on their second pass. The real dashboard says remakes "
                                        f"take {cfg.REAL_REMAKE_LEAD_PENALTY_DAYS} working days longer "
                                        "in total - the results tab shows what this setting produces.")

    st.divider()
    st.header("Order scheduling (Optimising)")
    st.caption(f"Online orders are taken until 4pm (hour {cfg.ONLINE_ORDER_CUTOFF_HOUR:.0f} of the day). Each "
               "weekday morning Optimising schedules the afternoon shift's remakes, the new orders and anything "
               "still pending onto the CNCs. An order placed on working day D goes on the schedule on the "
               "morning of working day D+n. Nobody on Optimising that morning = nothing released.")
    seed("release_days_thermo", int(cfg.DEFAULT_RELEASE_WORKING_DAYS[cfg.Route.THERMO]))
    seed("release_days_cutclash", int(cfg.DEFAULT_RELEASE_WORKING_DAYS[cfg.Route.CUT_AND_CLASH]))
    release_working_days = {
        cfg.Route.THERMO: st.number_input("Thermo: working days from order to CNC release", 0, 15, step=1,
                                          key="release_days_thermo",
                                          help="Read off the real CNC schedule: orders dated Wed 16 Sep were cut "
                                               "Mon 21 Sep, Thu 17 Sep on Tue 22 Sep - 3 working days."),
        cfg.Route.CUT_AND_CLASH: st.number_input("Cut & Clash: working days from order to 1536 release", 0, 15, step=1,
                                                 key="release_days_cutclash",
                                                 help="Not confirmed - assumed next morning."),
    }

    st.divider()
    st.header("Buffers & press batching")
    st.caption("The floor keeps WIP in front of the Cefla and the presses on purpose, and runs the presses "
               "in batches once a pile has built. The analysis checks these targets every running hour.")
    seed("buf_target_edging", float(cfg.BUFFER_TARGET_M2_BY_QUEUE.get("edging", 0.0)))
    seed("buf_target_press", float(cfg.BUFFER_TARGET_M2_BY_QUEUE.get(cfg.PRESS_QUEUE_ID, 0.0)))
    seed("press_start", float(cfg.DEFAULT_PRESS_BATCH_START_M2))
    seed("press_stop", float(cfg.DEFAULT_PRESS_BATCH_STOP_M2))
    buffer_targets_m2 = {
        "edging": st.number_input("Target WIP in front of Cefla (m²)", 0.0, 5000.0, step=10.0, key="buf_target_edging"),
        cfg.PRESS_QUEUE_ID: st.number_input("Target WIP in front of the presses (m²)", 0.0, 5000.0, step=10.0,
                                            key="buf_target_press"),
    }
    press_batch_start_m2 = st.number_input("Presses start a run at (m² in the pile; 0 = run continuously)",
                                           0.0, 5000.0, step=10.0, key="press_start")
    press_batch_stop_m2 = st.number_input("...and stop when the pile is down to (m²)", 0.0, 5000.0, step=10.0,
                                          key="press_stop")
    if press_batch_start_m2 > 0 and press_batch_stop_m2 >= press_batch_start_m2:
        settings_problems.append("Press batching: the stop level must be below the start level.")
    with st.expander("WIP space limits in front of each station (m²)", expanded=False):
        st.caption("Real rack / trolley space. 0 = no limit. A station whose downstream buffer is "
                   "full stops (\"blocked\") - the constraints analysis reports those hours.")
        buffer_caps_m2 = {}
        for qid in ["cnc_thermo", "sanding", "mb_sander", "edging", cfg.PRESS_QUEUE_ID, "despatch",
                    "optimising", "cnc_1536", "edge_bander", "drilling"]:
            lbl = "Press (shared pile)" if qid == cfg.PRESS_QUEUE_ID else station_label(qid)
            seed(f"buf_{qid}", float(cfg.BUFFER_CAP_M2_BY_STATION.get(qid, 0.0)))
            buffer_caps_m2[qid] = st.number_input(f"Before {lbl}", min_value=0.0, step=10.0, key=f"buf_{qid}")

    st.divider()
    st.header("Sick leave")
    seed("sick_enabled", False)
    seed("seed", 42)
    sick_enabled = st.checkbox("Enable random sick leave (per-person rate, edit in Staff tab)", key="sick_enabled")
    seed_value = st.number_input("Random seed (change to reroll)", step=1, key="seed")

    st.divider()
    # -- persist the sidebar: saved whenever anything changes, restored next open --
    current_values = {k: st.session_state[k] for k in SETTING_DEFAULTS}
    if current_values != st.session_state.saved_settings.get("values"):
        try:
            persist.save_settings(current_values, SETTING_DEFAULTS)
            st.session_state.saved_settings = {"values": dict(current_values), "defaults": dict(SETTING_DEFAULTS)}
        except OSError as e:
            st.warning(f"Could not save settings: {e}")
    st.caption(f"Settings are saved automatically to `{persist.SETTINGS_PATH}` and restored next time.")
    if st.button("Reset all settings to config.py defaults"):
        persist.clear_settings()
        for k in list(SETTING_DEFAULTS):
            st.session_state.pop(k, None)
        st.session_state.saved_settings = {"values": {}, "defaults": {}}
        st.rerun()


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------

def run_simulation() -> None:
    """Build settings from the sidebar, run, and store the result (or show why not)."""
    try:
        settings = SimulationSettings(
            horizon=horizon, intake_m2_by_route=intake_m2_by_route, mix_pct=mix_pct,
            target_lead_days=target_lead_days, shift_schedules=shift_schedules,
            station_capacity_m2_per_month=station_capacity_m2_per_month,
            remake_enabled=remake_enabled, remake_rate_pct=remake_rate_pct, remake_days=remake_days,
            special_order_pct=special_order_pct,
            release_working_days={k: int(v) for k, v in release_working_days.items()},
            sick_enabled=sick_enabled, random_seed=int(seed_value),
            buffer_caps_m2={q: v for q, v in buffer_caps_m2.items() if v > 0},
            buffer_targets_m2={q: v for q, v in buffer_targets_m2.items() if v > 0},
            press_batch_start_m2=press_batch_start_m2, press_batch_stop_m2=press_batch_stop_m2,
        )
        with st.spinner("Simulating..."):
            st.session_state.sim_result = Simulator(roster, settings).run()
    except (ValueError, RosterError) as e:
        st.error(f"Could not run the simulation:\n\n```\n{e}\n```")


def kpi_strip(result) -> None:
    """One-line summary of a run, for the floor tab."""
    cols = st.columns(5)
    cols[0].metric("Completion %", f"{result.completion_pct:.0f}%")
    cols[1].metric("DIFOT %", f"{result.overall_difot_pct:.0f}%" if result.has_completions else "—")
    for col, rm in zip(cols[2:4], result.by_route.values()):
        col.metric(f"{rm.label} lead ({day_unit_for(rm.route)})",
                   f"{rm.overall_avg_lead_days:.1f}" if rm.has_completions else "—",
                   f"DIFOT {rm.overall_difot_pct:.0f}%" if rm.has_completions else None,
                   delta_color="off")
    bottleneck = max(result.station_utilisation, key=result.station_utilisation.get)
    cols[4].metric("Bottleneck", short_station_label(bottleneck),
                   f"{result.station_utilisation[bottleneck] * 100:.0f}% utilised", delta_color="off")


tab_floor, tab_staff, tab_results = st.tabs(["🏭 Factory Floor", "Staff & Skills", "Simulation Results"])

with tab_floor:
    label("Factory floor", teal=True)
    st.subheader("Move people around the floor")
    st.caption("Each box is a station; each chip is a person on that shift. **Drag** a chip onto "
               "another station to change their home station, or into the other lane to change "
               "**Ctr + Drag** to copy staff into another station within their skill set. Copied staff is a shared resource. "
               "their shift. Then **Run** to see what it does to lead time and DIFOT. Changes stay "
               "in this session until you save the roster on the Staff & Skills tab.")

    event = floor_editor(roster, shift_schedules or None)
    if event and event.get("seq") != st.session_state.floor_last_seq:
        st.session_state.floor_last_seq = event.get("seq")
        before = snapshot(roster)
        described = apply_move(roster, event)
        if described:
            st.session_state.roster_history.append((described, before))
            st.session_state.roster_history = st.session_state.roster_history[-30:]
            persist_roster()
            st.rerun()

    ctl1, ctl2, ctl3 = st.columns([1, 1, 3])
    if ctl1.button("▶ Run simulation", type="primary", key="run_floor", disabled=bool(settings_problems)):
        run_simulation()
    history = st.session_state.roster_history
    if ctl2.button(f"↶ Undo last move ({len(history)})", disabled=not history, key="undo_floor"):
        described, before = history.pop()
        restore(roster, before)
        persist_roster()
        st.rerun()
    if history:
        ctl3.caption("Recent moves: " + " · ".join(d for d, _ in history[-4:]))

    if st.session_state.sim_result is not None:
        st.divider()
        kpi_strip(st.session_state.sim_result)
        st.caption("Full breakdown, charts and the live playback are on the **Simulation Results** tab.")

with tab_staff:
    label("Staff & skills", teal=True)
    st.subheader("Roster overview")
    roster_problems = roster.problems()
    if roster_problems:
        st.error("The roster has problems that will stop the simulation running:\n\n- "
                 + "\n- ".join(roster_problems))
    rows = []
    for op in roster.operators:
        rows.append({
            "ID": op.id, "Name": op.name,
            "Home station": station_label(op.home_station),
            "Shift": op.shift,
            "Extra skills": ", ".join(station_label(s) for s in sorted(op.skills)) or "-",
            "Absence rate %": op.absence_rate_pct,
        })
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

    if not roster.operators:
        st.warning("The roster is empty - add staff below before running a simulation.")
    else:
        st.subheader("Click a staff member to edit skills / roster details")
        names = {f"{op.name} - {station_label(op.home_station)}, {op.shift} [id {op.id}]": op.id
                 for op in roster.operators}
        selected_label = st.selectbox("Staff member", list(names.keys()))
        op = roster.get(names[selected_label])

        col1, col2 = st.columns(2)
        with col1:
            home_index = STATION_OPTIONS.index(op.home_station) if op.home_station in STATION_OPTIONS else 0
            new_home = st.selectbox("Home station", STATION_OPTIONS, index=home_index,
                                     format_func=station_label, key=f"home_{op.id}")
            shift_index = list(cfg.SHIFT_LABELS).index(op.shift) if op.shift in cfg.SHIFT_LABELS else 0
            new_shift = st.selectbox("Shift", list(cfg.SHIFT_LABELS), index=shift_index,
                                      key=f"shift_{op.id}")
        with col2:
            new_absence = st.number_input("Personal absence rate (% per working day)", 0.0, 100.0,
                                           float(op.absence_rate_pct), step=0.1, key=f"abs_{op.id}")
            new_skills = st.multiselect(
                "Extra skills (stations this person can also cover)",
                [s for s in STATION_OPTIONS if s != new_home],
                default=[s for s in op.skills if s != new_home and s in STATION_OPTIONS],
                format_func=station_label, key=f"skills_{op.id}",
            )

        st.text_area("Notes", value=op.notes, key=f"notes_{op.id}")

        btn_apply, btn_remove = st.columns(2)
        if btn_apply.button("Apply changes to this staff member", type="primary"):
            op.home_station = new_home
            op.shift = new_shift
            op.absence_rate_pct = new_absence
            op.skills = set(new_skills)
            op.notes = st.session_state[f"notes_{op.id}"]
            persist_roster()
            st.success(f"Updated {op.name} (saved).")
            # No explicit st.rerun() here: the button click itself already
            # triggers a rerun, so the mutation above is reflected as soon as
            # this script run finishes rendering - an extra rerun would just
            # wipe this success message before it's shown.
        if btn_remove.button(f"Remove {op.name} from roster"):
            roster.remove(op.id)
            persist_roster()
            st.rerun()

    st.divider()
    with st.expander("Add a new staff member"):
        new_id = st.text_input("ID (unique)").strip()
        new_name = st.text_input("Name").strip()
        add_home = st.selectbox("Home station", STATION_OPTIONS, format_func=station_label, key="add_home")
        add_shift = st.selectbox("Shift", list(cfg.SHIFT_LABELS), key="add_shift")
        add_absence = st.number_input("Absence rate %", 0.0, 100.0, cfg.DEFAULT_ABSENCE_RATE_PCT,
                                       key="add_absence")
        if st.button("Add staff member"):
            if not new_id or not new_name:
                st.error("ID and name are required.")
            elif roster.get(new_id):
                st.error(f"ID {new_id} already exists.")
            else:
                roster.add(Operator(id=new_id, name=new_name, home_station=add_home,
                                     shift=add_shift, absence_rate_pct=add_absence))
                persist_roster()
                st.rerun()

    st.caption(f"Every change to staff is saved straight to `{ROSTER_CSV_PATH}`. The first change of each "
               f"session takes a backup first (last {persist.ROSTER_BACKUPS_TO_KEEP} kept).")
    backups = persist.list_backups()
    if backups:
        b1, b2 = st.columns([3, 1])
        chosen = b1.selectbox("Restore the roster from a backup", backups, format_func=persist.backup_label,
                              key="restore_backup")
        if b2.button("Restore", key="restore_backup_btn"):
            try:
                restored = Roster.from_csv(chosen)
            except RosterError as e:
                st.error(f"That backup can't be loaded:\n\n```\n{e}\n```")
            else:
                st.session_state.roster = restored
                st.session_state.roster_history = []
                roster = restored
                persist_roster()
                st.rerun()

with tab_results:
    if settings_problems:
        st.error("Fix these sidebar settings before running:\n\n- " + "\n- ".join(settings_problems))
    if st.button("▶ Run simulation", type="primary", key="run_results", disabled=bool(settings_problems)):
        run_simulation()

    result = st.session_state.sim_result
    if result is None:
        st.info("Configure settings in the sidebar, then click **Run simulation**.")
    else:
        rs = result.settings
        st.caption(f"Horizon: **{rs.horizon}** ({rs.horizon_days()} days, after a "
                   f"{rs.warmup_days}-day warm-up so queues start at steady state). "
                   f"Intake of {rs.intake_m2_for_horizon:,.0f} m² spread over "
                   f"{result.intake_hours_in_window} weekday intake hours.")
        for note in result.notes:
            st.info(note)
        if rs.horizon == "day":
            st.caption("A single day's completion % is naturally noisy (Monday clears Friday's "
                       "intake, and the Thermo crews only run 4 days) - use week or month "
                       "for capacity questions.")

        completion_pct = result.completion_pct
        # Bottleneck = the busiest station while it is running. A station
        # that is never staffed shows 0% here - see the staffing gaps below.
        bottleneck = max(result.station_utilisation, key=result.station_utilisation.get)

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Intake (m²)", f"{result.cum_intake_m2:,.0f}")
        c1.metric("Completed (good, m²)", f"{result.cum_completed_m2:,.0f}")
        c2.metric("Completion %", f"{completion_pct:.1f}%",
                  help="Completed / intake over the window. Below ~100% at steady state means "
                       "WIP is growing somewhere - check the buffer chart.")
        c2.metric("Remade (m²)", f"{result.cum_remade_m2:,.0f}")
        c3.metric("Overdue backlog (m²)", f"{result.overdue_backlog_m2:,.0f}",
                  help="Work still inside the factory at the end of the run that has already "
                       "missed its target date. Not part of DIFOT (it counts as late when it "
                       "eventually completes).")
        c3.metric("Bottleneck", short_station_label(bottleneck),
                  f"{result.station_utilisation[bottleneck] * 100:.0f}% utilised")
        c4.metric("Avg lead time (days)",
                  f"{result.overall_avg_lead_days:.1f}" if result.has_completions else "—",
                  help="Blended across both ranges, in working days (Mon-Fri) - see the "
                       "per-range breakdown below.")
        c4.metric("DIFOT %", f"{result.overall_difot_pct:.0f}%" if result.has_completions else "—",
                  help="Share of completed m² that met its own range's target lead time - the "
                       "same definition as the real dashboard.")

        # -- staffing gaps: a running station with work waiting and nobody on it --
        gaps = {sid: h for sid, h in result.station_unstaffed_hours.items() if h > 0}
        if gaps:
            gap_lines = [f"**{station_label(sid)}**: {h:.0f} of {result.station_active_hours[sid]:.0f} "
                         f"running hours with work waiting and nobody on it"
                         for sid, h in sorted(gaps.items(), key=lambda kv: -kv[1])]
            st.warning("Staffing gaps (the crew was scheduled but no one qualified was rostered "
                       "on that shift - a bottleneck utilisation can't show this):\n\n- "
                       + "\n- ".join(gap_lines))

        st.divider()
        st.subheader("By product range (Thermo vs Cut & Clash) - each judged against its own target lead time")
        route_cols = st.columns(len(result.by_route))
        for col, rm in zip(route_cols, result.by_route.values()):
            unit = day_unit_for(rm.route)
            with col:
                st.markdown(f"**{rm.label}** (target {rm.target_lead_days:.0f} {unit})")
                st.metric("Completed (m²)", f"{rm.completed_m2:,.0f}")
                st.metric(f"Avg lead ({unit})", f"{rm.overall_avg_lead_days:.1f}" if rm.has_completions else "—")
                st.metric("DIFOT %", f"{rm.overall_difot_pct:.0f}%" if rm.has_completions else "—")
                st.metric("Overdue backlog (m²)", f"{rm.overdue_backlog_m2:,.0f}")
                penalty = rm.remake_lead_penalty_days
                st.metric("Remake lead-time penalty",
                          f"{penalty:+.2f} {unit}" if penalty is not None else "—",
                          help=f"Avg lead of remade work minus non-remade ({rm.remake_share_pct:.1f}% of "
                               "completed m² had been remade).")

        # -- Optimising's morning release onto the CNCs --
        st.divider()
        label("Scheduling", teal=True)
        st.subheader("Morning CNC schedule - what Optimising released each day")
        st.caption("Each weekday morning Optimising schedules the pending pool onto the CNCs: new orders that have "
                   "reached their release day, plus remakes from the afternoon shift. Orders wait in the pool "
                   "(counted in lead time) until then.")
        s1, s2, s3, s4 = st.columns(4)
        s1.metric("Scheduling mornings", f"{result.scheduling_mornings}")
        s2.metric("Mornings nobody scheduled", f"{result.scheduling_mornings_missed}",
                  help="Weekday mornings with nobody on Optimising - the pool carried over.")
        s3.metric("Pending scheduling (avg)", f"{result.pending_m2_avg:,.0f} m²",
                  help="Average m² ordered but not yet released to a CNC.")
        s4.metric("Pending at end of run", f"{result.pending_m2_end:,.0f} m²")
        if result.scheduling_mornings_missed:
            st.warning(f"On {result.scheduling_mornings_missed} morning(s) nobody was on Optimising, so nothing was "
                       "scheduled onto the CNCs - orders sat in the pool until the next staffed morning.")
        if result.release_log:
            weekday_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
            rows: dict[int, dict] = {}
            for e in result.release_log:
                row = rows.setdefault(e["day"], {"Day": e["day"] + 1, "Weekday": weekday_names[e["day"] % 7],
                                                 "Thermo CNCs (m²)": 0.0, "of which specials → C6": 0.0,
                                                 "Thermo remakes → C6": 0.0, "CNC 1536 (m²)": 0.0,
                                                 "Cut & Clash remakes": 0.0, "Oldest order (days ago)": 0})
                age = e["day"] - e["order_day"]
                row["Oldest order (days ago)"] = max(row["Oldest order (days ago)"], age)
                if e["route"] == cfg.Route.THERMO:
                    if e["remake"]:
                        row["Thermo remakes → C6"] += e["qty"]
                    else:
                        row["Thermo CNCs (m²)"] += e["qty"]
                        if e["special"]:
                            row["of which specials → C6"] += e["qty"]
                else:
                    if e["remake"]:
                        row["Cut & Clash remakes"] += e["qty"]
                    else:
                        row["CNC 1536 (m²)"] += e["qty"]
            sched_df = pd.DataFrame([rows[d] for d in sorted(rows)]).set_index("Day")
            sched_df["Total released (m²)"] = (sched_df["Thermo CNCs (m²)"] + sched_df["Thermo remakes → C6"]
                                               + sched_df["CNC 1536 (m²)"] + sched_df["Cut & Clash remakes"])
            st.dataframe(sched_df.round(1), use_container_width=True, height=min(400, 40 + 35 * len(sched_df)))
            chart_df = sched_df[["Thermo CNCs (m²)", "Thermo remakes → C6", "CNC 1536 (m²)", "Cut & Clash remakes"]]
            bar_chart(chart_df, x_title="day", y_title="m² released", height=220)

        # -- the C6 lane at the Thermo CNCs --
        lanes = result.cnc_thermo_lanes
        if lanes:
            st.divider()
            label("Thermo CNCs", teal=True)
            st.subheader(f"{cfg.CNC_THERMO_SPECIAL_MACHINE} lane - special orders & remakes")
            st.caption(f"{cfg.CNC_THERMO_SPECIAL_MACHINE} cuts specials and remakes first; the other three CNCs cut "
                       "regular work first; each takes the other kind only when its own lane runs dry. "
                       f"{cfg.CNC_THERMO_SPECIAL_MACHINE} is manned when someone on the roster is tagged to it "
                       "(drag a person onto its box on the Factory Floor).")
            l1, l2, l3, l4, l5 = st.columns(5)
            l1.metric(f"{cfg.CNC_THERMO_SPECIAL_MACHINE} utilisation", f"{lanes['c6_utilisation'] * 100:.0f}%")
            l2.metric("Other CNCs utilisation", f"{lanes['main_utilisation'] * 100:.0f}%")
            l3.metric(f"Remakes cut on {cfg.CNC_THERMO_SPECIAL_MACHINE}", f"{lanes['remakes_on_c6_pct']:.0f}%")
            l4.metric(f"Specials cut on {cfg.CNC_THERMO_SPECIAL_MACHINE}", f"{lanes['specials_on_c6_pct']:.0f}%")
            l5.metric(f"{cfg.CNC_THERMO_SPECIAL_MACHINE} unmanned",
                      f"{lanes['c6_unmanned_hours']:.0f} of {lanes['c6_active_hours']:.0f} h",
                      help="Hours the CNC crew was running but nobody was on "
                           f"{cfg.CNC_THERMO_SPECIAL_MACHINE} - specials and remakes queued behind regular work.")
            if lanes["c6_unmanned_hours"] > 0:
                st.warning(f"{cfg.CNC_THERMO_SPECIAL_MACHINE} had nobody on it for "
                           f"{lanes['c6_unmanned_hours']:.0f} running hours - remakes and special orders had to "
                           "queue behind regular work on the other machines.")

        # -- Theory of Constraints read-out --
        st.divider()
        label("Theory of Constraints", teal=True)
        st.subheader("Constraints, bottlenecks and where to improve")
        st.caption("A **bottleneck** is busy "
                   "and backing up; a **CCR** is busy but keeping up (no slack); **external** means the floor "
                   "isn't the limit at this intake. Buffers are shown in hours of the receiving station's work. "
                   "This diagnoses - test a suggestion by dragging people on the Factory Floor and re-running.")
        report = analyse_constraints(result, roster)
        toc_cols = st.columns(2)
        kind_word = {"bottleneck": "Bottleneck", "ccr": "CCR (busy, keeping up)", "external": "External"}
        for col, rc in zip(toc_cols, report.by_route.values()):
            with col:
                st.markdown(f"### {rc.label}")
                c = rc.constraint
                k1, k2 = st.columns(2)
                k1.metric("Constraint", short_station_label(c.station) if c else "None on the floor",
                          kind_word[rc.kind], delta_color="off")
                if c:
                    k2.metric("Utilisation", f"{c.utilisation * 100:.0f}%",
                              f"queue {c.queue_start_m2:,.0f} → {c.queue_end_m2:,.0f} m²", delta_color="off")
                elif rc.next_constraint:
                    k2.metric("Busiest station", short_station_label(rc.next_constraint.station),
                              f"{rc.next_constraint.utilisation * 100:.0f}% utilised", delta_color="off")
                if rc.next_constraint and c:
                    st.caption(f"Next in line: **{rc.next_constraint.label}** "
                               f"({rc.next_constraint.utilisation * 100:.0f}%, "
                               f"{rc.next_constraint.headroom_m2_per_week:,.0f} m²/week headroom).")

                st.markdown("**Stations on this line**")
                st.dataframe(pd.DataFrame([{
                    "Station": r.label + (" (shared)" if r.shared else ""),
                    "Util %": round(r.utilisation * 100),
                    "Labour %": round(r.labour_utilisation * 100),
                    "Flat-out h": round(r.flat_out_h),
                    "Starved h": round(r.starved_h),
                    "Unstaffed h": round(r.unstaffed_h),
                    "Blocked h": round(r.blocked_h),
                    "Held h": round(r.held_h),
                    "Queue start→end m²": f"{r.queue_start_m2:,.0f} → {r.queue_end_m2:,.0f}",
                    "Buffer (h of work)": round(r.buffer_avg_h, 1),
                    "Headroom m²/wk": round(r.headroom_m2_per_week),
                } for r in rc.rows]), width="stretch", hide_index=True)

                st.markdown("**Buffers in front of each station (hours of its own work, average)**")
                bar_chart(pd.DataFrame({"hours": [r.buffer_avg_h for r in rc.rows]},
                                       index=pd.Index([short_station_label(r.station) for r in rc.rows], name="Station")),
                          y_title="hours of work", height=220)
                for n in rc.buffer_notes:
                    st.caption("• " + n)
                if rc.protective_buffers:
                    st.markdown("**Protective buffers** (kept stocked on purpose - checked every running hour)")
                    st.dataframe(pd.DataFrame([{
                        "Buffer": pb.label,
                        "Target m²": round(pb.target_m2),
                        "Average m²": round(pb.avg_m2),
                        "Min m²": round(pb.min_m2),
                        "Hours below target": f"{pb.hours_below_target:.0f} of {pb.hours_checked:.0f}",
                        "Hours empty": round(pb.hours_empty),
                        "OK": "✅" if pb.ok else "⚠️",
                    } for pb in rc.protective_buffers]), width="stretch", hide_index=True)
                    for pb in rc.protective_buffers:
                        if not pb.ok:
                            st.caption(f"⚠️ WIP {pb.label} was below its {pb.target_m2:.0f} m² target for "
                                       f"{pb.hours_below_target:.0f} of {pb.hours_checked:.0f} running hours"
                                       + (f" and empty for {pb.hours_empty:.0f}" if pb.hours_empty else "")
                                       + " - the station before it isn't building the pile fast enough.")

                if rc.policy_constraints:
                    st.markdown("**Policy constraints** (things utilisation can't show)")
                    for n in rc.policy_constraints:
                        st.markdown(f"- {n}")

                st.markdown("**Where to improve, in order**")
                for i, sg in enumerate(rc.suggestions, start=1):
                    gain = f" *est. +{sg.gain_m2_per_week:,.0f} m²/week*" if sg.gain_m2_per_week else ""
                    st.markdown(f"{i}. **{sg.step}** · {sg.text}{gain}")

        st.markdown("**Staff utilisation by station** (labour % = productive share of staffed hours)")
        st.dataframe(pd.DataFrame(report.station_labour).round(0), width="stretch", hide_index=True)
        with st.expander("Per-person time study (rostered / producing / waiting / covering / absent hours)"):
            st.caption("Producing = their station's productive share of the hour; waiting = the rest. "
                       "Box packing and admin aren't simulated, so those people are listed but not rated. "
                       "Sorted lowest utilisation first.")
            st.dataframe(pd.DataFrame(report.people).round(1), width="stretch", hide_index=True)

        # -- reality check against the real plant figures in config.py --
        st.divider()
        label("Calibration", teal=True)
        st.subheader("Reality check - simulated month vs real plant history")
        if rs.horizon != "month":
            st.caption("Run the **month** horizon to compare against the real monthly figures.")
        else:
            thermo = result.by_route[cfg.Route.THERMO]
            checks = [
                ("Completion % (completed / intake)", f"{completion_pct:.0f}%",
                 "~100% (the plant ships what it takes in, over a month)",
                 abs(completion_pct - 100.0) <= 5.0),
                ("DIFOT % (both ranges)", f"{result.overall_difot_pct:.0f}%",
                 f"{cfg.REAL_DIFOT_HISTORY_PCT['mean']:.0f}% average "
                 f"({cfg.REAL_DIFOT_HISTORY_PCT['min']:.0f}-{cfg.REAL_DIFOT_HISTORY_PCT['max']:.0f}% month to month)",
                 cfg.REAL_DIFOT_HISTORY_PCT["min"] <= result.overall_difot_pct <= cfg.REAL_DIFOT_HISTORY_PCT["max"]),
                ("Thermo avg lead time (working days)",
                 f"{thermo.overall_avg_lead_days:.1f}" if thermo.has_completions else "—",
                 f"{cfg.REAL_AVG_LEAD_DAYS[cfg.Route.THERMO]:.2f} (live lead-time dashboard)",
                 thermo.has_completions and abs(thermo.overall_avg_lead_days - cfg.REAL_AVG_LEAD_DAYS[cfg.Route.THERMO]) <= 1.5),
                ("Remade m² in the month",
                 f"{result.cum_remade_m2:,.0f}",
                 f"{cfg.REAL_REMAKE_M2_PER_MONTH['mean']:.0f} average "
                 f"({cfg.REAL_REMAKE_M2_PER_MONTH['min']:.0f}-{cfg.REAL_REMAKE_M2_PER_MONTH['max']:.0f}, 2026 waste report)",
                 cfg.REAL_REMAKE_M2_PER_MONTH["min"] <= result.cum_remade_m2 <= cfg.REAL_REMAKE_M2_PER_MONTH["max"]),
                ("Thermo remake lead-time penalty (working days)",
                 f"{thermo.remake_lead_penalty_days:+.2f}" if thermo.remake_lead_penalty_days is not None else "—",
                 f"+{cfg.REAL_REMAKE_LEAD_PENALTY_DAYS:.2f} (live lead-time dashboard)",
                 thermo.remake_lead_penalty_days is not None
                 and abs(thermo.remake_lead_penalty_days - cfg.REAL_REMAKE_LEAD_PENALTY_DAYS) <= 0.25),
            ]
            check_df = pd.DataFrame([
                {"Measure": m, "Simulated": sim, "Real": real, "Match": "✅" if ok else "⚠️"}
                for m, sim, real, ok in checks
            ])
            st.dataframe(check_df, width="stretch", hide_index=True)
            st.caption("⚠️ means a calibration gap worth looking at, not a bug: the engine is "
                       "deterministic given the rates in the sidebar, so a persistent mismatch "
                       "points at a rate, roster or mix number (see CALIBRATION_NOTES.md).")

        st.divider()
        label("Playback", teal=True)
        st.subheader("Factory floor - live")
        st.caption("Play through the simulated period and watch units (m²) physically flow "
                   "station to station, with each box's current buffer, staffing and shift status.")
        components.html(build_floor_html(result.trace, result.staffing_by_shift), height=520, scrolling=True)

        st.divider()
        label("Charts", teal=True)
        st.subheader("Station utilisation (output / capacity while running)")
        util_df = pd.DataFrame({
            "Station": [station_label(s) for s in result.station_utilisation if cfg.is_flow_station(s)],
            "Utilisation %": [v * 100 for s, v in result.station_utilisation.items() if cfg.is_flow_station(s)],
        }).set_index("Station")
        bar_chart(util_df, y_title="Utilisation %")

        st.subheader("Buffer / queue levels over time (m²)")
        buf_df = pd.DataFrame([
            {"hour": t["h"], **{station_label(k): v for k, v in t["buf"].items() if cfg.is_flow_station(k)}}
            for t in result.trace
        ]).set_index("hour")
        line_chart(buf_df, x_title="hour", y_title="m² waiting")

        st.subheader("Cumulative intake vs completed vs remade (m²)")
        cum_df = pd.DataFrame([
            {"hour": t["h"], "Intake": t["cum_intake"], "Completed": t["cum_completed"],
             "Remade": t["cum_remade"]}
            for t in result.trace
        ]).set_index("hour")
        line_chart(cum_df, x_title="hour", y_title="m²")

        if result.has_completions:
            st.subheader("Lead time & DIFOT by day completed - Thermo vs Cut & Clash")
            for rm in result.by_route.values():
                unit = day_unit_for(rm.route)
                st.markdown(f"**{rm.label}** (target {rm.target_lead_days:.0f} {unit})")
                if not rm.has_completions:
                    st.caption("No completions yet for this range in this horizon.")
                    continue
                route_daily_df = pd.DataFrame([
                    {"Day": d.day, f"Avg lead ({unit})": d.avg_lead_days, "DIFOT %": d.difot_pct,
                     "Qty (m²)": d.qty_m2}
                    for d in rm.daily_stats
                ]).set_index("Day")
                col_a, col_b = st.columns(2)
                with col_a:
                    line_chart(route_daily_df[[f"Avg lead ({unit})"]], x_title="day", y_title=unit, height=240)
                with col_b:
                    bar_chart(route_daily_df[["DIFOT %"]], x_title="day", y_title="DIFOT %", height=240)
        else:
            st.info("No completions in this horizon.")

        st.subheader("Completed m² by product class")
        class_df = pd.DataFrame({
            "Class": [cfg.PRODUCT_CLASSES[c].label for c in result.completed_m2_by_class],
            "Completed m²": list(result.completed_m2_by_class.values()),
        }).set_index("Class")
        bar_chart(class_df, y_title="m²")

        st.subheader("Attendance by day (working / idle / absent / day off)")
        st.caption("Each person appears once per day under their own shift. *idle* = rostered on "
                   "but nothing they're qualified for was running that shift (a skills/shift "
                   "mismatch worth fixing); *day off* = their crew doesn't work that weekday.")
        if result.attendance_log:
            att_df = pd.DataFrame(result.attendance_log)
            pivot = att_df.groupby(["day", "status"]).size().unstack(fill_value=0)
            pivot.index.name = "day"
            bar_chart(pivot, x_title="day", y_title="people")
            idle_people = (att_df[att_df["status"] == "idle"]
                           .groupby("operator_name").size().sort_values(ascending=False))
            if len(idle_people):
                st.caption("Most often idle: " + ", ".join(f"{n} ({c} shifts)" for n, c in idle_people.head(5).items()))
            with st.expander("Raw attendance log (per operator, per day)"):
                st.dataframe(att_df, width="stretch")

brand_footer(f"{len(roster.operators)} staff on roster · see resources/CALIBRATION_NOTES.md for what is real vs assumed")
