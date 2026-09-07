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
import random
from dataclasses import dataclass, field
from pathlib import Path

from plant_sim.config import DEFAULT_ABSENCE_RATE_PCT, STATIONS


@dataclass
class Operator:
    id: str
    name: str
    home_station: str                    # station id, e.g. "cnc_thermo"
    skills: set[str] = field(default_factory=set)  # station ids they can cover
    shift: str = "day"                   # "day" or "aft" - which shift they're rostered to
    absence_rate_pct: float = DEFAULT_ABSENCE_RATE_PCT  # per-person override
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


class Roster:
    """The full staff list, plus the day-by-day absence draw."""

    def __init__(self, operators: list[Operator] | None = None):
        self.operators: list[Operator] = operators or []

    # -- CRUD -----------------------------------------------------------
    def add(self, operator: Operator) -> None:
        self.operators.append(operator)

    def remove(self, operator_id: str) -> None:
        self.operators = [o for o in self.operators if o.id != operator_id]

    def get(self, operator_id: str) -> Operator | None:
        return next((o for o in self.operators if o.id == operator_id), None)

    def by_shift(self, shift: str) -> list[Operator]:
        return [o for o in self.operators if o.shift == shift]

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
        """
        operators = []
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                extra_skills = {s.strip() for s in row.get("skills", "").split(";") if s.strip()}
                operators.append(Operator(
                    id=row["id"],
                    name=row["name"],
                    home_station=row["home_station"],
                    shift=row.get("shift", "day") or "day",
                    skills=extra_skills,
                    absence_rate_pct=float(row["absence_rate_pct"]) if row.get("absence_rate_pct") else DEFAULT_ABSENCE_RATE_PCT,
                    notes=row.get("notes", ""),
                ))
        return cls(operators)

    def to_csv(self, path: str | Path) -> None:
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["id", "name", "home_station", "shift", "skills",
                              "absence_rate_pct", "notes"])
            for op in self.operators:
                writer.writerow([
                    op.id, op.name, op.home_station, op.shift,
                    ";".join(sorted(op.skills)), op.absence_rate_pct, op.notes,
                ])
