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
from dataclasses import dataclass


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
#
# Route shares first, then each class's share WITHIN its route, so the
# arithmetic is checkable by eye (an earlier version had the S1/S3 numbers
# missing the x0.806 step, which quietly gave Thermo 83.8% of intake).
_THERMO_SHARE_PCT = 80.6        # [REAL] product_2026.xlsx
_CUTCLASH_SHARE_PCT = 19.4      # [REAL] product_2026.xlsx
_WITHIN_THERMO = {"S1": 58.8, "S2": 0.14, "S3": 34.9}          # [ASSUMPTION] old tool
_WITHIN_CUTCLASH = {"Acrylic": 616.9, "Melamine": 10777.4}     # [REAL] m2, product_2026.xlsx


def _split(total_pct: float, weights: dict[str, float]) -> dict[str, float]:
    w = sum(weights.values())
    return {k: total_pct * v / w for k, v in weights.items()}


DEFAULT_MIX_PCT: dict[str, float] = {
    **_split(_THERMO_SHARE_PCT, _WITHIN_THERMO),
    **_split(_CUTCLASH_SHARE_PCT, _WITHIN_CUTCLASH),
}
# Renormalise so the five shares always sum to exactly 100, regardless of the
# arithmetic above - recomputed at import time so hand edits never drift.
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
    # True for stations whose throughput is set by a machine's cycle, not by
    # how many hands are on it: putting MORE than ideal_ops people on it adds
    # nothing (the machine can't go faster), so capacity caps at ideal_ops.
    # False for manual stations (sanding, packing, drilling...) where an
    # extra pair of hands genuinely adds an extra pair of hands' worth.
    machine_bound: bool = False
    # Named machines inside the station, for the floor map and the roster's
    # optional "machine" column (which machine a person normally runs).
    # Purely descriptive - capacity still comes from num_machines / rates.
    machines: tuple[str, ...] = ()

    @property
    def max_useful_ops(self) -> int | None:
        """Most people who can usefully work here at once (None = no cap).
        Used by the allocator so it never parks a floater somewhere they'd
        just stand around."""
        if self.id in CNC_STATION_IDS:
            return self.num_machines
        if self.machine_bound:
            return self.ideal_ops
        return None


# capacity_m2_per_op_hour: optimising/sanding/mb_sander/press_1/press_2/despatch
# are now [REAL], calibrated from a 3-month WorkArea scan-checkpoint export
# (90th-percentile daily total per WorkArea, extrapolated to a month, divided
# by that station's real current roster op-hours/month) - see the per-station
# comments below for each one's derivation. cnc_thermo/cnc_1536/edge_bander/drilling
# remain [ASSUMPTION] (the CNC stations use a different, machine-minute-based
# model - see CNC_SETUP_MIN_PER_BOARD below; edge_bander/drilling have no matching real
# WorkArea checkpoint at all in the export).
STATIONS: dict[str, Station] = {
    # [REAL, from 3-month WorkArea scan-checkpoint data, see chat] rate is the
    # 90th-percentile daily "Optimisation" scan total, extrapolated to a month
    # and divided by Diana Savage's real hours (174/month, the only confirmed
    # Optimising operator). Remarkably close to the old 50.0 guess. NOTE: the
    # real Optimisation volume is nearly as large as TOTAL order intake
    # (Thermo + Cut & Clash combined), not just Cut & Clash's ~19% share -
    # unconfirmed whether Optimising actually processes some Thermo work too
    # (see chat) - if so, ROUTE_SEQUENCE needs Thermo to pass through it as
    # well, which would change this rate's true per-class attribution.
    "optimising": Station("optimising", "Optimising (nesting)", Route.CUT_AND_CLASH,
                           ideal_ops=1, capacity_m2_per_op_hour=57.164),
    "cnc_1536":   Station("cnc_1536", "CNC B1536 (Cut & Clash)", Route.CUT_AND_CLASH,
                           ideal_ops=1, capacity_m2_per_op_hour=0.0, num_machines=1),
    # [ASSUMPTION] Edge banding and drilling are two separate machines on the
    # real floor (Ali / Eric, both days only), run one after the other. No
    # real checkpoint data for either: the old combined station did 6.5
    # m2/op-hr with a 2-person crew, i.e. 13 m2/hr of edge-banded-AND-drilled
    # work, so each step gets 13 m2/op-hr at ideal_ops=1 to keep that same
    # overall throughput at the real one-person staffing. Replace with real
    # cycle times when the WorkArea export gains these checkpoints.
    "edge_bander": Station("edge_bander", "Edge bander", Route.CUT_AND_CLASH,
                            ideal_ops=1, capacity_m2_per_op_hour=13.0, num_machines=1, machine_bound=True),
    "drilling":   Station("drilling", "Drilling", Route.CUT_AND_CLASH,
                           ideal_ops=1, capacity_m2_per_op_hour=13.0, num_machines=1, machine_bound=True),
    # [REAL, shop-floor sheet Sep 2026] four Thermo CNCs - Weeke (old), B1224,
    # Weeke (new), C6 - each with one operator per shift (the new Weeke is
    # covered by a Press operator on both shifts). ideal_ops ==
    # num_machines on purpose: "ideal staffing" for a bank of CNCs means one
    # operator per machine. Extra people beyond the machine count add
    # nothing (see Station.max_useful_ops).
    "cnc_thermo": Station("cnc_thermo", "CNC (Thermo)", Route.THERMO,
                           ideal_ops=4, capacity_m2_per_op_hour=0.0, num_machines=4,
                           machines=("Weeke (old)", "B1224", "Weeke (new)", "C6")),
    # [REAL, Thermo capacity overview + plant] Manual sanding has no machine
    # limit (people sand profiles in parallel). The capacity overview lists
    # "Manual Profile Sanding" at 4,500 m2/month, but per the plant, sanding
    # done right behind the CNCs with the MB Sander running non-stop keeps
    # pace with the CNCs - so manual sanding is given the CNC line's 10,000
    # m2/month at its 2-person crew. Rate = 10000 / (2 * REFERENCE_HOURS_PER_MONTH).
    # (History: the scan checkpoint's ~8,007 m2/month was used before that;
    # an earlier 6.5 m2/op-hr placeholder made sanding the worst bottleneck.)
    "sanding":    Station("sanding", "Manual Sanding", Route.THERMO,
                           ideal_ops=2, capacity_m2_per_op_hour=14.384349827387803),
    # [REAL, Thermo capacity overview] MB Sander = 63,000 m2/month on the 5-day
    # x 16 h basis - the machine is far faster than anything feeding it, so
    # it is never the constraint. Run by ONE person per shift (shop-floor
    # sheet: Quyen days, Viet afternoons): rate = 63000 / (1 *
    # REFERENCE_HOURS_PER_MONTH). machine_bound: a second person can't make
    # the one machine run faster. (The scan checkpoint's ~8,007 m2/month used
    # earlier was the volume passing through it, not its capability.)
    "mb_sander":  Station("mb_sander", "MB Sander", Route.THERMO,
                           ideal_ops=1, capacity_m2_per_op_hour=181.24280782508632, num_machines=1,
                           machine_bound=True),
    # [REAL] "Edging" and "Glue" were originally modelled as two separate
    # stations, but there is only one real physical station here: the Cefla
    # automated gluing line, confirmed capacity 9,000 m2/month. Rate below is
    # back-solved from that figure at ideal_ops=2 on the reference schedule
    # (9000 / (2 * REFERENCE_HOURS_PER_MONTH)).
    # machine_bound: it's an automated line - the confirmed 9,000 m2/month is
    # the line's ceiling at its normal 2-person crew, not "per extra person".
    "edging":     Station("edging", "Cefla Automated Gluing Line", Route.THERMO,
                           ideal_ops=2, capacity_m2_per_op_hour=12.945914844649023,
                           machine_bound=True),
    # Press 1 and Press 2 are staffed (and edited) independently, but they're
    # two machines doing the same job on one shared pile of work - see
    # PRESS_QUEUE_ID / PRESS_STATIONS below and the dedicated handling in
    # simulation.py, rather than each getting its own separate buffer.
    # [REAL, per the plant] 9,500 m2/month per press at its 3-person crew on
    # the 5-day x 16 h basis (the capacity overview's 21,000 is theoretical).
    # Rate = 9500 / (3 * REFERENCE_HOURS_PER_MONTH). Kept per-person rather
    # than machine_bound: with fewer than 3 the press runs slower, with more
    # it doesn't run faster - see Station.machine_bound. (The scan-derived
    # 5.37 m2/op-hr used earlier was throughput at real staffing, not capacity.)
    "press_1":    Station("press_1", "Press 1", Route.THERMO,
                           ideal_ops=3, capacity_m2_per_op_hour=9.110088224012275, num_machines=1,
                           machine_bound=True),
    "press_2":    Station("press_2", "Press 2", Route.THERMO,
                           ideal_ops=3, capacity_m2_per_op_hour=9.110088224012275, num_machines=1,
                           machine_bound=True),
    # [REAL, from 3-month WorkArea scan-checkpoint data] rate = average of the
    # 90th-percentile daily "Packing" and "Despatch" scan totals (two separate
    # real checkpoints for what this model treats as one combined stage),
    # extrapolated to a month, divided by the despatch team's real op-hours
    # (1,390/month, 8 people). Close to the old 8.7 guess.
    # Packing is people-driven (NOT machine_bound): capacity = rate x whoever
    # is actually there, so absenteeism and cover moves flow straight into it.
    "despatch":   Station("despatch", "Packing / Despatch", None,
                           ideal_ops=3, capacity_m2_per_op_hour=9.124),
    # [REAL, shop-floor sheet Sep 2026] Hafele and DIY sit under Dispatch on
    # the org sheet, but they are box-packed product lines with their OWN
    # assigned packers. They are NOT part of the Thermo / Cut & Clash m2 flow
    # (nothing in this model feeds them), so their people don't count toward
    # general packing capacity - but they carry "despatch" as a skill in the
    # roster, so the allocator can pull them across when packing is drowning.
    "hafele":     Station("hafele", "Hafele box packing", None, ideal_ops=2, capacity_m2_per_op_hour=0.0),
    "diy":        Station("diy", "DIY box packing", None, ideal_ops=1, capacity_m2_per_op_hour=0.0),
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


# [REAL, Thermo capacity overview] the four Thermo CNCs together do 10,000
# m2/month on the 5-day x 16 h basis. The per-series cut times above are
# still old-tool placeholders, so this figure is used as the CNC line's
# default capacity and the cut times are scaled to hit it (their S1/S2/S3
# ratios are kept). Cut & Clash's B1536 has no such figure yet.
CNC_CAPACITY_M2_PER_MONTH_OVERRIDE: dict[str, float] = {
    Route.THERMO: 10000.0,
}


def cnc_capacity_from_cut_times(route: str) -> float:
    """What the CNC line on this route would produce per month, running the
    reference schedule flat out at ideal staffing, cutting the DEFAULT_MIX_PCT
    blend of its own classes AT THE PLACEHOLDER CUT TIMES. This is the
    baseline a target capacity is compared against to derive the cut-time
    scale factor in simulation.py.
    """
    station = next(s for s in STATIONS.values() if s.id in CNC_STATION_IDS and s.route == route)
    classes = [c for c, pc in PRODUCT_CLASSES.items() if pc.route == route]
    total_share = sum(DEFAULT_MIX_PCT[c] for c in classes)
    if total_share <= 0:
        # No default volume on this route at all - fall back to an unweighted
        # average cut time rather than dividing by zero.
        weights = {c: 1.0 / len(classes) for c in classes}
    else:
        weights = {c: DEFAULT_MIX_PCT[c] / total_share for c in classes}
    weighted_min_per_m2 = sum(
        weights[c] * cnc_min_per_m2(c) for c in classes
    )
    # "Ideal staffing" can never man more machines than exist (or more than
    # the ideal crew size) - whichever is smaller is the number that runs.
    machines_at_ideal = min(station.ideal_ops, station.num_machines)
    available_minutes_per_month = machines_at_ideal * REFERENCE_HOURS_PER_MONTH * 60.0 * CNC_UTILISATION
    return available_minutes_per_month / weighted_min_per_m2 if weighted_min_per_m2 > 0 else 0.0


def cnc_min_per_m2(product_class: str) -> float:
    """Machine-minutes to cut one m2 of this class (setup + cut, per board,
    spread over the board's area) at the default cut times."""
    pc = PRODUCT_CLASSES[product_class]
    return (pc.cnc_min_per_board + CNC_SETUP_MIN_PER_BOARD) / BOARD_M2


def default_cnc_capacity_m2_per_month(route: str) -> float:
    """The CNC line's default capacity: the real figure if we have one, else
    what the placeholder cut times imply."""
    return CNC_CAPACITY_M2_PER_MONTH_OVERRIDE.get(route, cnc_capacity_from_cut_times(route))


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
    Route.CUT_AND_CLASH: ["optimising", "cnc_1536", "edge_bander", "drilling"],
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


def queue_id_for(station_id: str) -> str:
    """Which physical pile of work a station pulls from. Every station has
    its own queue except the two presses, which share one (PRESS_QUEUE_ID).
    Used by both the engine and the allocator so "how much is waiting in
    front of Press 2?" always means the shared press pile, not zero."""
    return PRESS_QUEUE_ID if station_id in PRESS_STATIONS else station_id


# Which queue each station FEEDS (its downstream). Derived from the route
# sequences above; the two presses and Cut & Clash's last station all feed
# the shared despatch queue, and despatch feeds nothing (it's the exit).
def downstream_queue_for(station_id: str) -> str | None:
    if station_id == SHARED_TERMINAL_STATION or station_id == "admin":
        return None
    if station_id in PRESS_STATIONS:
        return SHARED_TERMINAL_STATION
    for route, seq in ROUTE_SEQUENCE.items():
        if station_id in seq:
            i = seq.index(station_id)
            if i + 1 < len(seq):
                return seq[i + 1]
            return PRESS_QUEUE_ID if route == Route.THERMO else SHARED_TERMINAL_STATION
    raise KeyError(station_id)


# The order stations are processed within one simulated hour: DOWNSTREAM
# FIRST. Each station pulls from whatever was in its queue at the START of
# the hour, so a part needs at least one hour per station to travel the
# line - the same way the real floor works. (Upstream-first would let a
# freshly-cut board be sanded, sanded again, glued, pressed and packed all
# within the same hour, which quietly shortens every lead time.)
PROCESSING_ORDER: list[str] = (
    [SHARED_TERMINAL_STATION]
    + list(PRESS_STATIONS)
    + list(reversed(ROUTE_SEQUENCE[Route.THERMO]))
    + list(reversed(ROUTE_SEQUENCE[Route.CUT_AND_CLASH]))
)

# Stations that m2 actually flows through. Everything else (admin, the
# Hafele/DIY box-packing lines) is staffed and shown on the floor but has no
# queue to work, no capacity number to edit and no utilisation to report.
FLOW_STATIONS: tuple[str, ...] = tuple(PROCESSING_ORDER)


def is_flow_station(station_id: str) -> bool:
    return station_id in FLOW_STATIONS


# ---------------------------------------------------------------------------
# Shift crews: groups of stations that share one shift schedule (days/week,
# shift lengths, whether an afternoon shift runs at all). Mirrors how the real
# plant is actually rostered - see the staff roster "Area" groupings.
# ---------------------------------------------------------------------------

SHIFT_LABELS = ("day", "aft")

# How far a crew's day may be shifted from the plant's 6am reference start.
MIN_CREW_START_HOUR = -6.0    # 6 hours early = midnight
MAX_CREW_START_HOUR = 12.0


@dataclass
class ShiftSchedule:
    days_per_week: int = 5
    day_hrs: float = 8.0
    aft_enabled: bool = False
    aft_hrs: float = 8.0
    # When this crew's day shift starts, in hours relative to the plant's
    # reference day start (hour 0 = 6am). -2 = the crew starts at 4am, so
    # its whole day (day shift, then afternoon shift) runs two hours ahead
    # of everyone else's - the way you'd start the CNCs early to have WIP in
    # front of the sanders and the Cefla by the time those crews arrive.
    start_hour: float = 0.0

    def __post_init__(self):
        # Fail loudly on nonsense (a negative shift length, 9 days a week)
        # rather than quietly simulating something impossible.
        if not (0 <= int(self.days_per_week) <= 7):
            raise ValueError(f"days_per_week must be 0-7, got {self.days_per_week}")
        if self.day_hrs < 0 or self.aft_hrs < 0:
            raise ValueError("shift lengths cannot be negative")
        if self.day_hrs + (self.aft_hrs if self.aft_enabled else 0.0) > 24.0 + 1e-9:
            raise ValueError("day + afternoon shift cannot exceed 24 hours")
        if not (MIN_CREW_START_HOUR <= self.start_hour <= MAX_CREW_START_HOUR):
            raise ValueError(f"start_hour must be between {MIN_CREW_START_HOUR:g} and {MAX_CREW_START_HOUR:g} "
                             f"(hours relative to the 6am day start), got {self.start_hour}")
        self.days_per_week = int(self.days_per_week)

    def local_clock(self, h: float) -> tuple[int, float] | None:
        """This crew's own (day index, hour-of-day) at absolute simulation
        hour h, or None before the crew's first day has begun. A crew that
        starts at -2 is at ITS hour 0 when the plant clock reads -2, i.e.
        22:00 of the previous plant day."""
        t = h - self.start_hour
        if t < 0:
            return None
        d = int(t // 24)
        return d, t - d * 24

    def shift_at(self, hour_of_day: float) -> str | None:
        """Which shift ('day'/'aft') covers this hour-of-day, or None if closed.
        Hour 0 of the simulated day is the START of the day shift (think
        6am), not midnight - the afternoon shift follows straight on."""
        if hour_of_day < self.day_hrs:
            return "day"
        if self.aft_enabled and hour_of_day < self.day_hrs + self.aft_hrs:
            return "aft"
        return None

    def offers_shift(self, shift_label: str) -> bool:
        """Does this crew run the given shift at all (on its working days)?"""
        if shift_label == "day":
            return self.day_hrs > 0
        return self.aft_enabled and self.aft_hrs > 0

    def shift_start_hour(self, shift_label: str) -> float:
        return 0.0 if shift_label == "day" else self.day_hrs

    def active_on_weekday(self, weekday: int) -> bool:
        """weekday 0 = Monday ... 6 = Sunday; a crew on N days/week works
        the first N weekdays."""
        return weekday < self.days_per_week


# Which crew schedule each station belongs to. One crew per area of the
# floor, so each can be given its own days, hours and start time - e.g. run
# the CNCs from 4am so the sanders and the Cefla find WIP waiting at 6am.
STATION_CREW: dict[str, str] = {
    "cnc_thermo": "cnc",
    "sanding": "sanding",
    "mb_sander": "sanding",
    "edging": "cefla",
    "press_1": "press",
    "press_2": "press",
    "despatch": "packing",
    "hafele": "packing",
    "diy": "packing",
    "optimising": "cutclash",
    "cnc_1536": "cutclash",
    "edge_bander": "cutclash",
    "drilling": "cutclash",
    "admin": "admin",
}

CREW_LABELS: dict[str, str] = {
    "cnc": "CNC crew (Thermo CNCs)",
    "sanding": "Sanding crew (manual sanding + MB Sander)",
    "cefla": "Cefla crew (gluing line)",
    "press": "Press crew (Press 1 & 2)",
    "packing": "Packing / Despatch crew (incl. Hafele & DIY)",
    "cutclash": "Cut & Clash crew (Optimising, 1536, edge bander, drilling)",
    "admin": "Admin",
}

# [ASSUMPTION] default schedules - edit freely, or override per-crew in the UI.
# All Thermo crews currently share the same 4 x (10 h + 10 h) pattern and
# the 6am start (start_hour 0); the split is so each can be changed alone.
_THERMO_DEFAULT = dict(days_per_week=4, day_hrs=10.0, aft_enabled=True, aft_hrs=10.0)
DEFAULT_SHIFT_SCHEDULES: dict[str, ShiftSchedule] = {
    "cnc": ShiftSchedule(**_THERMO_DEFAULT),
    "sanding": ShiftSchedule(**_THERMO_DEFAULT),
    "cefla": ShiftSchedule(**_THERMO_DEFAULT),
    "press": ShiftSchedule(**_THERMO_DEFAULT),
    "packing": ShiftSchedule(**_THERMO_DEFAULT),
    # [REAL, corrected] an afternoon Cut & Clash shift does run - Jerard
    # Mendoza works CNC 1536 + Edge Banding in the afternoon. Afternoon
    # hours are still an [ASSUMPTION] guess (8h) pending confirmation.
    "cutclash": ShiftSchedule(days_per_week=5, day_hrs=8.0, aft_enabled=True, aft_hrs=8.0),
    "admin": ShiftSchedule(days_per_week=5, day_hrs=8.0, aft_enabled=False, aft_hrs=8.0),
}


# ---------------------------------------------------------------------------
# Intake volume defaults
# ---------------------------------------------------------------------------

# WHEN orders arrive. Orders are placed by the office on working days, not
# at 3am on a Sunday - so the horizon's intake total is spread evenly over
# the intake window (hours of the simulated day, relative to day-shift
# start) on weekdays only. Spreading it over all 168 hours of the week
# (which the engine used to do) put ~29% of every week's orders on the
# weekend, where they sat and aged before anyone could touch them, which
# quietly inflated Cut & Clash's calendar-day lead time in particular.
# [REAL] Online orders are accepted until 4pm; anything placed later counts
# as the next day's. Hour 0 is the 6am day-shift start, so the cut-off is
# hour 10, and the day's orders are spread evenly over hours 0-10.
ONLINE_ORDER_CUTOFF_HOUR = 10.0
INTAKE_WEEKDAYS_ONLY = True
INTAKE_WINDOW_HOURS = (0.0, ONLINE_ORDER_CUTOFF_HOUR)   # [start, end) hour-of-day

# ---------------------------------------------------------------------------
# Order scheduling: Optimising's morning release onto the CNCs
# ---------------------------------------------------------------------------
# [REAL] Orders don't go straight from the web shop to a CNC. Every morning
# Optimising (Diana) processes the afternoon shift's remakes, the new orders
# and everything still pending scheduling, and schedules them onto the
# CNCs. Until then an order sits in the "pending scheduling" pool - it is
# in the lead-time clock but not on the floor.
#
# How far ahead she schedules is read off the real CNC schedule (Sep 2026
# sample): orders dated Wed 16 Sep were cut on Mon 21 Sep, orders dated
# Thu 17 Sep on Tue 22 Sep - both released on the 3rd working day after
# the order date. That 3-day planning lag was previously hidden inside a
# 1.5-day "pre-production" guess; it is now simulated, so pre_prod_days
# defaults to 0.
#   [REAL]       Thermo: 3 working days
#   [ASSUMPTION] Cut & Clash: next working morning (Optimising nests it
#                herself, so it is assumed to go on the 1536 the next day)
SCHEDULING_STATION = "optimising"
SCHEDULING_RELEASE_HOUR = 0          # start of the day shift = "in the morning"
DEFAULT_RELEASE_WORKING_DAYS = {Route.THERMO: 3, Route.CUT_AND_CLASH: 1}
# Remakes found while Optimising is staffed are scheduled straight away;
# remakes from the afternoon shift (or a weekend) wait for the next morning.

# How long each horizon runs. "month" is a flat 30 days (not 4.345 weeks)
# so it always ends on a day boundary and contains a whole number of intake
# days - the entered monthly intake is then spread over exactly the intake
# days inside the window, so the "Intake" KPI always equals what you typed.
HORIZON_DAYS = {"day": 1, "week": 7, "month": 30}

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
        # [REAL, updated again] 13-month history Jun-25 through Jun-26 from
        # your monthly production tracker - the most complete series yet
        # (supersedes both the product_2026.xlsx snapshot and the partial
        # Jan-Aug 2026 lead-time-dashboard figures used previously).
        # Total m2 by month: 6618, 9103, 7300, 7747, 8763, 8403, 6702, 5442,
        # 6706, 8409, 7702, 7049, 6373.
        "combined": {"median": 7300.0, "mean": 7409.0, "p90": 8763.0, "max": 9103.0},
        # Thermo m2 by month: 5333, 8236, 6638, 6549, 7849, 7503, 6280, 4870,
        # 6005, 6671, 6841, 5237, 5518.
        "thermo":   {"median": 6549.0, "mean": 6425.0, "p90": 7849.0, "max": 8236.0},
        # Cut & Clash = Total - Thermo per month (not given directly).
        "cutclash": {"median": 867.0, "mean": 984.0, "p90": 1738.0, "max": 1812.0},
    },
}

# [REAL] Reference-only historical benchmarks from the same 13-month tracker
# (Jun-25 through Jun-26) - not wired into the simulation directly (the exact
# scope of "Production Hours" - which stations/people it counts, and whether
# it's labour-hours or something else - hasn't been confirmed), but useful
# for sanity-checking simulated results against real plant performance:
#   - Real DIFOT ranged 55%-97% month to month, averaging ~82% - notably
#     higher than the ~75% figure from the shorter partial-year lead-time
#     dashboard, and a reminder that DIFOT swings hard month to month even
#     in the real plant (this isn't a single fixed number to hit).
#   - Real "Actual Production Hours" ranged 5,303-9,677/month (mean 7,745).
#   - Real throughput averaged ~0.97 m2 of Total output per actual production
#     hour (range roughly 0.84-1.08) - a whole-plant aggregate figure, not
#     broken down by station.
REAL_DIFOT_HISTORY_PCT = {"min": 55.0, "max": 97.0, "mean": 81.7}
REAL_PRODUCTION_HOURS_PER_MONTH = {"min": 5303.0, "max": 9677.0, "mean": 7745.0}
REAL_M2_PER_PRODUCTION_HOUR = 0.965

# [REAL] Thermo average lead time from the live lead-time dashboard (YTD to
# 11 Sep 2026): 8.64 working days for non-remakes, 8.97 for remakes - i.e.
# ~8.66 overall. Cut & Clash has no equivalent dashboard figure yet. These
# feed the "reality check" panel in the UI, which compares a simulated
# month against them so a miscalibration is visible instead of silent.
REAL_AVG_LEAD_DAYS = {
    Route.THERMO: 8.66,
    Route.CUT_AND_CLASH: None,
}
REAL_REMAKE_LEAD_PENALTY_DAYS = 0.32   # 8.97 - 8.64, working days

# [REAL] target lead time is different per product range - Cut & Clash quotes
# a 7-day lead time. Thermo's 10-day target is now [REAL, confirmed]: your
# live lead-time dashboard states it explicitly ("Promise: 10 working days")
# and measures DIFOT against it in WORKING days (Mon-Fri, weekends excluded) -
# NOT calendar days. The simulator's lead-time/DIFOT calculations now use the
# same working-day measure (see Simulator._working_days_elapsed in
# simulation.py) so this number means the same thing your dashboard means by
# it - a calendar-day target here would silently judge Thermo ~40% more
# harshly (10 working days spans ~14 calendar days across two weekends).
DEFAULT_TARGET_LEAD_DAYS = {
    Route.THERMO: 10.0,
    Route.CUT_AND_CLASH: 7.0,  # [REAL] 7 WORKING days (confirmed 21 Sep 2026)
}

# Whether lead-time/DIFOT day-counts exclude weekends for a given route (see
# Simulator._working_days_elapsed in simulation.py). [REAL] Thermo's dashboard
# explicitly measures "working time... weekends excluded"; [REAL] Cut & Clash's
# 7-day promise is working days too (confirmed 21 Sep 2026 - it was being
# judged in calendar days, which made a 9-calendar-day lead read as 0% DIFOT
# against a 7-day target when it was really ~6.5 working days).
LEAD_TIME_EXCLUDES_WEEKENDS = {
    Route.THERMO: True,
    Route.CUT_AND_CLASH: True,
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

# Real WIP space limits, per queue (station id, or "press" for the shared
# press pile), in m2. A station whose downstream buffer is full is BLOCKED
# and stops producing - the constraints analysis reports those hours.
# Empty = every buffer uses DEFAULT_BUFFER_CAP_M2 (effectively unlimited).
# Fill in real rack / trolley limits here as they become known, e.g.
#   BUFFER_CAP_M2_BY_STATION = {"mb_sander": 120.0, "press": 200.0}
BUFFER_CAP_M2_BY_STATION: dict[str, float] = {}

# [REAL, per the plant] PROTECTIVE buffers: the floor deliberately keeps WIP
# in front of the Cefla (the production bottleneck after manual sanding)
# and in front of the presses at all times - the Cefla is run to build a
# pile the presses can then "smash out". These are the target levels, in
# m2, the constraints analysis checks each hour the receiving station is
# running. [ASSUMPTION] the amounts: ~a shift of Cefla work in front of the
# Cefla; in front of the presses "never empty" = the level the press batch
# run stops at (below). Edit in the sidebar.
BUFFER_TARGET_M2_BY_QUEUE: dict[str, float] = {"edging": 250.0, PRESS_QUEUE_ID: 50.0}

# [REAL, per the plant] the presses run in BATCHES: they wait for the pile
# in front of them to build, then run until it is nearly empty. Start = pile
# size that triggers a run, stop = level at which the run ends (0 start =
# presses run continuously). [ASSUMPTION] the two numbers.
DEFAULT_PRESS_BATCH_START_M2 = 300.0
DEFAULT_PRESS_BATCH_STOP_M2 = 50.0     # stop with a little left so the pile is never empty

# [SIMULATION METHODOLOGY, not a plant fact] Every run starts with every
# queue completely empty - the real factory never does; it always has
# work-in-progress already sitting at every station. Without a warm-up, the
# first ~1.5 lead-times' worth of simulated hours show artificially low
# completion% and DIFOT simply because nothing has had time to travel all
# the way through the pipeline yet, even if long-run capacity is perfectly
# fine. To avoid that cold-start bias polluting the reported KPIs, the
# simulator actually runs for (this many days + the requested horizon),
# and only starts counting intake/completions/DIFOT/attendance once this
# warm-up period has elapsed - by which point queues hold a realistic
# steady-state level of WIP, same as walking onto the real factory floor
# on any ordinary day. 21 days covers Thermo's longest real lead time
# (10 working days = up to ~14 calendar days) plus pre/post-production
# buffer with room to spare. Raise this if a route's target lead time is
# ever set higher than ~15 days.
SIMULATION_WARMUP_DAYS = 21

# Staffing is decided once per shift, not re-shuffled every hour: each
# calendar day gets one "day" allocation snapshot and one "aft" snapshot.
# Which snapshot a station reads at a given hour is decided by ITS OWN
# crew's schedule (an 8-hour Cut & Clash day shift hands over to its
# afternoon crew at hour 8 while the 10-hour Thermo crews are still on
# days) - there is no single plant-wide handover hour.


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

# When someone is away, which stations get covered first. [REAL, per the
# plant] the CNCs, the Cefla line, the presses and manual sanding are the
# ones a supervisor fills first; the rest follow in this order. A station
# on this list is never robbed below its ideal crew to cover another; a
# station NOT on it can lend a person even if that leaves it short (but
# never empty).
ALLOCATION_COVER_PRIORITY = ("cnc_thermo", "edging", "press_1", "press_2", "sanding",
                             "mb_sander", "cnc_1536", "edge_bander", "drilling", "optimising")


# Which Thermo CNC is the "special" machine. [REAL, per the plant] C6 is
# mostly used for special orders and takes the majority of remakes, so the
# engine works it as its own lane: specials + remakes are cut on C6 first,
# the other machines cut regular work first, and each only takes the other
# kind of work when its own lane runs dry. Whether C6 is manned at all comes
# from the roster's "machine" column (see Simulator._cnc_thermo_lanes).
CNC_THERMO_SPECIAL_MACHINE = "C6"

# [REAL, per the plant] one CNC operator often runs two CNCs at once,
# especially on Series 3 where the machine cuts unattended for a long time.
# A person's own machine runs at full rate; their SECOND machine (roster
# column "machine_2", or Ctrl-drag on the floor) runs at this fraction of a
# fully-tended machine while they split their attention. [ASSUMPTION] 0.8 -
# close to 1 for long Series 3 cuts, lower for quick Series 1 boards.
CNC_SECOND_MACHINE_FACTOR = 0.8

# Anyone can be split across TWO stations (roster "station_2", or Ctrl-drag
# on the floor). Their time goes WHERE THE WORK IS, hour by hour: once the
# second station has at least this many hours of its own work waiting, the
# person gives it a share of their hour in proportion to the two piles
# (second / (home + second)); below that they stay home. A big pile at the
# second station and nothing at home puts them there almost full time. The
# one exception is a CNC operator running a second Thermo CNC: the machine
# cuts unattended, so they stay a full person on their own machine and the
# second runs at CNC_SECOND_MACHINE_FACTOR (above). [ASSUMPTION] the 1 h.
SPLIT_HELP_THRESHOLD_HOURS = 1.0

# [ASSUMPTION] share of Thermo intake that is a "special order" (non-standard
# work routed to C6). No data yet - replace with the real share when known.
DEFAULT_SPECIAL_ORDER_PCT = 10.0

# Remake loop.
#
# [REAL] Monthly remake / waste report, 2026 (Sep is a part month):
# remake count, m2 scrapped, and m2 per remake. This is the m2-based
# series the engine's remake RATE is calibrated to - the engine remakes a
# share of packed m2, so it has to match m2, not part counts.
REAL_REMAKE_WASTE_2026 = {
    #  month     remakes  waste m2  m2/remake
    "2026-01": (465,  162.82, 0.350),
    "2026-02": (1058, 385.76, 0.365),
    "2026-03": (1274, 468.22, 0.368),
    "2026-04": (936,  374.47, 0.400),
    "2026-05": (1122, 425.95, 0.380),
    "2026-06": (672,  261.47, 0.389),
    "2026-07": (606,  218.47, 0.361),
    "2026-08": (586,  225.71, 0.385),
    "2026-09": (309,  135.18, 0.437),   # part month
}
# Full months Jan-Aug: 315 m2/month remade on average (163-468). Against
# the same months' total output in the production tracker (Jan-Jun, the
# overlap): 2,079 m2 remade of 42,010 m2 = 4.95% of all m2, 5.8% of Thermo
# m2. That is the rate below. m2 per remake (0.35-0.44) matches the
# average Thermo part (0.38 m2) - remakes are single parts, as modelled.
REAL_REMAKE_M2_PER_MONTH = {"mean": 315.0, "min": 163.0, "max": 468.0}

# The live lead-time dashboard (Thermo, YTD to 11 Sep 2026) gives a
# PARTS-based rate: 4,015 of 57,110 complete parts were remakes (7.03%).
# It's higher than the m2 rate because the parts remade are mostly small
# ones - keep it for reference, but the engine works in m2. The same
# dashboard says remakes take only 0.32 days longer on average than
# non-remakes (8.97 vs 8.64 working days).
REAL_REMAKE_PARTS_RATE_PCT = 7.03
#
# That small penalty tells you something about HOW remakes flow: a part that
# went back to the start of the line and waited its turn again would take a
# whole extra lead time, not a third of a day. So the engine treats remakes
# as EXPEDITED - every queue is worked oldest-order-first, and a remake keeps
# its original order date, so it goes to the front of every queue on its
# second pass (this is how a real floor treats a late part). DEFAULT_REMAKE_DAYS
# is the hold before it re-enters (inspection / decision / re-programming);
# the UI reports the simulated remake penalty next to the real 0.32 so you
# can see whether this default reproduces it.
DEFAULT_REMAKE_RATE_PCT = 5.0   # [REAL, m2-based] see REAL_REMAKE_WASTE_2026 above
# Since the morning scheduling release is simulated (afternoon remakes wait
# for Optimising's next morning), the extra hold is only the inspect /
# re-program time on a day-shift remake.
DEFAULT_REMAKE_DAYS = 0.1


# ---------------------------------------------------------------------------
# Self-check: catch a typo in the tables above at import time, with a
# readable message, rather than as a KeyError deep inside the simulation.
# ---------------------------------------------------------------------------

def validate_config() -> None:
    problems = []
    for sid in STATIONS:
        if sid not in STATION_CREW:
            problems.append(f"station '{sid}' has no crew in STATION_CREW")
    for sid, crew in STATION_CREW.items():
        if sid not in STATIONS:
            problems.append(f"STATION_CREW refers to unknown station '{sid}'")
        if crew not in DEFAULT_SHIFT_SCHEDULES:
            problems.append(f"crew '{crew}' has no entry in DEFAULT_SHIFT_SCHEDULES")
        if crew not in CREW_LABELS:
            problems.append(f"crew '{crew}' has no entry in CREW_LABELS")
    for route, seq in ROUTE_SEQUENCE.items():
        for sid in seq:
            if sid not in STATIONS:
                problems.append(f"ROUTE_SEQUENCE[{route}] refers to unknown station '{sid}'")
            elif STATIONS[sid].route != route:
                problems.append(f"station '{sid}' is in ROUTE_SEQUENCE[{route}] but belongs to {STATIONS[sid].route}")
        if ROUTE_ENTRY_STATION.get(route) != seq[0]:
            problems.append(f"ROUTE_ENTRY_STATION[{route}] should be the first station of its sequence ({seq[0]})")
    for sid in PRESS_STATIONS:
        if sid not in STATIONS:
            problems.append(f"PRESS_STATIONS refers to unknown station '{sid}'")
    if SHARED_TERMINAL_STATION not in STATIONS:
        problems.append(f"SHARED_TERMINAL_STATION '{SHARED_TERMINAL_STATION}' is not a station")
    for cls, pc in PRODUCT_CLASSES.items():
        if pc.route not in ROUTE_SEQUENCE:
            problems.append(f"product class '{cls}' has unknown route '{pc.route}'")
        if cls not in DEFAULT_MIX_PCT:
            problems.append(f"product class '{cls}' has no DEFAULT_MIX_PCT entry")
    for cls in DEFAULT_MIX_PCT:
        if cls not in PRODUCT_CLASSES:
            problems.append(f"DEFAULT_MIX_PCT refers to unknown product class '{cls}'")
    for route in ROUTE_SEQUENCE:
        if route not in DEFAULT_TARGET_LEAD_DAYS:
            problems.append(f"route '{route}' has no DEFAULT_TARGET_LEAD_DAYS")
        if route not in LEAD_TIME_EXCLUDES_WEEKENDS:
            problems.append(f"route '{route}' has no LEAD_TIME_EXCLUDES_WEEKENDS entry")
    for sid in CNC_STATION_IDS:
        if sid not in STATIONS:
            problems.append(f"CNC_STATION_IDS refers to unknown station '{sid}'")
    if not (INTAKE_WINDOW_HOURS[0] >= 0 and INTAKE_WINDOW_HOURS[1] <= 24
            and INTAKE_WINDOW_HOURS[1] > INTAKE_WINDOW_HOURS[0]):
        problems.append(f"INTAKE_WINDOW_HOURS must be a [start, end) inside 0-24, got {INTAKE_WINDOW_HOURS}")
    if SIMULATION_WARMUP_DAYS < 0:
        problems.append("SIMULATION_WARMUP_DAYS cannot be negative")
    if CNC_THERMO_SPECIAL_MACHINE not in STATIONS["cnc_thermo"].machines:
        problems.append(f"CNC_THERMO_SPECIAL_MACHINE '{CNC_THERMO_SPECIAL_MACHINE}' is not one of cnc_thermo's machines")
    if problems:
        raise ValueError("plant_sim.config is inconsistent:\n  - " + "\n  - ".join(problems))


validate_config()
