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
  stations, buffers filling and draining, who is on each station) alongside
  the charts, so a bottleneck is something you can watch happen, not just
  a number in a table.
- Lets you **drag people around the floor**: the Factory Floor tab is a
  map of the real shop floor (traced from the hand-drawn plan) with every
  station as a box - individual CNCs and the edge bander / drill as tiles
  inside their station, Hafele and DIY packing inside Dispatch - and the
  rostered people as chips, one map per shift. Drag someone to another
  station, machine or shift, hit Run, and see what it did to lead time
  and DIFOT - with undo.

## How it's organized

```
Plant Simulator.bat        Double-click to run (no terminal); tools\ has the portable-ZIP builder
tools/launcher/            Source of "Plant Simulator.exe" (C#, tray icon; compiled by tools/build_launcher.ps1)
launch.py                  What the .bat runs: free port, Streamlit headless, opens the browser
app.py                     Streamlit UI - the only file that "wires up" the screen
plant_sim/config.py        Every constant: stations, rates, shifts, real-data references
plant_sim/staff.py         Operator/Roster - the people
plant_sim/orders.py        Batch/StationQueue - the work moving through stations
plant_sim/allocation.py    Who works where each shift (home station -> floater -> rebalance)
plant_sim/simulation.py    The hour-by-hour engine that ties it all together
plant_sim/floor_view.py    The live animated factory-floor visualization (playback)
plant_sim/floor_editor.py  The drag-and-drop floor (Streamlit component wrapper + move rule)
plant_sim/floor_editor/    ...and its single-file HTML/JS front end (no build step)
plant_sim/constraints.py   Theory-of-Constraints read-out of a run (bottleneck, buffers, staff utilisation, improvements)
plant_sim/charts.py        Fixed (non-zoomable) charts in the brand palette
plant_sim/persist.py       Saves staff changes to the roster CSV (with backups) and sidebar settings to data/app_settings.json
plant_sim/theme.py         Dezignatek brand styling (teal/navy, fonts, wordmark header, footer)
.streamlit/config.toml     Streamlit theme colours (dark, teal primary) - committed on purpose
data/staff_roster.csv      The actual roster - names, skills, shifts, absence rates (auto-saved by the app)
data/app_settings.json     Your sidebar settings, auto-saved by the app (delete it, or use the Reset button, to go back to config.py defaults)
data/backups/              Timestamped roster backups (last 20; not committed)
tests/test_plant_sim.py    Regression tests (python -m unittest discover -s tests)
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

The result reports the same bottlenecks the live scan data shows (MB
Sander and CNC 1536), and the remaining gaps are documented as open
questions in `CALIBRATION_NOTES.md`, not hidden inside a guessed number.

6. **Engine correctness pass** (Sep 2026): a line-by-line review of the
   engine against how the real floor behaves, fixing everything that was
   physically wrong rather than just uncalibrated - orders arriving on
   weekends, parts travelling the whole line in one hour, the allocator
   being blind to the shared press pile, a plant-wide shift handover hour
   that didn't match the 8-hour Cut & Clash day, DIFOT not matching the
   dashboard's definition, and a data-entry slip in the default product
   mix. Full list in `CALIBRATION_NOTES.md`. The engine now checks its
   own mass balance every run (intake = completed + WIP, or it refuses to
   report), validates the roster and every setting up front with readable
   messages, and ships with a regression test suite.

## How the engine models time (worth knowing before reading results)

- **Hour 0 of every simulated day is the start of the day shift** (think
  6am), and the afternoon shift follows straight on. Day 0 is a Monday.
- **Orders arrive on weekdays until the 4pm online cut-off** (hours 0-10
  of the day shift), spread evenly - the intake total you enter for the
  horizon is exactly what arrives inside the reporting window.
- **Orders wait for Optimising's morning release.** Every weekday morning
  Optimising (Diana) schedules the pending pool onto the CNCs: new orders
  on the 3rd working day after their order date (read off the real CNC
  schedule), plus the afternoon shift's remakes. If nobody is on
  Optimising that morning nothing is released. The pool counts in lead
  time but is not on the floor; the Results tab shows each morning's
  release as a schedule table.
- **Horizons**: day = one Monday, week = 7 days, month = 30 days. Every
  run is preceded by a 21-day warm-up so queues start at steady state;
  only the horizon is reported.
- **Six crews, each on its own clock.** CNC, Sanding (manual + MB), Cefla,
  Press, Packing/Despatch and Cut & Clash each have their own days/week,
  shift lengths and **start time** relative to the 6am reference (CNC at
  -2 = 4am, so the sanders and the Cefla find WIP waiting when they start).
  Staffing is decided once per shift, per crew, when that crew starts; each
  crew hands over from day to afternoon at its own shift length; a
  cross-skilled person whose crew is off (e.g. Thermo on a Friday) floats
  to a station that is running.
- **Stations are processed downstream-first** each hour, so work needs at
  least one hour per station to travel the line.
- **Queues are worked oldest-order-first.** Remakes keep their original
  order date, so they jump the queue on their second pass (which is why
  real remakes only cost ~0.3 days, not a whole extra lead time).
- **DIFOT = on-time completed m² / completed m²**, the dashboard's
  definition. Work still inside the factory past its date is shown
  separately as *overdue backlog*.
- **Thermo lead time is in working days** (Mon-Fri, exact to the hour);
  Cut & Clash is in calendar days until its basis is confirmed.

## Running it

**No terminal needed:** double-click `Plant Simulator.bat`. On a normal
checkout it uses the Python installed on the PC (3.9+; the first run
creates a private `.venv` and installs the packages, which needs internet
once) and opens the app in your browser. Leave the black window open while
you use it; close it to stop.

**Giving it to someone else:** double-click `tools\Build portable package.bat`
once. It builds `dist\PlantSimulator-<date>.zip` (~125 MB) containing a
bundled Python runtime, every package, the app, the roster and your current
settings, plus a native **`Plant Simulator.exe`** (built on the spot with the
C# compiler that ships in Windows - no SDK). Send them the ZIP; they unzip it
somewhere short (Desktop or Documents, not a deeply nested folder) and
double-click `Plant Simulator.exe` inside - nothing to install, no admin
rights, no internet needed after that. The exe runs the app hidden (no black
window), opens the browser, and sits in the system tray: right-click it for
*Open in browser*, *Show log* or *Stop*. Double-clicking it again just
re-opens the browser. Their staff and settings changes are saved inside their
own unzipped folder. (`Plant Simulator.bat` is in the ZIP too, as a fallback
if a PC's policy blocks unknown exes - same thing, with a console window.)

The exe is unsigned, so the first run on another PC shows a Windows
SmartScreen "unknown publisher" notice (*More info → Run anyway*); a
code-signing certificate would remove that.

From a terminal it is still just:

```
streamlit run app.py
```

Tab 1 (Factory Floor) is the drag-and-drop floor: move people between
stations and shifts, Run, and read the KPI strip underneath. Tab 2 (Staff
& Skills) edits the finer details (skills, absence rates, add/remove
people). Everything you change is saved as you go: staff changes go
straight to `data/staff_roster.csv` (a backup is taken first; restore one
from the Staff tab), sidebar settings to `data/app_settings.json` - so the
app opens where you left it. Tab 3 (Simulation Results) shows the
full outcome: KPIs, a reality check against the real plant's monthly
figures, staffing-gap warnings, the live factory-floor animation with
names, lead-time/DIFOT by product range, station utilisation, and
attendance.

### Tests

```
python -m unittest discover -s tests -v
```

Standard library only (no pytest needed). The suite covers mass balance,
intake timing, working-day arithmetic, queue ordering, per-crew shift
handover, allocator rules, roster/settings validation, and a headless run
of the Streamlit app. Run it after editing anything in `plant_sim/`.
