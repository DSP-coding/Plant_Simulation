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
from plant_sim.constraints import analyse  # noqa: E402

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
    # Tests of the floor mechanics run without warm-up and with same-morning
    # scheduling (release_working_days=0), so an order placed at hour 0 is on
    # the CNC at hour 0. The scheduling tests set their own release days.
    base = dict(horizon="week", warmup_days=0, sick_enabled=False, random_seed=1,
                release_working_days={cfg.Route.THERMO: 0, cfg.Route.CUT_AND_CLASH: 0})
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

    def test_cnc_lane_filter_leaves_other_work_in_place(self):
        q = StationQueue()
        q.add(Batch(0, 10, "S1"))
        q.add(Batch(1, 10, "S1", is_remake=True))
        q.add(Batch(2, 10, "S1", is_special=True))
        rates = {"S1": 1.0}
        out = q.consume_cnc(100, 100, rates, wants=lambda b: b.is_remake or b.is_special)
        self.assertEqual([(b.created_hour, b.is_remake, b.is_special) for b in out],
                         [(1, True, False), (2, False, True)])
        self.assertEqual([b.created_hour for b in q.batches], [0])   # regular work untouched, still first
        self.assertEqual([b.created_hour for b in q.consume_cnc(100, 100, rates)], [0])

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
        ops = [Operator("a", "A", "sanding", skills={"despatch", "edge_bander"})]
        a = allocate_shift(ops, {"despatch", "edge_bander"}, {"despatch": 10, "edge_bander": 500})
        self.assertEqual(a["a"], "edge_bander")

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
        ops = [Operator("a", "A", "cnc_1536", skills={"sanding"}),
               Operator("b", "B", "cnc_1536", skills={"sanding"}),
               Operator("c", "C", "sanding")]
        queue = {"cnc_1536": 50.0, "sanding": 900.0}
        a = allocate_shift(ops, {"cnc_1536", "sanding"}, queue)
        self.assertEqual(sum(1 for s in a.values() if s == "cnc_1536"), 1)
        self.assertEqual(sum(1 for s in a.values() if s == "sanding"), 2)

    def test_floater_not_parked_on_a_full_machine(self):
        ops = [Operator("a", "A", "cnc_1536"), Operator("b", "B", "sanding", skills={"cnc_1536"})]
        a = allocate_shift(ops, {"cnc_1536"}, {"cnc_1536": 5000.0})
        self.assertEqual(a["a"], "cnc_1536")
        self.assertIsNone(a["b"])   # machine already manned - standing beside it helps nobody

    def test_absence_is_covered_from_a_station_with_spare_people(self):
        # CNC ideal 4, only 3 present; Despatch (ideal 3) has 5 people, one of
        # whom can run a CNC - they get pulled across.
        ops = [Operator(f"c{i}", f"C{i}", "cnc_thermo") for i in range(3)]
        ops += [Operator(f"d{i}", f"D{i}", "despatch", skills={"cnc_thermo"} if i == 0 else set()) for i in range(5)]
        a = allocate_shift(ops, {"cnc_thermo", "despatch"}, {})
        self.assertEqual(a["d0"], "cnc_thermo")
        self.assertEqual(sum(1 for v in a.values() if v == "despatch"), 4)

    def test_cover_never_robs_a_priority_station_below_ideal(self):
        # Press 1 is exactly at ideal (3) - it must not lend to the CNC gap.
        ops = [Operator(f"c{i}", f"C{i}", "cnc_thermo") for i in range(3)]
        ops += [Operator(f"p{i}", f"P{i}", "press_1", skills={"cnc_thermo"}) for i in range(3)]
        a = allocate_shift(ops, {"cnc_thermo", "press_1"}, {})
        self.assertEqual(sum(1 for v in a.values() if v == "press_1"), 3)
        self.assertEqual(sum(1 for v in a.values() if v == "cnc_thermo"), 3)

    def test_non_priority_station_lends_even_below_ideal_but_never_to_zero(self):
        # Hafele (ideal 2, not a priority station) has 2 people, both able to
        # run the Cefla; Cefla has 1 of its ideal 2 -> one Hafele person moves.
        ops = [Operator("e", "E", "edging"),
               Operator("h1", "H1", "hafele", skills={"edging"}), Operator("h2", "H2", "hafele", skills={"edging"})]
        a = allocate_shift(ops, {"edging", "hafele"}, {})
        self.assertEqual(sum(1 for v in a.values() if v == "edging"), 2)
        self.assertEqual(sum(1 for v in a.values() if v == "hafele"), 1)

    def test_cover_priority_order_cnc_before_sanding(self):
        # One spare multi-skilled person, gaps at both CNC and sanding: CNC wins.
        ops = [Operator("c1", "C1", "cnc_thermo"), Operator("s1", "S1", "sanding"),
               Operator("d1", "D1", "despatch", skills={"cnc_thermo", "sanding"}), Operator("d2", "D2", "despatch")]
        a = allocate_shift(ops, {"cnc_thermo", "sanding", "despatch"}, {})
        self.assertEqual(a["d1"], "cnc_thermo")

    def test_cover_only_moves_qualified_people(self):
        ops = [Operator("c1", "C1", "cnc_thermo")] + [Operator(f"d{i}", f"D{i}", "despatch") for i in range(6)]
        a = allocate_shift(ops, {"cnc_thermo", "despatch"}, {})
        self.assertEqual(sum(1 for v in a.values() if v == "cnc_thermo"), 1)   # nobody qualified -> nobody moved

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

    def test_intake_is_entered_per_product_range(self):
        by_route = {cfg.Route.THERMO: 13000.0, cfg.Route.CUT_AND_CLASH: 1400.0}
        s = settings(horizon="month", intake_m2_by_route=by_route)
        self.assertEqual(s.intake_m2_for_horizon, 14400.0)
        r = Simulator(tiny_roster(), s).run()
        self.assertAlmostEqual(r.cum_intake_m2, 14400.0, places=6)
        # each range got its own number, whatever the mix sliders say (measured
        # on what Optimising released: the tail of the window is still pending,
        # so compare the ranges' proportions, not the absolute m2)
        released = {route: sum(e["qty"] for e in r.release_log
                                if e["route"] == route and not e["remake"])
                    for route in by_route}
        self.assertAlmostEqual(released[cfg.Route.THERMO] / released[cfg.Route.CUT_AND_CLASH],
                                13000.0 / 1400.0, delta=0.05)
        # ...and the mix splits inside a range, not across ranges
        rm = s.route_mix()
        for route, classes in rm.items():
            self.assertAlmostEqual(sum(classes.values()), 100.0, places=6, msg=route)

    def test_one_range_can_be_zero(self):
        s = settings(horizon="week", intake_m2_by_route={cfg.Route.THERMO: 500.0,
                                                         cfg.Route.CUT_AND_CLASH: 0.0})
        r = Simulator(tiny_roster(), s).run()
        self.assertAlmostEqual(r.cum_intake_m2, 500.0, places=6)
        self.assertEqual(r.by_route[cfg.Route.CUT_AND_CLASH].completed_m2, 0.0)
        self.assertGreater(r.by_route[cfg.Route.THERMO].completed_m2, 0.0)

    def test_a_range_with_no_mix_share_still_gets_its_intake(self):
        # Cut & Clash intake entered but Melamine/Acrylic both left at 0%
        s = settings(horizon="week", intake_m2_by_route={cfg.Route.THERMO: 0.0,
                                                         cfg.Route.CUT_AND_CLASH: 300.0},
                     mix_pct={"S1": 100.0, "Melamine": 0.0, "Acrylic": 0.0})
        r = Simulator(tiny_roster(), s).run()
        self.assertAlmostEqual(r.cum_intake_m2, 300.0, places=6)
        self.assertTrue(any("spread evenly" in n for n in r.notes))

    def test_intake_by_route_is_validated(self):
        with self.assertRaises(ValueError):
            settings(intake_m2_by_route={cfg.Route.THERMO: -5.0})
        with self.assertRaises(ValueError):
            settings(intake_m2_by_route={"nonsense": 100.0})

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
        # Shortest route is Cut & Clash: optimising, cnc_1536, edge_bander,
        # drilling, despatch = 5 stations, so nothing can complete before hour 4.
        r = Simulator(tiny_roster(), settings(horizon="day", mix_pct={"Melamine": 100.0})).run()
        self.assertEqual(r.trace[0]["cum_completed"], 0.0)
        self.assertEqual(r.trace[3]["cum_completed"], 0.0)
        self.assertGreater(r.trace[4]["cum_completed"], 0.0)

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
        caps["drilling"] = 0.0
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

    def test_unqualified_drop_is_refused(self):
        r = Roster([Operator("j", "Jay", "cnc_thermo", shift="day", skills={"cnc_1536"})])
        self.assertIsNone(apply_move(r, {"op_id": "j", "to_station": "sanding", "to_shift": "day"}))
        self.assertEqual(r.get("j").home_station, "cnc_thermo")

    def test_snapshot_restore_undoes_a_move(self):
        r = Roster([Operator("j", "Jay", "cnc_thermo", shift="day", skills={"cnc_1536", "sanding"})])
        before = snapshot(r)
        apply_move(r, {"op_id": "j", "to_station": "sanding", "to_shift": "aft"})
        self.assertEqual(r.get("j").home_station, "sanding")
        restore(r, before)
        op = r.get("j")
        self.assertEqual((op.home_station, op.shift, op.skills), ("cnc_thermo", "day", {"cnc_1536", "sanding"}))

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
        self.assertEqual(len(by[("edge_bander", "day")]), 1)       # Ali
        self.assertEqual(len(by[("drilling", "day")]), 1)          # Eric
        self.assertEqual(len(by[("cnc_1536", "day")]), 1)          # Nirmal
        self.assertEqual(len(by[("cnc_1536", "aft")]), 1)          # Jerald
        self.assertEqual(len(by[("mb_sander", "day")]), 1)         # Quyen
        self.assertEqual(len(by[("mb_sander", "aft")]), 1)         # Viet
        self.assertEqual(len(by[("hafele", "day")]), 2)            # Ben, Agnes
        self.assertEqual(len(by[("diy", "day")]) + len(by[("diy", "aft")]), 2)   # Dayna, Arona
        # Every Thermo CNC has someone tagged to it on both shifts - whether as
        # their home machine or as a second machine someone else also tends.
        # (Who covers the 4th machine moves around as the roster is edited in
        # the app, so this checks the coverage, not one particular person.)
        for shift in cfg.SHIFT_LABELS:
            covered = set()
            for o in r.operators:
                if o.shift != shift:
                    continue
                if o.home_station == "cnc_thermo" and o.machine:
                    covered.add(o.machine)
                if o.second_station == "cnc_thermo" and o.machine_2:
                    covered.add(o.machine_2)
            self.assertEqual(covered, set(cfg.STATIONS["cnc_thermo"].machines), shift)

    def test_cnc_thermo_lane_manning(self):
        f = Simulator._cnc_thermo_lanes
        m = 60.0 * cfg.CNC_UTILISATION
        self.assertEqual(f([]), (0.0, 0.0))
        self.assertEqual(f(["C6"]), (m, 0.0))
        self.assertEqual(f(["B1224", "Weeke (old)"]), (0.0, 2 * m))
        self.assertEqual(f(["", "", ""]), (0.0, 3 * m))              # cover fills production machines first
        self.assertEqual(f(["", "", "", ""]), (m, 3 * m))            # ...and C6 last
        self.assertEqual(f(["C6", "C6"]), (m, m))                    # 2nd person on C6 runs another machine
        self.assertEqual(f(["C6", "", "", "", "", ""]), (m, 3 * m))  # never more than 4 machines
        k = cfg.CNC_SECOND_MACHINE_FACTOR
        self.assertEqual(f([("B1224", 1.0), ("C6", k)]), (m * k, m))       # one person also running C6
        self.assertEqual(f([("B1224", 1.0), ("Weeke (old)", k)]), (0.0, m + m * k))
        self.assertEqual(f([("B1224", 1.0), ("C6", k), ("C6", 1.0)]), (m, m))   # a real C6 operator caps it at 1
        self.assertAlmostEqual(f([("", 0.5)])[1], 0.5 * m)                    # half a floater = half a machine
        c6, main = f([("C6", 1.0), ("C6", 1.0)])                              # 2nd whole person moves on
        self.assertAlmostEqual(c6, m); self.assertAlmostEqual(main, m)

    def test_second_machine_copy_and_unlink(self):
        r = Roster([Operator("j", "Jay", "cnc_thermo", shift="day", machine="B1224"),
                    Operator("r", "Ray", "cnc_thermo", shift="day", machine="C6")])
        msg = apply_move(r, {"op_id": "j", "to_station": "cnc_thermo", "to_shift": "day", "to_machine": "C6", "copy": True})
        self.assertIn("also at CNC (Thermo) / C6", msg)
        self.assertEqual((r.get("j").machine, r.get("j").machine_2), ("B1224", "C6"))
        r.validate()
        # not allowed: onto own machine, another station, another shift, or without a home machine
        self.assertIsNone(apply_move(r, {"op_id": "j", "to_station": "cnc_thermo", "to_shift": "day", "to_machine": "B1224", "copy": True}))
        self.assertIsNone(apply_move(r, {"op_id": "j", "to_station": "cnc_thermo", "to_shift": "aft", "to_machine": "C6", "copy": True}))
        # moving their home machine to the second one collapses the pair
        apply_move(r, {"op_id": "j", "to_station": "cnc_thermo", "to_shift": "day", "to_machine": "C6"})
        self.assertEqual((r.get("j").machine, r.get("j").machine_2), ("C6", ""))
        apply_move(r, {"op_id": "j", "to_station": "cnc_thermo", "to_shift": "day", "to_machine": "Weeke (old)", "copy": True})
        self.assertEqual(apply_move(r, {"op_id": "j", "unlink": True}), "Jay: no longer also at CNC (Thermo) / Weeke (old)")
        self.assertEqual(r.get("j").machine_2, "")
        bad = Roster([Operator("x", "X", "cnc_thermo", machine="B1224", machine_2="B1224")])
        with self.assertRaises(RosterError):
            bad.validate()

    def test_split_across_two_stations(self):
        r = Roster([Operator("h", "Hai", "sanding", shift="day", skills={"despatch"}),
                    Operator("d", "Dee", "despatch", shift="day")])
        msg = apply_move(r, {"op_id": "h", "to_station": "despatch", "to_shift": "day", "copy": True})
        self.assertIn("also at Packing / Despatch", msg)
        self.assertEqual((r.get("h").station_2, r.get("h").second_station), ("despatch", "despatch"))
        r.validate()
        # not skilled / other shift -> refused
        self.assertIsNone(apply_move(r, {"op_id": "d", "to_station": "sanding", "to_shift": "day", "copy": True}))
        self.assertIsNone(apply_move(r, {"op_id": "h", "to_station": "despatch", "to_shift": "aft", "copy": True}))
        # nothing waiting at despatch at hour 0 -> Hai is a whole person at home
        res = Simulator(r, settings(horizon="day")).run()
        self.assertAlmostEqual(res.trace[0]["ops"]["sanding"], 1.0)
        self.assertAlmostEqual(res.trace[0]["ops"]["despatch"], 1.0)
        self.assertIn("Hai (also)", res.staffing_by_shift["0|day"]["despatch"])
        # unlink -> whole person at home again
        self.assertIn("no longer also at", apply_move(r, {"op_id": "h", "unlink": True}))
        self.assertFalse(r.get("h").is_split)
        # a normal move resets a split
        apply_move(r, {"op_id": "h", "to_station": "despatch", "to_shift": "day", "copy": True})
        apply_move(r, {"op_id": "h", "to_station": "despatch", "to_shift": "day"})
        self.assertFalse(r.get("h").is_split)

    def test_split_share_follows_the_piles(self):
        f = Simulator._split_share
        self.assertEqual(f(0.0, 0.0), 0.0)
        self.assertEqual(f(5.0, 0.5), 0.0)                       # below the help threshold: stay home
        self.assertAlmostEqual(f(0.0, 5.0), 1.0)                 # nothing at home, big pile there: go help
        self.assertAlmostEqual(f(5.0, 5.0), 0.5)
        self.assertAlmostEqual(f(9.0, 3.0), 0.25)

    def test_split_person_helps_only_when_the_second_pile_builds(self):
        # Everyone staffed; the sanding day person also helps despatch. With
        # 100% S1 there is a steady flow, so despatch's pile builds at some
        # point and Hai's time moves there in proportion - and back.
        ops = [Operator(f"{sid}_{sh}", sid, sid, shift=sh) for sid in cfg.FLOW_STATIONS for sh in cfg.SHIFT_LABELS]
        for op in ops:
            if op.id == "sanding_day":
                op.skills.add("despatch"); op.station_2 = "despatch"
        res = Simulator(Roster(ops), settings(horizon="week", mix_pct={"S1": 100.0})).run()
        sanding_day_hours = [t["ops"]["sanding"] for t in res.trace if t["shift"].get("sanding") == "day"]
        self.assertTrue(all(0.0 <= h <= 1.0 + 1e-9 for h in sanding_day_hours))
        self.assertTrue(any(h < 1.0 - 1e-6 for h in sanding_day_hours))      # helped at despatch at some point
        self.assertTrue(any(abs(h - 1.0) < 1e-6 for h in sanding_day_hours))  # and was fully home at others
        helping = res.person_hours["sanding_day"]["helping"]
        self.assertGreater(helping, 0.0)
        self.assertLess(helping, res.person_hours["sanding_day"]["rostered"])
        # second station on a different shift than the person -> whole person at home
        r2 = Roster([Operator("h", "Hai", "sanding", shift="day", skills={"drilling"}, station_2="drilling")])
        res2 = Simulator(r2, settings(horizon="day")).run()
        self.assertAlmostEqual(res2.trace[9]["ops"]["sanding"], 1.0)   # drilling's 8 h day is over

    def test_second_machine_adds_capacity_in_a_run(self):
        base = [Operator(f"{sid}_{sh}", sid, sid, shift=sh) for sid in cfg.STATIONS if sid not in ("admin", "cnc_thermo")
                for sh in cfg.SHIFT_LABELS]
        one = Roster(base + [Operator("j", "J", "cnc_thermo", shift="day", machine="B1224")])
        two = Roster(base + [Operator("j", "J", "cnc_thermo", shift="day", machine="B1224", machine_2="Weeke (old)")])
        s1 = Simulator(one, settings(horizon="week", mix_pct={"S3": 100.0}, special_order_pct=0.0)).run()
        s2 = Simulator(two, settings(horizon="week", mix_pct={"S3": 100.0}, special_order_pct=0.0)).run()
        # more CNC output with the second machine
        out1 = sum(t["out"]["cnc_thermo"] for t in s1.trace)
        out2 = sum(t["out"]["cnc_thermo"] for t in s2.trace)
        self.assertGreater(out2, out1 * 1.3)

    def test_remakes_and_specials_go_to_c6_when_it_is_manned(self):
        base = [Operator(f"{sid}_{sh}", sid, sid, shift=sh) for sid in cfg.STATIONS if sid not in ("admin", "cnc_thermo")
                for sh in cfg.SHIFT_LABELS]
        with_c6 = Roster(base + [Operator("c6d", "C6 day", "cnc_thermo", shift="day", machine="C6"),
                                 Operator("c6a", "C6 aft", "cnc_thermo", shift="aft", machine="C6"),
                                 Operator("w1", "W old", "cnc_thermo", shift="day", machine="Weeke (old)"),
                                 Operator("w2", "W old aft", "cnc_thermo", shift="aft", machine="Weeke (old)")])
        r = Simulator(with_c6, settings(horizon="week", remake_rate_pct=10.0, special_order_pct=20.0)).run()
        self.assertEqual(r.cnc_thermo_lanes["c6_unmanned_hours"], 0)
        self.assertGreater(r.cnc_thermo_lanes["remakes_on_c6_pct"], 90.0)
        self.assertGreater(r.cnc_thermo_lanes["specials_on_c6_pct"], 90.0)
        # Nobody tagged to C6: it stays unmanned, specials/remakes still get cut (on the other machines).
        no_c6 = Roster(base + [Operator("w1", "W old", "cnc_thermo", shift="day", machine="Weeke (old)"),
                               Operator("w2", "W old aft", "cnc_thermo", shift="aft", machine="Weeke (old)")])
        r2 = Simulator(no_c6, settings(horizon="week", remake_rate_pct=10.0, special_order_pct=20.0)).run()
        self.assertEqual(r2.cnc_thermo_lanes["c6_unmanned_hours"], r2.cnc_thermo_lanes["c6_active_hours"])
        self.assertEqual(r2.cnc_thermo_lanes["specials_on_c6_pct"], 0.0)
        self.assertGreater(r2.cum_completed_m2, 0)

    def test_special_share_is_validated(self):
        with self.assertRaises(ValueError):
            SimulationSettings(special_order_pct=150)

    def test_result_records_who_was_where(self):
        roster = Roster([Operator("d", "Day Op", "cnc_1536", shift="day"),
                         Operator("a", "Aft Op", "cnc_1536", shift="aft")])
        r = Simulator(roster, settings(horizon="day")).run()
        self.assertEqual(r.staffing_by_shift["0|day"]["cnc_1536"], ["Day Op"])
        self.assertEqual(r.staffing_by_shift["0|aft"]["cnc_1536"], ["Aft Op"])
        self.assertEqual(r.trace[0]["day"], 0)


class CrewStartTests(unittest.TestCase):
    """Each crew runs on its own clock, offset from the 6am plant start."""

    def test_one_crew_per_area(self):
        self.assertEqual(cfg.STATION_CREW["cnc_thermo"], "cnc")
        self.assertEqual({cfg.STATION_CREW[s] for s in ("sanding", "mb_sander")}, {"sanding"})
        self.assertEqual(cfg.STATION_CREW["edging"], "cefla")
        self.assertEqual({cfg.STATION_CREW[s] for s in ("press_1", "press_2")}, {"press"})
        self.assertEqual({cfg.STATION_CREW[s] for s in ("despatch", "hafele", "diy")}, {"packing"})
        self.assertEqual({cfg.STATION_CREW[s] for s in ("optimising", "cnc_1536", "edge_bander", "drilling")}, {"cutclash"})

    def test_local_clock_with_an_early_start(self):
        early = cfg.ShiftSchedule(4, 10.0, True, 10.0, start_hour=-2.0)
        self.assertIsNone(early.local_clock(-3))              # before its first day
        self.assertEqual(early.local_clock(-2), (0, 0.0))     # its hour 0 = plant hour -2 (4am)
        self.assertEqual(early.local_clock(166), (7, 0.0))    # Sunday 22:00 plant time = its Monday 4am
        self.assertEqual(early.local_clock(8), (0, 10.0))     # plant 14:00 = its hour 10 -> afternoon shift
        with self.assertRaises(ValueError):
            cfg.ShiftSchedule(4, 10.0, True, 10.0, start_hour=-7.0)
        with self.assertRaises(ValueError):
            cfg.ShiftSchedule(4, 10.0, True, 10.0, start_hour=13.0)

    def test_cnc_crew_starting_early_runs_ahead_of_the_sanders(self):
        scheds = {k: cfg.ShiftSchedule(**vars(v)) for k, v in cfg.DEFAULT_SHIFT_SCHEDULES.items()}
        scheds["cnc"] = cfg.ShiftSchedule(4, 10.0, True, 10.0, start_hour=-2.0)
        r = Simulator(tiny_roster(), settings(horizon="month", shift_schedules=scheds, mix_pct={"S1": 100.0})).run()
        by_h = {t["h"]: t for t in r.trace}
        sun_night = by_h[7 * 24 - 2]                           # Sunday 22:00: only the CNC crew is in
        self.assertEqual(sun_night["shift"]["cnc_thermo"], "day")
        self.assertIsNone(sun_night["shift"]["sanding"])
        self.assertGreater(sun_night["ops"]["cnc_thermo"], 0)   # ...and staffed (Monday's allocation taken early)
        self.assertEqual(by_h[7 * 24 + 7]["shift"]["cnc_thermo"], "day")   # its 10 h day runs to plant hour 8
        self.assertEqual(by_h[7 * 24 + 8]["shift"]["cnc_thermo"], "aft")
        self.assertEqual(by_h[7 * 24 + 8]["shift"]["sanding"], "day")      # sanders still on days
        # the CNC crew's early Monday also ends early: at plant hour 18 the CNC afternoon is over, sanding's isn't
        self.assertIsNone(by_h[7 * 24 + 18]["shift"]["cnc_thermo"])
        self.assertEqual(by_h[7 * 24 + 18]["shift"]["sanding"], "aft")
        # WIP is waiting in front of sanding when its crew arrives Monday 6am
        self.assertGreater(by_h[7 * 24]["buf"]["sanding"], 0.0)
        self.assertEqual(r.attendance_log[0]["day"], 0)        # attendance days still line up with the plant calendar

    def test_default_start_is_unchanged_behaviour(self):
        # start_hour 0 everywhere: hour 8 handover between the 8 h Cut & Clash day and the 10 h Thermo day
        r = Simulator(tiny_roster(), settings(horizon="day")).run()
        self.assertEqual(r.trace[8]["shift"]["cnc_1536"], "aft")
        self.assertEqual(r.trace[8]["shift"]["cnc_thermo"], "day")


class LeadTimeBasisTests(unittest.TestCase):
    def test_both_routes_are_judged_in_working_days(self):
        self.assertTrue(cfg.LEAD_TIME_EXCLUDES_WEEKENDS[cfg.Route.THERMO])
        self.assertTrue(cfg.LEAD_TIME_EXCLUDES_WEEKENDS[cfg.Route.CUT_AND_CLASH])
        sim = Simulator(tiny_roster(), settings(horizon="day"))
        fri_noon, mon_noon = 4 * 24 + 6, 7 * 24 + 6
        self.assertAlmostEqual(sim._days_elapsed(fri_noon, mon_noon, cfg.Route.CUT_AND_CLASH), 1.0)   # weekend not counted
        self.assertAlmostEqual(sim._days_elapsed(fri_noon, mon_noon, cfg.Route.THERMO), 1.0)


class SettingsMigrationTests(unittest.TestCase):
    def test_a_saved_combined_intake_is_split_across_the_two_ranges(self):
        # What app.py does when it finds pre-split saved settings: 9,500 m2
        # total becomes Thermo + Cut & Clash in the default mix's proportions.
        old_total = 9500.0
        split = {}
        for route in cfg.ROUTE_SEQUENCE:
            share = sum(cfg.DEFAULT_MIX_PCT[c] for c, pc in cfg.PRODUCT_CLASSES.items()
                         if pc.route == route)
            split[route] = round(old_total * share / 100.0, 1)
        self.assertAlmostEqual(sum(split.values()), old_total, places=1)
        self.assertGreater(split[cfg.Route.THERMO], split[cfg.Route.CUT_AND_CLASH])
        r = Simulator(tiny_roster(), settings(horizon="month", intake_m2_by_route=split)).run()
        self.assertAlmostEqual(r.cum_intake_m2, old_total, places=4)


class PeopleGatedStationTests(unittest.TestCase):
    """Edge banding and drilling: nobody on them = nothing comes out, and a
    second person there adds nothing (they cover breaks / the afternoon)."""

    def _cutclash_only(self, ops):
        return Simulator(Roster(ops), settings(horizon="week", mix_pct={"Melamine": 100.0},
                                               intake_m2_by_route={cfg.Route.THERMO: 0.0,
                                                                   cfg.Route.CUT_AND_CLASH: 400.0})).run()

    def test_nobody_on_drilling_means_nothing_completes(self):
        full = [Operator(f"{sid}_{sh}", sid, sid, shift=sh) for sid in cfg.FLOW_STATIONS
                for sh in cfg.SHIFT_LABELS]
        self.assertGreater(self._cutclash_only(full).by_route[cfg.Route.CUT_AND_CLASH].completed_m2, 0.0)
        for gated in cfg.PEOPLE_GATED_STATIONS:
            without = [o for o in full if o.home_station != gated]
            r = self._cutclash_only(without)
            self.assertEqual(r.by_route[cfg.Route.CUT_AND_CLASH].completed_m2, 0.0, gated)
            self.assertEqual(r.station_out_m2[gated], 0.0, gated)
            self.assertGreater(r.station_unstaffed_hours[gated], 0, gated)

    def test_a_second_person_adds_no_throughput(self):
        f = Simulator._flat_capacity_m2_per_hour
        for gated in cfg.PEOPLE_GATED_STATIONS:
            st = cfg.STATIONS[gated]
            rate = st.capacity_m2_per_op_hour
            self.assertEqual(f(st, 0, rate), 0.0, gated)          # nobody = nothing
            self.assertEqual(f(st, 1, rate), rate, gated)          # one person = full rate
            self.assertEqual(f(st, 3, rate), rate, gated)          # more people = no faster
            self.assertEqual(st.max_useful_ops, 1, gated)          # so the allocator won't park people there

    def test_the_rate_covers_the_busiest_real_month_on_one_day_shift(self):
        # The back-fit that sets the rate: one person, 4 x 9 h days.
        hours = 4 * 9.0 * cfg.WEEKS_PER_MONTH
        busiest = cfg.REAL_INTAKE_M2["month"][cfg.Route.CUT_AND_CLASH]["max"]
        self.assertGreaterEqual(cfg.EDGE_DRILL_RATE_M2_PER_OP_HOUR * hours, busiest)


class SchedulingTests(unittest.TestCase):
    """Optimising's morning release: the 4pm order cut-off, the n-working-day
    scheduling lag, remakes waiting for the next morning, and what happens
    when nobody is on Optimising."""

    def test_online_orders_arrive_until_4pm(self):
        # Cut-off at hour 10 (6am start + 10h = 4pm): intake in hour 9, none in hour 10.
        self.assertEqual(cfg.INTAKE_WINDOW_HOURS, (0.0, 10.0))
        self.assertTrue(Simulator._is_intake_hour(9))
        self.assertFalse(Simulator._is_intake_hour(10))

    def test_working_day_after_skips_weekends(self):
        f = Simulator._working_day_after
        self.assertEqual(f(2, 3), 7)    # Wed -> Mon (the real schedule: ordered 16 Sep, cut 21 Sep)
        self.assertEqual(f(3, 3), 8)    # Thu -> Tue (ordered 17 Sep, cut 22 Sep)
        self.assertEqual(f(4, 1), 7)    # Fri -> Mon
        self.assertEqual(f(5, 1), 7)    # Sat -> Mon
        self.assertEqual(f(0, 0), 0)

    def test_orders_reach_the_cnc_on_the_third_working_morning(self):
        r = Simulator(tiny_roster(), settings(horizon="month", release_working_days=cfg.DEFAULT_RELEASE_WORKING_DAYS)).run()
        thermo_new = [e for e in r.release_log if e["route"] == cfg.Route.THERMO and not e["remake"]]
        self.assertTrue(thermo_new)
        for e in thermo_new:
            self.assertEqual(Simulator._working_days_elapsed(e["order_day"] * 24, e["day"] * 24), 3.0, e)
        cc_new = [e for e in r.release_log if e["route"] == cfg.Route.CUT_AND_CLASH and not e["remake"]]
        for e in cc_new:
            self.assertEqual(Simulator._working_days_elapsed(e["order_day"] * 24, e["day"] * 24), 1.0, e)
        # Nothing on the Thermo CNCs for the first three days (no warm-up), then work every weekday morning.
        self.assertEqual(max(t["buf"]["cnc_thermo"] for t in r.trace if t["day"] < 3), 0.0)
        self.assertGreater(r.trace[3 * 24]["buf"]["cnc_thermo"], 0.0)
        self.assertEqual(r.scheduling_mornings, 22)
        self.assertEqual(r.scheduling_mornings_missed, 0)
        # The pool is in the lead-time clock and in the mass balance (the engine checks the balance itself).
        self.assertGreater(r.pending_m2_avg, 0.0)
        self.assertGreater(r.by_route[cfg.Route.THERMO].overall_avg_lead_days, 3.0)

    def test_remakes_found_on_the_afternoon_shift_wait_for_the_morning(self):
        r = Simulator(tiny_roster(), settings(horizon="month", remake_rate_pct=20.0, remake_days=0.0,
                                              release_working_days=cfg.DEFAULT_RELEASE_WORKING_DAYS)).run()
        remakes = [e for e in r.release_log if e["remake"]]
        self.assertTrue(remakes)
        # A morning-released remake was ordered before that day, never on it.
        for e in remakes:
            self.assertLess(e["order_day"], e["day"], e)
        self.assertGreater(r.cum_remade_m2, 0.0)

    def test_nobody_on_optimising_means_nothing_is_scheduled(self):
        ops = [op for op in tiny_roster().operators if op.id != "optimising_day"]   # only an afternoon planner
        r = Simulator(Roster(ops), settings(horizon="week", release_working_days=cfg.DEFAULT_RELEASE_WORKING_DAYS)).run()
        self.assertEqual(r.scheduling_mornings, 5)
        self.assertEqual(r.scheduling_mornings_missed, 5)
        self.assertEqual(r.cum_completed_m2, 0.0)
        self.assertAlmostEqual(r.pending_m2_end, r.cum_intake_m2, places=6)
        # ...unless the roster has no planner at all: then scheduling is assumed to happen outside the model.
        none = [op for op in ops if op.home_station != "optimising"]
        r2 = Simulator(Roster(none), settings(horizon="week", release_working_days=cfg.DEFAULT_RELEASE_WORKING_DAYS)).run()
        self.assertEqual(r2.scheduling_mornings_missed, 0)
        self.assertTrue(any("scheduling release" in n for n in r2.notes))

    def test_release_days_are_validated(self):
        with self.assertRaises(ValueError):
            settings(release_working_days={cfg.Route.THERMO: -1, cfg.Route.CUT_AND_CLASH: 1})
        with self.assertRaises(ValueError):
            settings(release_working_days={cfg.Route.THERMO: 3})


class ConstraintsTests(unittest.TestCase):
    def test_report_covers_both_lines_and_finds_the_busy_station(self):
        r = Simulator(real_roster(), settings(horizon="month", warmup_days=cfg.SIMULATION_WARMUP_DAYS)).run()
        rep = analyse(r, real_roster())
        self.assertEqual(set(rep.by_route), {cfg.Route.THERMO, cfg.Route.CUT_AND_CLASH})
        for rc in rep.by_route.values():
            self.assertIn(rc.kind, ("bottleneck", "ccr", "external"))
            self.assertTrue(rc.suggestions)
            self.assertTrue(all(0.0 <= row.utilisation <= 1.0 for row in rc.rows))
            if rc.constraint is not None:
                self.assertEqual(rc.constraint, max(
                    (row for row in rc.rows if row.utilisation >= 0.85), key=lambda row: (row.growing, row.utilisation)))
        self.assertTrue(rep.people)
        self.assertTrue(any(p["Utilisation %"] is None for p in rep.people))   # box packers not rated

    def test_starving_a_station_is_detected(self):
        # No CNC operator at all: sanding is staffed but starved every hour; CNC unstaffed.
        ops = [Operator(f"{sid}_{sh}", sid, sid, shift=sh) for sid in cfg.FLOW_STATIONS if sid != "cnc_thermo"
               for sh in cfg.SHIFT_LABELS]
        r = Simulator(Roster(ops), settings(horizon="week", mix_pct={"S1": 100.0})).run()
        rep = analyse(r, Roster(ops))
        rows = {row.station: row for row in rep.by_route[cfg.Route.THERMO].rows}
        self.assertGreater(rows["cnc_thermo"].unstaffed_h, 0)
        self.assertGreater(rows["sanding"].starved_h, 0)
        self.assertTrue(any("nobody on it" in n for n in rep.by_route[cfg.Route.THERMO].policy_constraints))

    def test_buffer_limit_blocks_upstream(self):
        base = settings(horizon="week", mix_pct={"S1": 100.0})
        capped = SimulationSettings(**{**vars(base), "buffer_caps_m2": {"sanding": 5.0}})
        r = Simulator(tiny_roster(), capped).run()
        self.assertGreater(r.station_blocked_hours["cnc_thermo"], 0)
        self.assertLessEqual(max(t["buf"]["sanding"] for t in r.trace), 5.0 + 1e-6)

    def test_press_batching_waits_for_a_pile(self):
        base = settings(horizon="week", mix_pct={"S1": 100.0})
        batched = SimulationSettings(**{**vars(base), "press_batch_start_m2": 60.0, "press_batch_stop_m2": 5.0})
        r = Simulator(tiny_roster(), batched).run()
        self.assertGreater(r.station_held_hours["press_1"], 0)          # presses waited for the pile
        levels = [t["buf"]["press_1"] for t in r.trace if "press_1" in t["active"]]
        self.assertGreater(max(levels), 60.0 - 1e-6)                    # the pile built up to the start level
        self.assertGreater(r.cum_completed_m2, 0)
        continuous = SimulationSettings(**{**vars(base), "press_batch_start_m2": 0.0})
        r2 = Simulator(tiny_roster(), continuous).run()
        self.assertEqual(r2.station_held_hours["press_1"], 0)
        with self.assertRaises(ValueError):
            SimulationSettings(press_batch_start_m2=50.0, press_batch_stop_m2=60.0)

    def test_protective_buffers_are_reported(self):
        r = Simulator(real_roster(), settings(horizon="week", warmup_days=cfg.SIMULATION_WARMUP_DAYS)).run()
        rep = analyse(r, real_roster())
        pbs = {pb.queue: pb for pb in rep.by_route[cfg.Route.THERMO].protective_buffers}
        self.assertEqual(set(pbs), {"edging", cfg.PRESS_QUEUE_ID})
        for pb in pbs.values():
            self.assertGreater(pb.hours_checked, 0)
            self.assertLessEqual(pb.hours_below_target, pb.hours_checked)
        self.assertEqual(rep.by_route[cfg.Route.CUT_AND_CLASH].protective_buffers, [])

    def test_presses_share_the_pile(self):
        r = Simulator(real_roster(), settings(horizon="week", warmup_days=cfg.SIMULATION_WARMUP_DAYS)).run()
        u1, u2 = r.station_utilisation["press_1"], r.station_utilisation["press_2"]
        self.assertAlmostEqual(u1, u2, delta=0.05)

    def test_person_hours_add_up(self):
        r = Simulator(real_roster(), settings(horizon="week", warmup_days=cfg.SIMULATION_WARMUP_DAYS)).run()
        for h in r.person_hours.values():
            self.assertAlmostEqual(h["producing"] + h["waiting"], h["rostered"], places=6)
            self.assertLessEqual(h["covering"], h["rostered"] + 1e-9)


@unittest.skipUnless(os.environ.get("PLANT_SIM_UI_TESTS", "1") == "1", "UI tests disabled")
class LauncherTests(unittest.TestCase):
    """launch.py's port check (what the .bat / .exe launcher relies on)."""

    def test_port_in_use_sees_a_listener_on_any_interface(self):
        import socket
        import launch
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
            srv.bind(("0.0.0.0", 0))
            srv.listen(1)
            port = srv.getsockname()[1]
            self.assertTrue(launch.port_in_use(port))
            self.assertNotEqual(launch.free_port(port), port)
        self.assertFalse(launch.port_in_use(port))


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
