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
    mix_pct: dict[str, float] = field(default_factory=lambda: dict(cfg.DEFAULT_MIX_PCT))
    # Separate target lead time per product range - Cut & Clash (1536) quotes
    # 7 days; Thermo defaults to 10 (edit freely).
    target_lead_days: dict[str, float] = field(
        default_factory=lambda: dict(cfg.DEFAULT_TARGET_LEAD_DAYS))
    pre_prod_days: float = 1.5
    post_prod_days: float = 0.5
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

    def __post_init__(self):
        if self.intake_m2_for_horizon is None and self.horizon in cfg.REAL_INTAKE_M2:
            self.intake_m2_for_horizon = cfg.REAL_INTAKE_M2[self.horizon]["combined"]["median"]
        self.validate()

    def validate(self) -> None:
        """Raise ValueError listing everything wrong with these settings."""
        p = []
        if self.horizon not in cfg.HORIZON_DAYS:
            p.append(f"horizon must be one of {list(cfg.HORIZON_DAYS)}, got {self.horizon!r}")
        if self.intake_m2_for_horizon is None or self.intake_m2_for_horizon < 0:
            p.append(f"intake_m2_for_horizon must be >= 0, got {self.intake_m2_for_horizon!r}")
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
    notes: list[str] = field(default_factory=list)   # anything the engine wants the reader to know
    # Who was on which station, per reporting day and shift: "day|label" ->
    # {station_id: [operator names]} - drives the names in the floor playback.
    staffing_by_shift: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    # The C6 lane at the Thermo CNCs (see cfg.CNC_THERMO_SPECIAL_MACHINE):
    # utilisation of C6 vs the other machines, how much of the remake /
    # special work actually got cut on C6, and how often C6 was unmanned.
    cnc_thermo_lanes: dict[str, float] = field(default_factory=dict)

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
        mix = s.normalised_mix()
        if abs(sum(s.mix_pct.values()) - 100.0) > 0.05:
            notes.append(f"Product mix summed to {sum(s.mix_pct.values()):.1f}% and was normalised "
                         "to 100% so the intake total is honoured.")

        # press_1/press_2 don't get their own queue - they're two independently
        # staffed machines pulling from one shared physical pile of work.
        queue_ids = {cfg.queue_id_for(sid) for sid in cfg.STATIONS}
        queues: dict[str, StationQueue] = {qid: StationQueue() for qid in queue_ids}
        # Batches waiting to re-enter the line, as (hour they may re-enter, batch).
        remake_holding: list[tuple[float, Batch]] = []

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
        cnc_minutes_by_class = {c: 0.0 for c in cfg.PRODUCT_CLASSES}
        completed_m2_by_class = {c: 0.0 for c in cfg.PRODUCT_CLASSES}
        # C6-lane bookkeeping (machine-minutes, reporting window only)
        lanes = {"c6_cap": 0.0, "c6_used": 0.0, "main_cap": 0.0, "main_used": 0.0,
                 "remake_min_total": 0.0, "remake_min_on_c6": 0.0,
                 "special_min_total": 0.0, "special_min_on_c6": 0.0,
                 "c6_active_hours": 0.0, "c6_unmanned_hours": 0.0}
        machine_by_op = {op.id: (op.machine if op.home_station == "cnc_thermo" else "")
                         for op in self.roster.operators}

        cum_intake = cum_completed = cum_remade = 0.0
        # Whole-run totals (warm-up included) for the mass-balance self-check.
        intake_all = completed_all = 0.0

        # Resolve each station's user-edited "capacity in m2/month" into what
        # the engine actually consumes: a per-operator-hour rate for flat-rate
        # stations, or a per-class minutes-per-m2 table for the two CNC
        # stations (this keeps the S1/S2/S3 relative cut-time ratios intact
        # while calibrating the absolute level to the capacity you typed in).
        op_hour_rate, cnc_min_per_m2 = self._resolve_capacities()

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
            if is_intake_hour(h) and intake_per_hour > 0:
                for cls, pct in mix.items():
                    qty = intake_per_hour * pct / 100.0
                    if qty > EPS_M2:
                        route = cfg.PRODUCT_CLASSES[cls].route
                        entry = cfg.ROUTE_ENTRY_STATION[route]
                        # Thermo intake is split into special orders (routed to
                        # C6) and regular work; Cut & Clash has no such split.
                        special = qty * s.special_order_pct / 100.0 if route == cfg.Route.THERMO else 0.0
                        if qty - special > EPS_M2:
                            queues[entry].add(Batch(h, qty - special, cls))
                        if special > EPS_M2:
                            queues[entry].add(Batch(h, special, cls, is_special=True))
                        intake_all += qty
                        if recording:
                            cum_intake += qty

            # -- which shift (if any) each station's crew is on right now --
            label_by_station = self._shift_label_by_station(weekday, hour_of_day)
            active_now = {sid for sid, lbl in label_by_station.items() if lbl is not None}

            # -- staffing allocation snapshots, one per shift label per day --
            for label in cfg.SHIFT_LABELS:
                key = (day_index, label)
                if key in allocation_cache or hour_of_day != self._snapshot_hour(weekday, label):
                    continue
                active_for_label = self._active_stations_for_shift(weekday, label)
                available_ops = [op for op in self.roster.operators
                                  if op.shift == label and op.id not in absent_ids]
                queue_snapshot = {sid: queues[cfg.queue_id_for(sid)].total_m2() for sid in cfg.STATIONS}
                assignment = allocate_shift(available_ops, active_for_label, queue_snapshot)
                allocation_cache[key] = assignment
                if recording:
                    names_by_station: dict[str, list[str]] = {}
                    for op in self.roster.operators:
                        if op.shift != label:
                            continue
                        station = assignment.get(op.id) if op.id not in absent_ids else None
                        if station:
                            names_by_station.setdefault(station, []).append(op.name)
                        attendance_log.append({
                            "day": record_day_index, "operator_id": op.id, "operator_name": op.name,
                            "shift": label,
                            "status": self._attendance_status(op, weekday, label, absent_ids, assignment),
                            "station": station,
                        })
                    staffing_by_shift[f"{record_day_index}|{label}"] = names_by_station

            ops_per_station = self._headcount_per_station(allocation_cache, day_index, label_by_station)
            cnc_label = label_by_station["cnc_thermo"]
            cnc_tags = ([machine_by_op.get(oid, "") for oid, st in
                         allocation_cache.get((day_index, cnc_label), {}).items() if st == "cnc_thermo"]
                        if cnc_label else [])
            c6_minutes, main_minutes = self._cnc_thermo_lanes(cnc_tags)
            if recording and cnc_label:
                lanes["c6_active_hours"] += 1
                if c6_minutes <= 0:
                    lanes["c6_unmanned_hours"] += 1

            # -- diagnostics: is anyone there, is there anything to do? --
            if recording:
                for sid in active_now:
                    station_active_hours[sid] += 1
                    waiting = queues[cfg.queue_id_for(sid)].total_m2()
                    if ops_per_station[sid] == 0 and waiting > EPS_M2:
                        station_unstaffed_hours[sid] += 1
                    elif ops_per_station[sid] > 0 and waiting <= EPS_M2:
                        station_starved_hours[sid] += 1

            hour_out = self._process_hour(queues, ops_per_station, op_hour_rate, cnc_min_per_m2,
                                          cnc_minutes_by_class, station_out_sum, station_cap_sum,
                                          (c6_minutes, main_minutes), lanes if recording else None)

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
                    remake_holding.append((h + s.remake_days * 24.0, remake))
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
                    "cum_intake": cum_intake, "cum_completed": cum_completed, "cum_remade": cum_remade,
                })

        # -- mass balance: nothing is ever created or lost inside the engine --
        wip = sum(q.total_m2() for q in queues.values()) + sum(b.qty for _, b in remake_holding)
        imbalance = intake_all - (completed_all + wip)
        if abs(imbalance) > 1e-6 * max(1.0, intake_all):
            raise RuntimeError(f"Simulation mass balance broken: intake {intake_all:.6f} != "
                               f"completed {completed_all:.6f} + WIP {wip:.6f} (diff {imbalance:.6g})")

        # Combined metrics: every completed/backlogged item is judged against
        # its OWN product range's target lead time (Thermo vs Cut & Clash),
        # then pooled together for the headline KPIs.
        daily_stats = self._daily_stats(completed_log, s.target_lead_days)
        overall_avg_lead, overall_difot = self._lead_and_difot(completed_log, s.target_lead_days)
        overdue_backlog = self._overdue_backlog(queues, remake_holding, s.target_lead_days, end_hour)

        # Per-route breakdown - same calculations, filtered to one route at a
        # time, so Thermo and Cut & Clash each get their own DIFOT/lead-time
        # judged against their own target.
        by_route: dict[str, RouteMetrics] = {}
        for route, label in ROUTE_LABELS.items():
            route_log = [e for e in completed_log if e["route"] == route]
            r_avg_lead, r_difot = self._lead_and_difot(route_log, s.target_lead_days)
            r_overdue = self._overdue_backlog(queues, remake_holding, s.target_lead_days, end_hour,
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

    @classmethod
    def _working_days_elapsed(cls, start_hour: float, end_hour: float) -> float:
        if end_hour <= start_hour:
            return 0.0
        return (cls._working_hours_before(end_hour) - cls._working_hours_before(start_hour)) / 24.0

    # -----------------------------------------------------------------
    # Shifts and staffing
    # -----------------------------------------------------------------

    def _shift_label_by_station(self, weekday: int, hour_of_day: float) -> dict[str, str | None]:
        """Which shift each station's crew is on at this hour ('day', 'aft',
        or None if that crew isn't running)."""
        out = {}
        for sid, crew_name in cfg.STATION_CREW.items():
            sched = self.settings.shift_schedules[crew_name]
            out[sid] = sched.shift_at(hour_of_day) if sched.active_on_weekday(weekday) else None
        return out

    def _active_stations_for_shift(self, weekday: int, shift_label: str) -> set[str]:
        """Stations whose crew runs this shift label on this weekday."""
        active = set()
        for sid, crew_name in cfg.STATION_CREW.items():
            sched = self.settings.shift_schedules[crew_name]
            if sched.active_on_weekday(weekday) and sched.offers_shift(shift_label):
                active.add(sid)
        return active

    def _snapshot_hour(self, weekday: int, shift_label: str) -> int:
        """The hour-of-day at which this day's allocation for `shift_label`
        is decided: when the first crew starts that shift. If no crew runs
        that shift today at all, it's decided at hour 0 (everyone on it is
        simply logged as idle / on a day off)."""
        starts = [
            self.settings.shift_schedules[crew].shift_start_hour(shift_label)
            for crew in set(cfg.STATION_CREW.values())
            if (self.settings.shift_schedules[crew].active_on_weekday(weekday)
                and self.settings.shift_schedules[crew].offers_shift(shift_label))
        ]
        return int(math.floor(min(starts))) if starts else 0

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
    def _headcount_per_station(allocation_cache, day_index: int,
                                label_by_station: dict[str, str | None]) -> dict[str, int]:
        """How many people are on each station THIS hour: read from the
        snapshot for whichever shift that station's own crew is on."""
        counts = {sid: 0 for sid in cfg.STATIONS}
        for sid, label in label_by_station.items():
            if label is None:
                continue
            assignment = allocation_cache.get((day_index, label))
            if not assignment:
                continue
            counts[sid] = sum(1 for st in assignment.values() if st == sid)
        return counts

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
                default_month = cfg.default_cnc_capacity_m2_per_month(station.route)
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
    def _cnc_thermo_lanes(machine_tags: list[str]) -> tuple[float, float]:
        """Machine-minutes available this hour on the C6 lane and on the other
        Thermo CNCs, from the roster machine tags of everyone assigned to
        cnc_thermo. A person tagged to a machine mans that machine; people
        with no tag (floaters / cover) fill unmanned production machines
        first and C6 last - a cover operator is sent to keep production
        going, not to run specials. Two people tagged to one machine still
        only man one machine."""
        station = cfg.STATIONS["cnc_thermo"]
        special = cfg.CNC_THERMO_SPECIAL_MACHINE
        machines = list(station.machines)
        manned = {t for t in machine_tags if t in machines}
        spare = sum(1 for t in machine_tags if t not in machines)            # untagged floaters / cover
        spare += sum(max(0, machine_tags.count(m) - 1) for m in manned)      # 2nd person on one machine
        fill_order = [m for m in machines if m != special] + [special]
        for m in fill_order:
            if spare <= 0:
                break
            if m not in manned:
                manned.add(m)
                spare -= 1
        per_machine = 60.0 * cfg.CNC_UTILISATION
        c6 = per_machine if special in manned else 0.0
        main = per_machine * len([m for m in manned if m != special])
        return c6, main

    def _process_hour(self, queues, ops_per_station, op_hour_rate, cnc_min_per_m2,
                       cnc_minutes_by_class, station_out_sum, station_cap_sum,
                       thermo_lanes=(0.0, 0.0), lane_stats=None) -> dict[str, list[Batch]]:
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
            room = queues[next_qid].headroom(self.settings.buffer_cap_m2) if next_qid else math.inf

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
            else:
                capacity = self._flat_capacity_m2_per_hour(station, ops, op_hour_rate[sid])
                station_cap_sum[sid] += capacity
                produced = queue.consume_flat_rate(min(capacity, room))

            out[sid] = produced
            station_out_sum[sid] += sum(b.qty for b in produced)
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
        """m2 still inside the factory (queues + remake loop) that would
        already miss its target even if it were finished this instant."""
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
