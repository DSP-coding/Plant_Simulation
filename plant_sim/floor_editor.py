"""
The interactive factory-floor editor: every station drawn as a box with the
people rostered to it as draggable name chips, one lane per shift. Drag a
person onto another station (or into the other shift's lane) and the roster
changes - then run the simulation to see what it did.

This is a small bidirectional Streamlit component built from a single
vanilla HTML/JS file (floor_editor/index.html) - no npm build step. The
page talks to Streamlit with the standard postMessage protocol; each drop
comes back as {"seq", "op_id", "to_station", "to_shift"}, and app.py applies
it to the Roster via apply_move() below.

Only the visual layout and the move rule live here; who is qualified for
what still comes from staff.py, and station facts from config.py.
"""

from __future__ import annotations

import copy
from pathlib import Path

import streamlit.components.v1 as components

from plant_sim import config as cfg
from plant_sim.floor_view import STATION_ICONS
from plant_sim.staff import Roster

_COMPONENT_DIR = Path(__file__).parent / "floor_editor"
_floor_editor_component = components.declare_component("plant_floor_editor", path=str(_COMPONENT_DIR))

# How the floor is laid out: the two production lines as rows, then the
# stations both lines share (packing) and admin as tall boxes on the right.
FLOOR_ROWS = [
    {"label": "Cut & Clash (1536)", "stations": list(cfg.ROUTE_SEQUENCE[cfg.Route.CUT_AND_CLASH])},
    {"label": "Thermo (Series 1/2/3)",
     "stations": list(cfg.ROUTE_SEQUENCE[cfg.Route.THERMO]) + list(cfg.PRESS_STATIONS)},
]
FLOOR_SHARED = [cfg.SHARED_TERMINAL_STATION, "admin"]

SHIFT_LANES = [
    {"id": "day", "label": "Day shift"},
    {"id": "aft", "label": "Afternoon shift"},
]


def _crew_hint(shift_id: str, shift_schedules: dict[str, cfg.ShiftSchedule] | None) -> str:
    """One-line reminder of which crews actually run this shift."""
    if not shift_schedules:
        return ""
    parts = []
    for crew, sched in shift_schedules.items():
        if crew == "admin":
            continue
        if sched.offers_shift(shift_id):
            hrs = sched.day_hrs if shift_id == "day" else sched.aft_hrs
            parts.append(f"{crew} {hrs:g}h x {sched.days_per_week}d")
        else:
            parts.append(f"{crew}: none")
    return " · ".join(parts)


def floor_editor(roster: Roster, shift_schedules: dict[str, cfg.ShiftSchedule] | None = None,
                 key: str = "floor_editor") -> dict | None:
    """Render the editor and return the latest drop event (or None).

    The same event is returned again on every rerun until a new drop
    happens, so callers must de-duplicate on event["seq"] (see app.py).
    """
    stations = {
        sid: {
            "label": st.label,
            "icon": STATION_ICONS.get(sid, "🏢"),
            "ideal_ops": st.ideal_ops,
            "max_useful_ops": st.max_useful_ops,
        }
        for sid, st in cfg.STATIONS.items()
    }
    staff = [
        {"id": op.id, "name": op.name, "home": op.home_station, "shift": op.shift,
         "skills": sorted(op.skills), "absence": op.absence_rate_pct}
        for op in roster.operators
    ]
    shifts = [dict(lane, hint=_crew_hint(lane["id"], shift_schedules)) for lane in SHIFT_LANES]
    columns = max(len(r["stations"]) for r in FLOOR_ROWS)
    return _floor_editor_component(
        rows=FLOOR_ROWS, columns=columns, shared=FLOOR_SHARED, stations=stations,
        staff=staff, shifts=shifts, key=key, default=None,
    )


def apply_move(roster: Roster, event: dict) -> str | None:
    """Apply one drop event to the roster. Returns a human-readable
    description of what changed, or None if the event was a no-op / invalid.

    Rule: dropping someone on a station makes it their home station (their
    previous home is kept as an extra skill - they still know that job);
    dropping into the other lane changes their shift.
    """
    op = roster.get(str(event.get("op_id", "")))
    to_station = event.get("to_station")
    to_shift = event.get("to_shift")
    if op is None or to_station not in cfg.STATIONS or to_shift not in cfg.SHIFT_LABELS:
        return None
    changes = []
    if to_station != op.home_station:
        old_home = op.home_station
        op.skills.add(old_home)
        op.skills.discard(to_station)
        op.home_station = to_station
        changes.append(f"home {cfg.STATIONS[old_home].label} → {cfg.STATIONS[to_station].label}")
    if to_shift != op.shift:
        changes.append(f"shift {op.shift} → {to_shift}")
        op.shift = to_shift
    if not changes:
        return None
    return f"{op.name}: " + ", ".join(changes)


def snapshot(roster: Roster) -> list:
    """A deep copy of the roster's people, for undo."""
    return copy.deepcopy(roster.operators)


def restore(roster: Roster, snap: list) -> None:
    roster.operators = copy.deepcopy(snap)
