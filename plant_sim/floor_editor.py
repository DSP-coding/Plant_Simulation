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

# How the floor is laid out: a MAP of the real shop floor, traced from the
# hand-drawn plan (rotated to landscape so the labels read left-to-right).
# Positions are percentages of the map canvas. Flow runs roughly left to
# right: the Thermo CNCs and the Cut & Clash corner on the left, sanding /
# MB Sander / Cefla / presses across the middle, Dispatch as the tall area
# on the right with the Hafele and DIY packing benches inside it.
#
# Each entry is one drop target. "machines" draws named tiles inside the
# station (a person's roster "machine" decides which tile they sit in);
# "children" nests whole stations inside another station's area.
FLOOR_MAP = [
    {"station": "cnc_thermo", "x": 1,  "y": 2,  "w": 33, "h": 36},
    {"station": "cnc_1536",   "x": 16, "y": 42, "w": 11, "h": 24},
    {"station": "eb_drilling", "x": 1, "y": 70, "w": 24, "h": 28},
    {"station": "optimising", "x": 28, "y": 70, "w": 12, "h": 28},
    {"station": "sanding",    "x": 36, "y": 2,  "w": 11, "h": 34},
    {"station": "mb_sander",  "x": 42, "y": 40, "w": 12, "h": 30},
    {"station": "admin",      "x": 42, "y": 74, "w": 12, "h": 24},
    {"station": "edging",     "x": 56, "y": 2,  "w": 13, "h": 34},
    {"station": "press_2",    "x": 56, "y": 40, "w": 11, "h": 30},
    {"station": "press_1",    "x": 68, "y": 40, "w": 11, "h": 30},
    {"station": "despatch",   "x": 81, "y": 2,  "w": 18, "h": 96, "children": ["hafele", "diy"]},
]

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
            "machines": list(st.machines),
        }
        for sid, st in cfg.STATIONS.items()
    }
    staff = [
        {"id": op.id, "name": op.name, "home": op.home_station, "shift": op.shift,
         "machine": op.machine, "skills": sorted(op.skills), "absence": op.absence_rate_pct}
        for op in roster.operators
    ]
    shifts = [dict(lane, hint=_crew_hint(lane["id"], shift_schedules)) for lane in SHIFT_LANES]
    return _floor_editor_component(
        floor=FLOOR_MAP, stations=stations, staff=staff, shifts=shifts, key=key, default=None,
    )


def apply_move(roster: Roster, event: dict) -> str | None:
    """Apply one drop event to the roster. Returns a human-readable
    description of what changed, or None if the event was a no-op / invalid.

    Rule: dropping someone on a station makes it their home station (their
    previous home is kept as an extra skill - they still know that job);
    dropping onto a named machine tile also records that machine; dropping
    into the other lane changes their shift.
    """
    op = roster.get(str(event.get("op_id", "")))
    to_station = event.get("to_station")
    to_shift = event.get("to_shift")
    to_machine = str(event.get("to_machine") or "")
    if op is None or to_station not in cfg.STATIONS or to_shift not in cfg.SHIFT_LABELS:
        return None
    if to_machine and to_machine not in cfg.STATIONS[to_station].machines:
        to_machine = ""
    changes = []
    if to_station != op.home_station:
        old_home = op.home_station
        op.skills.add(old_home)
        op.skills.discard(to_station)
        op.home_station = to_station
        op.machine = ""
        changes.append(f"home {cfg.STATIONS[old_home].label} → {cfg.STATIONS[to_station].label}")
    if to_machine != op.machine:
        changes.append(f"machine {op.machine or '-'} → {to_machine or '-'}")
        op.machine = to_machine
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
