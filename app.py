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
from plant_sim.floor_editor import apply_move, floor_editor, restore, snapshot
from plant_sim.floor_view import build_floor_html
from plant_sim.simulation import Simulator, SimulationSettings
from plant_sim.staff import Operator, Roster, RosterError

st.set_page_config(page_title="Plant Simulator", layout="wide")

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

st.title("Thermoform + Cut & Clash Plant Simulator")
st.caption(
    "CNC (Thermo: Series 1/2/3) -> Sanding -> MB Sander -> Cefla gluing line -> Press -> Packing, "
    "and Optimising -> CNC 1536 (Cut & Clash: Melamine + Acrylic) -> Edge Band/Drilling -> Packing."
)

# ---------------------------------------------------------------------------
# Sidebar: intake, mix, target, shifts, remake, sick leave
# ---------------------------------------------------------------------------

settings_problems: list[str] = []

with st.sidebar:
    st.header("Order intake & target")
    horizon = st.selectbox("Horizon", list(cfg.HORIZON_DAYS.keys()), index=2,
                           help="day = 1 Monday, week = 7 days from Monday, month = 30 days from Monday")

    # The intake number means "total m2 FOR THE SELECTED HORIZON" - i.e. if
    # you pick Week, this is m2 for the whole week, not m2/day. Each horizon
    # remembers its own last-entered value (separate session_state keys) so
    # switching horizons doesn't silently reinterpret your number.
    intake_key = f"intake_m2_{horizon}"
    horizon_stats = cfg.REAL_INTAKE_M2[horizon]["combined"]
    if intake_key not in st.session_state:
        st.session_state[intake_key] = horizon_stats["median"]

    st.caption(f"Presets from real order data ({INTAKE_SOURCE[horizon]}) for a typical **{horizon}**. "
               f"Orders arrive on weekdays between hour {cfg.INTAKE_WINDOW_HOURS[0]:.0f} and "
               f"{cfg.INTAKE_WINDOW_HOURS[1]:.0f} of the day shift.")
    preset_cols = st.columns(2)
    if preset_cols[0].button(f"Median {horizon} ({horizon_stats['median']:.0f} m²)"):
        st.session_state[intake_key] = horizon_stats["median"]
    if preset_cols[1].button(f"Busiest {horizon} ({horizon_stats['max']:.0f} m²)"):
        st.session_state[intake_key] = horizon_stats["max"]

    # NOTE: no `value=` here - the widget's value lives entirely in
    # st.session_state[intake_key] (initialised above), since Streamlit
    # forbids passing `value=` together with a `key=` that other widgets
    # (the preset buttons) also write to.
    intake_m2_for_horizon = st.number_input(f"Intake (m² / {horizon})", min_value=0.0, step=10.0,
                                             key=intake_key)

    st.caption("Cut & Clash (1536) quotes its own lead time, separate from Thermo. "
               "Thermo's target/DIFOT is measured in WORKING days (Mon-Fri, weekends "
               "excluded), matching the real lead-time dashboard; Cut & Clash still "
               "uses calendar days pending confirmation of its measurement basis.")
    target_lead_thermo = st.number_input("Target lead time - Thermo (working days)", min_value=0.0,
                                          value=cfg.DEFAULT_TARGET_LEAD_DAYS[cfg.Route.THERMO], step=0.5)
    target_lead_cutclash = st.number_input("Target lead time - Cut & Clash (calendar days)", min_value=0.0,
                                            value=cfg.DEFAULT_TARGET_LEAD_DAYS[cfg.Route.CUT_AND_CLASH],
                                            step=0.5)
    target_lead_days = {cfg.Route.THERMO: target_lead_thermo, cfg.Route.CUT_AND_CLASH: target_lead_cutclash}

    st.divider()
    st.header("Area capacities (m² / month)")
    st.caption("Capacity of each area AT IDEAL STAFFING, on a fixed reference schedule "
               f"({cfg.REFERENCE_HOURS_PER_WEEK:.0f} hrs/week) - independent of the actual shift "
               "schedule you set below. Defaults come from config.py (see CALIBRATION_NOTES.md for "
               "which are real and which are guesses); edit any of these with a real number and the "
               "simulation recalculates from it. Setting one to 0 takes that area out of action.")
    station_capacity_m2_per_month = {}
    # Flow order matching how the factory actually runs, CNC/Sanding/MB Sander/
    # Press/Cefla first (the ones you're most likely to have real numbers for).
    capacity_order = ["cnc_thermo", "sanding", "mb_sander", "press_1", "press_2", "edging",
                       "despatch", "optimising", "cnc_1536", "eb_drilling"]
    for sid in capacity_order:
        default_val = round(cfg.default_station_capacity_m2_per_month(sid), 0)
        station_capacity_m2_per_month[sid] = st.number_input(
            f"{station_label(sid)}", min_value=0.0, value=default_val, step=100.0,
            key=f"cap_{sid}")

    st.divider()
    st.header("Product mix (%)")
    st.caption("Real Thermo(80.6%)/Cut&Clash(19.4%) split from product_2026.xlsx; "
               "the S1/S2/S3 breakdown within Thermo is still a placeholder pending "
               "Series Mix by Month.xlsx.")
    mix_pct = {}
    for cls, pc in cfg.PRODUCT_CLASSES.items():
        mix_pct[cls] = st.number_input(f"{pc.label}", min_value=0.0, max_value=100.0,
                                        value=round(cfg.DEFAULT_MIX_PCT[cls], 2), step=0.1,
                                        key=f"mix_{cls}")
    mix_sum = sum(mix_pct.values())
    if mix_sum <= 0:
        settings_problems.append("Product mix: at least one class needs a share above 0%.")
    elif abs(mix_sum - 100.0) > 0.5:
        st.warning(f"Mix sums to {mix_sum:.1f}%, not 100% - it will be scaled to 100% so the "
                   "intake total you entered is honoured.")

    st.divider()
    st.header("Shift schedules")
    st.caption("Hour 0 of each simulated day is the start of the day shift; the afternoon "
               "shift follows straight on. A crew on N days/week works Monday to day N.")
    shift_schedules = {}
    for crew_name, default_sched in cfg.DEFAULT_SHIFT_SCHEDULES.items():
        with st.expander(f"{crew_name} crew", expanded=False):
            days = st.slider("Days/week", 0, 7, default_sched.days_per_week, key=f"{crew_name}_days")
            day_hrs = st.number_input("Day shift hours", 0.0, 24.0, default_sched.day_hrs,
                                       key=f"{crew_name}_dayhrs")
            aft_enabled = st.checkbox("Afternoon shift enabled", default_sched.aft_enabled,
                                       key=f"{crew_name}_aftenabled")
            aft_hrs = st.number_input("Afternoon shift hours", 0.0, 24.0, default_sched.aft_hrs,
                                       key=f"{crew_name}_afthrs")
            try:
                shift_schedules[crew_name] = cfg.ShiftSchedule(days, day_hrs, aft_enabled, aft_hrs)
            except ValueError as e:
                st.error(str(e))
                settings_problems.append(f"{crew_name} crew schedule: {e}")

    st.divider()
    st.header("Remake loop")
    remake_enabled = st.checkbox("Enable remake loop", value=True)
    remake_rate_pct = st.number_input("Remake rate (% of packing output)", 0.0, 100.0,
                                       cfg.DEFAULT_REMAKE_RATE_PCT, step=0.1)
    remake_days = st.number_input("Remake hold before re-entering the line (days)", 0.0, 30.0,
                                   cfg.DEFAULT_REMAKE_DAYS, step=0.1,
                                   help="Remakes keep their original order date, so they jump the "
                                        "queue on their second pass. The real dashboard says remakes "
                                        f"take {cfg.REAL_REMAKE_LEAD_PENALTY_DAYS} working days longer "
                                        "in total - the results tab shows what this setting produces.")

    st.divider()
    st.header("Sick leave")
    sick_enabled = st.checkbox("Enable random sick leave (per-person rate, edit in Staff tab)",
                                value=False)
    seed = st.number_input("Random seed (change to reroll)", value=42, step=1)


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------

def run_simulation() -> None:
    """Build settings from the sidebar, run, and store the result (or show why not)."""
    try:
        settings = SimulationSettings(
            horizon=horizon, intake_m2_for_horizon=intake_m2_for_horizon, mix_pct=mix_pct,
            target_lead_days=target_lead_days, shift_schedules=shift_schedules,
            station_capacity_m2_per_month=station_capacity_m2_per_month,
            remake_enabled=remake_enabled, remake_rate_pct=remake_rate_pct, remake_days=remake_days,
            sick_enabled=sick_enabled, random_seed=int(seed),
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
    cols[4].metric("Bottleneck", station_label(bottleneck),
                   f"{result.station_utilisation[bottleneck] * 100:.0f}% utilised", delta_color="off")


tab_floor, tab_staff, tab_results = st.tabs(["🏭 Factory Floor", "Staff & Skills", "Simulation Results"])

with tab_floor:
    st.subheader("Move people around the floor")
    st.caption("Each box is a station; each chip is a person on that shift. **Drag** a chip onto "
               "another station to change their home station, or into the other lane to change "
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
            st.rerun()

    ctl1, ctl2, ctl3 = st.columns([1, 1, 3])
    if ctl1.button("▶ Run simulation", type="primary", key="run_floor", disabled=bool(settings_problems)):
        run_simulation()
    history = st.session_state.roster_history
    if ctl2.button(f"↶ Undo last move ({len(history)})", disabled=not history, key="undo_floor"):
        described, before = history.pop()
        restore(roster, before)
        st.rerun()
    if history:
        ctl3.caption("Recent moves: " + " · ".join(d for d, _ in history[-4:]))

    if st.session_state.sim_result is not None:
        st.divider()
        kpi_strip(st.session_state.sim_result)
        st.caption("Full breakdown, charts and the live playback are on the **Simulation Results** tab.")

with tab_staff:
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
            st.success(f"Updated {op.name}.")
            # No explicit st.rerun() here: the button click itself already
            # triggers a rerun, so the mutation above is reflected as soon as
            # this script run finishes rendering - an extra rerun would just
            # wipe this success message before it's shown.
        if btn_remove.button(f"Remove {op.name} from roster"):
            roster.remove(op.id)
            st.success(f"Removed {op.name} (not saved to CSV until you click Save).")
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
                st.success(f"Added {new_name}.")
                st.rerun()

    save_col, reload_col = st.columns(2)
    if save_col.button("💾 Save roster to CSV"):
        problems = roster.problems()
        if problems:
            st.error("Not saved - fix these first:\n\n- " + "\n- ".join(problems))
        else:
            try:
                roster.to_csv(ROSTER_CSV_PATH)
                st.success(f"Saved {len(roster.operators)} staff to {ROSTER_CSV_PATH}")
            except OSError as e:
                st.error(f"Could not write {ROSTER_CSV_PATH}: {e}")
    if reload_col.button("↺ Reload roster from CSV (discard unsaved edits)"):
        st.session_state.roster = load_roster()
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
        c3.metric("Bottleneck", station_label(bottleneck),
                  f"{result.station_utilisation[bottleneck] * 100:.0f}% utilised")
        c4.metric("Avg lead time (days)",
                  f"{result.overall_avg_lead_days:.1f}" if result.has_completions else "—",
                  help="Blended across both ranges - Thermo is measured in working days, "
                       "Cut & Clash in calendar days (see the per-range breakdown below).")
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

        # -- reality check against the real plant figures in config.py --
        st.divider()
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
        st.subheader("Factory floor - live")
        st.caption("Play through the simulated period and watch units (m²) physically flow "
                   "station to station, with each box's current buffer, staffing and shift status.")
        components.html(build_floor_html(result.trace, result.staffing_by_shift), height=520, scrolling=True)

        st.divider()
        st.subheader("Station utilisation (output / capacity while running)")
        util_df = pd.DataFrame({
            "Station": [station_label(s) for s in result.station_utilisation if s != "admin"],
            "Utilisation %": [v * 100 for s, v in result.station_utilisation.items() if s != "admin"],
        }).set_index("Station")
        st.bar_chart(util_df)

        st.subheader("Buffer / queue levels over time (m²)")
        buf_df = pd.DataFrame([
            {"hour": t["h"], **{station_label(k): v for k, v in t["buf"].items() if k != "admin"}}
            for t in result.trace
        ]).set_index("hour")
        st.line_chart(buf_df)

        st.subheader("Cumulative intake vs completed vs remade (m²)")
        cum_df = pd.DataFrame([
            {"hour": t["h"], "Intake": t["cum_intake"], "Completed": t["cum_completed"],
             "Remade": t["cum_remade"]}
            for t in result.trace
        ]).set_index("hour")
        st.line_chart(cum_df)

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
                col_a.line_chart(route_daily_df[[f"Avg lead ({unit})"]])
                col_b.bar_chart(route_daily_df[["DIFOT %"]])
        else:
            st.info("No completions in this horizon.")

        st.subheader("Completed m² by product class")
        class_df = pd.DataFrame({
            "Class": [cfg.PRODUCT_CLASSES[c].label for c in result.completed_m2_by_class],
            "Completed m²": list(result.completed_m2_by_class.values()),
        }).set_index("Class")
        st.bar_chart(class_df)

        st.subheader("Attendance by day (working / idle / absent / day off)")
        st.caption("Each person appears once per day under their own shift. *idle* = rostered on "
                   "but nothing they're qualified for was running that shift (a skills/shift "
                   "mismatch worth fixing); *day off* = their crew doesn't work that weekday.")
        if result.attendance_log:
            att_df = pd.DataFrame(result.attendance_log)
            pivot = att_df.groupby(["day", "status"]).size().unstack(fill_value=0)
            st.bar_chart(pivot)
            idle_people = (att_df[att_df["status"] == "idle"]
                           .groupby("operator_name").size().sort_values(ascending=False))
            if len(idle_people):
                st.caption("Most often idle: " + ", ".join(f"{n} ({c} shifts)" for n, c in idle_people.head(5).items()))
            with st.expander("Raw attendance log (per operator, per day)"):
                st.dataframe(att_df, width="stretch")
