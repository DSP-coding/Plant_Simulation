# Thermoform + Cut & Clash Plant Simulator — Session Notes

A running log of what this simulator models, which numbers are real vs.
guessed, and what's still open. Written so you (or anyone picking this up
later) doesn't have to re-read the whole chat to know what's been decided.

## What this is

A Python/Streamlit simulation of the plant's two product lines:

- **Thermo** (Series 1/2/3): CNC → Sanding → MB Sander → Cefla Automated
  Gluing Line → Press (1 or 2) → Packing/Despatch
- **Cut & Clash** (Melamine + Acrylic): Optimising → CNC 1536 → Edge
  Band/Drilling → Packing/Despatch (shared terminal with Thermo)

It steps through the schedule hour by hour, with named staff (skills,
home station, shift, personal absence rate), and reports completion %,
DIFOT, lead time, and station utilisation - each tracked separately for
Thermo (target 10 working days) and Cut & Clash (target 7 calendar days,
unconfirmed whether that's really calendar or working days).

## Running it

```
streamlit run app.py
```

Tab 1 (Staff & Skills) edits the roster; Tab 2 (Simulation Results) runs
it and shows the outcome, including a live animated factory-floor view.

## Data provenance - what's real, what's guessed

| Station | Rate | Status | Source |
|---|---|---|---|
| Optimising | 57.16 m²/op-hr | **REAL** | 90th-pct daily "Optimisation" scan total ÷ real op-hours (3-month export) |
| CNC (Thermo) | **10,000 m²/month** (4 machines, 5-day × 16 h basis) | **REAL** (Thermo capacity overview) | the per-series cut times are still old-tool placeholders, scaled to hit this total; their S1/S2/S3 ratios are assumptions |
| CNC 1536 (Cut & Clash) | cut-time model, 1 machine | ASSUMPTION | same as above |
| Edge bander / Drilling | 13 m²/op-hr each, one operator each, in series | ASSUMPTION | split into two stations (session 4b) to match the floor; each keeps the old combined station's 13 m²/hr throughput at the real 1+1 staffing. No real checkpoint for either yet |
| Manual Sanding | **10,000 m²/month** (= CNC line) | **REAL, per the plant** | sanding right behind the CNCs with the MB running non-stop keeps pace with the CNCs (the overview's 4,500 "manual profile sanding" figure is the standalone rate) |
| MB Sander | **63,000 m²/month**, one operator | **REAL** (Thermo capacity overview) | far faster than anything feeding it - never the constraint. (The ~8,007 m²/month used earlier was the volume passing through, not capability) |
| Cefla Automated Gluing Line | 12.95 m²/op-hr | **REAL** | confirmed capacity 9,000 m²/month (was wrongly split into two stations "Edging" + "Glue" - merged) |
| Press 1 / Press 2 | **9,500 m²/month each** at a 3-person crew | **REAL, per the plant** | the overview's 21,000/press is theoretical. Machine-paced: fewer than 3 people runs slower, more doesn't run faster |
| Despatch/Packing | 9.12 m²/op-hr, **people-driven** | **REAL** | average of the real "Packing" and "Despatch" checkpoints; capacity = rate × people present, so absenteeism hits it directly |
| Remake rate | 5.0% of packed m² | **REAL** | 2026 monthly waste report: 2,079 m² remade of 42,010 m² output Jan-Jun (4.95%); 315 m²/month average Jan-Aug. (The lead-time dashboard's 7.03% is a *parts* ratio - remade parts are small, so the m² rate is lower; the engine works in m².) |
| Remake lead-time penalty | +0.32 working days | **REAL** | live lead-time dashboard. The 0.32 is the *total* extra time a remake takes; the engine's `remake_days` is the hold before re-entry, and the results tab reports the simulated penalty next to the real one |
| Thermo avg lead time | 8.66 working days | **REAL** | live lead-time dashboard (8.64 non-remake / 8.97 remake) - used by the reality-check panel |
| Product mix (Thermo / Cut & Clash) | 80.6% / 19.4% | **REAL** | product_2026.xlsx. S1/S2/S3 split within Thermo still ASSUMPTION |
| Staff roster (who, where, which shift) | 44 people | **REAL** | shop-floor sheet, Sep 2026 (see session 4); Press 1/2 split and cross-skills still ASSUMPTION |
| C6 = special orders + most remakes | lane rule | **REAL** (per the plant) | C6 cuts specials/remakes first, the other CNCs cut regular work first, crossover only when a lane runs dry |
| Special-order share of Thermo intake | 10% | ASSUMPTION | no data yet - sidebar input `Special orders (% of Thermo intake)` |
| Intake timing | weekdays, first 8 h of day shift | ASSUMPTION | office hours; was spread over all 168 h/week before session 3 |
| Target lead time - Thermo | 10 working days | **REAL** | confirmed, excludes weekends |
| Target lead time - Cut & Clash | 7 calendar days | UNCONFIRMED | assumed calendar days pending confirmation |

Real intake, production-hours, and DIFOT-history reference ranges are in
`plant_sim/config.py` (`REAL_INTAKE_M2`, `REAL_PRODUCTION_HOURS_PER_MONTH`,
`REAL_DIFOT_HISTORY_PCT`).

## What changed this session (chronological)

1. **MB Sander had zero dedicated staff.** It was only given as a guessed
   skill to Press operators, leaving it unstaffed 320 of 730 active hours.
   Confirmed fact: the Sanding team runs it. Split each Sanding shift's
   roster between `sanding` and `mb_sander` home stations (day 2/2, aft
   1/1), cross-skilled so allocation can still flex.
2. **Removed cold-start bias.** Every run started with empty queues,
   understating completion%/DIFOT for the first ~1-2 lead-times. Added a
   21-day warm-up (`cfg.SIMULATION_WARMUP_DAYS`) that runs before the
   reporting window and isn't counted in the KPIs.
3. **Merged duplicate "Edging" and "Glue" stations** into one real
   station, the Cefla Automated Gluing Line (confirmed 9,000 m²/month),
   and moved its 2 real staff onto it (now 3 people total, was 1).
4. **Calibrated Optimising, Press 1/2, and Despatch** from a real 3-month
   WorkArea scan-checkpoint export (daily part counts + m² per station).
5. **Fixed MB Sander's calibration a second time**: the scan data's
   combined "Sanding" checkpoint total was first split 7:1 between
   manual sanding and MB Sander, which wrongly made manual sanding the
   bottleneck. Corrected per the user: MB Sander is a mandatory serial
   funnel (everything passes through the one machine after manual
   sanding, which has no machine limit) - gave the full real ceiling to
   MB Sander instead, reverted Sanding to a generous non-bottleneck rate.
6. **Fixed two roster shift errors**: Toupou Lolo and Romal Baldevind
   (CNC Thermo) were wrongly defaulted to `day`; confirmed `aft` per the
   staff sheet. This gave CNC (Thermo) its first confirmed afternoon
   coverage.

Net effect on the month-horizon smoke test (median real intake, 7,300
m²/month): completion % went from an artificially-inflated early number
down to a low of 29% (mid-fix, when MB Sander was wrongly the
bottleneck) and has settled at **~70%**, with CNC 1536 now the clearest
constraint (99% utilised).

## Session 3 - engine correctness pass (16 Sep 2026)

A line-by-line review of the engine for things that were *physically
wrong* (as opposed to uncalibrated). Every item below changed results;
none of them is a tuning knob.

**Modelling errors fixed**

1. **Orders arrived around the clock, 7 days a week.** Intake was spread
   over every hour of the horizon, so ~29% of each week's orders landed
   on the weekend and aged before anyone could touch them. Cut & Clash's
   calendar-day lead time was 16 days (0% DIFOT) purely from this. Orders
   now arrive on weekdays during office hours (`INTAKE_WEEKDAYS_ONLY`,
   `INTAKE_WINDOW_HOURS`), and the entered intake is spread over exactly
   the intake hours inside the reporting window, so the Intake KPI always
   equals what you typed. Month horizon is now a flat 30 days for the same
   reason (4.345 weeks ended mid-day).
2. **Parts could travel the whole line in one hour.** Stations were
   processed upstream-first, so a board cut at 9am could be sanded, MB-
   sanded, glued, pressed and packed by 9:59. Now downstream-first
   (`PROCESSING_ORDER`): at least one hour per station.
3. **One plant-wide shift handover at hour 10.** The Cut & Clash crews
   hand over at hour 8, so their day people were credited with hours 8-9
   and the afternoon operator lost them. Each station now reads the
   allocation for whichever shift *its own crew* is on.
4. **Crews' days off were invisible to the allocator.** The shift snapshot
   ignored the weekday, so on a Friday (Thermo crews off) a cross-skilled
   Thermo CNC operator sat "working" on an idle CNC instead of floating to
   CNC 1536. Snapshots are now weekday-aware; the attendance log gained a
   `day_off` status so this is visible.
5. **The allocator could not see the shared press pile.** Backlog pressure
   was looked up per station id, and the presses' work lives under the
   shared "press" queue - so Press 1 and Press 2 always read as having
   zero backlog, were never helped, and were treated as the quietest
   stations to poach from. Fixed via `cfg.queue_id_for()`; pressure pools
   both presses' people against the one pile.
6. **Floaters were parked on full machines.** Sending a 2nd person to the
   single-machine CNC 1536 (or a 6th to five Thermo CNCs) added nothing but
   showed as "working". `Station.max_useful_ops` now caps this; surplus
   people are the first to be moved elsewhere; nobody is moved twice in a
   shift (the old loop could ping-pong one person back and forth).
7. **Extra hands made machines run faster.** A 3rd person on the Cefla
   line or the MB Sander increased its rate linearly. `machine_bound`
   stations (MB Sander, Cefla) now cap at their crew rate. Presses are
   deliberately left linear because their real rate was derived per
   op-hour across 8 people - flip `machine_bound` once a per-press cycle
   time is known.
8. **DIFOT didn't mean what the dashboard means.** It was on-time /
   (completed + overdue-in-WIP), which double counts: work still in the
   factory would be counted late again when it eventually completed. Now
   on-time completed / completed, with overdue backlog reported separately.
9. **Remakes went to the back of the queue.** A remake re-entered the line
   behind three weeks of newer work and cost a whole extra lead time, which
   contradicts the measured +0.32 days. Queues are now oldest-order-first,
   so a remake (which keeps its original order date) is expedited. The
   results tab shows the simulated penalty vs the real 0.32.
10. **Working-day count ignored partial weekend days.** Friday noon to
    Saturday noon counted as a full working day. Now exact to the hour.
11. **Default product mix had a data-entry slip.** The S1/S2/S3 shares
    were meant to be rescaled to Thermo's real 80.6% but the ×0.806 step
    was missing; after normalisation Thermo got 83.8% of intake. The mix
    is now built from the route split and within-route weights explicitly.
12. **Manual Sanding at 6.5 m²/op-hr was the plant's worst bottleneck**
    (100% utilised, ~4,500 m²/month capacity against ~5,900 m²/month of
    Thermo intake), which contradicted both the note calling it
    "deliberately generous / not the constraint" and the scan data showing
    ≥8,007 m²/month passing through sanding + MB Sander. Set to the
    checkpoint-derived 11.52 m²/op-hr (same as the MB Sander). **Please
    confirm** - this is the single biggest lever on Thermo's numbers.
13. **`SimulationSettings` defaulted every horizon to the monthly intake**,
    so a `day` run built outside the UI pushed 7,300 m² through in one
    day. The default now follows the horizon.

**Robustness added**

- Mass-balance self-check every run (intake = completed + WIP, else it
  refuses to report a result).
- Roster validation with every problem listed (unknown station, bad
  shift, duplicate id, absence rate out of range, missing columns); the
  UI shows these instead of crashing. Atomic CSV save.
- Settings validation (horizon, mix, targets, schedules, capacities);
  impossible shift schedules (day + afternoon > 24 h) disable the Run
  button with a message. Zero capacity on a CNC no longer divides by zero.
- Product mix that doesn't sum to 100% is normalised (and says so) so
  the intake total is honoured.
- Config self-check at import (`validate_config()`), so a typo in a
  station table fails loudly with a readable message.
- Staffing-gap diagnostics: hours a station was scheduled with work
  waiting and nobody on it (a utilisation chart cannot show this).
- Reality-check panel: simulated month vs the real completion %, DIFOT,
  Thermo lead time and remake penalty recorded in `config.py`.
- 41 regression tests (`python -m unittest discover -s tests`).

**Net effect on the month smoke test (7,300 m²/month, no sick leave)**

| | Before session 3 | After |
|---|---|---|
| Completion % | 70% | 97% |
| DIFOT (both) | 24% | 88% |
| Thermo avg lead (working days) | 11.7 | 4.7 (real: 8.66) |
| Thermo DIFOT | 29% | 100% |
| Cut & Clash avg lead (calendar days) | 16.3 | 7.1 (target 7) |
| Cut & Clash DIFOT | 0% | 43% |
| Top utilisation | Sanding 99%, CNC 1536 99% | CNC 1536 100%, MB Sander 98%, Press 1 98% |

## Session 4 - the real shop-floor sheet (17 Sep 2026)

The roster was rebuilt from the actual shop-floor sheet (day / afternoon
by machine). What it told us, and what changed:

1. **Thermo CNC is 4 machines, not 5** - 1224, C6, Weeke 100 (old), Weeke
   480 (new) - one operator each per shift. The Weeke 480 is *covered* by a
   Press operator on both shifts (Bang days, Jeremy afternoons), so those
   two are rostered to Press with `cnc_thermo` as a skill.
   `num_machines` 5 → 4 (default CNC capacity 9,233 → 7,392 m²/month).
2. **CNC 1536 does have a day operator (Nirmal)** - closes the old open
   question #2. Jerald stays on afternoons and also covers the edge bander.
3. **The MB Sander is run by ONE person per shift** (Quyen days, Viet
   afternoons). The 2-person infeed/outfeed model and its "45% with one
   operator" rule were old-tool assumptions - removed. The real ~8,007
   m²/month ceiling now belongs to a single operator (`ideal_ops=1`,
   23.04 m²/op-hr); a second person can't make the machine faster.
4. **Manual sanding** is 1 person days (Hai), 2 afternoons (Bao, Jett).
5. **Edge bander (Ali) and Drill (Eric)** are both days only - no afternoon
   drill operator on the sheet. Now two separate stations in series
   (Optimising → B1536 → Edge bander → Drilling → Dispatch), each at 13
   m²/op-hr so the pair keeps the old combined throughput [ASSUMPTION].
6. **Hafele and DIY** sit under Dispatch on the sheet but are box-packed
   lines with their own assigned packers (Ben, Agnes / Dayna, Arona).
   Modelled as two non-flow stations under the finishing crew: staffed and
   shown on the floor, but they don't count toward Thermo / Cut & Clash
   packing capacity. Their people carry `despatch` as a skill so the
   allocator can pull them across when packing is drowning.
7. Moves vs the earlier sheet: Ahkuino → Cefla aft, Viavia → Cefla aft,
   Vili Fonua → Despatch aft, Prabh → Despatch day, Bang/Jett/Bao
   re-sorted between manual sanding and MB. Four temps added (Vaa, Peter,
   Zach, Anish), two new names (Ricky, Vili Alofi), two people no longer on
   the sheet removed (Lloyd Tadlip, Stuart Lyon - "Stuart (rotation)" on
   Press is assumed to be Stuart Taueetia).

**Net effect (month, 7,300 m², no sick leave):** completion 97% → 99%,
DIFOT 88% → 100%, Cut & Clash lead 7.1 → 3.5 days (CNC 1536 100% → 72%),
Thermo lead 4.7 → 4.4 working days. The tightest stations are now the
four Thermo CNCs (99%) and manual sanding (97%).

Also from session 4: the roster gained an optional **machine** column
(which named machine a person normally runs - 1224 / C6 / Weeke 100 /
Weeke 480, Edge bander / Drill). It's descriptive only: capacity still
comes from the station's machine count and rates. The floor map uses it
to seat people on the right tile.

## Session 4c - C6 as the specials / remakes machine

Per the plant, C6 is mostly used for special orders and takes the majority
of remakes. The Thermo CNCs are now worked as two lanes: **C6** cuts
specials and remakes first, the other three machines cut regular work
first, and each only takes the other kind of work when its own lane runs
dry. Whether C6 is manned comes from the roster's `machine` tag (people
with no tag - cover / floaters - fill the production machines first and
C6 last). "Special order" is a new share of Thermo intake (10%
[ASSUMPTION]) tagged on the work. The results tab shows C6 vs other-CNC
utilisation, the % of remakes and specials actually cut on C6, and the
hours C6 was unmanned.

Effect at the real roster: remakes and specials are 100% on C6; moving
Raymond (C6 days) to Press leaves C6 unmanned 130 of 360 running hours
and lifts the Thermo remake penalty from +0.77 to +0.93 working days.

## Session 4d - covering absences, and the skills constraint

- **Cover pass in the allocator.** Any station short of its ideal crew
  (someone away, or simply a thin roster) gets a qualified person pulled
  in, in this order: CNC, Cefla, Press, manual sanding, MB Sander, then
  the Cut & Clash stations (`cfg.ALLOCATION_COVER_PRIORITY`). Donors come
  from stations with people to spare; a non-priority station (Dispatch,
  Hafele, DIY) will lend even if it drops below its own ideal, a priority
  station won't; no station is ever emptied; nobody is moved anywhere
  they aren't skilled for. On the real roster with sick leave on, a month
  shows ~37 cover moves for ~46 absent person-shifts - mostly Dispatch
  → CNC (Prabh) and Dispatch → Press (Vili Fonua).
- **Skills constraint on the floor.** A person can only be dropped on a
  station that is their home or one of their extra skills; other drops
  are refused (red). New skills are given on the Staff & Skills tab.

## Session 4f - anyone can be split across two stations

Generalised from the CNC case: Ctrl-drag anyone onto a second station
they're skilled for (same shift) and they work both - half a person at
each (`SPLIT_STATION_SHARE` = 0.5 [ASSUMPTION]). A CNC operator's second
Thermo CNC is the exception and keeps the unattended-machine rule below.
The split only applies while they're at their home station and the
second station is running on their shift; if the allocator moves them to
cover elsewhere they're a whole person there. Roster columns `station_2`
/ `machine_2`; the playback shows them on both boxes as "(also)".

## Session 4e - one operator running two CNCs

Per the plant, one CNC operator often runs two machines at once (typically
on Series 3, where the machine cuts unattended for a long time). The
roster gained a `machine_2` column; on the floor, Ctrl-drag a CNC operator
onto another CNC to have them run both (they stay on their own machine, a
ghost chip appears on the second, × to stop). Their own machine runs at
full rate; the second runs at `CNC_SECOND_MACHINE_FACTOR` = 0.8
[ASSUMPTION - close to 1 for long Series 3 cuts, lower for quick Series 1
boards]. A real operator on that machine caps it at full rate. Nobody on
the current roster is set up this way yet - it's there for what-ifs.

## Session 4g - real remake / waste series

Monthly 2026 remake report added (`REAL_REMAKE_WASTE_2026`): remake count,
m² scrapped, m² per remake. Remakes are ~5% of output by m² (4.95% of all
m² Jan-Jun; 5.8% of Thermo m²), 315 m²/month on average Jan-Aug. The
engine remakes a share of packed m², so the default rate moved from the
parts-based 7.03% to **5.0%** - it was producing ~510 m²/month of rework
against a real ~315. The reality-check panel now compares the month's
remade m² with the real 163-468 range. Seasonality (Feb-May high, ~1,000+
remakes/month; Jan and Jun-Aug ~600) isn't modelled - the rate is flat.

## Session 5 - Theory of Constraints analysis

The Results tab gained a **Constraints** section (`plant_sim/constraints.py`),
one block per line, built from the run just done (no automatic what-ifs):

- **Identify**: the constraint = the station with the highest utilisation
  that is also backing up (a *bottleneck*); if it's busy but keeping up
  it's reported as a *CCR*; if nothing is above 85% the constraint is
  *external* (intake). Plus "next in line". Thresholds at the top of the
  module are analysis settings, not plant facts.
- **Evidence** per station: utilisation, labour utilisation (productive
  share of staffed hours), flat-out / starved / unstaffed / blocked hours,
  queue start → end, buffer in **hours of that station's own work**,
  headroom m²/week.
- **Policy constraints**: crews running fewer days than orders arrive,
  no afternoon shift, single-machine one-person stations, unstaffed hours,
  C6 unmanned, blocked-by-full-buffer hours.
- **Buffers**: thin buffer in front of the constraint (< 8 h) flagged;
  fat buffers elsewhere (> 24 h) flagged as waiting, not protection.
- **Where to improve**, TOC order: Exploit (unstaffed / starved / blocked
  hours at the constraint), Subordinate (upstream over-feeding), Elevate
  (+1 person, +2 h/day, 5th day, with m²/week estimates and named
  cross-training candidates), Next.
- **Staff utilisation**: per station (labour %) and a per-person time
  study (rostered / producing / waiting / covering / absent). Box packing
  and admin aren't simulated, so those people are listed but not rated.

Two engine changes came out of building it:
- **The presses now share their pile in proportion to capacity** (Press 1
  used to be processed first and hog the work: 83% vs 25%; now 57/57).
- **Real buffer limits** can be entered per station (sidebar → Buffer
  space limits, or `cfg.BUFFER_CAP_M2_BY_STATION`); a full buffer blocks
  the station before it and the analysis reports the blocked hours.
- The sheet's "cover" people are now rostered as splits: Jerald 1536 +
  edge bander (afternoons), Bang / Jeremy Press + Weeke (new).

At the median month the read-out is: Thermo constraint = Manual Sanding
(97%, queue growing), next Dispatch; Cut & Clash CCR = B1536 (94%,
keeping up); policy constraints = 4-day Thermo crews vs 5-day intake, no
afternoon drill operator, one-person MB Sander.

## Session 5b - Thermo capacity overview (5-day × 16 h basis)

The plant's capacity overview gave per-area ceilings on exactly the model's
reference schedule (80 h/week), so they slot straight in as "capacity at
ideal staffing": CNCs 10,000 m²/month (the placeholder cut times are now
scaled to hit it), MB Sander 63,000 (never a constraint), Cefla 9,000
(unchanged), presses **9,500 each** (per the plant; the sheet's 21,000 is
theoretical), and manual sanding set equal to the CNC line's 10,000
because sanding runs right behind the CNCs with the MB non-stop. Packing
stays people-driven so absenteeism flows straight into its capacity.

## Session 5c - protective buffers and press batching

Per the plant: the Cefla is the production bottleneck after manual
sanding, so the floor deliberately keeps WIP in front of the Cefla and in
front of the presses at all times, running the Cefla to build a pile the
presses then "smash out".

- **Protective buffer targets** (`cfg.BUFFER_TARGET_M2_BY_QUEUE`, sidebar):
  250 m² in front of the Cefla (~a shift of its work [ASSUMPTION]) and
  50 m² in front of the presses ("never empty"). The constraints section
  checks them every running hour and reports average / minimum / hours
  below target / hours empty; those buffers are no longer flagged as
  "waiting, not protection".
- **Press batching** (`press_batch_start_m2` / `_stop_m2`, sidebar): the
  presses wait for the pile to reach 300 m² [ASSUMPTION], run until it is
  down to 50, then wait again. Idle-by-choice hours are reported as "held"
  (a policy, not a capacity limit) and press capacity is still counted, so
  press utilisation reads ~33% rather than a false 99%.

**What the model says at the median month:** the pile in front of the
Cefla does NOT build (avg ~29 m², below target every hour) - with the
overview's capacities the Cefla (9,000 m²/month) out-runs everything
feeding it, and at ~5,900 m²/month of Thermo intake nothing upstream is
running ahead of it. In other words the model does not yet see the Cefla
as a bottleneck at all (69% utilised), which contradicts the floor's
experience - see open question 0g.

## Open questions (need your input, not guessable from data)

0. **Thermo is now *faster* than the real plant** (4.7 vs 8.66 working
   days average) once the modelling errors are out. The engine measures
   each m² from order to despatch; the dashboard measures orders/parts,
   and an order waits for its slowest part, gets batched, and queues for
   scheduling in ways a pure capacity model doesn't see. The two knobs
   that stand for "time outside the floor" are `pre_prod_days` (1.5) and
   `post_prod_days` (0.5) in `simulation.py` - if the ~4-day gap is mostly
   order-processing / scheduling / delivery, raise those; if it's floor
   congestion, the station rates are too generous. Real order-level data
   (order date, release-to-floor date, despatch date) would settle which.
0b. ~~Cut & Clash DIFOT is 43%~~ **Resolved (session 4)** by the real
   roster: with Nirmal on 1536 days, Cut & Clash runs at 100% DIFOT and
   3.5-day lead time.
0g. **Why does the floor see the Cefla as a bottleneck?** In the model it
   runs at ~69% (9,000 m²/month capacity vs ~5,900 of Thermo intake) and
   never backs up. Either its real throughput is lower than 9,000 (glue
   changeovers, downtime, one-person running?), or CNC/sanding output
   arrives in bursts. A few days of actual Cefla m²-per-shift from the
   scan data would settle it and let the buffer target be set properly.
0f. **How fast does a second CNC really run** when one person tends two?
   Currently 80% of a fully-tended machine, regardless of series.
0e. **What share of Thermo intake is a special order?** Currently a 10%
   placeholder in the sidebar.
0d. **Press 1 vs Press 2**: the sheet just says "Press"; the 6 day / 5
   afternoon people are split between the two machines as a placeholder.
   Also, is the MB Sander genuinely a one-person job (as rostered), or is
   the second person simply missing from the sheet? The rate now assumes
   one person delivers the full ~8,007 m²/month.
0c. **The two real intake sources disagree on the route split**: the
   13-month tracker gives Cut & Clash ~13% of m² (984 of 7,409/month);
   product_2026.xlsx gives 19.4%. The mix uses 19.4%; the monthly intake
   preset uses the tracker's 7,300. Worth deciding which one the sim
   should be judged against.

1. **Does Optimising process Thermo work too, or just Cut & Clash?** The
   real Optimising volume (~9,900 m²/month) is nearly as large as total
   combined intake, not just Cut & Clash's ~19% share. If Thermo also
   passes through it, `ROUTE_SEQUENCE` in `config.py` needs a change.
2. ~~CNC 1536 has zero confirmed day-shift staff~~ **Resolved (session 4)**:
   Nirmal runs it on days, Jerald on afternoons.
3. **Edge Band/Drilling's real rate** - no matching checkpoint existed in
   the 3-month export; still a guess (6.5 m²/op-hr).
4. **Cut & Clash's target lead time** - confirmed as 7 days, but working
   days (like Thermo) or calendar days? Currently assumed calendar.
5. **The real SQL "Remake" business rule** - the naive "more than one
   Barcode per OrderID" logic was wrong (flagged 100% of rows); still
   need the actual definition to rebuild it in the lead-time SQL.

## Useful next data to provide

- A version of the 3-month WorkArea export that also has an Edge
  Band/Drilling checkpoint (closes the eb_drilling gap).
- Confirmation on the Optimising-scope question above - if you can check
  whether any Thermo barcodes ever scan through "Optimisation", that
  settles it directly.
- Real CNC 1536 day-shift names, if any exist.
