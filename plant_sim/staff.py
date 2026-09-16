"""
Named staff, skills, and absenteeism.

This is the piece that replaces the old HTML tool's "operators per shift" sliders.
Instead of an abstract headcount per station, we track real people: each has a
name, a home station (their normal job), a set of stations they are *skilled*
to cover, and a personal probability of being away on any given working day.

Editing a person's skills or absence rate here (or via the Streamlit UI, which
just calls the methods below) is the whole point of "click staff, set skills".
"""

from __future__ import annotations

import csv
import os
import random
from dataclasses import dataclass, field
from pathlib import Path

from plant_sim.config import DEFAULT_ABSENCE_RATE_PCT, SHIFT_LABELS, STATIONS

ROSTER_COLUMNS = ["id", "name", "home_station", "shift", "skills", "absence_rate_pct", "machine",
                  "station_2", "machine_2", "notes"]


class RosterError(ValueError):
    """Raised when a roster (file or in-memory) contains something the
    simulation can't run with - every problem is listed, not just the first."""


@dataclass
class Operator:
    id: str
    name: str
    home_station: str                    # station id, e.g. "cnc_thermo"
    skills: set[str] = field(default_factory=set)  # station ids they can cover
    shift: str = "day"                   # "day" or "aft" - which shift they're rostered to
    absence_rate_pct: float = DEFAULT_ABSENCE_RATE_PCT  # per-person override
    machine: str = ""                    # which named machine at home_station they normally run (optional)
    station_2: str = ""                  # a second station they also work at the same time (split attention)
    machine_2: str = ""                  # ...and which named machine there, if that station has machines

    @property
    def second_station(self) -> str:
        """Where their split time goes: station_2, or home when machine_2 is
        a second machine at their own station (the CNC-runs-two case)."""
        if self.station_2:
            return self.station_2
        return self.home_station if self.machine_2 else ""

    @property
    def is_split(self) -> bool:
        return bool(self.station_2 or self.machine_2)

    @property
    def runs_second_cnc(self) -> bool:
        """A Thermo CNC operator tending a second Thermo CNC (unattended machine)."""
        return (self.home_station == "cnc_thermo" and self.second_station == "cnc_thermo"
                and bool(self.machine) and bool(self.machine_2) and self.machine_2 != self.machine)
    notes: str = ""

    def can_work(self, station_id: str) -> bool:
        return station_id == self.home_station or station_id in self.skills

    def add_skill(self, station_id: str) -> None:
        if station_id not in STATIONS:
            raise ValueError(f"Unknown station id: {station_id}")
        self.skills.add(station_id)

    def remove_skill(self, station_id: str) -> None:
        self.skills.discard(station_id)

    @property
    def all_qualified_stations(self) -> set[str]:
        """Home station plus every extra skill, deduplicated."""
        return {self.home_station} | self.skills

    def problems(self) -> list[str]:
        """Human-readable list of anything wrong with this record (empty = fine)."""
        out = []
        who = f"{self.name or '?'} (id {self.id or '?'})"
        if not str(self.id).strip():
            out.append("an operator has an empty id")
        if not str(self.name).strip():
            out.append(f"operator id {self.id!r} has an empty name")
        if self.home_station not in STATIONS:
            out.append(f"{who}: unknown home station {self.home_station!r} "
                       f"(valid: {', '.join(STATIONS)})")
        for s in self.skills:
            if s not in STATIONS:
                out.append(f"{who}: unknown skill station {s!r}")
        if self.shift not in SHIFT_LABELS:
            out.append(f"{who}: shift must be one of {SHIFT_LABELS}, got {self.shift!r}")
        if self.home_station in STATIONS:
            valid = STATIONS[self.home_station].machines
            if self.machine and self.machine not in valid:
                out.append(f"{who}: machine {self.machine!r} is not one of {self.home_station}'s machines "
                           f"({', '.join(valid) or 'none'})")
        if self.station_2:
            if self.station_2 not in STATIONS:
                out.append(f"{who}: unknown station_2 {self.station_2!r}")
            elif self.station_2 not in self.all_qualified_stations:
                out.append(f"{who}: station_2 {self.station_2!r} is not their home or one of their skills")
        second = self.second_station
        if self.machine_2 and second in STATIONS:
            valid2 = STATIONS[second].machines
            if self.machine_2 not in valid2:
                out.append(f"{who}: machine_2 {self.machine_2!r} is not one of {second}'s machines "
                           f"({', '.join(valid2) or 'none'})")
            if second == self.home_station:
                if not self.machine:
                    out.append(f"{who}: machine_2 at their own station needs machine set too")
                elif self.machine_2 == self.machine:
                    out.append(f"{who}: machine_2 is the same as machine ({self.machine!r})")
        if self.station_2 and self.station_2 == self.home_station and not self.machine_2:
            out.append(f"{who}: station_2 is their home station - set machine_2 for a second machine, or clear it")
        try:
            rate = float(self.absence_rate_pct)
            if not (0.0 <= rate <= 100.0):
                out.append(f"{who}: absence rate must be 0-100%, got {rate}")
        except (TypeError, ValueError):
            out.append(f"{who}: absence rate is not a number ({self.absence_rate_pct!r})")
        return out


class Roster:
    """The full staff list, plus the day-by-day absence draw."""

    def __init__(self, operators: list[Operator] | None = None):
        self.operators: list[Operator] = list(operators) if operators else []

    # -- CRUD -----------------------------------------------------------
    def add(self, operator: Operator) -> None:
        if self.get(operator.id) is not None:
            raise RosterError(f"operator id {operator.id!r} already exists")
        self.operators.append(operator)

    def remove(self, operator_id: str) -> None:
        self.operators = [o for o in self.operators if o.id != operator_id]

    def get(self, operator_id: str) -> Operator | None:
        return next((o for o in self.operators if o.id == operator_id), None)

    def by_shift(self, shift: str) -> list[Operator]:
        return [o for o in self.operators if o.shift == shift]

    # -- Validation ---------------------------------------------------------
    def problems(self) -> list[str]:
        out = []
        seen: set[str] = set()
        for op in self.operators:
            out.extend(op.problems())
            if op.id in seen:
                out.append(f"duplicate operator id {op.id!r}")
            seen.add(op.id)
        return out

    def validate(self) -> None:
        """Raise RosterError listing every problem, or return silently."""
        problems = self.problems()
        if problems:
            raise RosterError("Roster has problems:\n  - " + "\n  - ".join(problems))

    # -- Absenteeism ------------------------------------------------------
    def roll_daily_absences(self, day_index: int, sick_enabled: bool,
                             rng: random.Random) -> set[str]:
        """
        Decide who is out sick on a given working day.

        Each operator is drawn independently against their own absence_rate_pct.
        Returns the set of operator ids who are ABSENT that day. When
        sick_enabled is False, nobody is absent (fully deterministic run).
        """
        if not sick_enabled:
            return set()
        absent = set()
        for op in self.operators:
            if rng.random() * 100 < op.absence_rate_pct:
                absent.add(op.id)
        return absent

    # -- Loading / saving --------------------------------------------------
    @classmethod
    def from_csv(cls, path: str | Path) -> "Roster":
        """
        Load a roster from a CSV with columns:
            id,name,home_station,shift,skills,absence_rate_pct,notes
        where `skills` is a semicolon-separated list of extra station ids
        (the home_station is always implicitly included, no need to repeat it).

        Raises RosterError (listing every problem) if the file can't be used.
        """
        path = Path(path)
        if not path.exists():
            raise RosterError(f"roster file not found: {path}")
        operators = []
        problems = []
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            missing = [c for c in ("id", "name", "home_station") if c not in (reader.fieldnames or [])]
            if missing:
                raise RosterError(f"{path}: missing required column(s) {missing}; "
                                  f"expected header {','.join(ROSTER_COLUMNS)}")
            for line_no, row in enumerate(reader, start=2):
                # Blank/whitespace-only lines are skipped, not treated as a person.
                if not any((v or "").strip() for v in row.values() if isinstance(v, str)):
                    continue
                rate_raw = (row.get("absence_rate_pct") or "").strip()
                try:
                    rate = float(rate_raw) if rate_raw else DEFAULT_ABSENCE_RATE_PCT
                except ValueError:
                    problems.append(f"line {line_no}: absence_rate_pct {rate_raw!r} is not a number")
                    rate = DEFAULT_ABSENCE_RATE_PCT
                home = (row.get("home_station") or "").strip()
                extra_skills = {s.strip() for s in (row.get("skills") or "").split(";") if s.strip()}
                extra_skills.discard(home)   # home is implicit; don't double-list it
                operators.append(Operator(
                    id=(row.get("id") or "").strip(),
                    name=(row.get("name") or "").strip(),
                    home_station=home,
                    shift=(row.get("shift") or "").strip().lower() or "day",
                    skills=extra_skills,
                    absence_rate_pct=rate,
                    machine=(row.get("machine") or "").strip(),
                    station_2=(row.get("station_2") or "").strip(),
                    machine_2=(row.get("machine_2") or "").strip(),
                    notes=(row.get("notes") or "").strip(),
                ))
        roster = cls(operators)
        problems.extend(roster.problems())
        if problems:
            raise RosterError(f"{path} has problems:\n  - " + "\n  - ".join(problems))
        return roster

    def to_csv(self, path: str | Path) -> None:
        """Write the roster out. The file is written to a temporary name and
        then swapped into place, so a crash mid-write can never leave a
        half-written roster behind."""
        path = Path(path)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(ROSTER_COLUMNS)
            for op in self.operators:
                writer.writerow([
                    op.id, op.name, op.home_station, op.shift,
                    ";".join(sorted(op.skills - {op.home_station})), op.absence_rate_pct, op.machine,
                    op.station_2, op.machine_2, op.notes,
                ])
        os.replace(tmp, path)
