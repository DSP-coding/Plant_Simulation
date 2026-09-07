"""
Daily/shift staffing allocation: decide which station each available operator
actually works today.

This is deliberately a simple, readable three-pass heuristic rather than an
optimiser, so you can follow (and tweak) the logic by eye:

    Pass 1 - Home station: everyone works their normal job, if it's running
              this shift at all.
    Pass 2 - Idle floaters: anyone whose home station ISN'T running today
              (e.g. their crew is off, or they're rostered but their station
              has no work) gets sent to whichever station they're skilled for
              has the biggest backlog per head right now.
    Pass 3 - Rebalancing: if some station is still drowning in backlog after
              Pass 1/2, pull a limited number of multi-skilled people off
              comfortably-staffed stations to go help, mirroring what a real
              supervisor would do. A station is never stripped down to zero.

The result is a plain dict: {operator_id: station_id_or_None}. None means the
operator is rostered on today but nothing they're qualified for is running
(they show up as "idle" in the results, which is useful information itself -
it flags mismatched skills/shifts).
"""

from __future__ import annotations

from plant_sim.config import ALLOCATION_MAX_REBALANCE_MOVES, ALLOCATION_PRESSURE_THRESHOLD_M2
from plant_sim.staff import Operator


def allocate_shift(
    operators: list[Operator],
    active_stations: set[str],
    queue_m2: dict[str, float],
) -> dict[str, str | None]:
    """
    operators:       the people rostered to work THIS shift today (already
                      filtered for absence elsewhere)
    active_stations: station ids whose crew schedule is actually running this
                      shift today
    queue_m2:        current backlog (m2 waiting) in front of each station,
                      used purely to decide who needs help most
    """
    assignment: dict[str, str | None] = {}

    # --- Pass 1: home station -------------------------------------------------
    for op in operators:
        assignment[op.id] = op.home_station if op.home_station in active_stations else None

    # --- Pass 2: place idle floaters at the neediest station they can cover ---
    by_id = {op.id: op for op in operators}
    for op in operators:
        if assignment[op.id] is not None:
            continue
        qualified_active = [s for s in op.all_qualified_stations if s in active_stations]
        if not qualified_active:
            continue  # genuinely nothing for them to do this shift
        assignment[op.id] = _neediest_station(qualified_active, assignment, queue_m2)

    # --- Pass 3: rebalance floaters toward the most backed-up station --------
    for _ in range(ALLOCATION_MAX_REBALANCE_MOVES):
        loads = _current_loads(assignment, active_stations)
        pressures = {s: queue_m2.get(s, 0.0) / max(1, len(loads[s])) for s in active_stations}
        if not pressures:
            break
        busiest = max(pressures, key=pressures.get)
        if pressures[busiest] < ALLOCATION_PRESSURE_THRESHOLD_M2:
            break  # nobody is drowning badly enough to justify moving people

        moved = _try_move_one_operator_to(busiest, loads, pressures, by_id, assignment)
        if not moved:
            break  # no quiet, qualified, spare operator available to help

    return assignment


def _neediest_station(candidate_stations: list[str], assignment: dict[str, str | None],
                       queue_m2: dict[str, float]) -> str:
    def pressure(station: str) -> float:
        current_count = sum(1 for s in assignment.values() if s == station)
        return queue_m2.get(station, 0.0) / max(1, current_count)

    return max(candidate_stations, key=pressure)


def _current_loads(assignment: dict[str, str | None], active_stations: set[str]) -> dict[str, list[str]]:
    loads: dict[str, list[str]] = {s: [] for s in active_stations}
    for op_id, station in assignment.items():
        if station in loads:
            loads[station].append(op_id)
    return loads


def _try_move_one_operator_to(busiest: str, loads: dict[str, list[str]],
                               pressures: dict[str, float], by_id: dict[str, Operator],
                               assignment: dict[str, str | None]) -> bool:
    """
    Look at every other active station, quietest first, and try to poach one
    operator who is (a) skilled for `busiest` and (b) not the last person
    keeping their current station staffed. Mutates `assignment` in place.
    """
    quiet_stations = sorted((s for s in loads if s != busiest), key=lambda s: pressures.get(s, 0.0))
    for quiet in quiet_stations:
        if pressures.get(quiet, 0.0) >= ALLOCATION_PRESSURE_THRESHOLD_M2:
            break  # even the quietest remaining station is itself under pressure
        if len(loads[quiet]) <= 1:
            continue  # never strip a station down to zero staff
        for op_id in loads[quiet]:
            if busiest in by_id[op_id].all_qualified_stations:
                assignment[op_id] = busiest
                return True
    return False
