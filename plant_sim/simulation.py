"""
The hourly-stepping factory simulator.

Design, in one paragraph: time advances one hour at a time. Each hour, fresh
orders arrive (on weekdays, during office hours, split into product classes
by the mix %), then every station - downstream first, so a part needs at
least an hour per station - pulls work off the front of its queue based on
how many operators are actually allocated to it *this shift* (see
allocation.py) and pushes finished work to the next station in its route.
Packed/despatched output splits into "good" (counted as completed, with its
full lead time) and "remade" (held for remake_days, then re-enters the
factory at the start of its own route, jumping the queue because it keeps
its original order date). All of this mirrors the original spreadsheet-style
tool, but capacity now comes from real named people with skills and absences
instead of abstract sliders.

Time conventions (used consistently everywhere):
  - hour 0 of every simulated day is the START of the day shift (~6am), and
    the afternoon shift follows straight on - not midnight.
  - day 0 is a Monday; weekday 5/6 are the weekend.
  - the run is (warm-up days + horizon); KPIs count only the horizon.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from plant_sim import config as cfg
from plant_sim.allocation import allocate_shift
from plant_sim.orders import EPS_M2, Batch, StationQueue
from plant_sim.staff import Roster

ROUTE_LABELS = {cfg.Route.THERMO: "Thermo", cfg.Route.CUT_AND_CLASH: "Cut & Clash"}


@dataclass
class SimulationSettings:
    horizon: str = "month"                 # "day" | "week" | "month"
    # Total intake FOR THE SELECTED HORIZON (e.g. if horizon="week", this is
    # m2 for the whole week, not m2/day). None = the real median for that
    # horizon (cfg.REAL_INTAKE_M2), resolved in __post_init__ - so a "day"
    # run never silently gets a month's worth of orders.
    intake_m2_for_horizon: float | None = None
    # Intake entered SEPARATELY per product range ({route: m2 for the
    # horizon}) - the two lines take orders independently, so one can be
    # pushed to 13,000 m2 without inventing Cut & Clash volume to match.
    # Left as None, it is split out of intake_m2_for_horizon using the
    # product mix, which is how the single-total behaviour is preserved.
    intake_m2_by_route: dict[str, float] | None = None
    mix_pct: dict[str, float] = field(default_factory=lambda: dict(cfg.DEFAULT_MIX_PCT))
    # Separate target lead time per product range - Cut & Clash (1536) quotes
    # 7 days; Thermo defaults to 10 (edit freely).
    target_lead_days: dict[str, float] = field(
        default_factory=lambda: dict(cfg.DEFAULT_TARGET_LEAD_DAYS))
    # Days added to every lead time outside the simulated floor. Pre-production
    # (order entry -> CNC release) is now simulated - see release_working_days
    # - so it defaults to 0; post-production covers despatch -> delivery.
    pre_prod_days: float = 0.0
    post_prod_days: float = 0.5
    # Optimising's morning release: an order placed on working day D goes onto
    # the CNC schedule on the morning of working day D+n. See
    # cfg.DEFAULT_RELEASE_WORKING_DAYS for where the numbers come from.
    release_working_days: dict[str, int] = field(
        default_factory=lambda: dict(cfg.DEFAULT_RELEASE_WORKING_DAYS))
    shift_schedules: dict[str, cfg.ShiftSchedule] = field(
        default_factory=lambda: {k: cfg.ShiftSchedule(**vars(v))
                                  for k, v in cfg.DEFAULT_SHIFT_SCHEDULES.items()})
    remake_enabled: bool = True
    remake_rate_pct: float = cfg.DEFAULT_REMAKE_RATE_PCT
    remake_days: float = cfg.DEFAULT_REMAKE_DAYS
    # Share of Thermo intake that is a special (non-standard) order. Specials
    # and remakes are cut on C6 first at the Thermo CNCs - see
    # cfg.CNC_THERMO_SPECIAL_MACHINE.
    special_order_pct: float = cfg.DEFAULT_SPECIAL_ORDER_PCT
    sick_enabled: bool = False
    random_seed: int | None = None
    # Editable "area capacity" per station, in m2/month AT IDEAL STAFFING on
    # the reference schedule (cfg.REFERENCE_HOURS_PER_MONTH) - e.g. "CNC =
    # 10,000 m2/month". Defaults to whatever the current rate constants in
    # config.py already imply, so leaving this untouched reproduces the old
    # behaviour exactly. See config.default_station_capacity_m2_per_month().
    station_capacity_m2_per_month: dict[str, float] = field(
        default_factory=lambda: {sid: cfg.default_station_capacity_m2_per_month(sid)
                                  for sid in cfg.FLOW_STATIONS})
    warmup_days: int = cfg.SIMULATION_WARMUP_DAYS
    buffer_cap_m2: float = cfg.DEFAULT_BUFFER_CAP_M2
    # Per-queue WIP limits in m2 (queue id -> cap); anything not listed uses
    # buffer_cap_m2. 0 or less means unlimited. See cfg.BUFFER_CAP_M2_BY_STATION.
    buffer_caps_m2: dict[str, float] = field(default_factory=lambda: dict(cfg.BUFFER_CAP_M2_BY_STATION))

    # Protective buffer targets (m2) the analysis checks - see cfg.BUFFER_TARGET_M2_BY_QUEUE.
    buffer_targets_m2: dict[str, float] = field(default_factory=lambda: dict(cfg.BUFFER_TARGET_M2_BY_QUEUE))
    # Press batching: run only once the pile reaches `start`, stop when it falls
    # to `stop`. start <= 0 = continuous running. See cfg.DEFAULT_PRESS_BATCH_*.
    press_batch_start_m2: float = cfg.DEFAULT_PRESS_BATCH_START_M2
    press_batch_stop_m2: float = cfg.DEFAULT_PRESS_BATCH_STOP_M2

    def buffer_cap_for(self, queue_id: str) -> float:
        cap = self.buffer_caps_m2.get(queue_id, self.buffer_cap_m2)
        return cap if cap and cap > 0 else math.inf

    def __post_init__(self):
        if self.intake_m2_by_route:
            self.intake_m2_by_route = {r: float(v) for r, v in self.intake_m2_by_route.items()}
            if self.intake_m2_for_horizon is None:
                self.intake_m2_for_horizon = sum(self.intake_m2_by_route.values())
        else:
            if self.intake_m2_for_horizon is None and self.horizon in cfg.REAL_INTAKE_M2:
                self.intake_m2_for_horizon = cfg.REAL_INTAKE_M2[self.horizon]["combined"]["median"]
            # No per-route figures given: split the total by the product mix,
            # exactly as the engine did when intake was one number.
            total = self.intake_m2_for_horizon or 0.0
            mix = self.mix_pct
            share = sum(v for v in mix.values() if v > 0) or 1.0
            self.intake_m2_by_route = {
                route: total * sum(mix.get(c, 0.0) for c, pc in cfg.PRODUCT_CLASSES.items()
                                    if pc.route == route) / share
                for route in cfg.ROUTE_SEQUENCE
            }
        self.validate()

    def validate(self) -> None:
        """Raise ValueError listing everything wrong with these settings."""
        p = []
        if self.horizon not in cfg.HORIZON_DAYS:
            p.append(f"horizon must be one of {list(cfg.HORIZON_DAYS)}, got {self.horizon!r}")
        if self.intake_m2_for_horizon is None or self.intake_m2_for_horizon < 0:
            p.append(f"intake_m2_for_horizon must be >= 0, got {self.intake_m2_for_horizon!r}")
        for route, v in (self.intake_m2_by_route or {}).items():
            if route not in cfg.ROUTE_SEQUENCE:
                p.append(f"intake_m2_by_route has unknown product range {route!r}")
            elif v < 0:
                p.append(f"intake_m2_by_route[{route!r}] must be >= 0, got {v}")
        unknown = [c for c in self.mix_pct if c not in cfg.PRODUCT_CLASSES]
        if unknown:
            p.append(f"mix_pct has unknown product class(es) {unknown}")
        if any(v < 0 for v in self.mix_pct.values()):
            p.append("mix_pct percentages cannot be negative")
        if sum(self.mix_pct.values()) <= 0:
            p.append("mix_pct must have at least one class with a positive share")
        for route in cfg.ROUTE_SEQUENCE:
            if route not in self.target_lead_days:
                p.append(f"target_lead_days is missing route {route!r}")
            elif self.target_lead_days[route] < 0:
                p.append(f"target_lead_days[{route!r}] cannot be negative")
        if self.pre_prod_days < 0 or self.post_prod_days < 0:
            p.append("pre_prod_days / post_prod_days cannot be negative")
        for crew in set(cfg.STATION_CREW.values()):
            if crew not in self.shift_schedules:
                p.append(f"shift_schedules is missing crew {crew!r}")
        if not (0 <= self.remake_rate_pct <= 100):
            p.append(f"remake_rate_pct must be 0-100, got {self.remake_rate_pct}")
        if self.remake_days < 0:
            p.append("remake_days cannot be negative")
        for route in cfg.ROUTE_SEQUENCE:
            if route not in self.release_working_days:
                p.append(f"release_working_days is missing route {route!r}")
            elif int(self.release_working_days[route]) < 0:
                p.append(f"release_working_days[{route!r}] cannot be negative")
        if not (0 <= self.special_order_pct <= 100):
            p.append(f"special_order_pct must be 0-100, got {self.special_order_pct}")
        for sid, v in self.station_capacity_m2_per_month.items():
            if sid not in cfg.STATIONS:
                p.append(f"station_capacity_m2_per_month has unknown station {sid!r}")
            elif v < 0:
                p.append(f"station_capacity_m2_per_month[{sid!r}] cannot be negative")
        if self.warmup_days < 0:
            p.append("warmup_days cannot be negative")
        if self.buffer_cap_m2 <= 0:
            p.append("buffer_cap_m2 must be positive")
        for qid in self.buffer_caps_m2:
            if qid not in cfg.STATIONS and qid != cfg.PRESS_QUEUE_ID:
                p.append(f"buffer_caps_m2 has unknown queue {qid!r}")
        for qid in self.buffer_targets_m2:
            if qid not in cfg.STATIONS and qid != cfg.PRESS_QUEUE_ID:
                p.append(f"buffer_targets_m2 has unknown queue {qid!r}")
        if self.press_batch_start_m2 < 0 or self.press_batch_stop_m2 < 0:
            p.append("press batch levels cannot be negative")
        if self.press_batch_start_m2 > 0 and self.press_batch_stop_m2 >= self.press_batch_start_m2:
            p.append("press_batch_stop_m2 must be below press_batch_start_m2")
        if p:
            raise ValueError("SimulationSettings problems:\n  - " + "\n  - ".join(p))

    def horizon_days(self) -> int:
        return cfg.HORIZON_DAYS[self.horizon]

    def horizon_hours(self) -> int:
        return self.horizon_days() * 24

    def normalised_mix(self) -> dict[str, float]:
        """The product mix scaled so it sums to exactly 100%, so "intake =
        X m2" always means X m2 no matter how the sliders were left."""
        total = sum(self.mix_pct.values())
        return {c: 100.0 * v / total for c, v in self.mix_pct.items()}

    def route_mix(self) -> dict[str, dict[str, float]]:
        """{route: {class: share % WITHIN that range}}. Now that intake is
        entered per range, the mix sliders decide the split inside each one
        (S1/S2/S3 within Thermo; Melamine/Acrylic within Cut & Clash) - the
        across-range share comes from the two intake numbers instead. A range
        whose classes are all at 0% is spread evenly, so entering intake for
        it never silently disappears."""
        out: dict[str, dict[str, float]] = {r: {} for r in cfg.ROUTE_SEQUENCE}
        for cls, pc in cfg.PRODUCT_CLASSES.items():
            out[pc.route][cls] = max(0.0, self.mix_pct.get(cls, 0.0))
        for route, classes in out.items():
            total = sum(classes.values())
            if total > 0:
                out[route] = {c: 100.0 * v / total for c, v in classes.items()}
            elif classes:
                out[route] = {c: 100.0 / len(classes) for c in classes}
        return out


@dataclass
class DailyStat:
    day: int
    avg_lead_days: float
    difot_pct: float
    qty_m2: float


@dataclass
class RouteMetrics:
    """DIFOT/lead-time/backlog figures for one product range (Thermo or Cut
    & Clash), judged against that range's own target lead time."""
    route: str
    label: str
    target_lead_days: float
    completed_m2: float
    overall_avg_lead_days: float
    overall_difot_pct: float
    overdue_backlog_m2: float
    daily_stats: list[DailyStat]
    # Remake diagnostics: how much of the completed m2 had been through the
    # remake loop, and how much longer those took - compare against the real
    # cfg.REAL_REMAKE_LEAD_PENALTY_DAYS.
    remake_share_pct: float = 0.0
    avg_lead_days_remakes: float | None = None
    avg_lead_days_non_remakes: float | None = None

    @property
    def has_completions(self) -> bool:
        return self.completed_m2 > 0

    @property
    def remake_lead_penalty_days(self) -> float | None:
        if self.avg_lead_days_remakes is None or self.avg_lead_days_non_remakes is None:
            return None
        return self.avg_lead_days_remakes - self.avg_lead_days_non_remakes


@dataclass
class SimulationResult:
    settings: SimulationSettings
    trace: list[dict]                     # one entry per simulated hour
    daily_stats: list[DailyStat]          # combined across both routes
    cum_intake_m2: float
    cum_completed_m2: float
    cum_remade_m2: float
    overdue_backlog_m2: float             # combined across both routes
    overall_avg_lead_days: float          # combined (each item judged by its own route's target)
    overall_difot_pct: float              # combined
    station_utilisation: dict[str, float]      # 0..1, output / available capacity
    completed_m2_by_class: dict[str, float]
    attendance_log: list[dict]             # one row per (day, operator) - for absenteeism charts
    by_route: dict[str, RouteMetrics]      # per-product-range breakdown (Thermo vs Cut & Clash)
    # Diagnostics (per station, hours within the reporting window):
    station_active_hours: dict[str, float]     # crew scheduled to run
    station_unstaffed_hours: dict[str, float]  # running, work waiting, but nobody on it
    station_starved_hours: dict[str, float]    # running and staffed, but nothing to do
    intake_hours_in_window: int
    # More per-station diagnostics for the constraints analysis (reporting window):
    station_staffed_hours: dict[str, float] = field(default_factory=dict)   # running with someone on it
    station_busy_hours: dict[str, float] = field(default_factory=dict)      # staffed AND had work at the start of the hour
    station_flat_out_hours: dict[str, float] = field(default_factory=dict)  # staffed and could not clear its queue (capacity-limited)
    station_blocked_hours: dict[str, float] = field(default_factory=dict)   # output limited by a full downstream buffer
    station_held_hours: dict[str, float] = field(default_factory=dict)      # staffed but idle BY POLICY (press batching)
    station_queue_avg_m2: dict[str, float] = field(default_factory=dict)    # average m2 waiting (over running hours)
    station_queue_max_m2: dict[str, float] = field(default_factory=dict)
    station_capacity_m2_per_hour: dict[str, float] = field(default_factory=dict)  # average while staffed (CNCs converted from minutes)
    station_out_m2: dict[str, float] = field(default_factory=dict)          # m2 processed in the window
    # Per-person time study: id -> {"rostered", "producing", "waiting", "covering", "absent"} hours
    person_hours: dict[str, dict[str, float]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)   # anything the engine wants the reader to know
    # Who was on which station, per reporting day and shift: "day|label" ->
    # {station_id: [operator names]} - drives the names in the floor playback.
    staffing_by_shift: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    # The C6 lane at the Thermo CNCs (see cfg.CNC_THERMO_SPECIAL_MACHINE):
    # utilisation of C6 vs the other machines, how much of the remake /
    # special work actually got cut on C6, and how often C6 was unmanned.
    cnc_thermo_lanes: dict[str, float] = field(default_factory=dict)
    # Optimising's morning releases inside the reporting window: one row per
    # batch scheduled onto the CNCs - {"day", "route", "cls", "qty",
    # "special", "remake", "order_day"} (order_day is relative to day 0 of
    # the window; negative = ordered during warm-up).
    release_log: list[dict] = field(default_factory=list)
    # Weekday mornings on which nobody was on Optimising, so nothing was
    # scheduled and the pending pool carried over to the next morning.
    scheduling_mornings_missed: int = 0
    scheduling_mornings: int = 0
    pending_m2_end: float = 0.0          # still waiting to be scheduled when the run ended
    pending_m2_avg: float = 0.0          # average over the window's hours

    @property
    def has_completions(self) -> bool:
        return self.cum_completed_m2 > 0

    @property
    def completion_pct(self) -> float:
        return 100.0 * self.cum_completed_m2 / self.cum_intake_m2 if self.cum_intake_m2 > 0 else 0.0


class Simulator:
    def __init__(self, roster: Roster, settings: SimulationSettings):
        roster.validate()
        settings.validate()
        self.roster = roster
        self.settings = settings
        self.rng = random.Random(settings.random_seed)

    # -----------------------------------------------------------------
    # Main loop
    # -----------------------------------------------------------------

    def run(self) -> SimulationResult:
        s = self.settings
        notes: list[str] = []

        # Run a warm-up period before the reporting window so every queue
        # already holds realistic steady-state WIP by the time we start
        # counting KPIs - see cfg.SIMULATION_WARMUP_DAYS for why. The clock
        # keeps running continuously across the boundary (shift schedules,
        # absences, and queue contents all carry through); only the stats
        # collected below get gated on `recording`.
        warmup_days = int(s.warmup_days)
        warmup_hours = warmup_days * 24
        end_hour = warmup_hours + s.horizon_hours()
        total_days = end_hour // 24

        # -- intake schedule: the horizon's total spread evenly over the
        # intake hours (weekdays, office window) that fall inside the
        # reporting window. Warm-up days get the same per-hour rate.
        intake_hours_in_window = sum(1 for h in range(warmup_hours, end_hour) if self._is_intake_hour(h))
        if intake_hours_in_window > 0:
            is_intake_hour = self._is_intake_hour
            intake_per_hour = s.intake_m2_for_horizon / intake_hours_in_window
        else:
            # Can only happen with an unusual warm-up length that lands a
            # one-day horizon on a weekend. Honour the entered intake anyway
            # by spreading it over every hour of the window.
            is_intake_hour = lambda h: True  # noqa: E731
            intake_hours_in_window = end_hour - warmup_hours
            intake_per_hour = s.intake_m2_for_horizon / intake_hours_in_window
            notes.append("No weekday intake hours fell inside the reporting window, so intake "
                         "was spread over every hour of it instead.")
        # Each range's own intake, spread over the same intake hours, split
        # inside the range by the mix sliders.
        route_mix = s.route_mix()
        intake_per_hour_by_route = {r: v / intake_hours_in_window
                                    for r, v in (s.intake_m2_by_route or {}).items()}
        if abs(sum(s.mix_pct.values()) - 100.0) > 0.05:
            notes.append(f"Product mix summed to {sum(s.mix_pct.values()):.1f}%, not 100% - it is read as "
                         "shares WITHIN each product range and normalised there, so each range's intake "
                         "total is exactly what you entered.")
        for route, classes in route_mix.items():
            if sum(s.mix_pct.get(c, 0.0) for c in classes) <= 0 and (s.intake_m2_by_route or {}).get(route, 0) > 0:
                notes.append(f"{ROUTE_LABELS[route]} has intake but every class in it is at 0% - its intake "
                             "was spread evenly over its classes instead of being dropped.")

        # press_1/press_2 don't get their own queue - they're two independently
        # staffed machines pulling from one shared physical pile of work.
        queue_ids = {cfg.queue_id_for(sid) for sid in cfg.STATIONS}
        queues: dict[str, StationQueue] = {qid: StationQueue() for qid in queue_ids}
        # Batches waiting to re-enter the line, as (hour they may re-enter, batch).
        remake_holding: list[tuple[float, Batch]] = []
        # Orders (and afternoon remakes) waiting for Optimising's morning
        # release, as (earliest day index they may be scheduled, batch).
        pending: list[tuple[int, Batch]] = []
        release_log: list[dict] = []
        mornings = 0
        mornings_missed = 0
        pending_sum = 0.0
        release_days = {r: int(s.release_working_days[r]) for r in cfg.ROUTE_SEQUENCE}
        # If nobody on the roster can work the scheduling station at all, the
        # release is treated as happening outside the model (every weekday
        # morning) rather than never - a roster with no Optimising person is
        # a valid what-if, and it shouldn't stall the whole plant.
        scheduler_on_roster = any(op.can_work(cfg.SCHEDULING_STATION) for op in self.roster.operators)
        if not scheduler_on_roster:
            notes.append(f"Nobody on the roster can work {cfg.STATIONS[cfg.SCHEDULING_STATION].label}, so "
                         "the morning scheduling release is assumed to happen regardless.")

        # Per-day absence draw, decided once per calendar day and reused for
        # every hour of that day.
        absences_by_day: dict[int, set[str]] = {}

        # Per (day, shift-label) staffing allocation snapshot - see the note
        # above cfg.SIMULATION_WARMUP_DAYS for why staffing is decided once
        # per shift rather than re-shuffled every hour.
        allocation_cache: dict[tuple[int, str], dict[str, str | None]] = {}

        trace: list[dict] = []
        attendance_log: list[dict] = []
        staffing_by_shift: dict[str, dict[str, list[str]]] = {}
        completed_log: list[dict] = []   # {"day", "qty", "lead_days", "cls", "route", "remake"}
        station_out_sum = {sid: 0.0 for sid in cfg.STATIONS}
        station_cap_sum = {sid: 0.0 for sid in cfg.STATIONS}
        station_active_hours = {sid: 0.0 for sid in cfg.STATIONS}
        station_unstaffed_hours = {sid: 0.0 for sid in cfg.STATIONS}
        station_starved_hours = {sid: 0.0 for sid in cfg.STATIONS}
        station_staffed_hours = {sid: 0.0 for sid in cfg.STATIONS}
        station_busy_hours = {sid: 0.0 for sid in cfg.STATIONS}
        station_flat_out_hours = {sid: 0.0 for sid in cfg.STATIONS}
        station_blocked_hours = {sid: 0.0 for sid in cfg.STATIONS}
        station_held_hours = {sid: 0.0 for sid in cfg.STATIONS}
        self._press_running = s.press_batch_start_m2 <= 0     # batch state carries hour to hour
        station_queue_sum = {sid: 0.0 for sid in cfg.STATIONS}
        station_queue_max = {sid: 0.0 for sid in cfg.STATIONS}
        person_hours = {op.id: {"rostered": 0.0, "producing": 0.0, "waiting": 0.0, "covering": 0.0, "absent": 0.0,
                                "helping": 0.0}
                        for op in self.roster.operators}
        cnc_minutes_by_class = {c: 0.0 for c in cfg.PRODUCT_CLASSES}
        completed_m2_by_class = {c: 0.0 for c in cfg.PRODUCT_CLASSES}
        # C6-lane bookkeeping (machine-minutes, reporting window only)
        lanes = {"c6_cap": 0.0, "c6_used": 0.0, "main_cap": 0.0, "main_used": 0.0,
                 "remake_min_total": 0.0, "remake_min_on_c6": 0.0,
                 "special_min_total": 0.0, "special_min_on_c6": 0.0,
                 "c6_active_hours": 0.0, "c6_unmanned_hours": 0.0}
        # Which Thermo CNC(s) each person mans when they're on the CNCs: their
        # own machine at full weight, a second machine (if any) at the
        # split-attention weight, floaters/cover untagged.
        ops_by_id = {op.id: op for op in self.roster.operators}

        cum_intake = cum_completed = cum_remade = 0.0
        # Whole-run totals (warm-up included) for the mass-balance self-check.
        intake_all = completed_all = 0.0

        # Resolve each station's user-edited "capacity in m2/month" into what
        # the engine actually consumes: a per-operator-hour rate for flat-rate
        # stations, or a per-class minutes-per-m2 table for the two CNC
        # stations (this keeps the S1/S2/S3 relative cut-time ratios intact
        # while calibrating the absolute level to the capacity you typed in).
        op_hour_rate, cnc_min_per_m2 = self._resolve_capacities()
        # Hours-of-work a queue represents at each station (for splitting a
        # person's time by where the pile is): m2 per hour at the ideal crew.
        hours_per_m2 = self._hours_per_m2(op_hour_rate, cnc_min_per_m2)

        for h in range(end_hour):
            day_index = h // 24
            weekday = day_index % 7
            hour_of_day = h % 24
            recording = h >= warmup_hours
            record_day_index = day_index - warmup_days

            if day_index not in absences_by_day:
                absences_by_day[day_index] = self.roster.roll_daily_absences(
                    day_index, s.sick_enabled, self.rng)
            absent_ids = absences_by_day[day_index]

            # -- release matured remakes back into their own route's entry station --
            if remake_holding:
                still_held = []
                for release_hour, b in remake_holding:
                    if release_hour <= h:
                        entry = cfg.ROUTE_ENTRY_STATION[cfg.PRODUCT_CLASSES[b.product_class].route]
                        queues[entry].add(b)
                    else:
                        still_held.append((release_hour, b))
                remake_holding = still_held

            # -- fresh intake, split by class mix --
            if is_intake_hour(h):
                for route, per_hour in intake_per_hour_by_route.items():
                    for cls, pct in route_mix[route].items():
                        qty = per_hour * pct / 100.0
                        if qty <= EPS_M2:
                            continue
                        entry = cfg.ROUTE_ENTRY_STATION[route]
                        # Thermo intake is split into special orders (routed to
                        # C6) and regular work; Cut & Clash has no such split.
                        special = qty * s.special_order_pct / 100.0 if route == cfg.Route.THERMO else 0.0
                        # New orders wait in the pending pool until Optimising's
                        # morning release (see cfg.DEFAULT_RELEASE_WORKING_DAYS).
                        release_day = self._working_day_after(day_index, release_days[route])
                        if qty - special > EPS_M2:
                            pending.append((release_day, Batch(h, qty - special, cls)))
                        if special > EPS_M2:
                            pending.append((release_day, Batch(h, special, cls, is_special=True)))
                        intake_all += qty
                        if recording:
                            cum_intake += qty

            # -- which shift (if any) each station's crew is on right now, and
            #    which of the crew's own days it is (crews can start early/late) --
            label_by_station, day_by_station = self._shift_label_by_station(h)
            active_now = {sid for sid, lbl in label_by_station.items() if lbl is not None}

            # -- staffing allocation snapshots, one per shift label per day,
            #    taken when the first crew starts that shift. A crew that
            #    starts before hour 0 begins day d inside plant day d-1, so
            #    both today's and tomorrow's snapshot hours are checked. --
            for label in cfg.SHIFT_LABELS:
                for d in (day_index, day_index + 1):
                    key = (d, label)
                    if key in allocation_cache or h != self._snapshot_abs_hour(d, label):
                        continue
                    d_weekday = d % 7
                    if d not in absences_by_day:
                        absences_by_day[d] = self.roster.roll_daily_absences(d, s.sick_enabled, self.rng)
                    absent_d = absences_by_day[d]
                    active_for_label = self._active_stations_for_shift(d_weekday, label)
                    available_ops = [op for op in self.roster.operators
                                      if op.shift == label and op.id not in absent_d]
                    queue_snapshot = {sid: queues[cfg.queue_id_for(sid)].total_m2() for sid in cfg.STATIONS}
                    assignment = allocate_shift(available_ops, active_for_label, queue_snapshot)
                    allocation_cache[key] = assignment
                    rec_day = d - warmup_days
                    if 0 <= rec_day < total_days - warmup_days:
                        names_by_station: dict[str, list[str]] = {}
                        for op in self.roster.operators:
                            if op.shift != label:
                                continue
                            station = assignment.get(op.id) if op.id not in absent_d else None
                            if station:
                                names_by_station.setdefault(station, []).append(op.name)
                                if station == op.home_station and op.second_station and op.second_station != station:
                                    names_by_station.setdefault(op.second_station, []).append(op.name + " (also)")
                            attendance_log.append({
                                "day": rec_day, "operator_id": op.id, "operator_name": op.name,
                                "shift": label,
                                "status": self._attendance_status(op, d_weekday, label, absent_d, assignment),
                                "station": station,
                            })
                        staffing_by_shift[f"{rec_day}|{label}"] = names_by_station

            waiting_now = {sid: queues[cfg.queue_id_for(sid)].total_m2() for sid in cfg.STATIONS}
            ops_per_station, cnc_tags, helping_now = self._headcount_per_station(
                allocation_cache, day_by_station, label_by_station, ops_by_id, waiting_now, hours_per_m2)
            if recording:
                for op_id, share in helping_now.items():
                    person_hours[op_id]["helping"] += share

            # -- Optimising's morning release: schedule pending work onto the CNCs --
            scheduler_here = ops_per_station.get(cfg.SCHEDULING_STATION, 0.0) > 0 or not scheduler_on_roster
            if weekday < 5 and hour_of_day == cfg.SCHEDULING_RELEASE_HOUR:
                if recording:
                    mornings += 1
                if scheduler_here:
                    still_pending = []
                    for release_day, b in pending:
                        if release_day <= day_index:
                            entry = cfg.ROUTE_ENTRY_STATION[cfg.PRODUCT_CLASSES[b.product_class].route]
                            queues[entry].add(b)
                            if recording:
                                release_log.append({
                                    "day": record_day_index,
                                    "route": cfg.PRODUCT_CLASSES[b.product_class].route,
                                    "cls": b.product_class, "qty": b.qty,
                                    "special": b.is_special, "remake": b.is_remake,
                                    "order_day": int(b.created_hour // 24) - warmup_days,
                                })
                        else:
                            still_pending.append((release_day, b))
                    pending = still_pending
                elif recording:
                    mornings_missed += 1
            if recording:
                pending_sum += sum(b.qty for _, b in pending)
            cnc_label = label_by_station["cnc_thermo"]
            c6_minutes, main_minutes = self._cnc_thermo_lanes(cnc_tags)
            if recording and cnc_label:
                lanes["c6_active_hours"] += 1
                if c6_minutes <= 0:
                    lanes["c6_unmanned_hours"] += 1

            # -- diagnostics: is anyone there, is there anything to do? --
            if recording:
                for sid in active_now:
                    station_active_hours[sid] += 1
                    waiting = waiting_now[sid]
                    station_queue_sum[sid] += waiting
                    station_queue_max[sid] = max(station_queue_max[sid], waiting)
                    if ops_per_station[sid] > 0:
                        station_staffed_hours[sid] += 1
                    if ops_per_station[sid] == 0 and waiting > EPS_M2:
                        station_unstaffed_hours[sid] += 1
                    elif ops_per_station[sid] > 0 and waiting <= EPS_M2:
                        station_starved_hours[sid] += 1

            blocked_now: set[str] = set()
            held_now: set[str] = set()
            util_now: dict[str, float] = {}
            hour_out = self._process_hour(queues, ops_per_station, op_hour_rate, cnc_min_per_m2,
                                          cnc_minutes_by_class, station_out_sum, station_cap_sum,
                                          (c6_minutes, main_minutes), lanes if recording else None,
                                          blocked_now, util_now, held_now)
            if recording:
                for sid in active_now:
                    if ops_per_station[sid] <= 0:
                        continue
                    u = util_now.get(sid, 0.0)
                    station_busy_hours[sid] += u                # productive share of the staffed hour
                    if sid in held_now:
                        station_held_hours[sid] += 1
                    elif sid in blocked_now:
                        station_blocked_hours[sid] += 1
                    elif u >= 0.98:
                        station_flat_out_hours[sid] += 1        # capacity-limited this hour
                # per-person time study: producing = their station's productive
                # share of the hour, waiting = the rest.
                for op in self.roster.operators:
                    home_label = label_by_station.get(op.home_station)
                    if op.id in absent_ids:
                        if home_label == op.shift:
                            person_hours[op.id]["absent"] += 1
                        continue
                    st = allocation_cache.get((day_index, op.shift), {}).get(op.id)
                    if st is None or label_by_station.get(st) != op.shift:
                        continue                                # not on the floor this hour
                    ph = person_hours[op.id]
                    u = util_now.get(st, 0.0)
                    ph["rostered"] += 1
                    ph["producing"] += u
                    ph["waiting"] += 1.0 - u
                    if st != op.home_station:
                        ph["covering"] += 1

            # -- packing/despatch output -> completed or remade --
            for batch in hour_out.get(cfg.SHARED_TERMINAL_STATION, []):
                good_qty = batch.qty
                bad_qty = 0.0
                if s.remake_enabled:
                    bad_qty = batch.qty * (s.remake_rate_pct / 100.0)
                    good_qty = batch.qty - bad_qty
                route = cfg.PRODUCT_CLASSES[batch.product_class].route
                if good_qty > EPS_M2:
                    completed_all += good_qty
                    if recording:
                        lead_days = (s.pre_prod_days
                                     + self._days_elapsed(batch.created_hour, h, route)
                                     + s.post_prod_days)
                        completed_log.append({"day": record_day_index, "qty": good_qty,
                                              "lead_days": lead_days, "cls": batch.product_class,
                                              "route": route, "remake": batch.is_remake})
                        completed_m2_by_class[batch.product_class] += good_qty
                        cum_completed += good_qty
                if bad_qty > EPS_M2:
                    remake = Batch(batch.created_hour, bad_qty, batch.product_class,
                                   is_remake=True, is_special=batch.is_special)
                    if scheduler_here and weekday < 5:
                        # Found while Optimising is in: scheduled straight away
                        # (after the inspect / re-program hold).
                        remake_holding.append((h + s.remake_days * 24.0, remake))
                    else:
                        # Afternoon-shift or weekend remake: waits for the next
                        # morning's release.
                        pending.append((self._working_day_after(day_index, 1), remake))
                    if recording:
                        cum_remade += bad_qty

            if recording:
                # The shared "press" queue isn't a station in its own right
                # (see PRESS_QUEUE_ID) - report its level under both press_1
                # and press_2 so charts/floor-view can show "buffer waiting
                # for a press" against each machine's own box.
                buf = {sid: queues[cfg.queue_id_for(sid)].total_m2() for sid in cfg.STATIONS}
                trace.append({
                    "h": h - warmup_hours,
                    "day": record_day_index,
                    "buf": buf,
                    "out": {sid: sum(b.qty for b in batches) for sid, batches in hour_out.items()},
                    "ops": ops_per_station,
                    "active": sorted(active_now),
                    "shift": label_by_station,
                    "remake_held": sum(b.qty for _, b in remake_holding),
                    "pending": sum(b.qty for _, b in pending),
                    "cum_intake": cum_intake, "cum_completed": cum_completed, "cum_remade": cum_remade,
                })

        # -- mass balance: nothing is ever created or lost inside the engine --
        wip = (sum(q.total_m2() for q in queues.values()) + sum(b.qty for _, b in remake_holding)
               + sum(b.qty for _, b in pending))
        imbalance = intake_all - (completed_all + wip)
        if abs(imbalance) > 1e-6 * max(1.0, intake_all):
            raise RuntimeError(f"Simulation mass balance broken: intake {intake_all:.6f} != "
                               f"completed {completed_all:.6f} + WIP {wip:.6f} (diff {imbalance:.6g})")

        # Combined metrics: every completed/backlogged item is judged against
        # its OWN product range's target lead time (Thermo vs Cut & Clash),
        # then pooled together for the headline KPIs.
        daily_stats = self._daily_stats(completed_log, s.target_lead_days)
        overall_avg_lead, overall_difot = self._lead_and_difot(completed_log, s.target_lead_days)
        overdue_backlog = self._overdue_backlog(queues, remake_holding + pending, s.target_lead_days, end_hour)

        # Per-route breakdown - same calculations, filtered to one route at a
        # time, so Thermo and Cut & Clash each get their own DIFOT/lead-time
        # judged against their own target.
        by_route: dict[str, RouteMetrics] = {}
        for route, label in ROUTE_LABELS.items():
            route_log = [e for e in completed_log if e["route"] == route]
            r_avg_lead, r_difot = self._lead_and_difot(route_log, s.target_lead_days)
            r_overdue = self._overdue_backlog(queues, remake_holding + pending, s.target_lead_days, end_hour,
                                              route_filter=route)
            remakes = [e for e in route_log if e["remake"]]
            firsts = [e for e in route_log if not e["remake"]]
            total_qty = sum(e["qty"] for e in route_log)
            remake_qty = sum(e["qty"] for e in remakes)
            by_route[route] = RouteMetrics(
                route=route, label=label, target_lead_days=s.target_lead_days[route],
                completed_m2=total_qty,
                overall_avg_lead_days=r_avg_lead, overall_difot_pct=r_difot,
                overdue_backlog_m2=r_overdue,
                daily_stats=self._daily_stats(route_log, s.target_lead_days),
                remake_share_pct=(100.0 * remake_qty / total_qty) if total_qty > 0 else 0.0,
                avg_lead_days_remakes=self._weighted_avg_lead(remakes),
                avg_lead_days_non_remakes=self._weighted_avg_lead(firsts),
            )

        # Utilisation = output / available capacity, both in the same units.
        # For CNC stations, capacity was tracked in machine-*minutes* (cycle
        # time depends on product class) rather than m2, so their utilisation
        # has to compare minutes-used to minutes-available, not m2 to minutes.
        utilisation = {}
        for sid, station in cfg.STATIONS.items():
            if sid in cfg.CNC_STATION_IDS:
                minutes_used = sum(cnc_minutes_by_class[c] for c, pc in cfg.PRODUCT_CLASSES.items()
                                    if pc.route == station.route)
                utilisation[sid] = minutes_used / station_cap_sum[sid] if station_cap_sum[sid] > 0 else 0.0
            else:
                utilisation[sid] = (station_out_sum[sid] / station_cap_sum[sid]
                                     if station_cap_sum[sid] > 0 else 0.0)

        return SimulationResult(
            settings=s, trace=trace, daily_stats=daily_stats,
            cum_intake_m2=cum_intake, cum_completed_m2=cum_completed, cum_remade_m2=cum_remade,
            overdue_backlog_m2=overdue_backlog, overall_avg_lead_days=overall_avg_lead,
            overall_difot_pct=overall_difot, station_utilisation=utilisation,
            completed_m2_by_class=completed_m2_by_class, attendance_log=attendance_log,
            by_route=by_route,
            station_active_hours=station_active_hours,
            station_unstaffed_hours=station_unstaffed_hours,
            station_starved_hours=station_starved_hours,
            intake_hours_in_window=intake_hours_in_window,
            notes=notes,
            staffing_by_shift=staffing_by_shift,
            station_staffed_hours=station_staffed_hours,
            station_busy_hours=station_busy_hours,
            station_flat_out_hours=station_flat_out_hours,
            station_blocked_hours=station_blocked_hours,
            station_held_hours=station_held_hours,
            station_queue_avg_m2={sid: (station_queue_sum[sid] / station_active_hours[sid]
                                        if station_active_hours[sid] > 0 else 0.0) for sid in cfg.STATIONS},
            station_queue_max_m2=station_queue_max,
            station_capacity_m2_per_hour=self._capacity_m2_per_hour(
                station_cap_sum, station_staffed_hours, station_out_sum, cnc_minutes_by_class),
            station_out_m2=dict(station_out_sum),
            person_hours=person_hours,
            release_log=release_log,
            scheduling_mornings=mornings,
            scheduling_mornings_missed=mornings_missed,
            pending_m2_end=sum(b.qty for _, b in pending),
            pending_m2_avg=pending_sum / max(1, end_hour - warmup_hours),
            cnc_thermo_lanes={
                "c6_utilisation": lanes["c6_used"] / lanes["c6_cap"] if lanes["c6_cap"] > 0 else 0.0,
                "main_utilisation": lanes["main_used"] / lanes["main_cap"] if lanes["main_cap"] > 0 else 0.0,
                "remakes_on_c6_pct": (100.0 * lanes["remake_min_on_c6"] / lanes["remake_min_total"]
                                      if lanes["remake_min_total"] > 0 else 0.0),
                "specials_on_c6_pct": (100.0 * lanes["special_min_on_c6"] / lanes["special_min_total"]
                                       if lanes["special_min_total"] > 0 else 0.0),
                "c6_active_hours": lanes["c6_active_hours"],
                "c6_unmanned_hours": lanes["c6_unmanned_hours"],
            },
        )

    # -----------------------------------------------------------------
    # Time helpers
    # -----------------------------------------------------------------

    @staticmethod
    def _is_intake_hour(h: int) -> bool:
        weekday = (h // 24) % 7
        if cfg.INTAKE_WEEKDAYS_ONLY and weekday >= 5:
            return False
        hod = h % 24
        return cfg.INTAKE_WINDOW_HOURS[0] <= hod < cfg.INTAKE_WINDOW_HOURS[1]

    def _days_elapsed(self, start_hour: float, end_hour: float, route: str) -> float:
        """Lead-time day-count between two simulation hours, for the given
        route. Thermo's real lead-time dashboard measures its 10-day promise
        in WORKING days (Mon-Fri, weekends excluded), not raw calendar time -
        see cfg.LEAD_TIME_EXCLUDES_WEEKENDS. Day 0 of the simulation is a
        Monday, matching the weekday convention the shift schedules use."""
        if cfg.LEAD_TIME_EXCLUDES_WEEKENDS.get(route, False):
            return self._working_days_elapsed(start_hour, end_hour)
        return max(0.0, (end_hour - start_hour) / 24.0)

    @staticmethod
    def _working_hours_before(t: float) -> float:
        """Cumulative Mon-Fri hours between simulation hour 0 (a Monday
        morning) and hour t. Exact for any fractional t - a batch created
        on Friday afternoon and finished Saturday morning is charged only
        the Friday part."""
        if t <= 0:
            return 0.0
        weeks = math.floor(t / 168.0)
        remainder = t - weeks * 168.0
        return weeks * 120.0 + min(remainder, 120.0)

    @staticmethod
    def _working_day_after(day_index: int, n: int) -> int:
        """The day index of the n-th working day (Mon-Fri) after day_index.
        n=0 returns day_index itself, even on a weekend - the release loop
        only fires on weekday mornings, so it is picked up on the next one."""
        d = day_index
        for _ in range(max(0, n)):
            d += 1
            while d % 7 >= 5:
                d += 1
        return d

    @classmethod
    def _working_days_elapsed(cls, start_hour: float, end_hour: float) -> float:
        if end_hour <= start_hour:
            return 0.0
        return (cls._working_hours_before(end_hour) - cls._working_hours_before(start_hour)) / 24.0

    # -----------------------------------------------------------------
    # Shifts and staffing
    # -----------------------------------------------------------------

    def _shift_label_by_station(self, h: int) -> tuple[dict[str, str | None], dict[str, int]]:
        """Which shift each station's crew is on at absolute hour h ('day',
        'aft', or None if that crew isn't running), and which of the crew's
        OWN day indexes it is. Each crew runs on its own clock, offset from
        the plant's 6am reference by its start_hour, so a CNC crew starting
        at -2 is on day 7 (Monday) from Sunday 22:00 plant time."""
        labels: dict[str, str | None] = {}
        days: dict[str, int] = {}
        for sid, crew_name in cfg.STATION_CREW.items():
            sched = self.settings.shift_schedules[crew_name]
            clock = sched.local_clock(h)
            if clock is None:
                labels[sid], days[sid] = None, h // 24
                continue
            d, hod = clock
            days[sid] = d
            labels[sid] = sched.shift_at(hod) if sched.active_on_weekday(d % 7) else None
        return labels, days

    def _active_stations_for_shift(self, weekday: int, shift_label: str) -> set[str]:
        """Stations whose crew runs this shift label on this weekday."""
        active = set()
        for sid, crew_name in cfg.STATION_CREW.items():
            sched = self.settings.shift_schedules[crew_name]
            if sched.active_on_weekday(weekday) and sched.offers_shift(shift_label):
                active.add(sid)
        return active

    def _snapshot_abs_hour(self, day_index: int, shift_label: str) -> int:
        """The absolute simulation hour at which day `day_index`'s allocation
        for `shift_label` is decided: when the first crew (on its own clock)
        starts that shift. If no crew runs that shift that day at all, it's
        decided at the day's hour 0 (everyone on it is simply logged as idle
        / on a day off)."""
        weekday = day_index % 7
        starts = [
            day_index * 24 + sched.start_hour + sched.shift_start_hour(shift_label)
            for crew in set(cfg.STATION_CREW.values())
            for sched in (self.settings.shift_schedules[crew],)
            if sched.active_on_weekday(weekday) and sched.offers_shift(shift_label)
        ]
        return int(math.floor(min(starts))) if starts else day_index * 24

    def _attendance_status(self, op, weekday: int, label: str, absent_ids: set[str],
                           assignment: dict[str, str | None]) -> str:
        if op.id in absent_ids:
            return "absent"
        if assignment.get(op.id):
            return "working"
        home_sched = self.settings.shift_schedules[cfg.STATION_CREW[op.home_station]]
        if not home_sched.active_on_weekday(weekday):
            return "day_off"       # their crew doesn't work this weekday
        return "idle"              # rostered on, but nothing they can do is running

    @staticmethod
    def _hours_per_m2(op_hour_rate, cnc_min_per_m2) -> dict[str, float]:
        """How many hours of a station's work (at its ideal crew) one m2 in
        its queue represents - used to compare piles between stations."""
        out = {}
        for sid, st in cfg.STATIONS.items():
            if sid in cfg.CNC_STATION_IDS:
                classes = [c for c, pc in cfg.PRODUCT_CLASSES.items() if pc.route == st.route]
                share = sum(cfg.DEFAULT_MIX_PCT[c] for c in classes) or 1.0
                mpm = sum(cfg.DEFAULT_MIX_PCT[c] / share * min(cnc_min_per_m2[sid][c], 1e9) for c in classes)
                per_hour = min(st.ideal_ops, st.num_machines) * 60.0 * cfg.CNC_UTILISATION / mpm if mpm > 0 else 0.0
            else:
                per_hour = st.ideal_ops * op_hour_rate.get(sid, 0.0)
            out[sid] = 1.0 / per_hour if per_hour > 0 else 0.0
        return out

    @staticmethod
    def _split_share(home_hours: float, second_hours: float) -> float:
        """Share of a split person's hour that goes to their SECOND station,
        given the hours of work waiting at home and at the second station.
        Nothing until the second pile is worth helping with; then in
        proportion to the two piles."""
        if second_hours < cfg.SPLIT_HELP_THRESHOLD_HOURS:
            return 0.0
        total = home_hours + second_hours
        return second_hours / total if total > 0 else 0.0

    @classmethod
    def _presence(cls, op, waiting_now=None, hours_per_m2=None) -> list[tuple[str, float, str]]:
        """Where a person's time goes when the allocator has them at their
        HOME station, as [(station, weight, machine)]. A whole person at one
        place, unless they're split across two: then their hour is divided
        by where the work is (see cfg.SPLIT_HELP_THRESHOLD_HOURS). A CNC
        operator running a second Thermo CNC stays a whole person on their
        own machine and the unattended second one runs at a reduced rate."""
        home, second = op.home_station, op.second_station
        if not second:
            return [(home, 1.0, op.machine)]
        if op.runs_second_cnc:
            return [(home, 1.0, op.machine), (home, cfg.CNC_SECOND_MACHINE_FACTOR, op.machine_2)]
        if waiting_now is None or hours_per_m2 is None:
            return [(home, 1.0, op.machine)]
        h_home = waiting_now.get(home, 0.0) * hours_per_m2.get(home, 0.0)
        h_second = waiting_now.get(second, 0.0) * hours_per_m2.get(second, 0.0)
        share = cls._split_share(h_home, h_second)
        if share <= 0:
            return [(home, 1.0, op.machine)]
        return [(home, 1.0 - share, op.machine), (second, share, op.machine_2)]

    @classmethod
    def _headcount_per_station(cls, allocation_cache, day_by_station: dict[str, int],
                                label_by_station: dict[str, str | None],
                                ops_by_id, waiting_now, hours_per_m2) -> tuple[dict[str, float], list, dict]:
        """How much of a person is on each station THIS hour (people split
        across two stations count fractionally, by where the work is), read
        from the snapshot for whichever shift that station's own crew is on.
        Also returns the (machine, weight) tags for everyone on the Thermo
        CNCs (for the C6 lane logic) and {op_id: share} for anyone helping
        at their second station this hour. A split only applies while the
        person is at their home station and the second station is running
        on the same shift; moved by the allocator, they are a whole person
        wherever they were sent."""
        counts = {sid: 0.0 for sid in cfg.STATIONS}
        cnc_tags: list = []
        helping: dict[str, float] = {}
        seen: set[str] = set()
        for sid, label in label_by_station.items():
            if label is None:
                continue
            assignment = allocation_cache.get((day_by_station[sid], label))
            if not assignment:
                continue
            for op_id, st in assignment.items():
                if st is None:
                    continue
                op = ops_by_id[op_id]
                parts = cls._presence(op, waiting_now, hours_per_m2) if st == op.home_station else [(st, 1.0, "")]
                # if the second station isn't running on this shift right now, the whole person is at home
                if len(parts) == 2 and parts[1][0] != parts[0][0] and label_by_station.get(parts[1][0]) != label:
                    parts = [(parts[0][0], 1.0, parts[0][2])]
                for where, w, machine in parts:
                    if where != sid:
                        continue
                    counts[sid] += w
                    if sid == "cnc_thermo":
                        cnc_tags.append((machine, w))
                    if len(parts) == 2 and where == parts[1][0] and where != parts[0][0] and op_id not in seen:
                        helping[op_id] = w
                        seen.add(op_id)
        return counts, cnc_tags, helping

    # -----------------------------------------------------------------
    # Capacity
    # -----------------------------------------------------------------

    def _resolve_capacities(self) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
        """Turn the editable 'm2/month at ideal staffing' numbers into engine
        rates. Returns (m2 per operator-hour by flat-rate station,
        machine-minutes per m2 by class for each CNC station)."""
        s = self.settings
        op_hour_rate: dict[str, float] = {}
        cnc_min_per_m2: dict[str, dict[str, float]] = {}
        for sid, station in cfg.STATIONS.items():
            if not cfg.is_flow_station(sid):
                continue   # admin / box-packing lines: staffed, but no m2 flows through
            target_month = s.station_capacity_m2_per_month.get(
                sid, cfg.default_station_capacity_m2_per_month(sid))
            if sid in cfg.CNC_STATION_IDS:
                default_month = cfg.cnc_capacity_from_cut_times(station.route)
                # scale > 1 means the edited capacity is HIGHER than the
                # default, i.e. the machine cuts faster than the default
                # per-class times - so time-per-m2 shrinks by the same
                # factor. scale == 0 means "this CNC is down".
                scale = target_month / default_month if default_month > 0 else 1.0
                cnc_min_per_m2[sid] = {
                    cls: (cfg.cnc_min_per_m2(cls) / scale if scale > 0 else math.inf)
                    for cls, pc in cfg.PRODUCT_CLASSES.items() if pc.route == station.route
                }
            else:
                op_hour_rate[sid] = (target_month / (station.ideal_ops * cfg.REFERENCE_HOURS_PER_MONTH)
                                      if station.ideal_ops > 0 else station.capacity_m2_per_op_hour)
        return op_hour_rate, cnc_min_per_m2

    @staticmethod
    def _capacity_m2_per_hour(station_cap_sum, station_staffed_hours, station_out_sum,
                              cnc_minutes_by_class) -> dict[str, float]:
        """Average capacity while staffed, in m2/hour, for every station. CNC
        capacity is tracked in machine-minutes, so it is converted with the
        average minutes-per-m2 of what that CNC actually cut (or the default
        mix if it cut nothing)."""
        out = {}
        for sid, station in cfg.STATIONS.items():
            staffed = station_staffed_hours.get(sid, 0.0)
            if staffed <= 0:
                out[sid] = 0.0
                continue
            if sid in cfg.CNC_STATION_IDS:
                classes = [c for c, pc in cfg.PRODUCT_CLASSES.items() if pc.route == station.route]
                minutes_used = sum(cnc_minutes_by_class[c] for c in classes)
                m2_out = station_out_sum.get(sid, 0.0)
                if m2_out > 0 and minutes_used > 0:
                    min_per_m2 = minutes_used / m2_out
                else:
                    share = sum(cfg.DEFAULT_MIX_PCT[c] for c in classes) or 1.0
                    min_per_m2 = sum(cfg.DEFAULT_MIX_PCT[c] / share * cfg.cnc_min_per_m2(c) for c in classes) or 1.0
                out[sid] = (station_cap_sum[sid] / staffed) / min_per_m2
            else:
                out[sid] = station_cap_sum[sid] / staffed
        return out

    @staticmethod
    def _flat_capacity_m2_per_hour(station: cfg.Station, ops: int, rate_m2_per_op_hour: float) -> float:
        if ops <= 0:
            return 0.0
        if station.machine_bound:
            ops = min(ops, station.ideal_ops)   # the machine can't go faster than full rate
        return ops * rate_m2_per_op_hour

    # -----------------------------------------------------------------
    # One hour of production
    # -----------------------------------------------------------------

    @staticmethod
    def _cnc_thermo_lanes(machine_tags: list) -> tuple[float, float]:
        """Machine-minutes available this hour on the C6 lane and on the other
        Thermo CNCs. `machine_tags` is one (machine, weight) per machine a
        person is tending: their own machine at weight 1.0, a second machine
        they also run at cfg.CNC_SECOND_MACHINE_FACTOR, and ("", 1.0) for
        untagged floaters / cover. A machine's manning is the sum of the
        weights on it, capped at 1 (two people on one machine don't make it
        faster - the spare full-weight person moves on to an unmanned
        production machine first, C6 last). Plain strings are accepted too
        (weight 1.0)."""
        station = cfg.STATIONS["cnc_thermo"]
        special = cfg.CNC_THERMO_SPECIAL_MACHINE
        machines = list(station.machines)
        tags = [(t, 1.0) if isinstance(t, str) else (t[0], float(t[1])) for t in machine_tags]
        weight = {m: 0.0 for m in machines}
        spare = 0.0
        # Whole people first, so a real operator on a machine takes precedence
        # over someone's split-attention share of it (which is then simply
        # not needed - it was tied to that machine, it doesn't become spare).
        for m, w in sorted(tags, key=lambda t: -t[1]):
            if m not in weight:
                spare += w                                   # untagged floater / cover
            else:
                room = 1.0 - weight[m]
                weight[m] += min(w, room)
                if w >= 1.0:
                    spare += w - min(w, room)                # a whole person on a manned machine moves on
        fill_order = [m for m in machines if m != special] + [special]
        for m in fill_order:
            if spare <= 1e-9:
                break
            take = min(spare, 1.0 - weight[m])
            weight[m] += take
            spare -= take
        per_machine = 60.0 * cfg.CNC_UTILISATION
        c6 = per_machine * weight[special]
        main = per_machine * sum(w for m, w in weight.items() if m != special)
        return c6, main

    def _process_hour(self, queues, ops_per_station, op_hour_rate, cnc_min_per_m2,
                       cnc_minutes_by_class, station_out_sum, station_cap_sum,
                       thermo_lanes=(0.0, 0.0), lane_stats=None, blocked_now=None,
                       util_now=None, held_now=None) -> dict[str, list[Batch]]:
        """Run one simulated hour of production through every station, in
        cfg.PROCESSING_ORDER (downstream first), and return
        {station_id: [output batches]}.

        Both routes' last stations and both presses feed the shared
        "despatch" queue; despatch itself is processed once (it has a single
        shared headcount - splitting it per route would double-count
        capacity). Despatch's output is the factory's finished output.
        """
        out: dict[str, list[Batch]] = {}
        for sid in cfg.PROCESSING_ORDER:
            station = cfg.STATIONS[sid]
            ops = ops_per_station[sid]
            queue = queues[cfg.queue_id_for(sid)]
            next_qid = cfg.downstream_queue_for(sid)
            room = queues[next_qid].headroom(self.settings.buffer_cap_for(next_qid)) if next_qid else math.inf

            cap_before = station_cap_sum[sid]
            if sid == "cnc_thermo":
                produced = self._run_thermo_cncs(queue, room, cnc_min_per_m2[sid], thermo_lanes,
                                                 cnc_minutes_by_class, station_cap_sum, lane_stats)
            elif sid in cfg.CNC_STATION_IDS:
                machines_manned = min(ops, station.num_machines)
                available_minutes = machines_manned * 60.0 * cfg.CNC_UTILISATION
                station_cap_sum[sid] += available_minutes  # minutes, not m2 - see utilisation
                if any(math.isinf(v) for v in cnc_min_per_m2[sid].values()):
                    available_minutes = 0.0                # this CNC has been set to zero capacity
                produced = queue.consume_cnc(available_minutes, room, cnc_min_per_m2[sid])
                for b in produced:
                    cnc_minutes_by_class[b.product_class] += b.qty * cnc_min_per_m2[sid][b.product_class]
            elif sid in cfg.PRESS_STATIONS:
                # The two presses share one pile and share the work in
                # proportion to their capacity this hour (processing one
                # "first" would let it hog the pile and make the other look
                # idle). Both are handled when the first is reached.
                if sid != cfg.PRESS_STATIONS[0]:
                    continue
                caps = {p: self._flat_capacity_m2_per_hour(cfg.STATIONS[p], ops_per_station[p], op_hour_rate[p])
                        for p in cfg.PRESS_STATIONS}
                # Batch running: wait for the pile to build, then run it down.
                start, stop = self.settings.press_batch_start_m2, self.settings.press_batch_stop_m2
                pile = queue.total_m2()
                if start > 0:
                    if not self._press_running and pile >= start:
                        self._press_running = True
                    elif self._press_running and pile <= stop:
                        self._press_running = False
                # Capacity is counted whether or not the presses choose to run
                # this hour - waiting for a pile is a policy, not a capacity
                # limit, so utilisation stays honest (idle time shows as such).
                for p, c in caps.items():
                    station_cap_sum[p] += c
                if not self._press_running and sum(caps.values()) > EPS_M2:
                    if held_now is not None:
                        held_now.update(p for p, c in caps.items() if c > EPS_M2)
                    caps = {p: 0.0 for p in caps}
                total_cap = sum(caps.values())
                pulled = queue.consume_flat_rate(min(total_cap, room)) if total_cap > EPS_M2 else []
                made_all = sum(b.qty for b in pulled)
                for p, c in caps.items():
                    share = c / total_cap if total_cap > EPS_M2 else 0.0
                    out[p] = [b.slice(b.qty * share) for b in pulled] if share > 0 else []
                    station_out_sum[p] += made_all * share
                    if util_now is not None:
                        util_now[p] = min(1.0, made_all * share / c) if c > EPS_M2 else 0.0
                    if p not in out:
                        out[p] = []
                if blocked_now is not None and room < math.inf and made_all >= room - EPS_M2 and queue.total_m2() > EPS_M2:
                    blocked_now.update(p for p, c in caps.items() if c > EPS_M2)
                for b in pulled:
                    queues[next_qid].add(b)
                continue
            else:
                capacity = self._flat_capacity_m2_per_hour(station, ops, op_hour_rate[sid])
                station_cap_sum[sid] += capacity
                produced = queue.consume_flat_rate(min(capacity, room))

            out[sid] = produced
            made = sum(b.qty for b in produced)
            station_out_sum[sid] += made
            if util_now is not None:
                # share of this hour's capacity actually used (minutes for CNCs, m2 otherwise)
                cap_this_hour = station_cap_sum[sid] - cap_before
                used = (sum(b.qty * cnc_min_per_m2[sid][b.product_class] for b in produced)
                        if sid in cfg.CNC_STATION_IDS else made)
                util_now[sid] = min(1.0, used / cap_this_hour) if cap_this_hour > EPS_M2 else 0.0
            # Blocked = downstream space, not capacity or work, limited the output.
            if blocked_now is not None and next_qid is not None and room < math.inf \
                    and made >= room - EPS_M2 and queue.total_m2() > EPS_M2:
                blocked_now.add(sid)
            if next_qid is not None:
                for b in produced:
                    queues[next_qid].add(b)
        return out

    @staticmethod
    def _run_thermo_cncs(queue, room, min_per_m2, thermo_lanes, cnc_minutes_by_class,
                         station_cap_sum, lane_stats) -> list[Batch]:
        """The Thermo CNCs as two lanes. C6 cuts specials and remakes first,
        the other machines cut regular work first; whatever minutes a lane has
        left over go to the other kind of work, so nothing sits idle while
        there is work of any kind waiting."""
        c6_minutes, main_minutes = thermo_lanes
        down = any(math.isinf(v) for v in min_per_m2.values())   # capacity set to zero in the UI
        station_cap_sum["cnc_thermo"] += c6_minutes + main_minutes
        if lane_stats is not None:
            lane_stats["c6_cap"] += c6_minutes
            lane_stats["main_cap"] += main_minutes
        if down:
            return []

        def is_special_work(b):
            return b.is_remake or b.is_special

        def cost(batches):
            return sum(b.qty * min_per_m2[b.product_class] for b in batches)

        produced: list[Batch] = []
        room_left = room
        # 1. each lane takes its own kind of work first
        c6_first = queue.consume_cnc(c6_minutes, room_left, min_per_m2, wants=is_special_work)
        room_left -= sum(b.qty for b in c6_first)
        main_first = queue.consume_cnc(main_minutes, room_left, min_per_m2, wants=lambda b: not is_special_work(b))
        room_left -= sum(b.qty for b in main_first)
        # 2. leftover minutes cross over
        c6_left = c6_minutes - cost(c6_first)
        main_left = main_minutes - cost(main_first)
        c6_over = queue.consume_cnc(c6_left, room_left, min_per_m2, wants=lambda b: not is_special_work(b))
        room_left -= sum(b.qty for b in c6_over)
        main_over = queue.consume_cnc(main_left, room_left, min_per_m2, wants=is_special_work)

        on_c6 = c6_first + c6_over
        on_main = main_first + main_over
        produced = on_c6 + on_main
        for b in produced:
            cnc_minutes_by_class[b.product_class] += b.qty * min_per_m2[b.product_class]
        if lane_stats is not None:
            lane_stats["c6_used"] += cost(on_c6)
            lane_stats["main_used"] += cost(on_main)
            for b in produced:
                m = b.qty * min_per_m2[b.product_class]
                if b.is_remake:
                    lane_stats["remake_min_total"] += m
                if b.is_special:
                    lane_stats["special_min_total"] += m
            for b in on_c6:
                m = b.qty * min_per_m2[b.product_class]
                if b.is_remake:
                    lane_stats["remake_min_on_c6"] += m
                if b.is_special:
                    lane_stats["special_min_on_c6"] += m
        return produced

    # -----------------------------------------------------------------
    # KPIs
    # -----------------------------------------------------------------

    @staticmethod
    def _weighted_avg_lead(entries: list[dict]) -> float | None:
        qty = sum(e["qty"] for e in entries)
        if qty <= 0:
            return None
        return sum(e["lead_days"] * e["qty"] for e in entries) / qty

    @staticmethod
    def _daily_stats(completed_log, target_lookup: dict[str, float]) -> list[DailyStat]:
        """Each entry is judged against the target for ITS OWN route, so
        combined and per-route calls both just work."""
        by_day: dict[int, dict] = {}
        for e in completed_log:
            d = by_day.setdefault(e["day"], {"qty": 0.0, "lead_sum": 0.0, "on_time": 0.0})
            d["qty"] += e["qty"]
            d["lead_sum"] += e["lead_days"] * e["qty"]
            if e["lead_days"] <= target_lookup[e["route"]]:
                d["on_time"] += e["qty"]
        return [
            DailyStat(day=d, avg_lead_days=v["lead_sum"] / v["qty"],
                      difot_pct=100.0 * v["on_time"] / v["qty"], qty_m2=v["qty"])
            for d, v in sorted(by_day.items())
        ]

    @staticmethod
    def _lead_and_difot(completed_log, target_lookup: dict[str, float]) -> tuple[float, float]:
        """m2-weighted average lead time, and DIFOT = share of COMPLETED m2
        that met its route's target. This is the same definition the real
        dashboard uses (delivered on time / delivered); work still sitting in
        the factory past its date is reported separately as overdue backlog,
        not folded into DIFOT - it will count as late when it does complete."""
        lead_sum = on_time_sum = qty_sum = 0.0
        for e in completed_log:
            lead_sum += e["lead_days"] * e["qty"]
            qty_sum += e["qty"]
            if e["lead_days"] <= target_lookup[e["route"]]:
                on_time_sum += e["qty"]
        if qty_sum <= 0:
            return 0.0, 0.0
        return lead_sum / qty_sum, 100.0 * on_time_sum / qty_sum

    def _overdue_backlog(self, queues, remake_holding, target_lookup: dict[str, float],
                         now_hour: float, route_filter: str | None = None) -> float:
        """m2 still inside the factory (queues + remake loop + pending
        scheduling) that would already miss its target even if it were
        finished this instant."""
        s = self.settings
        overdue = 0.0
        in_factory = [b for q in queues.values() for b in q.batches] + [b for _, b in remake_holding]
        for b in in_factory:
            route = cfg.PRODUCT_CLASSES[b.product_class].route
            if route_filter and route != route_filter:
                continue
            hyp_lead = s.pre_prod_days + self._days_elapsed(b.created_hour, now_hour, route) + s.post_prod_days
            if hyp_lead > target_lookup[route]:
                overdue += b.qty
        return overdue
