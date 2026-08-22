# OPM Flow feedback — grounded proposals from driving Flow via MCP

Observations collected while building and using [opm-mcp](https://github.com/ojaogezi/opm-mcp)
to run OPM Flow **2025.10** (apt packages, WSL2 Ubuntu-24.04 and native Linux)
against SPE1-class decks, the Equinor gas-lift suite, and `opm-tests/gconinje`.
Each item states the evidence, why it matters to a working reservoir engineer,
and a concrete proposed change. Written 2026-08-22; intended to be lifted into
upstream issues.

A quick search of the upstream trackers found no existing issue covering items
1–2 (nearest restart-related issues,
[opm-simulators#5485](https://github.com/OPM/opm-simulators/issues/5485) and
[opm-simulators#720](https://github.com/OPM/opm-simulators/issues/720), concern
different problems).

---

## 1. PVT completeness checks should fail at parse/validation time, not at problem allocation

**Component:** opm-common (parser / EclipseState), opm-material.

**Evidence (reproduced 2026-08-22).** Take a valid live-oil deck and remove the
undersaturated extension from the *last* Rs node of `PVTO`:

```
  0.9354 4014.7  1.5160  0.580 /     <-- no undersaturated branch
/
```

- `Parser().parse()` + `EclipseState` + `Schedule` construction: **accepted**, no
  diagnostics.
- `flow` run: aborts after grid processing with
  `Could not allocate the problem: [LiveOilPvt.cpp:142] PVTO tables are
  invalid: The last table must exhibit at least one entry for undersaturated oil!`

**Impact.** On a large model the user pays full deck parse + grid construction
(minutes) before learning about a table defect that is knowable from the deck
text alone. Deck-QC tooling built on opm-common's Python bindings (the natural
place to validate) reports the deck as clean.

**Proposal.** Hoist the LiveOilPvt/DeadOilPvt/WetGasPvt table-shape invariants
(undersaturated branch presence, monotonicity) into a consistency check that
runs at EclipseState construction — or expose them via the existing
`--check-satfunc-consistency`-style mechanism (`--check-pvt-consistency`) and
report them with deck file/line context.

---

## 2. `rst_deck` should emit `UNIFIN` when the restart source is a unified file

**Component:** opm-common (`rst_deck` utility).

**Evidence (reproduced 2026-06-08).** History deck with `UNIFOUT` (writes
`CASE.UNRST`). `rst_deck HIST.DATA CASE:2 OUT.DATA -m inline -s` produces a
deck whose RUNSPEC is copied verbatim — `UNIFOUT` present, `UNIFIN` absent.
Running the generated deck aborts:

```
Problem with keyword RESTART
The restart file .../SPE1_MINI.X0002 does not exist
```

because without `UNIFIN` flow expects separate `.X000N` files, while the history
run produced a unified `.UNRST`. Inserting `UNIFIN` fixes the run (our
`make_restart_deck` tool now does this automatically).

**Impact.** The single most common restart configuration (`UNIFOUT` history →
prediction) produces a deck that cannot run as generated. The failure message
points at a file the user never expected to exist.

**Proposal.** `rst_deck` knows the restart source it was given; when that source
resolves to a unified restart file, insert `UNIFIN` into RUNSPEC (or warn). A
one-line addition that makes the generated deck self-consistent.

---

## 3. Solver diagnostics: document the INFOSTEP/INFOITER schema and add a machine-readable problem-cell report

**Component:** opm-simulators.

**Evidence.** `--output-extra-convergence-info=steps,iterations` produces
genuinely excellent per-step/per-iteration tables — we built a convergence
analyzer on them. But the schema (column meanings, units, the `Conv` flag
semantics, `LocSol`, penalty columns) is undocumented; we reverse-engineered it
from source and experiment. Worst-cell information (which IJK failed CNV) exists
only as free text in the PRT.

**Impact.** Everyone building tooling (dashboards, LLM copilots, CI regression
checks) re-derives the schema, and "which cells are killing my timestep" —
the first question on any hard run — requires PRT text scraping.

**Proposal.**
- A short reference section in the Flow manual (or a header comment block in
  the files) documenting each column and its unit.
- `--output-extra-convergence-info=cells` (or extend `iterations`) writing the
  N worst CNV/MB cells per failing iteration as structured columns (I, J, K,
  phase, residual) — the machine-readable twin of the PRT's problem-cell text.

---

## 4. Well convergence-shut cascades: surface the *reason* and damp control-mode oscillation

**Component:** opm-simulators (well model / gas-lift optimization).

**Evidence (GASLIFT-12, Equinor opm-tests, flow 2025.10, run 2026-06-11).**
MSW + `LIFTOPT`/`WLIFTOPT` gas-lift optimization case:

- Default run: all 9 wells (including injectors) are convergence-shut one by
  one from ~19-May-2020; the field is dead by 1-Aug; FOPT ≈ 759 ksm³. Root
  cause observed: ORAT↔THP control-mode oscillation of MSW wells sitting at the
  30-bara THP floor (first casualty C-1H, the highest-WCT well).
- `--solver-max-time-step-in-days=5`: completes the year, B-2H survives, FOPT ≈
  932 ksm³ — **+19% cumulative oil from a stepping flag alone**.
- `--enable-tuning=true` (deck TUNING, TSMINZ = 0.1 d): aborts at 2020-04-01.
- Contributing geometry: VFP table 1's THP axis tops out at 35 bara while wells
  operate from ~35.2 bara (permanent edge extrapolation); ALQ axis spacing
  (31k) is far coarser than the LIFTOPT increment (12.5k).

**Impact.** A user who runs this case once gets a dead field and a low FOPT
with rc=0 and no prominent indication that the outcome is numerical, not
physical. Results this sensitive to stepping flags silently undermine screening
studies.

**Proposal (separable pieces).**
1. **End-of-run well-event summary** in PRT (and ideally a small structured
   file): per well, when it was shut/stopped and *why* — economic limit,
   physical constraint, or repeated convergence failure. A closing line like
   `WARNING: 9 wells shut due to convergence failures — results may be
   timestep-sensitive` would have saved a day of diagnosis.
2. **Hysteresis/damping option for control-mode switching** (ORAT↔THP) when a
   well oscillates at a THP limit across Newton iterations — e.g. hold the
   switched mode for N substeps before allowing a switch back.
3. **VFP extrapolation warning**: when a well's operating point sits at/beyond
   a VFP axis edge for a sustained period, say so once in the PRT (table id,
   axis, fraction of time extrapolating).

---

## 5. Document the 2025.10 per-phase gas RESV group-control semantics and refresh `opm-tests` references

**Component:** opm-tests, release notes / manual.

**Evidence (opm-tests `gconinje`, commit `eae07d22`, run 2026-06-11).**
GCONINJE-02's stored reference was generated by an older flow in which a group
gas `RESV` target was charged with the **water** injection reservoir volume
(GVIR:PLAT-A pinned at 380 rm³/d total → gas got ~120 rm³/d; GGIT 2.89 Msm³).
flow 2025.10 applies RESV per phase (gas alone gets 380 rm³/d; GGIT 9.16 Msm³ —
**3.17×** the stored reference; end FPR 1.0 bar higher). The 2025.10 reading
matches E100 semantics and is the defensible one.

**Impact.** Anyone using the bundled references for regression QC sees large
"failures" on injection vectors that are semantic, not solver error — and may
be tempted to tune decks toward the stale reference.

**Proposal.** Regenerate the `gconinje` references with current flow, and add a
release-note/manual paragraph stating the per-phase RESV interpretation for
group injection control (with the E100-compatibility rationale).

---

## Context for reviewers

Environment: flow 2025.10 (`~noble` apt packages), WSL2 Ubuntu-24.04 on
Windows 11 and ubuntu-latest CI; results read via `resdata`; runs orchestrated
by the opm-mcp server (job manager captures logs, exit codes, INFOSTEP/INFOITER).
Reproducer decks: `examples/SPE1_MINI.DATA` (+ scripted mutation) for item 1;
any `UNIFOUT` deck for item 2; `opm-tests` Gaslift/model5 and gconinje suites
for items 4–5.
