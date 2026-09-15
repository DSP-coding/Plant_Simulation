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
              has the biggest backlog per head right now - as long as that
              station can actually use another pair of hands (a one-machine
              CNC with someone already on it can't).
    Pass 3 - Rebalancing: if some station is still drowning in backlog after
              Pass 1/2, pull a limited number of multi-skilled people off
              comfortably-staffed stations to go help, mirroring what a real
              supervisor would do. Surplus people (more bodies than a machine
              can use) are moved first; a station is never stripped to zero;
              nobody is moved twice in one shift.

The result is a plain dict: {operator_id: station_id_or_None}. None means the
operator is rostered on today but nothing they're qualified for is running
(they show up as "idle" in the results, which is useful information itself -
it flags mismatched skills/shifts).

Backlog "pressure" is measured per station as m2 waiting / people on it. The
two presses share one physical pile of work, so their pressure pools both
machines' people against that one pile (see cfg.queue_id_for).
"""

from __future__ import annotations

from plant_sim import config as cfg
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
    queue_m2:        current backlog (m2 waiting) in front of each STATION
                      (the two presses both report the shared press pile),
                      used purely to decide who needs help most
    """
    assignment: dict[str, str | None] = {}
    by_id = {op.id: op for op in operators}

    # --- Pass 1: home station -------------------------------------------------
    for op in operators:
        assignment[op.id] = op.home_station if op.home_station in active_stations else None

    # --- Pass 2: place idle floaters at the neediest station they can cover ---
    for op in operators:
        if assignment[op.id] is not None:
            continue
        qualified_active = [s for s in op.all_qualified_stations if s in active_stations]
        if not qualified_active:
            continue  # genuinely nothing for them to do this shift
        with_room = [s for s in qualified_active if _has_room(s, assignment)]
        if not with_room:
            continue  # every station they know is already as full as it can usefully be
        assignment[op.id] = _neediest_station(with_room, assignment, queue_m2)

    # --- Pass 3: rebalance floaters toward the most backed-up station --------
    already_moved: set[str] = set()
    for _ in range(ALLOCATION_MAX_REBALANCE_MOVES):
        loads = _current_loads(assignment, active_stations)
        pressures = {s: _pressure(s, assignment, queue_m2) for s in active_stations}
        # Stations drowning badly enough to justify moving people, that can
        # still USE another person - worst first. If nobody qualified can be
        # freed for the worst one, try the next (e.g. Press 1 and Press 2
        # share a pile and tie on pressure, but a floater may only know one).
        drowning = sorted(
            (s for s in pressures
             if pressures[s] >= ALLOCATION_PRESSURE_THRESHOLD_M2 and _has_room(s, assignment)),
            key=pressures.get, reverse=True)
        moved = False
        for busiest in drowning:
            if _try_move_one_operator_to(busiest, loads, pressures, by_id, assignment, already_moved):
                moved = True
                break
        if not moved:
            break  # nobody drowning, or no quiet, qualified, spare operator available to help

    return assignment


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _heads_on_queue(station: str, assignment: dict[str, str | None]) -> int:
    """People currently working any station that pulls from this station's
    pile (both presses count toward the shared press pile)."""
    q = cfg.queue_id_for(station)
    return sum(1 for s in assignment.values() if s is not None and cfg.queue_id_for(s) == q)


def _heads_on_station(station: str, assignment: dict[str, str | None]) -> int:
    return sum(1 for s in assignment.values() if s == station)


def _pressure(station: str, assignment: dict[str, str | None], queue_m2: dict[str, float]) -> float:
    return queue_m2.get(station, 0.0) / max(1, _heads_on_queue(station, assignment))


def _has_room(station: str, assignment: dict[str, str | None]) -> bool:
    """Can this station usefully take one more person?"""
    cap = cfg.STATIONS[station].max_useful_ops
    return cap is None or _heads_on_station(station, assignment) < cap


def _neediest_station(candidate_stations: list[str], assignment: dict[str, str | None],
                       queue_m2: dict[str, float]) -> str:
    return max(candidate_stations, key=lambda s: _pressure(s, assignment, queue_m2))


def _current_loads(assignment: dict[str, str | None], active_stations: set[str]) -> dict[str, list[str]]:
    loads: dict[str, list[str]] = {s: [] for s in active_stations}
    for op_id, station in assignment.items():
        if station in loads:
            loads[station].append(op_id)
    return loads


def _try_move_one_operator_to(busiest: str, loads: dict[str, list[str]],
                               pressures: dict[str, float], by_id: dict[str, Operator],
                               assignment: dict[str, str | None],
                               already_moved: set[str]) -> bool:
    """
    Find one operator to send to `busiest` and move them. Mutates
    `assignment` (and `already_moved`) in place. Donor preference:

      1. Anyone SURPLUS at a machine-limited station (more people than the
         machine can use) - moving them costs that station nothing.
      2. Otherwise, the quietest station first, as long as it isn't itself
         under pressure and wouldn't be left empty.
    """
    def qualified_spare(op_id: str) -> bool:
        return (op_id not in already_moved
                and busiest in by_id[op_id].all_qualified_stations)

    def move(op_id: str) -> bool:
        assignment[op_id] = busiest
        already_moved.add(op_id)
        return True

    # 1. Surplus people first.
    for station, op_ids in loads.items():
        if station == busiest:
            continue
        cap = cfg.STATIONS[station].max_useful_ops
        if cap is None or len(op_ids) <= cap:
            continue
        for op_id in op_ids:
            if qualified_spare(op_id):
                return move(op_id)

    # 2. Then the quietest stations.
    quiet_stations = sorted((s for s in loads if s != busiest), key=lambda s: pressures.get(s, 0.0))
    for quiet in quiet_stations:
        if pressures.get(quiet, 0.0) >= ALLOCATION_PRESSURE_THRESHOLD_M2:
            break  # even the quietest remaining station is itself under pressure
        if len(loads[quiet]) <= 1:
            continue  # never strip a station down to zero staff
        for op_id in loads[quiet]:
            if qualified_spare(op_id):
                return move(op_id)
    return False
