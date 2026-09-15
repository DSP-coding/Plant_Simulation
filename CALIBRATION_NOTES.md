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
| CNC (Thermo) | cut-time model, 5 machines | ASSUMPTION | S1/S2/S3 min-per-board guesses carried from the old tool |
| CNC 1536 (Cut & Clash) | cut-time model, 1 machine | ASSUMPTION | same as above |
| Edge Band/Drilling | 6.5 m²/op-hr | ASSUMPTION | no matching real WorkArea checkpoint in the export |
| Manual Sanding | 6.5 m²/op-hr | ASSUMPTION (deliberately generous) | not the real constraint - see MB Sander |
| MB Sander | 11.52 m²/op-hr | **REAL** | everything sanded is fed through this one machine - real ceiling ÷ ideal_ops=2 on reference hours |
| Cefla Automated Gluing Line | 12.95 m²/op-hr | **REAL** | confirmed capacity 9,000 m²/month (was wrongly split into two stations "Edging" + "Glue" - merged) |
| Press 1 / Press 2 | 5.37 m²/op-hr each | **REAL** | 90th-pct daily "Thermoform Press" scan total (can't tell the two machines apart in the data) |
| Despatch/Packing | 9.12 m²/op-hr | **REAL** | average of the real "Packing" and "Despatch" checkpoints |
| Remake rate/days | 7.03% / 0.32 days | **REAL** | live lead-time dashboard |
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

## Open questions (need your input, not guessable from data)

1. **Does Optimising process Thermo work too, or just Cut & Clash?** The
   real Optimising volume (~9,900 m²/month) is nearly as large as total
   combined intake, not just Cut & Clash's ~19% share. If Thermo also
   passes through it, `ROUTE_SEQUENCE` in `config.py` needs a change.
2. **CNC 1536 has zero confirmed day-shift staff** (only Jerard Mendoza,
   aft) and is the current top bottleneck at 99% utilised. Real day-shift
   names, or is it genuinely aft-only?
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
