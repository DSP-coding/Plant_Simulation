"""
Streamlit front-end for the thermoform / cut-and-clash plant simulator.

Run with:
    streamlit run app.py

Layout:
    Sidebar   - order intake, target lead time, product mix, shift schedules,
                remake and sick-leave settings.
    Tab 1     - Staff & Skills: view every operator, click one to edit their
                skills / home station / shift / personal absence rate.
    Tab 2     - Simulation Results: run the sim and see the outcome (KPIs,
                buffer levels, DIFOT, lead time, absenteeism, per-class mix).

Everything here just calls into plant_sim/*.py - if you want to change how the
factory *behaves*, edit that package. This file only wires up the UI.
"""

import pandas as pd
import streamlit as st

from plant_sim import config as cfg
from plant_sim.simulation import Simulator, SimulationSettings
from plant_sim.staff import Operator, Roster

st.set_page_config(page_title="Plant Simulator", layout="wide")

ROSTER_CSV_PATH = "data/staff_roster.csv"
STATION_OPTIONS = list(cfg.STATIONS.keys())
STATION_LABELS = {sid: st_.label for sid, st_ in cfg.STATIONS.items()}


def station_label(sid: str) -> str:
    return STATION_LABELS.get(sid, sid)


# ---------------------------------------------------------------------------
# Session state bootstrap
# ---------------------------------------------------------------------------

if "roster" not in st.session_state:
    st.session_state.roster = Roster.from_csv(ROSTER_CSV_PATH)
if "sim_result" not in st.session_state:
    st.session_state.sim_result = None

roster: Roster = st.session_state.roster

st.title("Thermoform + Cut & Clash Plant Simulator")
st.caption(
    "CNC (Thermo: Series 1/2/3) -> Sanding -> MB Sander -> Edging -> Press -> Packing, "
    "and Optimising -> CNC 1536 (Cut & Clash: Melamine + Acrylic) -> Edge Band/Drilling -> Packing."
)

# ---------------------------------------------------------------------------
# Sidebar: intake, mix, target, shifts, remake, sick leave
# ---------------------------------------------------------------------------

with st.sidebar:
    st.header("Order intake & target")
    horizon = st.selectbox("Horizon", ["day", "week", "month"], index=2)

    st.caption("Presets from real order data (product 2026.xlsx, Jan-Sep 2026):")
    if "intake_m2" not in st.session_state:
        st.session_state["intake_m2"] = cfg.REAL_DAILY_M2["median"]
    preset_cols = st.columns(2)
    if preset_cols[0].button(f"Median day ({cfg.REAL_DAILY_M2['median']:.0f} m²)"):
        st.session_state["intake_m2"] = cfg.REAL_DAILY_M2["median"]
    if preset_cols[1].button(f"Busiest day ({cfg.REAL_DAILY_M2['max']:.0f} m²)"):
        st.session_state["intake_m2"] = cfg.REAL_DAILY_M2["max"]

    # NOTE: no `value=` here - the widget's value lives entirely in
    # st.session_state["intake_m2"] (initialised above), since Streamlit
    # forbids passing `value=` together with a `key=` that other widgets
    # (the preset buttons) also write to.
    intake_m2 = st.number_input("Intake (m²/day)", min_value=0.0, step=10.0, key="intake_m2")
    target_lead_days = st.number_input("Target lead time (days)", min_value=0.0, value=10.0, step=0.5)

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
    if abs(mix_sum - 100.0) > 0.5:
        st.warning(f"Mix sums to {mix_sum:.1f}%, not 100% - will be used as-is (not auto-normalised).")

    st.divider()
    st.header("Shift schedules")
    shift_schedules = {}
    for crew_name, default_sched in cfg.DEFAULT_SHIFT_SCHEDULES.items():
        with st.expander(f"{crew_name} crew", expanded=False):
            days = st.slider("Days/week", 1, 7, default_sched.days_per_week, key=f"{crew_name}_days")
            day_hrs = st.number_input("Day shift hours", 0.0, 24.0, default_sched.day_hrs,
                                       key=f"{crew_name}_dayhrs")
            aft_enabled = st.checkbox("Afternoon shift enabled", default_sched.aft_enabled,
                                       key=f"{crew_name}_aftenabled")
            aft_hrs = st.number_input("Afternoon shift hours", 0.0, 24.0, default_sched.aft_hrs,
                                       key=f"{crew_name}_afthrs")
            shift_schedules[crew_name] = cfg.ShiftSchedule(days, day_hrs, aft_enabled, aft_hrs)

    st.divider()
    st.header("Remake loop")
    remake_enabled = st.checkbox("Enable remake loop", value=True)
    remake_rate_pct = st.number_input("Remake rate (% of packing output)", 0.0, 100.0,
                                       cfg.DEFAULT_REMAKE_RATE_PCT, step=0.1)
    remake_days = st.number_input("Remake processing time (days)", 0.0, 30.0,
                                   cfg.DEFAULT_REMAKE_DAYS, step=0.5)

    st.divider()
    st.header("Sick leave")
    sick_enabled = st.checkbox("Enable random sick leave (per-person rate, edit in Staff tab)",
                                value=False)
    seed = st.number_input("Random seed (change to reroll)", value=42, step=1)


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------

tab_staff, tab_results = st.tabs(["Staff & Skills", "Simulation Results"])

with tab_staff:
    st.subheader("Roster overview")
    rows = []
    for op in roster.operators:
        rows.append({
            "ID": op.id, "Name": op.name,
            "Home station": station_label(op.home_station),
            "Shift": op.shift,
            "Extra skills": ", ".join(station_label(s) for s in sorted(op.skills)) or "-",
            "Absence rate %": op.absence_rate_pct,
        })
    st.dataframe(pd.DataFrame(rows), width='stretch', hide_index=True)

    st.subheader("Click a staff member to edit skills / roster details")
    names = {f"{op.name} ({station_label(op.home_station)})": op.id for op in roster.operators}
    selected_label = st.selectbox("Staff member", list(names.keys()))
    selected_id = names[selected_label]
    op = roster.get(selected_id)

    col1, col2 = st.columns(2)
    with col1:
        new_home = st.selectbox("Home station", STATION_OPTIONS,
                                 index=STATION_OPTIONS.index(op.home_station),
                                 format_func=station_label, key=f"home_{op.id}")
        new_shift = st.selectbox("Shift", ["day", "aft"], index=["day", "aft"].index(op.shift),
                                  key=f"shift_{op.id}")
    with col2:
        new_absence = st.number_input("Personal absence rate (% per working day)", 0.0, 100.0,
                                       op.absence_rate_pct, step=0.1, key=f"abs_{op.id}")
        new_skills = st.multiselect(
            "Extra skills (stations this person can also cover)",
            [s for s in STATION_OPTIONS if s != new_home],
            default=[s for s in op.skills if s != new_home],
            format_func=station_label, key=f"skills_{op.id}",
        )

    st.text_area("Notes", value=op.notes, key=f"notes_{op.id}")

    if st.button("Apply changes to this staff member", type="primary"):
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

    st.divider()
    with st.expander("Add a new staff member"):
        new_id = st.text_input("ID (unique)")
        new_name = st.text_input("Name")
        add_home = st.selectbox("Home station", STATION_OPTIONS, format_func=station_label, key="add_home")
        add_shift = st.selectbox("Shift", ["day", "aft"], key="add_shift")
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

    if st.button("💾 Save roster to CSV"):
        roster.to_csv(ROSTER_CSV_PATH)
        st.success(f"Saved {len(roster.operators)} staff to {ROSTER_CSV_PATH}")

with tab_results:
    if st.button("▶ Run simulation", type="primary"):
        settings = SimulationSettings(
            horizon=horizon, intake_m2_per_day=intake_m2, mix_pct=mix_pct,
            target_lead_days=target_lead_days, shift_schedules=shift_schedules,
            remake_enabled=remake_enabled, remake_rate_pct=remake_rate_pct, remake_days=remake_days,
            sick_enabled=sick_enabled, random_seed=int(seed),
        )
        with st.spinner("Simulating..."):
            st.session_state.sim_result = Simulator(roster, settings).run()

    result = st.session_state.sim_result
    if result is None:
        st.info("Configure settings in the sidebar, then click **Run simulation**.")
    else:
        completion_pct = (100 * result.cum_completed_m2 / result.cum_intake_m2
                           if result.cum_intake_m2 > 0 else 0.0)
        bottleneck = max(result.station_utilisation, key=result.station_utilisation.get)

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Intake (m²)", f"{result.cum_intake_m2:,.0f}")
        c1.metric("Completed (good, m²)", f"{result.cum_completed_m2:,.0f}")
        c2.metric("Completion %", f"{completion_pct:.1f}%")
        c2.metric("Remade (m²)", f"{result.cum_remade_m2:,.0f}")
        c3.metric("Overdue backlog (m²)", f"{result.overdue_backlog_m2:,.0f}")
        c3.metric("Bottleneck", station_label(bottleneck),
                  f"{result.station_utilisation[bottleneck] * 100:.0f}% utilised")
        c4.metric("Avg lead time (days)",
                  f"{result.overall_avg_lead_days:.1f}" if result.has_completions else "—")
        c4.metric("DIFOT %", f"{result.overall_difot_pct:.0f}%" if result.has_completions else "—")

        st.divider()
        st.subheader("Station utilisation")
        util_df = pd.DataFrame({
            "Station": [station_label(s) for s in result.station_utilisation],
            "Utilisation %": [v * 100 for v in result.station_utilisation.values()],
        }).set_index("Station")
        st.bar_chart(util_df)

        st.subheader("Buffer / queue levels over time (m²)")
        buf_df = pd.DataFrame([
            {"hour": t["h"], **{station_label(k): v for k, v in t["buf"].items()}}
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
            st.subheader("Lead time & DIFOT by day completed")
            daily_df = pd.DataFrame([
                {"Day": d.day, "Avg lead (days)": d.avg_lead_days, "DIFOT %": d.difot_pct,
                 "Qty (m²)": d.qty_m2}
                for d in result.daily_stats
            ]).set_index("Day")
            col_a, col_b = st.columns(2)
            col_a.line_chart(daily_df[["Avg lead (days)"]])
            col_b.bar_chart(daily_df[["DIFOT %"]])
        else:
            st.info("No completions yet in this horizon - try Week or Month.")

        st.subheader("Completed m² by product class")
        class_df = pd.DataFrame({
            "Class": [cfg.PRODUCT_CLASSES[c].label for c in result.completed_m2_by_class],
            "Completed m²": list(result.completed_m2_by_class.values()),
        }).set_index("Class")
        st.bar_chart(class_df)

        if sick_enabled:
            st.subheader("Attendance by day (working / idle / absent / off-shift)")
            att_df = pd.DataFrame(result.attendance_log)
            pivot = att_df.groupby(["day", "status"]).size().unstack(fill_value=0)
            st.bar_chart(pivot)
        else:
            st.caption("Enable sick leave in the sidebar to see the attendance breakdown here.")

        with st.expander("Raw attendance log (per operator, per shift, per day)"):
            st.dataframe(pd.DataFrame(result.attendance_log), width='stretch')
