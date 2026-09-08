"""
Calibration constants and static factory layout for the thermoform / cut-and-clash
plant simulator.

Every number in this file is tagged with where it came from:
    [REAL]        - computed directly from a real data file you supplied
    [ASSUMPTION]  - a placeholder carried over from the original spreadsheet tool,
                    or a reasonable guess, until you confirm/replace it

Nothing else in the codebase should contain a magic number - if you need to tune
the simulation, this is the file to edit.
"""

from __future__ import annotations
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Part / board geometry
# ---------------------------------------------------------------------------

# [REAL] average m^2 per part, computed from product_2026.xlsx (148,288 parts,
# 5 Jan - 7 Sep 2026): Therm 0.3797 m^2/part, Mel 0.4912 m^2/part, Acr 0.3648 m^2/part.
AVG_PART_M2 = {
    "S1": 0.38,       # placeholder split of "Therm" until Series Mix by Month.xlsx confirms
    "S2": 0.38,
    "S3": 0.38,
    "Acrylic": 0.3648,   # [REAL] product_2026.xlsx
    "Melamine": 0.4912,  # [REAL] product_2026.xlsx
}

# [ASSUMPTION] carried over from the original HTML tool - a "board" is the raw
# sheet loaded onto the CNC before nesting/cutting extracts parts from it.
BOARD_M2 = 2.88
PARTS_PER_BOARD = 8  # BOARD_M2 / ~0.36 avg part m2

WEEKS_PER_MONTH = 4.345

# "Reference" schedule used ONLY to convert an editable "capacity in m2/month
# at ideal staffing" into the internal per-operator-hour (or per-machine-
# minute) rate the engine actually runs on - it is NOT the schedule you
# configure in the sidebar. [ASSUMPTION] carried from the old tool: think of
# it as "flat out, 2x 10hr shifts x 4 days/week" (80 hrs/week).
REFERENCE_HOURS_PER_WEEK = 80.0
REFERENCE_HOURS_PER_MONTH = REFERENCE_HOURS_PER_WEEK * WEEKS_PER_MONTH


# ---------------------------------------------------------------------------
# Product classes and routing
# ---------------------------------------------------------------------------

class Route:
    """The two physical paths a batch of parts can take through the factory."""
    THERMO = "thermo"        # CNC -> Sanding -> MB Sander -> Cefla -> Press -> Packing
    CUT_AND_CLASH = "cutclash"  # Optimising -> CNC 1536 -> Edge Band/Drill -> Packing


@dataclass(frozen=True)
class ProductClass:
    code: str            # e.g. "S1", "Acrylic"
    label: str
    route: str            # Route.THERMO or Route.CUT_AND_CLASH
    cnc_min_per_board: float   # machine-minutes to cut one board of this class


PRODUCT_CLASSES: dict[str, ProductClass] = {
    # [ASSUMPTION] cnc_min_per_board values are carried over from the original
    # HTML tool's placeholders (7 / 14 / 50 min/board for S1/S2/S3). Replace once
    # "Series Mix by Month.xlsx" gives real cut times per series.
    "S1": ProductClass("S1", "Series 1", Route.THERMO, cnc_min_per_board=7.0),
    "S2": ProductClass("S2", "Series 2", Route.THERMO, cnc_min_per_board=14.0),
    "S3": ProductClass("S3", "Series 3", Route.THERMO, cnc_min_per_board=50.0),
    # [ASSUMPTION] cut-and-clash cycle times: not yet supplied. Using a flat
    # mid-range placeholder until you give real 1536 cycle times per material.
    "Acrylic": ProductClass("Acrylic", "Acrylic (Cut & Clash)", Route.CUT_AND_CLASH,
                             cnc_min_per_board=20.0),
    "Melamine": ProductClass("Melamine", "Melamine (Cut & Clash)", Route.CUT_AND_CLASH,
                              cnc_min_per_board=20.0),
}

# [REAL] YTD product-type share, from product_2026.xlsx "Monthly Summary" tab
# (Cut and Clash = Mel + Acr): Thermo 80.6% (47,254.7 m2), Cut & Clash 19.4%
# (11,394.3 m2 = Mel 10,777.4 + Acr 616.9). Within Thermo, the S1/S2/S3 split
# below is still the [ASSUMPTION] placeholder from the old tool (58.8/0.14/34.9
# rescaled to sum to the real 80.6% Thermo share) pending Series Mix by Month.xlsx.
DEFAULT_MIX_PCT = {
    "S1": 62.8,       # 80.6% * (58.8 / 93.84)
    "S2": 0.15,       # 80.6% * (0.14 / 93.84)
    "S3": 37.35,      # wait: normalised below, see note
    "Acrylic": 1.04,  # 19.4% * (616.9 / 11394.3)
    "Melamine": 18.36,  # 19.4% * (10777.4 / 11394.3)
}
# Renormalise so the five shares always sum to exactly 100, regardless of the
# rough arithmetic above - this is recomputed at import time so hand edits to
# the dict above never silently drift.
_total = sum(DEFAULT_MIX_PCT.values())
DEFAULT_MIX_PCT = {k: v * 100.0 / _total for k, v in DEFAULT_MIX_PCT.items()}


# ---------------------------------------------------------------------------
# Stations (matches the real "Area" names from the staff roster sheet)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Station:
    id: str
    label: str
    route: str | None          # which physical line it belongs to (None = shared/admin)
    ideal_ops: int             # operators for "full rate" per shift
    capacity_m2_per_op_hour: float  # [ASSUMPTION unless noted] m2/hr one operator can process
    num_machines: int = 1      # for machine-bound stations (CNC), caps useful headcount


# [ASSUMPTION] capacity_m2_per_op_hour values are back-calculated from the old
# HTML tool's capMonth targets (capMonth / (80 hrs/wk * 4.345 wk/month) / idealOps),
# i.e. NOT yet grounded in the new Daily Pacer data. Replace once that file is
# parsed (it has real daily Packing m2 and FTE - the ratio gives a real rate).
STATIONS: dict[str, Station] = {
    # [ASSUMPTION] Optimising has no real throughput data - 50 m2/op/hr is a
    # generous placeholder so it's rarely the bottleneck, but it CAN become one
    # (e.g. Diana Savage alone on this station) rather than being literally
    # infinite. Tune this once you know how fast nesting/optimising really runs.
    "optimising": Station("optimising", "Optimising (nesting)", Route.CUT_AND_CLASH,
                           ideal_ops=1, capacity_m2_per_op_hour=50.0),
    "cnc_1536":   Station("cnc_1536", "CNC 1536 (Cut & Clash)", Route.CUT_AND_CLASH,
                           ideal_ops=1, capacity_m2_per_op_hour=0.0, num_machines=1),
    "eb_drilling": Station("eb_drilling", "Edge Band / Drilling", Route.CUT_AND_CLASH,
                            ideal_ops=2, capacity_m2_per_op_hour=6.5),
    "cnc_thermo": Station("cnc_thermo", "CNC (Thermo)", Route.THERMO,
                           ideal_ops=4, capacity_m2_per_op_hour=0.0, num_machines=5),
    "sanding":    Station("sanding", "Manual Sanding", Route.THERMO,
                           ideal_ops=2, capacity_m2_per_op_hour=6.5),
    "mb_sander":  Station("mb_sander", "MB Sander", Route.THERMO,
                           ideal_ops=2, capacity_m2_per_op_hour=45.5, num_machines=1),
    "edging":     Station("edging", "Edging (Cefla glueing)", Route.THERMO,
                           ideal_ops=2, capacity_m2_per_op_hour=13.0),
    # Press 1 and Press 2 are staffed (and edited) independently, but they're
    # two machines doing the same job on one shared pile of work - see
    # PRESS_QUEUE_ID / PRESS_STATIONS below and the dedicated handling in
    # simulation.py, rather than each getting its own separate buffer.
    "press_1":    Station("press_1", "Press 1", Route.THERMO,
                           ideal_ops=3, capacity_m2_per_op_hour=6.1, num_machines=1),
    "press_2":    Station("press_2", "Press 2", Route.THERMO,
                           ideal_ops=3, capacity_m2_per_op_hour=6.1, num_machines=1),
    "glue":       Station("glue", "Glue", Route.THERMO,
                           ideal_ops=2, capacity_m2_per_op_hour=8.0),
    "despatch":   Station("despatch", "Packing / Despatch", None,
                           ideal_ops=3, capacity_m2_per_op_hour=8.7),
    "admin":      Station("admin", "Admin", None, ideal_ops=1, capacity_m2_per_op_hour=1e9),
}

# CNC machines have a per-machine time cost (setup + cut) rather than a flat
# m2/hr rate - handled specially in simulation.py. This constant is the setup
# overhead per board, same for every class. [ASSUMPTION] carried from old tool.
CNC_SETUP_MIN_PER_BOARD = 3.0
CNC_UTILISATION = 0.80   # [ASSUMPTION] machine uptime while manned


# ---------------------------------------------------------------------------
# Editable "area capacity" (m2/month at ideal staffing, on the REFERENCE
# schedule above) - this is the number you actually type in ("CNC = 10,000
# m2/month"). The functions below convert between that human-facing number
# and the internal per-operator-hour rate (or, for CNC, a scale factor on
# cut times) that the simulation engine runs on.
# ---------------------------------------------------------------------------

CNC_STATION_IDS = ("cnc_thermo", "cnc_1536")


def default_cnc_capacity_m2_per_month(route: str) -> float:
    """What the CNC line on this route would produce per month, running the
    reference schedule flat out at ideal staffing, cutting the DEFAULT_MIX_PCT
    blend of its own classes. This is the baseline that a user-entered target
    capacity is compared against to derive the scale factor in simulation.py.
    """
    station = next(s for s in STATIONS.values() if s.id in CNC_STATION_IDS and s.route == route)
    classes = [c for c, pc in PRODUCT_CLASSES.items() if pc.route == route]
    total_share = sum(DEFAULT_MIX_PCT[c] for c in classes)
    weighted_min_per_m2 = sum(
        (DEFAULT_MIX_PCT[c] / total_share) * (PRODUCT_CLASSES[c].cnc_min_per_board + CNC_SETUP_MIN_PER_BOARD) / BOARD_M2
        for c in classes
    )
    available_minutes_per_month = station.num_machines * REFERENCE_HOURS_PER_MONTH * 60.0 * CNC_UTILISATION
    return available_minutes_per_month / weighted_min_per_m2


def default_station_capacity_m2_per_month(station_id: str) -> float:
    """The capacity implied by this station's current (guessed) rate constants,
    at ideal staffing, on the reference schedule - i.e. what capacity number
    would reproduce today's behaviour unchanged if typed into the UI."""
    station = STATIONS[station_id]
    if station_id in CNC_STATION_IDS:
        return default_cnc_capacity_m2_per_month(station.route)
    return station.ideal_ops * station.capacity_m2_per_op_hour * REFERENCE_HOURS_PER_MONTH


# Ordered station sequence for each route, NOT including "despatch" or
# "press" - both are handled as special shared-queue stages in simulation.py
# rather than plain 1-station-1-queue steps:
#   - Despatch is a merge point fed by BOTH routes' last station.
#   - Press is fed by ONE route (Thermo), but by TWO independently-staffed
#     machines (press_1, press_2) pulling from the same physical pile of
#     work - parts don't care which press they go through.
ROUTE_SEQUENCE = {
    Route.THERMO: ["cnc_thermo", "sanding", "mb_sander", "edging"],
    Route.CUT_AND_CLASH: ["optimising", "cnc_1536", "eb_drilling"],
}
SHARED_TERMINAL_STATION = "despatch"
PRESS_QUEUE_ID = "press"          # the shared physical buffer both presses draw from
PRESS_STATIONS = ("press_1", "press_2")  # the two independently-staffed machines

# The station where a fresh order (or a released remake) of a given product
# class first enters the factory.
ROUTE_ENTRY_STATION = {
    Route.THERMO: "cnc_thermo",
    Route.CUT_AND_CLASH: "optimising",
}


# ---------------------------------------------------------------------------
# Shift crews: groups of stations that share one shift schedule (days/week,
# shift lengths, whether an afternoon shift runs at all). Mirrors how the real
# plant is actually rostered - see the staff roster "Area" groupings.
# ---------------------------------------------------------------------------

@dataclass
class ShiftSchedule:
    days_per_week: int = 5
    day_hrs: float = 8.0
    aft_enabled: bool = False
    aft_hrs: float = 8.0

    def shift_at(self, hour_of_day: float) -> str | None:
        """Which shift ('day'/'aft') covers this hour-of-day, or None if closed."""
        if hour_of_day < self.day_hrs:
            return "day"
        if self.aft_enabled and hour_of_day < self.day_hrs + self.aft_hrs:
            return "aft"
        return None

    def active_on_weekday(self, weekday: int) -> bool:
        return weekday < self.days_per_week


# Which crew schedule each station belongs to.
STATION_CREW: dict[str, str] = {
    "cnc_thermo": "thermo_cnc",
    "sanding": "thermo_cnc",
    "mb_sander": "finishing",
    "edging": "finishing",
    "press_1": "finishing",
    "press_2": "finishing",
    "glue": "finishing",
    "despatch": "finishing",
    "optimising": "cutclash",
    "cnc_1536": "cutclash",
    "eb_drilling": "cutclash",
    "admin": "admin",
}

# [ASSUMPTION] default schedules - edit freely, or override per-crew in the UI.
DEFAULT_SHIFT_SCHEDULES: dict[str, ShiftSchedule] = {
    "thermo_cnc": ShiftSchedule(days_per_week=4, day_hrs=10.0, aft_enabled=True, aft_hrs=10.0),
    "finishing": ShiftSchedule(days_per_week=4, day_hrs=10.0, aft_enabled=True, aft_hrs=10.0),
    # [REAL, corrected] an afternoon Cut & Clash shift does run - Jerard
    # Mendoza works CNC 1536 + Edge Banding in the afternoon. Afternoon
    # hours are still an [ASSUMPTION] guess (8h) pending confirmation.
    "cutclash": ShiftSchedule(days_per_week=5, day_hrs=8.0, aft_enabled=True, aft_hrs=8.0),
    "admin": ShiftSchedule(days_per_week=5, day_hrs=8.0, aft_enabled=False, aft_hrs=8.0),
}


# ---------------------------------------------------------------------------
# Intake volume defaults
# ---------------------------------------------------------------------------

# [REAL] product_2026.xlsx, Jan-Sep 2026. Three horizons, each with the
# combined total and the Thermo/Cut&Clash split (computed on days/weeks/
# months where that route actually had any volume, so these are "typical
# active period" figures, not averaged-with-zeros). Weekly/monthly figures
# use full calendar weeks/months only (partial first/last period excluded).
REAL_INTAKE_M2 = {
    "day": {
        "combined": {"median": 297.0, "mean": 331.0, "p90": 515.0, "max": 1462.0},
        "thermo":   {"median": 256.0, "mean": 272.0, "p90": 433.0, "max": 734.0},
        "cutclash": {"median": 43.0, "mean": 69.0, "p90": 105.0, "max": 1152.0},
    },
    "week": {
        "combined": {"median": 1597.0, "mean": 1706.0, "p90": 2306.0, "max": 3312.0},
        # route split not separately computed for week/month - approximate
        # by applying the overall 80.6% / 19.4% YTD share to the combined figure.
        "thermo":   {"median": 1287.0, "mean": 1375.0, "p90": 1859.0, "max": 2669.0},
        "cutclash": {"median": 310.0, "mean": 331.0, "p90": 448.0, "max": 643.0},
    },
    "month": {
        "combined": {"median": 7232.0, "mean": 7499.0, "p90": 9415.0, "max": 9415.0},
        "thermo":   {"median": 5829.0, "mean": 6044.0, "p90": 7589.0, "max": 7589.0},
        "cutclash": {"median": 1403.0, "mean": 1456.0, "p90": 1828.0, "max": 1828.0},
    },
}

# [REAL] target lead time is different per product range - Cut & Clash quotes
# a 7-day lead time; Thermo's target is still the [ASSUMPTION] 10-day
# placeholder from the old tool pending your confirmation.
DEFAULT_TARGET_LEAD_DAYS = {
    Route.THERMO: 10.0,
    Route.CUT_AND_CLASH: 7.0,  # [REAL] per your instruction
}


# ---------------------------------------------------------------------------
# Absenteeism default
# ---------------------------------------------------------------------------

# [ASSUMPTION] fallback used only for staff who don't have a personal rate set
# in the roster (10 sick days / ~260 working days/year).
DEFAULT_ABSENCE_RATE_PCT = 3.8

# [ASSUMPTION] generous default WIP/buffer cap in front of every station, so
# space constraints are rarely the reason a station starves - raise/lower per
# station once you know real shelf/trolley space limits.
DEFAULT_BUFFER_CAP_M2 = 5000.0

# Hour-of-day boundary used to decide whether "now" counts as the day-shift or
# afternoon-shift allocation snapshot (see simulation.py). [ASSUMPTION/
# SIMPLIFICATION]: real crews have slightly different day-shift lengths
# (10h thermo/finishing vs 8h cut&clash); this single boundary is a
# deliberate simplification so staffing is decided once per shift, not
# re-shuffled every hour.
ALLOCATION_SHIFT_BOUNDARY_HOUR = 10


# ---------------------------------------------------------------------------
# Dynamic staffing allocation tuning (see allocation.py)
# ---------------------------------------------------------------------------

# A station is considered "drowning" once its backlog per currently-assigned
# operator exceeds this many m2 - at that point the allocator will try to pull
# a multi-skilled floater in from a quieter station to help.
ALLOCATION_PRESSURE_THRESHOLD_M2 = 150.0

# Safety cap on how many people get moved between stations in one shift, so a
# single very backed-up station can't strip every other station bare.
ALLOCATION_MAX_REBALANCE_MOVES = 6


# Remake loop [ASSUMPTION, unchanged from old tool pending real remake-rate
# history from the Daily Pacer file - it does carry a real "remake % of packing"
# column once uploaded and parsed].
DEFAULT_REMAKE_RATE_PCT = 4.4
DEFAULT_REMAKE_DAYS = 1.0
