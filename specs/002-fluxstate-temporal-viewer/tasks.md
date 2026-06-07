# Tasks: FluxState Temporal Viewer ("Temporal Ghost")

**Input**: Design documents from `specs/002-fluxstate-temporal-viewer/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/ (cli.md, viewer-data.md, parity.schema.json), quickstart.md

**Tests**: INCLUDED — the spec mandates them (SC-003 reconstruction parity gate; SC-004 fixture reproducibility;
SC-006 full CLI capability; quickstart §3/§4). New tests are written per story and MUST fail before implementation.

**Organization**: Tasks grouped by user story (US1–US7) for independent implementation/testing. Priority order
from spec.md: US1 (P1, CLI pre-work) → US7 (P1, fixture) → US2 (P1, viewer core) → US3 (P2) → US4 (P2) →
US5 (P3) → US6 (P3).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: parallelizable (different files, no incomplete-task dependency)
- Python lib/CLI/fixture at repo root; viewer is a Svelte 5 + Vite app under `viewer/` (per plan.md Structure Decision)

---

## Phase 1: Setup (Shared Infrastructure)

- [x] T001 Add the `flux` console entry point (`flux = flux_cli:main`) and demo/test-only dev deps (`faker`, `numpy` already present, `duckdb`, `pytz`) to `pyproject.toml` `[tool.poetry.group.dev.dependencies]`; confirm `import fluxstate` and `flux --help` succeed in the venv (no NEW runtime dep)
- [x] T002 [P] Scaffold the viewer app in `viewer/`: Svelte 5 + Vite + DuckDB-WASM (`viewer/package.json`, `viewer/vite.config.ts`, `viewer/src/routes/+page.svelte` shell, `viewer/index.html`); `npm install` clean
- [x] T003 [P] Create skeletons: `flux_cli.py` (argparse subcommand stubs), `scripts/demo_fixture.py`, `viewer/src/lib/reconstruct.ts`, `viewer/src/lib/duckdb.ts` (typed stubs)

---

## Phase 2: Foundational (Blocking Prerequisites)

**⚠️ CRITICAL**: the data-access + JS reconstruction + parity-export substrate every viewer story depends on.

- [x] T004 Implement the DuckDB-WASM data layer in `viewer/src/lib/duckdb.ts`: open a `<name>.flux/` store, read the manifest-valid `events/*.parquet` (manifest is authoritative, STORE-4), as-of slice `WHERE timestamp <= T` with manifest file-skip pruning, keyset-paged windows + prefetch buffer, **bounded LRU** window cache (R2 / VD-1/VD-4)
- [x] T005 [P] Implement the JS reconstruction port in `viewer/src/lib/reconstruct.ts`: `asOf`/`_as_of`, `getTimeline`, `changeCount`, `rowState`, `buildMirrorView`, `snapPoints`, `densityBuckets`; decode values via the `dtype` tag; honor genuine-null vs `__deleted__`, single-marker deletion, same-`entity_id` resurrection, UTC compare (data-model + contracts/viewer-data §2)
- [x] T006 Implement the Python parity-export harness in `TESTS/test_parity_export.py`: build a known store (incl. a delete, a resurrection, a genuine-null cell), run a probe grid `(entity_id, field, T)` + whole-view at several `T`, write `parity.schema.json`-shaped ground-truth JSON (must_include_cases all true) (contracts/parity.schema.json)

**Checkpoint**: data access + JS primitives + parity ground truth ready — CLI and viewer stories can begin.

---

## Phase 3: User Story 1 - FluxState fully CLI-capable + launcher (Priority: P1) 🎯 pre-work

**Goal**: Drive the whole change-log from the terminal (capture/travel/timeline/row-state/view/info/gen-fixture/serve).
**Independent Test**: each `flux` subcommand's output equals the library function; `gen-fixture --seed` reproducible.

### Tests (write first, MUST fail)

- [x] T007 [P] [US1] `TESTS/test_cli.py`: CLI-1 capture idempotency (2nd differing→1 file, 3rd identical→no-op), CLI-2 `travel --as-of`==`build_mirror_view` + before-history empty(not error), CLI-3 `timeline`/`row-state`==`reconstruct.*` typed (incl. deleted+resurrected), CLI-4 `gen-fixture --seed` byte-identical (contracts/cli.md)

### Implementation

- [x] T008 [US1] Implement read subcommands in `flux_cli.py`: `travel`, `timeline`, `row-state`, `view`, `info` as thin wrappers over `reconstruct.*` / `FluxState.save_mirror_table` / `read_manifest`; text + `--json`; errors→stderr, non-zero on failure (FR-015)
- [x] T009 [US1] Implement write/launch subcommands in `flux_cli.py`: `capture` (→`ChangeLogStore.capture`), `gen-fixture` (→`scripts/demo_fixture.py`), `serve` (launch `viewer/` Vite over the chosen store, print URL) (FR-015/016)
- [x] T010 [US1] Wire the `flux = flux_cli:main` entry point; make `TESTS/test_cli.py` green (CLI-1..5)

**Checkpoint**: FluxState is fully CLI-driveable (SC-006).

---

## Phase 4: User Story 7 - 1000×20 demo + stress fixture (Priority: P1) 🎯 the final test

**Goal**: One seeded fixture that is BOTH the stress test and the demo.
**Independent Test**: generator reproducible; sparse change; ≥1 birth, delete, resurrection; viewer exercises every feature.

### Tests (write first, MUST fail)

- [x] T011 [P] [US7] `TESTS/test_demo_fixture.py`: same `--seed`→byte-identical store; 1000×20 mixed dtypes; immutable cols never change across history; per-step change ~1–5% (not all cells); ≥1 birth, ≥1 `__deleted__`, ≥1 resurrection present (SC-004)

### Implementation

- [x] T012 [US7] Implement `scripts/demo_fixture.py`: **Faker** (names/PCPs/categorical states/dates) + seeded **numpy.random** (floats/ints) build each time-step `pl.DataFrame`; column classes immutable (`id`/`birth_date`/`cohort`/`mrn`) / mutable (`risk`/`score`/`last_seen`) / categorical (`status A→B→C`/`tier`); 20 cols, 6 dtypes (research R5 / data-model Demo Fixture)
- [x] T013 [US7] Drive the passage of time via `ChangeLogStore.capture()` over ~30–60 seeded snapshots with **sparse** curated changes (~1–5%/step), scripted births/deletions/resurrections, and a few volatile hotspots; expose `--rows/--cols/--steps/--seed`; **NOT** flux-generated values
- [x] T014 [US7] Generate the canonical `demo.flux` (`flux gen-fixture demo.flux --seed 42`); make `TESTS/test_demo_fixture.py` green

**Checkpoint**: a real, reproducible, legible 1000×20 store the viewer reads.

---

## Phase 5: User Story 2 - Time-travel the table (Priority: P1) 🎯 viewer core / MVP

**Goal**: Scrub the date slider → cells snap to as-of values with daff-style diff-on-scrub.
**Independent Test**: scrub across history → correct typed as-of values + transitions; matches CLI/library `as-of`.

### Tests (write first, MUST fail)

- [x] T015 [P] [US2] `viewer/tests/reconstruct.parity.test.ts`: load the Python ground-truth JSON (T006) and assert `reconstruct.ts` `asOf`/`getTimeline`/`buildMirrorView` are identical for every probe incl. deleted/resurrected/null (SC-003 / VD-2)

### Implementation

- [x] T016 [US2] Implement `viewer/src/lib/scrubber.svelte`: capsule slider; **change-density histogram** track (from `densityBuckets`); elapsed fill + round knob; **event-snap** over `snapPoints` for ←/→ ‹/› + Play; ▶/⏸, ⟳ loop, step, `as of <date>` readout (FR-005/006/007)
- [x] T017 [US2] Implement as-of render + diff-on-scrub in `viewer/src/lib/table.svelte`: each cell `asOf(T)` decoded-typed; `~~old~~ → **new**` + amber flash; **pinned** on discrete step, **linger-then-fade** on play/drag; `⇄ diff` toggle (FR-008)
- [x] T018 [US2] Wire viewer shell (App.svelte) shell over the store (`duckdb.ts` + `reconstruct.ts` + scrubber + table); load `demo.flux`; make the parity test (T015) green

**Checkpoint**: the demonstrable time-travel viewer (MVP).

---

## Phase 6: User Story 3 - Inspect cell history & row lifecycle (Priority: P2)

**Goal**: Hover peek + click inspector + lifecycle/heat encodings.
**Independent Test**: hover/click across a store with deletes+resurrection; inspector shows full lifecycle w/ slider-tracked "now".

### Tests (write first, MUST fail)

- [x] T019 [P] [US3] Extend `viewer/tests/reconstruct.parity.test.ts` with `rowState` + lifecycle-timeline probes (active/deleted/unborn/resurrected) asserted against Python ground truth

### Implementation

- [x] T020 [US3] Implement `viewer/src/lib/inspector.svelte`: hover popover (last 3 `{date,value}`); click → pinned full history (inline-SVG sparkline + every event incl. `__deleted__`/resurrection) with a "now" marker that tracks the slider (FR-009)
- [x] T021 [US3] Implement row-lifecycle encodings in `viewer/src/lib/table.svelte`: heat-tint `v1..v4` (per-cell volatility), deleted red stripe+strikethrough, resurrected green edge `✦`, unborn faded `·`, per-row Δ gutter (FR-010)

**Checkpoint**: time-travel + full audit inspection.

---

## Phase 7: User Story 4 - Scale to large tables (Priority: P2)

**Goal**: 100k rows scroll+scrub ~60fps, constant DOM.
**Independent Test**: open a 100k-row store; rendered-node count stays ~constant; only changed cells repaint.

### Tests (write first, MUST fail)

- [x] T022 [P] [US4] `viewer/tests/interaction.spec.ts` (Playwright): 100k synthetic rows scroll+scrub ~60fps; rendered-row count ~constant; delta-only repaint; bulk-pill when a step flips many visible cells (SC-002) — 5 specs pass; scrub step avg ~44ms/max ~49ms at 100k; rendered rows ≤~37 across a 3.4M-px scroll; delta-only proven (≤15/608 cells diffed/step). Run: `npm run test:e2e` (needs the 100k store + dev server).

### Implementation

- [x] T023 [US4] Implement row virtualization (done via freeze-fix) in `viewer/src/lib/table.svelte`: ~25–40 absolutely-positioned rows over a sized spacer, recycle on scroll; constant DOM (FR-011)
- [x] T024 [US4] Implement delta-only cell repaint (done via freeze-fix) + change-throttling (suppress per-cell flashes on mass change → bulk pill) in `viewer/src/lib/table.svelte` (FR-011)
- [x] T025 [P] [US4] Verify/extend streamed windowing + prefetch + bounded-LRU in `viewer/src/lib/duckdb.ts` at 100k scale; add a 100k synthetic path to `scripts/demo_fixture.py` / `flux gen-fixture --rows 100000` (FR-012) — `isLargeStore`/`totalEvents`/`fetchEntityOrder`/`fetchWindowEvents` added to duckdb.ts (bounded-LRU windows via existing `windowCacheKey`); App.svelte branches small (load-all, demo unchanged) vs large (windowed); new `windowed-table.svelte` virtualizes over the full entity-id list + streams each visible window. `flux gen-fixture --rows 100000 --steps 18` → 100k entities / 2.77M events (gen ~2m43s, ~1.7GB RSS; not committed).

**Checkpoint**: viewer holds at production size.

---

## Phase 8: User Story 5 - Filter rows (Priority: P3)

**Goal**: simple + SQL filter compiling to the same predicate; stable row set; "showing X of N".

### Tests (write first, MUST fail)

- [x] T026 [P] [US5] `viewer/tests/filter.test.ts`: an equivalent simple filter and SQL `WHERE` return identical rows; meta-fields (`changes`/`deleted`/`resurrected`/`id`) filterable; invalid SQL → inline error, prior result preserved; "showing X of N" accurate (SC-007)

### Implementation

- [x] T027 [US5] Implement `viewer/src/lib/filter.svelte`: simple controls (category selects, numeric range, min-changes, id-contains) + SQL `WHERE` box, both compiling to the **same** DuckDB predicate; default **stable row set** (not re-evaluated per T); inline error on invalid; "showing X of N" (FR-013)

**Checkpoint**: focused audits via filtering.

---

## Phase 9: User Story 6 - Static / print audit view (Priority: P3)

**Goal**: non-interactive, print-ready audit render.

### Tests (write first, MUST fail)

- [x] T028 [P] [US6] `viewer/tests/static-view.test.ts`: renders latest value + prior-value ghost (↑/↓), lifecycle spark-path column, auto-callouts on most-volatile cells; `@media print` styles applied

### Implementation

- [x] T029 [US6] Implement `viewer/src/lib/static-view.svelte`: ghosted latest values, leading lifecycle spark-path, auto-callouts on volatile cells, print stylesheet (FR-014)

**Checkpoint**: all 7 stories independently functional.

---

## Phase 10: Polish & Cross-Cutting Concerns

- [x] T030 Run the full Python suite `pytest TESTS/ -q` (CLI + fixture + parity export) → green, no Snowflake (SC-006)
- [x] T031 Run the viewer suites `cd viewer && npm run test && npm run test:e2e` → JS parity 100% (SC-003) + interaction/perf smoke (SC-001/002/005)
- [x] T032 [P] Execute `quickstart.md` end-to-end: `flux gen-fixture` → `flux serve` → walk the locked interaction model → parity → suite
- [~] T033 [P] Performance validation: confirm SC-001 (1000×20 ~60fps) and SC-002 (100k rows ~60fps, constant rendered-node count) — **SC-002 CONFIRMED** (US4): 100k store scrolls + scrubs with a constant ~37-row DOM (≤4-row spread across a 0→3.4M-px scroll) and scrub steps avg ~44ms / max ~49ms (well under the ~100ms responsive bar), no OOM/freeze, no console errors; verified in-browser + by `viewer/tests/interaction.spec.ts`. SC-001 (1000×20) tracked separately; the demo was regression-verified intact (load-all path, filter+sort+print view, no streamed badge).
- [x] T034 [P] Update `README.md` to document the `flux` CLI, the viewer (`flux serve`), and the demo fixture; note Faker/numpy are demo/test-only

---

## Dependencies & Execution Order

### Phase dependencies

- **Setup (P1)** → **Foundational (P2)** blocks all stories.
- **US1 (CLI)** depends only on Foundational (the library is already shipped); enables `gen-fixture`/`serve`.
- **US7 (fixture)** depends on Foundational + US1's `gen-fixture` (T009); produces the store every viewer story reads.
- **US2 (core)** depends on Foundational (duckdb.ts, reconstruct.ts, parity ground truth) + a store (US7).
- **US3/US4** extend `table.svelte` from US2 (soft dependency; each independently testable via its own store/probe).
- **US5/US6** depend on US2's shell; independently testable.
- **Polish** depends on the stories delivered.

### Within a story

- Tests written first and FAIL before implementation.
- `duckdb.ts` (read) + `reconstruct.ts` (compute) before the Svelte components that consume them.
- Parity test green is the gate for US2/US3 (FR-003 / SC-003).

### Parallel opportunities

- Setup: T002, T003 parallel.
- Foundational: T005 parallel with T004; T006 (Python) parallel with both.
- US1 tests T007 parallel; US7 test T011 parallel.
- Cross-story after Foundational: US1 (Python) and the viewer foundational pieces can proceed alongside each other (different trees: `flux_cli.py`/`scripts/` vs `viewer/`).
- Polish: T032, T033, T034 parallel.

---

## Implementation Strategy

### MVP scope

**US1 (CLI) + US7 (fixture) + US2 (time-travel core)** = the demonstrable MVP: a real seeded store you can
drive from the CLI and scrub in the viewer, with reconstruction proven equal to the Python engine.

### Incremental delivery

1. Setup + Foundational → data access + JS primitives + parity ground truth ready.
2. US1 → full CLI capability (pre-work) → validate.
3. US7 → reproducible 1000×20 demo/stress store → validate.
4. US2 → time-travel + diff-on-scrub (parity-gated) → **STOP & DEMO** (MVP).
5. US3 → inspect + lifecycle; US4 → scale; US5 → filter; US6 → static/print.
6. Polish → full suites green, quickstart e2e, perf validation, README.

### Notes

- Lightweight above all: no new runtime dep (stdlib argparse CLI; Faker/numpy demo-test-only); no heavy grid lib.
- The viewer reconstructs ONLY from the real change-log via DuckDB-WASM; the parity test forbids drift from `reconstruct.py`.
- Deferred (NOT in these tasks): checkpoint compaction, live-tail watcher, `pack`/`unpack` export, storage adapters.

---

## Phase: User Story 8 - Anomaly Watcher + change direction (added 2026-06-06, user request)

**Goal**: Flag rows where a watched event occurs — primarily a change to an IMMUTABLE column (a
data-integrity violation) — highlight them red at the moment it happens, surface change DIRECTION
(+/− ↑/↓) on numeric changes, and let the user collect flagged rows via the filter.

- [x] T035 [US8] Seed demonstrable immutable-column violations into `scripts/demo_fixture.py` (a `--violations N` option / a few rows whose `cohort`/`mrn`/`birth_date` changes once mid-history); regenerate `demo.flux`. These are the anomalies the watcher catches.
- [x] T036 [US8] In `viewer/src/lib/reconstruct.ts`: `immutableViolations(index, immutableCols)` → set of entity_ids (+ the offending field/time) whose immutable column ever changed; and a `changeDirection(prev, next)` helper (sign/↑↓ + signed delta) for numeric cells. Parity-safe (additive; don't change existing fn semantics).
- [x] T037 [US8] In `viewer/src/lib/table.svelte`: highlight flagged (immutable-violation) rows RED (at/after the violation time); render change DIRECTION (↑ green / ↓ red + signed delta) on numeric diff-on-scrub cells (+ inspector).
- [x] T038 [US8] Filter pane (`viewer/src/lib/filter.svelte`, US5) + a `flagged`/`violation` meta-field so the user can collect the watched rows; simple + SQL modes compile to the same predicate; "showing X of N". (Absorbs/extends US5 T026/T027.)
