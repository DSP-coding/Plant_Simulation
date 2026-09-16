"""
Regression tests for the simulation engine. Run from the project root with:

    python -m unittest discover -s tests -v

Only the standard library is needed (no pytest). Each test pins down one
piece of engine behaviour that has bitten before, or that a future edit to
config.py / simulation.py could quietly break: mass balance, intake timing,
working-day arithmetic, queue ordering, per-crew shift handover, allocator
rules, and roster/settings validation.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from plant_sim import config as cfg                          # noqa: E402
from plant_sim.allocation import allocate_shift              # noqa: E402
from plant_sim.orders import Batch, StationQueue             # noqa: E402
from plant_sim.simulation import Simulator, SimulationSettings  # noqa: E402
from plant_sim.staff import Operator, Roster, RosterError    # noqa: E402
from plant_sim.floor_editor import apply_move, restore, snapshot  # noqa: E402

ROSTER_CSV = PROJECT_ROOT / "data" / "staff_roster.csv"


def real_roster() -> Roster:
    return Roster.from_csv(ROSTER_CSV)


def tiny_roster() -> Roster:
    """One qualified person on every station, day and afternoon."""
    ops = []
    for sid in cfg.STATIONS:
        if sid == "admin":
            continue
        for shift in cfg.SHIFT_LABELS:
            ops.append(Operator(id=f"{sid}_{shift}", name=f"{sid} {shift}", home_station=sid, shift=shift))
    return Roster(ops)


def settings(**overrides) -> SimulationSettings:
    base = dict(horizon="week", warmup_days=0, sick_enabled=False, random_seed=1)
    base.update(overrides)
    return SimulationSettings(**base)


# ---------------------------------------------------------------------------
# config.py
# ---------------------------------------------------------------------------

class ConfigTests(unittest.TestCase):
    def test_config_is_self_consistent(self):
        cfg.validate_config()   # raises if any table is inconsistent

    def test_default_mix_sums_to_100_with_real_route_split(self):
        self.assertAlmostEqual(sum(cfg.DEFAULT_MIX_PCT.values()), 100.0, places=9)
        thermo = sum(v for c, v in cfg.DEFAULT_MIX_PCT.items()
                     if cfg.PRODUCT_CLASSES[c].route == cfg.Route.THERMO)
        self.assertAlmostEqual(thermo, 80.6, places=6)

    def test_processing_order_is_downstream_first(self):
        order = cfg.PROCESSING_ORDER
        for sid in order:
            downstream = cfg.downstream_queue_for(sid)
            if downstream is None or downstream == cfg.PRESS_QUEUE_ID:
                continue
            self.assertLess(order.index(downstream), order.index(sid),
                            f"{downstream} must be processed before {sid}")
        for press in cfg.PRESS_STATIONS:
            self.assertLess(order.index(press), order.index("edging"))

    def test_press_stations_share_one_queue(self):
        self.assertEqual(cfg.queue_id_for("press_1"), cfg.queue_id_for("press_2"))
        self.assertEqual(cfg.queue_id_for("sanding"), "sanding")

    def test_shift_schedule_rejects_impossible_values(self):
        with self.assertRaises(ValueError):
            cfg.ShiftSchedule(days_per_week=8)
        with self.assertRaises(ValueError):
            cfg.ShiftSchedule(day_hrs=20, aft_enabled=True, aft_hrs=10)
        with self.assertRaises(ValueError):
            cfg.ShiftSchedule(day_hrs=-1)
        s = cfg.ShiftSchedule(days_per_week=4, day_hrs=10, aft_enabled=True, aft_hrs=10)
        self.assertEqual(s.shift_at(0), "day")
        self.assertEqual(s.shift_at(9.99), "day")
        self.assertEqual(s.shift_at(10), "aft")
        self.assertIsNone(s.shift_at(20))
        self.assertTrue(s.active_on_weekday(3))
        self.assertFalse(s.active_on_weekday(4))

    def test_max_useful_ops(self):
        self.assertEqual(cfg.STATIONS["cnc_1536"].max_useful_ops, 1)
        self.assertEqual(cfg.STATIONS["cnc_thermo"].max_useful_ops, cfg.STATIONS["cnc_thermo"].num_machines)
        self.assertEqual(cfg.STATIONS["mb_sander"].max_useful_ops, 1)   # one-person machine (shop-floor sheet)
        self.assertIsNone(cfg.STATIONS["hafele"].max_useful_ops)
        self.assertIsNone(cfg.STATIONS["sanding"].max_useful_ops)

    def test_default_capacity_roundtrip(self):
        # Typing the default capacity back in must reproduce the config rate.
        for sid, station in cfg.STATIONS.items():
            if sid == "admin" or sid in cfg.CNC_STATION_IDS:
                continue
            cap = cfg.default_station_capacity_m2_per_month(sid)
            rate = cap / (station.ideal_ops * cfg.REFERENCE_HOURS_PER_MONTH)
            self.assertAlmostEqual(rate, station.capacity_m2_per_op_hour, places=9)


# ---------------------------------------------------------------------------
# orders.py
# ---------------------------------------------------------------------------

class QueueTests(unittest.TestCase):
    def test_oldest_order_first_even_if_added_later(self):
        q = StationQueue()
        q.add(Batch(10, 5, "S1"))
        q.add(Batch(20, 5, "S1"))
        q.add(Batch(5, 5, "S1", is_remake=True))   # older order arriving late (a remake)
        self.assertEqual([b.created_hour for b in q.batches], [5, 10, 20])
        out = q.consume_flat_rate(7)
        self.assertEqual([(b.created_hour, b.qty) for b in out], [(5, 5), (10, 2)])
        self.assertTrue(out[0].is_remake)
        self.assertAlmostEqual(q.total_m2(), 8)

    def test_same_age_batches_stay_fifo(self):
        q = StationQueue()
        q.add(Batch(3, 1, "S1"))
        q.add(Batch(3, 1, "S3"))
        q.add(Batch(3, 1, "Melamine"))
        self.assertEqual([b.product_class for b in q.batches], ["S1", "S3", "Melamine"])

    def test_zero_and_negative_batches_are_ignored(self):
        q = StationQueue()
        q.add(Batch(0, 0.0, "S1"))
        q.add(Batch(0, -1.0, "S1"))
        self.assertEqual(len(q), 0)

    def test_cnc_consumption_respects_minutes_and_room(self):
        q = StationQueue()
        q.add(Batch(0, 10, "S1"))
        q.add(Batch(1, 10, "S3"))
        rates = {"S1": 1.0, "S3": 4.0}   # minutes per m2
        out = q.consume_cnc(available_minutes=18, room_m2=100, min_per_m2_by_class=rates)
        # 10 m2 of S1 costs 10 min, leaving 8 min = 2 m2 of S3.
        self.assertEqual([(b.product_class, b.qty) for b in out], [("S1", 10), ("S3", 2)])
        out = q.consume_cnc(available_minutes=1000, room_m2=3, min_per_m2_by_class=rates)
        self.assertEqual([(b.product_class, b.qty) for b in out], [("S3", 3)])
        self.assertAlmostEqual(q.total_m2(), 5)


# ---------------------------------------------------------------------------
# staff.py
# ---------------------------------------------------------------------------

class RosterTests(unittest.TestCase):
    def test_real_roster_loads_and_validates(self):
        r = real_roster()
        self.assertGreater(len(r.operators), 30)
        r.validate()

    def test_validation_lists_every_problem(self):
        r = Roster([
            Operator("1", "A", "nowhere", shift="day"),
            Operator("1", "B", "sanding", shift="night", skills={"bogus"}, absence_rate_pct=150),
        ])
        with self.assertRaises(RosterError) as ctx:
            r.validate()
        msg = str(ctx.exception)
        for needle in ("unknown home station", "duplicate operator id", "shift must be", "bogus", "0-100"):
            self.assertIn(needle, msg)

    def test_csv_round_trip_and_bad_file(self):
        r = real_roster()
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "roster.csv"
            r.to_csv(p)
            r2 = Roster.from_csv(p)
            self.assertEqual([(o.id, o.name, o.home_station, o.shift, sorted(o.skills), o.absence_rate_pct)
                              for o in r.operators],
                             [(o.id, o.name, o.home_station, o.shift, sorted(o.skills), o.absence_rate_pct)
                              for o in r2.operators])
            self.assertFalse((Path(d) / "roster.csv.tmp").exists())
            p.write_text("id,name,home_station,shift\n7,Someone,not_a_station,day\n", encoding="utf-8")
            with self.assertRaises(RosterError):
                Roster.from_csv(p)
            with self.assertRaises(RosterError):
                Roster.from_csv(Path(d) / "missing.csv")

    def test_add_rejects_duplicate_id(self):
        r = Roster([Operator("1", "A", "sanding")])
        with self.assertRaises(RosterError):
            r.add(Operator("1", "B", "sanding"))


# ---------------------------------------------------------------------------
# allocation.py
# ---------------------------------------------------------------------------

class AllocationTests(unittest.TestCase):
    def test_everyone_starts_at_home(self):
        ops = [Operator("a", "A", "sanding"), Operator("b", "B", "despatch")]
        a = allocate_shift(ops, {"sanding", "despatch"}, {})
        self.assertEqual(a, {"a": "sanding", "b": "despatch"})

    def test_idle_when_home_station_not_running_and_no_skills(self):
        ops = [Operator("a", "A", "sanding")]
        a = allocate_shift(ops, {"despatch"}, {})
        self.assertEqual(a, {"a": None})

    def test_floater_goes_to_neediest_qualified_station(self):
        ops = [Operator("a", "A", "sanding", skills={"despatch", "eb_drilling"})]
        a = allocate_shift(ops, {"despatch", "eb_drilling"}, {"despatch": 10, "eb_drilling": 500})
        self.assertEqual(a["a"], "eb_drilling")

    def test_press_pile_is_visible_to_the_allocator(self):
        # Two people on sanding (quiet), one of them can press. The shared
        # press pile is huge but press_1 has nobody - they must be moved.
        ops = [Operator("a", "A", "sanding", skills={"press_1"}), Operator("b", "B", "sanding"),
               Operator("c", "C", "press_2")]
        queue = {"sanding": 0.0, "press_1": 2000.0, "press_2": 2000.0}
        a = allocate_shift(ops, {"sanding", "press_1", "press_2"}, queue)
        self.assertEqual(a["a"], "press_1")
        self.assertEqual(a["b"], "sanding")

    def test_never_exceeds_machine_headcount(self):
        # Two people whose home is the single-machine CNC 1536: the surplus
        # one should be moved to help a drowning station they can cover.
        ops = [Operator("a", "A", "cnc_1536", skills={"eb_drilling"}),
               Operator("b", "B", "cnc_1536", skills={"eb_drilling"}),
               Operator("c", "C", "eb_drilling")]
        queue = {"cnc_1536": 50.0, "eb_drilling": 900.0}
        a = allocate_shift(ops, {"cnc_1536", "eb_drilling"}, queue)
        self.assertEqual(sum(1 for s in a.values() if s == "cnc_1536"), 1)
        self.assertEqual(sum(1 for s in a.values() if s == "eb_drilling"), 2)

    def test_floater_not_parked_on_a_full_machine(self):
        ops = [Operator("a", "A", "cnc_1536"), Operator("b", "B", "sanding", skills={"cnc_1536"})]
        a = allocate_shift(ops, {"cnc_1536"}, {"cnc_1536": 5000.0})
        self.assertEqual(a["a"], "cnc_1536")
        self.assertIsNone(a["b"])   # machine already manned - standing beside it helps nobody

    def test_never_strips_a_station_to_zero(self):
        ops = [Operator("a", "A", "sanding", skills={"despatch"}), Operator("b", "B", "despatch")]
        a = allocate_shift(ops, {"sanding", "despatch"}, {"sanding": 0.0, "despatch": 5000.0})
        self.assertEqual(a["a"], "sanding")


# ---------------------------------------------------------------------------
# simulation.py
# ---------------------------------------------------------------------------

class TimeTests(unittest.TestCase):
    wd = staticmethod(Simulator._working_days_elapsed)

    def test_working_days_are_exact(self):
        self.assertAlmostEqual(self.wd(0, 168), 5.0)          # Mon 0h -> next Mon 0h
        self.assertAlmostEqual(self.wd(4 * 24 + 12, 5 * 24 + 12), 0.5)   # Fri noon -> Sat noon
        self.assertAlmostEqual(self.wd(5 * 24, 7 * 24), 0.0)  # whole weekend
        self.assertAlmostEqual(self.wd(4 * 24, 7 * 24), 1.0)  # Fri -> Mon
        self.assertAlmostEqual(self.wd(6 * 24 + 12, 7 * 24 + 6), 0.25)   # Sun noon -> Mon 6h
        self.assertAlmostEqual(self.wd(10, 10), 0.0)
        self.assertAlmostEqual(self.wd(20, 10), 0.0)

    def test_intake_hours_are_weekday_office_hours(self):
        f = Simulator._is_intake_hour
        self.assertTrue(f(0))
        self.assertTrue(f(int(cfg.INTAKE_WINDOW_HOURS[1]) - 1))
        self.assertFalse(f(int(cfg.INTAKE_WINDOW_HOURS[1])))
        self.assertFalse(f(5 * 24 + 2))   # Saturday
        self.assertFalse(f(6 * 24 + 2))   # Sunday
        self.assertTrue(f(7 * 24 + 2))    # next Monday


class EngineTests(unittest.TestCase):
    def test_default_intake_follows_horizon(self):
        for horizon in cfg.HORIZON_DAYS:
            s = SimulationSettings(horizon=horizon)
            self.assertEqual(s.intake_m2_for_horizon, cfg.REAL_INTAKE_M2[horizon]["combined"]["median"])

    def test_intake_total_is_honoured_exactly(self):
        for horizon, intake in [("day", 300.0), ("week", 1600.0), ("month", 7300.0)]:
            r = Simulator(tiny_roster(), settings(horizon=horizon, intake_m2_for_horizon=intake)).run()
            self.assertAlmostEqual(r.cum_intake_m2, intake, places=6, msg=horizon)

    def test_mix_is_normalised_so_intake_is_honoured(self):
        mix = {"S1": 30.0, "Melamine": 30.0}   # sums to 60, not 100
        r = Simulator(tiny_roster(), settings(intake_m2_for_horizon=1000.0, mix_pct=mix)).run()
        self.assertAlmostEqual(r.cum_intake_m2, 1000.0, places=6)
        self.assertTrue(any("normalised" in n for n in r.notes))

    def test_no_intake_on_weekends(self):
        r = Simulator(tiny_roster(), settings(horizon="week")).run()
        by_hour = {t["h"]: t["cum_intake"] for t in r.trace}
        self.assertEqual(by_hour[5 * 24 - 1], by_hour[7 * 24 - 1])   # Fri end == Sun end
        self.assertGreater(by_hour[5 * 24 - 1], 0)

    def test_mass_balance_holds_with_remakes_and_sick_leave(self):
        # The engine raises RuntimeError itself if intake != completed + WIP;
        # run the stressful combination to make sure it never does.
        r = Simulator(real_roster(), settings(horizon="month", sick_enabled=True, random_seed=7,
                                              remake_rate_pct=25.0, remake_days=0.0)).run()
        self.assertGreater(r.cum_completed_m2, 0)

    def test_parts_need_one_hour_per_station(self):
        # Shortest route is Cut & Clash: optimising, cnc_1536, eb_drilling,
        # despatch = 4 stations, so nothing can complete before hour 3.
        r = Simulator(tiny_roster(), settings(horizon="day", mix_pct={"Melamine": 100.0})).run()
        self.assertEqual(r.trace[0]["cum_completed"], 0.0)
        self.assertEqual(r.trace[2]["cum_completed"], 0.0)
        self.assertGreater(r.trace[3]["cum_completed"], 0.0)

    def test_per_crew_shift_handover(self):
        # Cut & Clash day shift is 8h; Thermo/finishing day shifts are 10h.
        # At hour 8 cutclash stations must already be on 'aft' while thermo
        # stations are still on 'day', and cnc_1536's headcount must come
        # from the AFTERNOON allocation (the day person has gone home).
        roster = Roster([
            Operator("d", "Day Op", "cnc_1536", shift="day"),
            Operator("a", "Aft Op", "cnc_1536", shift="aft"),
            Operator("t", "Thermo", "sanding", shift="day"),
        ])
        r = Simulator(roster, settings(horizon="day")).run()
        t8 = r.trace[8]
        self.assertEqual(t8["shift"]["cnc_1536"], "aft")
        self.assertEqual(t8["shift"]["sanding"], "day")
        self.assertEqual(t8["ops"]["cnc_1536"], 1)
        self.assertEqual(r.trace[7]["ops"]["cnc_1536"], 1)
        self.assertEqual(r.trace[16]["ops"]["cnc_1536"], 0)   # aft finished at 8+8
        self.assertEqual(r.trace[10]["ops"]["sanding"], 0)     # thermo day shift over

    def test_thermo_crew_floats_to_cut_and_clash_on_its_day_off(self):
        # Thermo crews run 4 days; on Friday a cross-skilled Thermo CNC
        # operator should end up on CNC 1536, not sit idle.
        roster = Roster([Operator("j", "Jay", "cnc_thermo", shift="day", skills={"cnc_1536"})])
        r = Simulator(roster, settings(horizon="week", mix_pct={"Melamine": 100.0})).run()
        friday_noon = r.trace[4 * 24 + 4]
        self.assertEqual(friday_noon["ops"]["cnc_1536"], 1)
        fri_rows = [a for a in r.attendance_log if a["day"] == 4 and a["operator_id"] == "j"]
        self.assertEqual(fri_rows[0]["status"], "working")

    def test_attendance_statuses(self):
        roster = Roster([
            Operator("x", "Admin Aft", "admin", shift="aft"),      # admin has no aft shift -> idle
            Operator("y", "Sander", "sanding", shift="day"),       # off on Fri (4-day crew)
        ])
        r = Simulator(roster, settings(horizon="week")).run()
        by = {(a["day"], a["operator_id"]): a["status"] for a in r.attendance_log}
        self.assertEqual(by[(0, "x")], "idle")
        self.assertEqual(by[(0, "y")], "working")
        self.assertEqual(by[(4, "y")], "day_off")
        # exactly one row per operator per day
        self.assertEqual(len(r.attendance_log), 7 * 2)

    def test_zero_capacity_station_stops_flow_without_crashing(self):
        caps = {sid: cfg.default_station_capacity_m2_per_month(sid) for sid in cfg.STATIONS if sid != "admin"}
        caps["cnc_thermo"] = 0.0
        caps["eb_drilling"] = 0.0
        r = Simulator(tiny_roster(), settings(station_capacity_m2_per_month=caps)).run()
        self.assertEqual(r.cum_completed_m2, 0.0)
        self.assertEqual(r.station_utilisation["cnc_thermo"], 0.0)

    def test_machine_bound_capacity_caps_at_ideal_ops(self):
        rate = 10.0
        mb = cfg.STATIONS["mb_sander"]          # ideal_ops=1, machine_bound
        f = Simulator._flat_capacity_m2_per_hour
        self.assertEqual(f(mb, 0, rate), 0.0)
        self.assertAlmostEqual(f(mb, 1, rate), rate)
        self.assertAlmostEqual(f(mb, 5, rate), rate)   # a second person can't speed the machine up
        cefla = cfg.STATIONS["edging"]           # ideal_ops=2, machine_bound
        self.assertAlmostEqual(f(cefla, 1, rate), rate)
        self.assertAlmostEqual(f(cefla, 3, rate), 2 * rate)
        sanding = cfg.STATIONS["sanding"]
        self.assertAlmostEqual(f(sanding, 5, rate), 5 * rate)   # manual: more hands, more output

    def test_remakes_are_expedited(self):
        r = Simulator(real_roster(), settings(horizon="month", warmup_days=cfg.SIMULATION_WARMUP_DAYS)).run()
        thermo = r.by_route[cfg.Route.THERMO]
        self.assertGreater(thermo.remake_share_pct, 3.0)
        penalty = thermo.remake_lead_penalty_days
        self.assertIsNotNone(penalty)
        # A remake going back to the END of the queue would cost a whole
        # extra lead time (several days); expedited it should cost about
        # the hold time plus a re-pass (well under 2 days).
        self.assertGreater(penalty, 0.0)
        self.assertLess(penalty, 2.0)

    def test_difot_is_share_of_completed(self):
        r = Simulator(real_roster(), settings(horizon="month", warmup_days=cfg.SIMULATION_WARMUP_DAYS)).run()
        for rm in r.by_route.values():
            on_time = sum(d.qty_m2 * d.difot_pct / 100.0 for d in rm.daily_stats)
            total = sum(d.qty_m2 for d in rm.daily_stats)
            if total > 0:
                self.assertAlmostEqual(rm.overall_difot_pct, 100.0 * on_time / total, places=6)

    def test_deterministic_for_a_given_seed(self):
        a = Simulator(real_roster(), settings(horizon="week", sick_enabled=True, random_seed=3)).run()
        b = Simulator(real_roster(), settings(horizon="week", sick_enabled=True, random_seed=3)).run()
        self.assertEqual(a.cum_completed_m2, b.cum_completed_m2)
        self.assertEqual(a.attendance_log, b.attendance_log)

    def test_settings_validation(self):
        with self.assertRaises(ValueError):
            SimulationSettings(horizon="fortnight")
        with self.assertRaises(ValueError):
            SimulationSettings(intake_m2_for_horizon=-1)
        with self.assertRaises(ValueError):
            SimulationSettings(mix_pct={"Plywood": 100.0})
        with self.assertRaises(ValueError):
            SimulationSettings(mix_pct={"S1": 0.0})
        with self.assertRaises(ValueError):
            SimulationSettings(remake_rate_pct=120)
        with self.assertRaises(ValueError):
            SimulationSettings(target_lead_days={cfg.Route.THERMO: 10.0})   # missing a route
        with self.assertRaises(ValueError):
            SimulationSettings(station_capacity_m2_per_month={"sanding": -5})

    def test_simulator_rejects_broken_roster(self):
        with self.assertRaises(RosterError):
            Simulator(Roster([Operator("1", "A", "not_a_station")]), settings())


class FloorEditorTests(unittest.TestCase):
    """The drag-and-drop move rule and undo, independent of the browser."""

    def test_drop_on_station_changes_home_and_keeps_old_home_as_skill(self):
        r = Roster([Operator("j", "Jay", "cnc_thermo", shift="day", skills={"cnc_1536"})])
        msg = apply_move(r, {"seq": 1, "op_id": "j", "to_station": "cnc_1536", "to_shift": "day"})
        op = r.get("j")
        self.assertEqual(op.home_station, "cnc_1536")
        self.assertEqual(op.shift, "day")
        self.assertEqual(op.skills, {"cnc_thermo"})   # old home kept, new home not double-listed
        self.assertIn("CNC (Thermo)", msg)
        r.validate()

    def test_drop_in_other_lane_changes_shift(self):
        r = Roster([Operator("j", "Jay", "cnc_thermo", shift="day")])
        msg = apply_move(r, {"seq": 1, "op_id": "j", "to_station": "cnc_thermo", "to_shift": "aft"})
        self.assertEqual(r.get("j").shift, "aft")
        self.assertIn("day → aft", msg)

    def test_noop_and_invalid_drops(self):
        r = Roster([Operator("j", "Jay", "cnc_thermo", shift="day")])
        self.assertIsNone(apply_move(r, {"op_id": "j", "to_station": "cnc_thermo", "to_shift": "day"}))
        self.assertIsNone(apply_move(r, {"op_id": "nobody", "to_station": "sanding", "to_shift": "day"}))
        self.assertIsNone(apply_move(r, {"op_id": "j", "to_station": "not_a_station", "to_shift": "day"}))
        self.assertIsNone(apply_move(r, {"op_id": "j", "to_station": "sanding", "to_shift": "night"}))
        self.assertEqual(r.get("j").home_station, "cnc_thermo")

    def test_snapshot_restore_undoes_a_move(self):
        r = Roster([Operator("j", "Jay", "cnc_thermo", shift="day", skills={"cnc_1536"})])
        before = snapshot(r)
        apply_move(r, {"op_id": "j", "to_station": "sanding", "to_shift": "aft"})
        self.assertEqual(r.get("j").home_station, "sanding")
        restore(r, before)
        op = r.get("j")
        self.assertEqual((op.home_station, op.shift, op.skills), ("cnc_thermo", "day", {"cnc_1536"}))

    def test_box_packing_lines_are_staffed_but_outside_the_flow(self):
        for sid in ("hafele", "diy", "admin"):
            self.assertFalse(cfg.is_flow_station(sid))
            self.assertNotIn(sid, SimulationSettings().station_capacity_m2_per_month)
        for sid in ("despatch", "cnc_1536", "press_2"):
            self.assertTrue(cfg.is_flow_station(sid))
        roster = Roster([Operator("b", "Ben", "hafele", shift="day", skills={"despatch"})])
        r = Simulator(roster, settings(horizon="day")).run()
        self.assertEqual(r.trace[0]["ops"]["hafele"], 1)
        self.assertEqual(r.station_utilisation["hafele"], 0.0)
        self.assertEqual(r.trace[0]["buf"].get("hafele", 0.0), 0.0)

    def test_box_packers_float_to_despatch_when_it_is_drowning(self):
        ops = [Operator("b", "Ben", "hafele", skills={"despatch"}), Operator("a", "Agnes", "hafele", skills={"despatch"}),
               Operator("s", "Saimone", "despatch")]
        a = allocate_shift(ops, {"hafele", "despatch"}, {"hafele": 0.0, "despatch": 3000.0})
        self.assertEqual(sum(1 for v in a.values() if v == "despatch"), 2)
        self.assertEqual(sum(1 for v in a.values() if v == "hafele"), 1)   # never stripped to zero

    def test_real_roster_matches_the_shop_floor_sheet(self):
        r = real_roster()
        by = {(o.home_station, o.shift): [] for o in r.operators}
        for o in r.operators:
            by[(o.home_station, o.shift)].append(o.name)
        self.assertEqual(len(by[("cnc_1536", "day")]), 1)          # Nirmal
        self.assertEqual(len(by[("cnc_1536", "aft")]), 1)          # Jerald
        self.assertEqual(len(by[("mb_sander", "day")]), 1)         # Quyen
        self.assertEqual(len(by[("mb_sander", "aft")]), 1)         # Viet
        self.assertEqual(len(by[("hafele", "day")]), 2)            # Ben, Agnes
        self.assertEqual(len(by[("diy", "day")]) + len(by[("diy", "aft")]), 2)   # Dayna, Arona
        # 3 home CNC operators per shift + a Press cover each = the 4 machines
        cnc_day = len(by[("cnc_thermo", "day")]) + sum(1 for o in r.operators
                                                        if o.shift == "day" and "cnc_thermo" in o.skills and o.home_station.startswith("press"))
        self.assertEqual(cnc_day, cfg.STATIONS["cnc_thermo"].num_machines)

    def test_result_records_who_was_where(self):
        roster = Roster([Operator("d", "Day Op", "cnc_1536", shift="day"),
                         Operator("a", "Aft Op", "cnc_1536", shift="aft")])
        r = Simulator(roster, settings(horizon="day")).run()
        self.assertEqual(r.staffing_by_shift["0|day"]["cnc_1536"], ["Day Op"])
        self.assertEqual(r.staffing_by_shift["0|aft"]["cnc_1536"], ["Aft Op"])
        self.assertEqual(r.trace[0]["day"], 0)


@unittest.skipUnless(os.environ.get("PLANT_SIM_UI_TESTS", "1") == "1", "UI tests disabled")
class AppSmokeTests(unittest.TestCase):
    """Runs the Streamlit app headlessly and clicks Run simulation."""

    def test_app_runs_without_exceptions(self):
        try:
            from streamlit.testing.v1 import AppTest
        except ImportError:
            self.skipTest("streamlit not installed")
        cwd = os.getcwd()
        os.chdir(PROJECT_ROOT)
        try:
            at = AppTest.from_file("app.py", default_timeout=180)
            at.run()
            self.assertEqual([str(e.value) for e in at.exception], [])
            run_btn = [b for b in at.button if "Run simulation" in b.label][0]
            run_btn.click().run()
            self.assertEqual([str(e.value) for e in at.exception], [])
            labels = [m.label for m in at.metric]
            self.assertIn("DIFOT %", labels)
            self.assertIn("Completion %", labels)
        finally:
            os.chdir(cwd)


if __name__ == "__main__":
    unittest.main(verbosity=2)
