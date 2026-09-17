"""
Theory-of-Constraints read-out of a finished simulation run.

Pure analysis - nothing here changes the engine; it reads a
SimulationResult and turns the per-station diagnostics into the five
focusing steps, separately for Thermo and Cut & Clash:

  1. IDENTIFY  - the constraint: the station on the line with the highest
                 utilisation that is also holding or growing a queue; and the
                 next in line ("fix that and this becomes the constraint").
  2. EXPLOIT   - hours the constraint sat unstaffed with work waiting, or
                 starved with nobody feeding it - lost capacity that costs
                 nothing to recover.
  3. SUBORDINATE - upstream stations piling work in front of the constraint
                 faster than it can take it: queues that are waiting, not
                 protection.
  4. ELEVATE   - what would raise the constraint's capacity: a person, hours,
                 a day, a skill - with a rough m2/week estimate.
  5. (REPEAT)  - the next constraint, so you know what you'll hit next.

Buffers are reported in HOURS OF WORK at the receiving station (drum-
buffer-rope style): the buffer in front of the constraint should be big
enough that it never starves; buffers elsewhere are just waiting time.

Everything is diagnosis and suggestion - it does not run what-ifs. Drag
people on the Factory Floor and re-run to test a suggestion.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from plant_sim import config as cfg
from plant_sim.simulation import ROUTE_LABELS, SimulationResult

# A station is treated as an active constraint when it is this busy while
# staffed AND its queue is not shrinking. [ANALYSIS THRESHOLD, not a plant fact]
CONSTRAINT_UTILISATION = 0.85
# A queue whose end level is this much above its start is "growing".
GROWING_QUEUE_RATIO = 1.10
# Buffer in front of the constraint below this many hours of its own work
# is "thin"; a buffer anywhere above this many hours is pure waiting.
THIN_BUFFER_HOURS = 8.0
FAT_BUFFER_HOURS = 24.0


@dataclass
class StationRow:
    station: str
    label: str
    shared: bool                      # despatch: fed by both lines
    utilisation: float                # output / capacity while staffed (0..1)
    labour_utilisation: float         # busy hours / staffed hours (0..1)
    running_h: float
    staffed_h: float
    unstaffed_h: float                # running, work waiting, nobody on it
    starved_h: float                  # staffed, nothing to do
    blocked_h: float                  # downstream buffer full
    flat_out_h: float                 # staffed, worked, still couldn't clear the queue
    queue_start_m2: float
    queue_end_m2: float
    queue_avg_m2: float
    queue_max_m2: float
    capacity_m2_per_h: float          # average while staffed
    buffer_avg_h: float               # avg queue / capacity per hour
    buffer_end_h: float
    headroom_m2_per_week: float       # (capacity - output) per week, while staffed
    growing: bool

    @property
    def is_bottleneck(self) -> bool:
        """Busy AND backing up - the classic constraint."""
        return self.utilisation >= CONSTRAINT_UTILISATION and self.growing

    @property
    def is_ccr(self) -> bool:
        """Capacity-constrained resource: busy, keeping up for now, no slack."""
        return self.utilisation >= CONSTRAINT_UTILISATION and not self.growing


@dataclass
class Suggestion:
    step: str            # "Exploit" | "Subordinate" | "Elevate" | "Policy"
    station: str
    text: str
    gain_m2_per_week: float | None = None


@dataclass
class RouteConstraints:
    route: str
    label: str
    rows: list[StationRow]
    constraint: StationRow | None
    next_constraint: StationRow | None
    kind: str                         # "bottleneck" | "ccr" | "external"
    external: bool                    # nothing is binding - demand below capacity
    policy_constraints: list[str]
    buffer_notes: list[str]
    suggestions: list[Suggestion]


@dataclass
class ConstraintsReport:
    by_route: dict[str, RouteConstraints]
    station_labour: list[dict]         # per station, both routes + shared
    people: list[dict]                 # per-person time study
    weeks: float


# ---------------------------------------------------------------------------

def analyse(result: SimulationResult, roster=None) -> ConstraintsReport:
    s = result.settings
    weeks = s.horizon_days() / 7.0
    first_buf = result.trace[0]["buf"] if result.trace else {}
    last_buf = result.trace[-1]["buf"] if result.trace else {}

    def row(sid: str, shared: bool = False) -> StationRow:
        st = cfg.STATIONS[sid]
        cap_h = result.station_capacity_m2_per_hour.get(sid, 0.0)
        staffed = result.station_staffed_hours.get(sid, 0.0)
        out = result.station_out_m2.get(sid, 0.0)
        q0, q1 = first_buf.get(sid, 0.0), last_buf.get(sid, 0.0)
        q_avg = result.station_queue_avg_m2.get(sid, 0.0)
        capacity_in_window = cap_h * staffed
        return StationRow(
            station=sid, label=st.label, shared=shared,
            utilisation=result.station_utilisation.get(sid, 0.0),
            labour_utilisation=(result.station_busy_hours.get(sid, 0.0) / staffed) if staffed > 0 else 0.0,
            running_h=result.station_active_hours.get(sid, 0.0), staffed_h=staffed,
            unstaffed_h=result.station_unstaffed_hours.get(sid, 0.0),
            starved_h=result.station_starved_hours.get(sid, 0.0),
            blocked_h=result.station_blocked_hours.get(sid, 0.0),
            flat_out_h=result.station_flat_out_hours.get(sid, 0.0),
            queue_start_m2=q0, queue_end_m2=q1, queue_avg_m2=q_avg,
            queue_max_m2=result.station_queue_max_m2.get(sid, 0.0),
            capacity_m2_per_h=cap_h,
            buffer_avg_h=(q_avg / cap_h) if cap_h > 0 else 0.0,
            buffer_end_h=(q1 / cap_h) if cap_h > 0 else 0.0,
            headroom_m2_per_week=((capacity_in_window - out) / weeks) if weeks > 0 else 0.0,
            growing=q1 > max(q0 * GROWING_QUEUE_RATIO, q0 + 20.0),
        )

    by_route: dict[str, RouteConstraints] = {}
    for route, label in ROUTE_LABELS.items():
        seq = list(cfg.ROUTE_SEQUENCE[route]) + (list(cfg.PRESS_STATIONS) if route == cfg.Route.THERMO else [])
        rows = [row(sid) for sid in seq] + [row(cfg.SHARED_TERMINAL_STATION, shared=True)]
        ranked = sorted(rows, key=lambda r: (r.utilisation, r.queue_end_m2), reverse=True)
        bottlenecks = [r for r in ranked if r.is_bottleneck]
        ccrs = [r for r in ranked if r.is_ccr]
        if bottlenecks:
            constraint, kind = bottlenecks[0], "bottleneck"
        elif ccrs:
            constraint, kind = ccrs[0], "ccr"
        else:
            constraint, kind = None, "external"
        next_c = next((r for r in ranked if r is not constraint), None)
        by_route[route] = RouteConstraints(
            route=route, label=label, rows=rows, constraint=constraint, next_constraint=next_c,
            kind=kind, external=constraint is None,
            policy_constraints=_policy_constraints(route, rows, result),
            buffer_notes=_buffer_notes(rows, constraint, seq),
            suggestions=_suggestions(route, rows, constraint, next_c, result, roster, weeks),
        )

    return ConstraintsReport(
        by_route=by_route,
        station_labour=_station_labour(result),
        people=_people(result, roster),
        weeks=weeks,
    )


# ---------------------------------------------------------------------------
# Policy constraints: things the utilisation numbers can't show
# ---------------------------------------------------------------------------

def _policy_constraints(route: str, rows: list[StationRow], result: SimulationResult) -> list[str]:
    s = result.settings
    notes = []
    crews = {cfg.STATION_CREW[r.station] for r in rows if not r.shared}
    intake_days = 5 if cfg.INTAKE_WEEKDAYS_ONLY else 7
    for crew in sorted(crews):
        sched = s.shift_schedules[crew]
        if sched.days_per_week < intake_days:
            notes.append(f"The {crew} crew runs {sched.days_per_week} days a week but orders arrive {intake_days}: "
                         f"work that arrives after their last day waits until the next run day.")
        if not sched.offers_shift("aft"):
            notes.append(f"The {crew} crew has no afternoon shift - its stations only run {sched.day_hrs:g} h/day.")
    for r in rows:
        if r.shared:
            continue
        st = cfg.STATIONS[r.station]
        if r.unstaffed_h > 0:
            notes.append(f"{r.label}: {r.unstaffed_h:.0f} of {r.running_h:.0f} running hours had work waiting and "
                         f"nobody on it (a staffing/skills gap, not a capacity limit).")
        if st.machine_bound and st.ideal_ops == 1 and st.num_machines == 1 and r.utilisation >= 0.7:
            notes.append(f"{r.label} is a single machine with a one-person crew - there is no way to add capacity "
                         f"there without more hours.")
        if r.blocked_h > 0:
            notes.append(f"{r.label}: blocked for {r.blocked_h:.0f} h because the buffer after it was full "
                         f"(a space limit, not a capacity limit).")
    if route == cfg.Route.THERMO:
        lanes = result.cnc_thermo_lanes
        if lanes.get("c6_unmanned_hours", 0) > 0:
            notes.append(f"{cfg.CNC_THERMO_SPECIAL_MACHINE} (specials & remakes) was unmanned "
                         f"{lanes['c6_unmanned_hours']:.0f} of {lanes['c6_active_hours']:.0f} running hours - "
                         f"remakes and specials queued behind regular work.")
    return notes


# ---------------------------------------------------------------------------
# Buffers
# ---------------------------------------------------------------------------

def _buffer_notes(rows: list[StationRow], constraint: StationRow | None, seq: list[str]) -> list[str]:
    notes = []
    if constraint is not None and not constraint.shared:
        if constraint.buffer_avg_h < THIN_BUFFER_HOURS:
            risk = " - and it did starve" if constraint.starved_h > 0 else ""
            notes.append(f"Buffer in front of the constraint ({constraint.label}) averages only "
                         f"{constraint.buffer_avg_h:.1f} h of its own work{risk}. The drum needs a buffer of at least "
                         f"a shift so it never waits for upstream.")
        else:
            notes.append(f"Buffer in front of the constraint ({constraint.label}) averages "
                         f"{constraint.buffer_avg_h:.1f} h of its work - enough to keep it fed.")
    for r in rows:
        if constraint is not None and r.station == constraint.station:
            continue
        if r.buffer_avg_h >= FAT_BUFFER_HOURS:
            notes.append(f"{r.label} has {r.buffer_avg_h:.0f} h of work waiting on average - that is lead time, "
                         f"not protection; the station before it is running ahead of what {r.label} can take.")
    if not notes:
        notes.append("No buffer is starving its station or piling up beyond a day's work.")
    return notes


# ---------------------------------------------------------------------------
# Suggestions - ranked
# ---------------------------------------------------------------------------

def _suggestions(route, rows, constraint, next_c, result, roster, weeks) -> list[Suggestion]:
    out: list[Suggestion] = []
    s = result.settings
    if constraint is None:
        best = max(rows, key=lambda r: r.utilisation)
        out.append(Suggestion("Identify", "-",
                              f"No station on this line is the constraint at this intake - the busiest is "
                              f"{best.label} at {best.utilisation*100:.0f}%. The constraint is outside the "
                              f"floor (order intake). Try the busiest-month preset to see what breaks first."))
        return out

    c = constraint
    st = cfg.STATIONS[c.station]
    cap_h = c.capacity_m2_per_h

    # Exploit ---------------------------------------------------------
    if c.unstaffed_h > 0:
        out.append(Suggestion("Exploit", c.label,
                              f"{c.label} had work waiting for {c.unstaffed_h:.0f} h with nobody on it. Roster or "
                              f"cross-train cover for every hour the crew runs - this is free capacity.",
                              gain_m2_per_week=c.unstaffed_h * cap_h / weeks))
    if c.starved_h > 0:
        out.append(Suggestion("Exploit", c.label,
                              f"{c.label} was staffed but had nothing to do for {c.starved_h:.0f} h. A constraint "
                              f"should never wait: release work to it earlier / keep a buffer in front of it.",
                              gain_m2_per_week=c.starved_h * cap_h / weeks))
    if c.blocked_h > 0:
        out.append(Suggestion("Exploit", c.label,
                              f"{c.label} was blocked for {c.blocked_h:.0f} h by a full buffer after it - clear "
                              f"space downstream (or raise that buffer's limit).",
                              gain_m2_per_week=c.blocked_h * cap_h / weeks))
    if route == cfg.Route.THERMO and c.station == "cnc_thermo":
        lanes = result.cnc_thermo_lanes
        if lanes.get("c6_unmanned_hours", 0) > 0:
            out.append(Suggestion("Exploit", c.label,
                                  f"Keep {cfg.CNC_THERMO_SPECIAL_MACHINE} manned ({lanes['c6_unmanned_hours']:.0f} h "
                                  f"unmanned) or let a neighbouring operator run it as a second machine."))

    # Subordinate -------------------------------------------------------
    seq_ids = [r.station for r in rows]
    if c.station in seq_ids:
        idx = seq_ids.index(c.station)
        for up in rows[:idx]:
            if up.station in cfg.PRESS_STATIONS:
                continue
            if c.buffer_avg_h >= FAT_BUFFER_HOURS and up.utilisation < c.utilisation:
                out.append(Suggestion("Subordinate", up.label,
                                      f"{up.label} is feeding {c.label} faster than it can process "
                                      f"({c.buffer_avg_h:.0f} h of work queued). Pace releases to the constraint's "
                                      f"rate instead of building WIP; put the spare {up.label} hours into "
                                      f"cover elsewhere."))
                break

    # Elevate -----------------------------------------------------------
    active_h_per_week = c.running_h / weeks if weeks > 0 else 0.0
    crew = s.shift_schedules[cfg.STATION_CREW[c.station]]
    shift_hours = crew.day_hrs + (crew.aft_hrs if crew.aft_enabled else 0.0)
    fifth_day = (f"; a 5th day ≈ +{shift_hours * cap_h:,.0f} m²/week" if crew.days_per_week < 5 else "")
    if c.station in cfg.CNC_STATION_IDS:
        out.append(Suggestion("Elevate", c.label,
                              f"{c.label} is machine-limited ({st.num_machines} machine(s)) - more people only help "
                              f"if a machine is unmanned; more hours do: +2 h/day on the "
                              f"{cfg.STATION_CREW[c.station]} crew ≈ +{2 * crew.days_per_week * cap_h:,.0f} m²/week"
                              f"{fifth_day}. Real cut times (S1/S2/S3 are still placeholders) may change this picture.",
                              gain_m2_per_week=2 * crew.days_per_week * cap_h))
    elif st.machine_bound:
        out.append(Suggestion("Elevate", c.label,
                              f"{c.label} is a machine-paced station: extra hands beyond its {st.ideal_ops}-person "
                              f"crew add nothing. Add hours: +2 h/day ≈ +{2 * crew.days_per_week * cap_h:,.0f} m²/week"
                              f"{fifth_day}.",
                              gain_m2_per_week=2 * crew.days_per_week * cap_h))
    else:
        rate = st.capacity_m2_per_op_hour if c.staffed_h == 0 else cap_h / max(1.0, _avg_heads(result, c.station))
        out.append(Suggestion("Elevate", c.label,
                              f"Add one skilled person to {c.label} for its running hours ≈ "
                              f"+{rate * active_h_per_week:,.0f} m²/week (or +2 h/day ≈ "
                              f"+{2 * crew.days_per_week * cap_h:,.0f} m²/week{fifth_day}). Candidates to cross-train: "
                              f"{_cross_train_candidates(c.station, roster)}.",
                              gain_m2_per_week=rate * active_h_per_week))
    # Next --------------------------------------------------------------
    if next_c is not None:
        out.append(Suggestion("Next", next_c.label,
                              f"Once {c.label} is relieved, {next_c.label} ({next_c.utilisation*100:.0f}% utilised, "
                              f"{next_c.headroom_m2_per_week:,.0f} m²/week headroom) becomes the constraint."))
    return out


def _avg_heads(result: SimulationResult, sid: str) -> float:
    hours = [t["ops"].get(sid, 0) for t in result.trace if sid in t.get("active", [])]
    staffed = [h for h in hours if h > 0]
    return sum(staffed) / len(staffed) if staffed else 1.0


def _cross_train_candidates(sid: str, roster) -> str:
    if roster is None:
        return "see Staff & Skills"
    names = [op.name for op in roster.operators
             if sid not in op.all_qualified_stations and op.home_station not in cfg.ALLOCATION_COVER_PRIORITY
             and op.home_station != "admin"]
    return ", ".join(names[:4]) + (f" (+{len(names) - 4} more)" if len(names) > 4 else "") if names else "none free"


# ---------------------------------------------------------------------------
# Staff utilisation
# ---------------------------------------------------------------------------

def _station_labour(result: SimulationResult) -> list[dict]:
    rows = []
    for sid in cfg.FLOW_STATIONS:
        st = cfg.STATIONS[sid]
        staffed = result.station_staffed_hours.get(sid, 0.0)
        busy = result.station_busy_hours.get(sid, 0.0)
        rows.append({
            "Station": st.label,
            "Line": "Shared" if st.route is None else ROUTE_LABELS[st.route],
            "Running h": result.station_active_hours.get(sid, 0.0),
            "Staffed h": staffed,
            "Had work h": busy,
            "Labour utilisation %": (100.0 * busy / staffed) if staffed > 0 else 0.0,
            "Capacity utilisation %": 100.0 * result.station_utilisation.get(sid, 0.0),
            "Unstaffed h": result.station_unstaffed_hours.get(sid, 0.0),
            "Starved h": result.station_starved_hours.get(sid, 0.0),
        })
    return rows


def _people(result: SimulationResult, roster) -> list[dict]:
    if roster is None:
        return []
    rows = []
    for op in roster.operators:
        h = result.person_hours.get(op.id)
        if not h:
            continue
        rostered = h["rostered"]
        if not cfg.is_flow_station(op.home_station) and h["covering"] == 0:
            # Box packing / admin isn't simulated - no m2 flows through, so
            # "waiting" would be meaningless. Shown, but not rated.
            rows.append({"Name": op.name, "Home": cfg.STATIONS[op.home_station].label, "Shift": op.shift,
                         "Rostered h": rostered, "Producing h": None, "Waiting h": None,
                         "Covering h": h["covering"], "Absent h": h["absent"], "Utilisation %": None})
            continue
        rows.append({
            "Name": op.name,
            "Home": cfg.STATIONS[op.home_station].label,
            "Shift": op.shift,
            "Rostered h": rostered,
            "Producing h": h["producing"],
            "Waiting h": h["waiting"],
            "Covering h": h["covering"],
            "Absent h": h["absent"],
            "Utilisation %": (100.0 * h["producing"] / rostered) if rostered > 0 else 0.0,
        })
    rows.sort(key=lambda r: (r["Utilisation %"] if r["Utilisation %"] is not None else 999.0, -r["Rostered h"]))
    return rows
