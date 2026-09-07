"""
The hourly-stepping factory simulator.

Design, in one paragraph: time advances one hour at a time. Each hour, fresh
orders arrive (split into product classes by the mix %), each station pulls
work off the front of its queue based on how many operators are actually
allocated to it *this shift* (see allocation.py) and pushes finished work to
the next station in its route. Packed/despatched output splits into "good"
(counted as completed, with its full lead time) and "remade" (held for
remake_days, then re-enters the factory at the start of its own route). All
of this mirrors the original spreadsheet-style tool, but capacity now comes
from real named people with skills and absences instead of abstract sliders.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from plant_sim import config as cfg
from plant_sim.allocation import allocate_shift
from plant_sim.orders import Batch, StationQueue
from plant_sim.staff import Roster


@dataclass
class SimulationSettings:
    horizon: str = "month"                 # "day" | "week" | "month"
    intake_m2_per_day: float = cfg.REAL_DAILY_M2["median"]
    mix_pct: dict[str, float] = field(default_factory=lambda: dict(cfg.DEFAULT_MIX_PCT))
    target_lead_days: float = 10.0
    pre_prod_days: float = 1.5
    post_prod_days: float = 0.5
    shift_schedules: dict[str, cfg.ShiftSchedule] = field(
        default_factory=lambda: {k: cfg.ShiftSchedule(**vars(v))
                                  for k, v in cfg.DEFAULT_SHIFT_SCHEDULES.items()})
    remake_enabled: bool = True
    remake_rate_pct: float = cfg.DEFAULT_REMAKE_RATE_PCT
    remake_days: float = cfg.DEFAULT_REMAKE_DAYS
    sick_enabled: bool = False
    random_seed: int | None = None

    def horizon_hours(self) -> float:
        if self.horizon == "day":
            return 24.0
        if self.horizon == "week":
            return 168.0
        return cfg.WEEKS_PER_MONTH * 168.0


@dataclass
class DailyStat:
    day: int
    avg_lead_days: float
    difot_pct: float
    qty_m2: float


@dataclass
class SimulationResult:
    settings: SimulationSettings
    trace: list[dict]                     # one entry per simulated hour
    daily_stats: list[DailyStat]
    cum_intake_m2: float
    cum_completed_m2: float
    cum_remade_m2: float
    overdue_backlog_m2: float
    overall_avg_lead_days: float
    overall_difot_pct: float
    station_utilisation: dict[str, float]      # 0..1, output / available capacity
    completed_m2_by_class: dict[str, float]
    attendance_log: list[dict]             # one row per (day, operator) - for absenteeism charts

    @property
    def has_completions(self) -> bool:
        return self.cum_completed_m2 > 0


class Simulator:
    def __init__(self, roster: Roster, settings: SimulationSettings):
        self.roster = roster
        self.settings = settings
        self.rng = random.Random(settings.random_seed)

    def run(self) -> SimulationResult:
        s = self.settings
        H = s.horizon_hours()
        steps = round(H)
        total_days = int(H // 24) + 2

        queues: dict[str, StationQueue] = {sid: StationQueue() for sid in cfg.STATIONS}
        remake_holding: list[dict] = []  # {"release_hour", "created_hour", "qty", "cls"}

        # Per-day absence draw, decided once per calendar day and reused for
        # every hour of that day.
        absences_by_day: dict[int, set[str]] = {}

        # Per (day, shift-label) staffing allocation cache - see docstring at
        # the top of this file for why we snapshot rather than reallocate hourly.
        allocation_cache: dict[tuple[int, str], dict[str, str | None]] = {}

        trace: list[dict] = []
        attendance_log: list[dict] = []
        completed_log: list[dict] = []   # {"day", "qty", "lead_days", "cls"}
        station_out_sum = {sid: 0.0 for sid in cfg.STATIONS}
        station_cap_sum = {sid: 0.0 for sid in cfg.STATIONS}
        cnc_minutes_by_class = {c: 0.0 for c in cfg.PRODUCT_CLASSES}
        completed_m2_by_class = {c: 0.0 for c in cfg.PRODUCT_CLASSES}

        cum_intake = cum_completed = cum_remade = 0.0
        intake_per_hour = s.intake_m2_per_day / 24.0

        for h in range(steps):
            day_index = h // 24
            weekday = day_index % 7
            hour_of_day = h % 24

            if day_index not in absences_by_day:
                absences_by_day[day_index] = self.roster.roll_daily_absences(
                    day_index, s.sick_enabled, self.rng)
            absent_ids = absences_by_day[day_index]

            # -- release matured remakes back into their own route's entry station --
            for r in list(remake_holding):
                if r["release_hour"] <= h:
                    entry = cfg.ROUTE_ENTRY_STATION[cfg.PRODUCT_CLASSES[r["cls"]].route]
                    queues[entry].add(Batch(r["created_hour"], r["qty"], r["cls"]))
                    remake_holding.remove(r)

            # -- fresh intake, split by class mix --
            for cls, pct in s.mix_pct.items():
                qty = intake_per_hour * pct / 100.0
                if qty > 1e-9:
                    entry = cfg.ROUTE_ENTRY_STATION[cfg.PRODUCT_CLASSES[cls].route]
                    queues[entry].add(Batch(h, qty, cls))
                cum_intake += qty

            # -- which stations are actually running this hour? --
            active_now = self._active_stations(weekday, hour_of_day)

            # -- staffing allocation snapshot for this shift block --
            shift_label = "day" if hour_of_day < cfg.ALLOCATION_SHIFT_BOUNDARY_HOUR else "aft"
            cache_key = (day_index, shift_label)
            if cache_key not in allocation_cache:
                available_ops = [op for op in self.roster.operators
                                  if op.shift == shift_label and op.id not in absent_ids]
                queue_snapshot = {sid: q.total_m2() for sid, q in queues.items()}
                # active_stations for the *snapshot* purpose = anything that
                # could possibly run this shift label, ignoring the exact hour
                active_for_snapshot = self._active_stations_for_shift_label(shift_label)
                assignment = allocate_shift(available_ops, active_for_snapshot, queue_snapshot)
                allocation_cache[cache_key] = assignment
                for op in self.roster.operators:
                    attendance_log.append({
                        "day": day_index, "operator_id": op.id, "operator_name": op.name,
                        "shift": shift_label,
                        "status": "absent" if op.id in absent_ids else
                                  ("working" if op.id in assignment and assignment[op.id] else
                                   ("idle" if op.shift == shift_label else "off_shift")),
                        "station": assignment.get(op.id) if op.id not in absent_ids else None,
                    })
            assignment = allocation_cache[cache_key]

            ops_per_station = self._headcount_per_station(assignment, active_now)

            hour_out = self._process_hour(
                h, queues, ops_per_station, active_now,
                cnc_minutes_by_class, station_out_sum, station_cap_sum)

            # -- packing/despatch output -> completed or remade --
            for batch in hour_out.get("despatch", []):
                good_qty = batch.qty
                bad_qty = 0.0
                if s.remake_enabled:
                    bad_qty = batch.qty * (s.remake_rate_pct / 100.0)
                    good_qty = batch.qty - bad_qty
                if good_qty > 1e-9:
                    lead_days = s.pre_prod_days + (h - batch.created_hour) / 24.0 + s.post_prod_days
                    completed_log.append({"day": day_index, "qty": good_qty,
                                           "lead_days": lead_days, "cls": batch.product_class})
                    completed_m2_by_class[batch.product_class] += good_qty
                    cum_completed += good_qty
                if bad_qty > 1e-9:
                    remake_holding.append({"release_hour": h + s.remake_days * 24,
                                            "created_hour": batch.created_hour,
                                            "qty": bad_qty, "cls": batch.product_class})
                    cum_remade += bad_qty

            trace.append({
                "h": h,
                "buf": {sid: q.total_m2() for sid, q in queues.items()},
                "out": {sid: sum(b.qty for b in batches) for sid, batches in hour_out.items()},
                "remake_held": sum(r["qty"] for r in remake_holding),
                "cum_intake": cum_intake, "cum_completed": cum_completed, "cum_remade": cum_remade,
            })

        daily_stats = self._daily_stats(completed_log, s.target_lead_days)
        overall_avg_lead, overall_difot, overdue_backlog = self._overall_metrics(
            completed_log, queues, remake_holding, s.target_lead_days, s.pre_prod_days,
            s.post_prod_days, H)
        # Utilisation = output / available capacity, both in the same units.
        # For CNC stations, capacity was tracked in machine-*minutes* (cycle
        # time depends on product class) rather than m2, so their utilisation
        # has to compare minutes-used to minutes-available, not m2 to minutes.
        utilisation = {}
        for sid in cfg.STATIONS:
            if sid in ("cnc_thermo", "cnc_1536"):
                route = cfg.STATIONS[sid].route
                minutes_used = sum(cnc_minutes_by_class[c] for c, pc in cfg.PRODUCT_CLASSES.items()
                                    if pc.route == route)
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
        )

    # -----------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------

    def _active_stations(self, weekday: int, hour_of_day: float) -> set[str]:
        active = set()
        for sid, crew_name in cfg.STATION_CREW.items():
            sched = self.settings.shift_schedules[crew_name]
            if sched.active_on_weekday(weekday) and sched.shift_at(hour_of_day) is not None:
                active.add(sid)
        return active

    def _active_stations_for_shift_label(self, shift_label: str) -> set[str]:
        """Stations whose crew *could* run during this shift label, any weekday."""
        active = set()
        for sid, crew_name in cfg.STATION_CREW.items():
            sched = self.settings.shift_schedules[crew_name]
            if shift_label == "day" and sched.day_hrs > 0:
                active.add(sid)
            elif shift_label == "aft" and sched.aft_enabled and sched.aft_hrs > 0:
                active.add(sid)
        return active

    @staticmethod
    def _headcount_per_station(assignment: dict[str, str | None],
                                active_now: set[str]) -> dict[str, int]:
        counts = {sid: 0 for sid in cfg.STATIONS}
        for station in assignment.values():
            if station in active_now:
                counts[station] += 1
        return counts

    def _process_hour(self, h, queues, ops_per_station, active_now,
                       cnc_minutes_by_class, station_out_sum, station_cap_sum):
        """Run one simulated hour of production through every station, in
        flow order, and return {station_id: [output batches]}.

        Both routes' last station feeds into the shared "despatch" queue, so
        despatch is processed once at the end, not once per route (it has a
        single, shared headcount - splitting it per-route would double-count
        capacity).
        """
        out: dict[str, list[Batch]] = {}
        despatch_sid = cfg.SHARED_TERMINAL_STATION

        for route, sequence in cfg.ROUTE_SEQUENCE.items():
            for i, sid in enumerate(sequence):
                station = cfg.STATIONS[sid]
                ops = ops_per_station[sid] if sid in active_now else 0
                next_sid = sequence[i + 1] if i + 1 < len(sequence) else despatch_sid
                room = queues[next_sid].headroom(cfg.DEFAULT_BUFFER_CAP_M2)

                if sid in ("cnc_thermo", "cnc_1536"):
                    produced = self._run_cnc(sid, station, ops, room, queues[sid],
                                              cnc_minutes_by_class, station_cap_sum)
                else:
                    capacity = self._flat_capacity_m2_per_hour(sid, station, ops)
                    station_cap_sum[sid] += capacity
                    capacity = min(capacity, room)
                    produced = queues[sid].consume_flat_rate(capacity)

                out[sid] = produced
                station_out_sum[sid] += sum(b.qty for b in produced)
                for b in produced:
                    queues[next_sid].add(b)

        # -- shared despatch/packing station, processed once --
        despatch_station = cfg.STATIONS[despatch_sid]
        despatch_ops = ops_per_station[despatch_sid] if despatch_sid in active_now else 0
        despatch_capacity = self._flat_capacity_m2_per_hour(despatch_sid, despatch_station, despatch_ops)
        station_cap_sum[despatch_sid] += despatch_capacity
        despatch_out = queues[despatch_sid].consume_flat_rate(despatch_capacity)
        out[despatch_sid] = despatch_out
        station_out_sum[despatch_sid] += sum(b.qty for b in despatch_out)

        return out

    @staticmethod
    def _flat_capacity_m2_per_hour(sid: str, station, ops: int) -> float:
        if ops <= 0:
            return 0.0
        if sid == "mb_sander":
            full_rate = station.ideal_ops * station.capacity_m2_per_op_hour
            if ops == 1:
                return full_rate * 0.45  # [ASSUMPTION, from original tool]:
                # one operator can run the MB Sander but only at ~45% of the
                # two-operator (infeed+outfeed) rate.
            return full_rate * min(1.0, ops / station.ideal_ops)
        return ops * station.capacity_m2_per_op_hour

    def _run_cnc(self, sid, station, ops, room, queue, cnc_minutes_by_class, station_cap_sum):
        machines_manned = min(ops, station.num_machines)
        available_minutes = machines_manned * 60.0 * cfg.CNC_UTILISATION
        station_cap_sum[sid] += available_minutes  # minutes, not m2 - utilisation is minutes-based for CNC
        route = station.route
        min_per_m2 = {
            cls: (pc.cnc_min_per_board + cfg.CNC_SETUP_MIN_PER_BOARD) / cfg.BOARD_M2
            for cls, pc in cfg.PRODUCT_CLASSES.items() if pc.route == route
        }
        produced = queue.consume_cnc(available_minutes, room, min_per_m2)
        for b in produced:
            cnc_minutes_by_class[b.product_class] += b.qty * min_per_m2[b.product_class]
        return produced

    def _daily_stats(self, completed_log, target_lead_days) -> list[DailyStat]:
        by_day: dict[int, dict] = {}
        for e in completed_log:
            d = by_day.setdefault(e["day"], {"qty": 0.0, "lead_sum": 0.0, "on_time": 0.0})
            d["qty"] += e["qty"]
            d["lead_sum"] += e["lead_days"] * e["qty"]
            if e["lead_days"] <= target_lead_days:
                d["on_time"] += e["qty"]
        return [
            DailyStat(day=d, avg_lead_days=v["lead_sum"] / v["qty"],
                      difot_pct=100.0 * v["on_time"] / v["qty"], qty_m2=v["qty"])
            for d, v in sorted(by_day.items())
        ]

    def _overall_metrics(self, completed_log, queues, remake_holding, target_lead_days,
                          pre_prod_days, post_prod_days, H):
        lead_sum = on_time_sum = qty_sum = 0.0
        for e in completed_log:
            lead_sum += e["lead_days"] * e["qty"]
            qty_sum += e["qty"]
            if e["lead_days"] <= target_lead_days:
                on_time_sum += e["qty"]
        overall_avg_lead = lead_sum / qty_sum if qty_sum > 0 else 0.0

        overdue = 0.0
        for q in queues.values():
            for b in q.batches:
                hyp_lead = pre_prod_days + (H - b.created_hour) / 24.0 + post_prod_days
                if hyp_lead > target_lead_days:
                    overdue += b.qty
        for r in remake_holding:
            hyp_lead = pre_prod_days + (H - r["created_hour"]) / 24.0 + post_prod_days
            if hyp_lead > target_lead_days:
                overdue += r["qty"]

        denom = qty_sum + overdue
        overall_difot = (100.0 * on_time_sum / denom) if denom > 0 else (100.0 if qty_sum > 0 else 0.0)
        return overall_avg_lead, overall_difot, overdue
