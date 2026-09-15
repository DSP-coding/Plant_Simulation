# Thermoform + Cut & Clash Plant Simulator

## Why this exists

The plant used to plan staffing, shifts, and order intake against a
spreadsheet-style HTML/JS tool: a set of sliders and abstract capacity
numbers with no connection to who actually works where, or what happens
when someone calls in sick. It worked, but it couldn't answer the
questions that actually matter day to day - "what happens to lead time if
I move Jerard to a different shift?", "can we absorb another 500 m² of
Cut & Clash orders this week?", "is CNC really our bottleneck, or does it
just look that way?" - because it didn't know about real people, real
skills, or real absenteeism.

This project rebuilds that tool from scratch as a plain, commented Python
program instead: something the plant can read, understand, and keep
editing itself, rather than a black box. Every number in it is tagged as
either **[REAL]** (backed by an actual dashboard, SQL query, or confirmed
fact) or **[ASSUMPTION]** (a placeholder guess, clearly flagged so it
doesn't get mistaken for data) - see `CALIBRATION_NOTES.md` for the full
breakdown of which is which today.

## What it does

- Models the plant as two physically separate product lines that only
  share a Packing/Despatch stage at the end:
  - **Thermo** (Series 1/2/3): CNC → Sanding → MB Sander → Cefla
    Automated Gluing Line → Press (1 or 2) → Packing/Despatch
  - **Cut & Clash** (Melamine + Acrylic): Optimising → CNC 1536 → Edge
    Band/Drilling → Packing/Despatch
- Tracks **named staff**, not headcount sliders: each person has a home
  station, a shift, a set of extra skills (e.g. "can also run CNC 1536"),
  and their own absence likelihood - all editable by clicking through the
  Staff & Skills tab, not by digging through code.
- Simulates a day, a week, or a month at a time, stepping hour by hour
  through real shift schedules, so you can see the effect of changing
  order intake, shifts, staffing levels, or absenteeism before it happens
  on the real floor.
- Reports completion %, DIFOT, and lead time **separately for Thermo and
  Cut & Clash**, each judged against its own real target (10 working days
  for Thermo, 7 days for Cut & Clash) rather than one blended number that
  hides which line is actually struggling.
- Shows a live animated view of the factory floor (units flowing between
  stations, buffers filling and draining) alongside the charts, so a
  bottleneck is something you can watch happen, not just a number in a
  table.

## How it's organized

```
app.py                     Streamlit UI - the only file that "wires up" the screen
plant_sim/config.py        Every constant: stations, rates, shifts, real-data references
plant_sim/staff.py         Operator/Roster - the people
plant_sim/orders.py        Batch/StationQueue - the work moving through stations
plant_sim/allocation.py    Who works where each shift (home station -> floater -> rebalance)
plant_sim/simulation.py    The hour-by-hour engine that ties it all together
plant_sim/floor_view.py    The live animated factory-floor visualization
data/staff_roster.csv      The actual roster - names, skills, shifts, absence rates
```

If you want to change how the factory *behaves*, edit `plant_sim/`. If
you want to change how it *looks*, edit `app.py` or `floor_view.py`. If
you want to change a *number* (a rate, a target, a shift length), it's
almost certainly in `config.py`, with a comment explaining where it came
from.

## How this came together

1. **Started from the old spreadsheet tool.** The original HTML/JS
   source, a hand-drawn factory-floor sketch, and a staff/area screenshot
   were the starting point - the brief was "rebuild this as Python,
   commented, with classes, that I can understand and edit."
2. **Added the real structure.** Two distinct product lines sharing only
   Despatch, individual staff with skills and personal absence rates, a
   clickable UI to edit them, and day/week/month simulation horizons.
3. **Fixed real bugs as they surfaced from actual use**: an intake-units
   bug that inflated monthly totals ~30x, Despatch capacity being
   double-counted across both routes, Press needing independent Press 1 /
   Press 2 staffing, Thermo's lead time being measured in calendar days
   when the real 10-day promise excludes weekends.
4. **Replaced guesses with real data, one dashboard at a time**: a live
   lead-time SQL dashboard, a monthly order-intake-vs-completed tracker, a
   13-month production-hours/DIFOT history, and finally a direct 3-month
   per-station scan-checkpoint export - each one replacing an
   [ASSUMPTION] tag with a [REAL] one, or overturning a wrong assumption
   entirely (MB Sander's capacity was originally guessed 3x too high; it
   turned out to be a mandatory single-machine funnel, not a
   parallel-capacity station like Sanding).
5. **Corrected the roster against ground truth** as specific facts came
   in: who really runs the MB Sander (Sanding's team, not Press), which
   two stations turned out to be the same physical line ("Edging" and
   "Glue" are both the Cefla automated gluing line), and which named
   individuals were on the wrong shift.

The result tracks the real plant closely enough now that its reported
bottlenecks (Sanding, MB Sander, CNC 1536) match what the live scan data
actually shows - and the remaining gaps are documented as open questions
in `CALIBRATION_NOTES.md`, not hidden inside a guessed number.

## Running it

```
streamlit run app.py
```

Tab 1 (Staff & Skills) edits the roster. Tab 2 (Simulation Results) runs
the simulation and shows the outcome: KPIs, the live factory-floor
animation, lead-time/DIFOT by product range, station utilisation, and
attendance.
