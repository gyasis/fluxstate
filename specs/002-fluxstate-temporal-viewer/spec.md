# Feature Specification: FluxState Temporal Viewer ("Temporal Ghost")

**Feature Branch**: `002-fluxstate-temporal-viewer`
**Created**: 2026-06-06
**Status**: Draft
**Input**: PRD `~/dev/prd/scratch/fluxstate_temporal_viewer_2026-06-06.md` + LOCKED dev-spec `docs/viewer/fluxstate-viewer-dev-spec.md` + reference prototypes in `docs/viewer/`. Downstream consumer of the shipped change-log store (PRD `library/L001_fluxstate_changelog_pivot`).

## Overview

An interactive viewer over **one FluxState-tracked table/view** that lets a user **time-travel** the
table (scrub a date slider → every cell snaps to its as-of value), **see transitions** (daff-style
`old → new` in-cell during scrub), inspect any cell's **full change history**, read **row lifecycle**
(deleted / resurrected / unborn), and **filter** rows — at thousands-to-millions of rows. Mental model:
*"daff aligns two tables; FluxState remembers all of them."*

The viewer consumes the **real `.flux` change-log** (the shipped store) through the **already-implemented
reconstruction primitives** — it does not invent or re-derive history. As foundational **pre-work**, this
feature also makes FluxState **fully driveable from the command line** (a `flux` CLI launcher) so the store
can be captured, reconstructed, inspected, fixture-generated, and the viewer launched without writing code.

### Locked / out-of-scope boundaries

- **LOCKED:** the "Temporal Ghost" interaction model and the data contract from the dev-spec §2/§3. The
  visual skin may be re-polished; behavior is reproduced exactly from the reference prototypes.
- **OUT OF SCOPE (fast-follow):** checkpoint compaction, live-tail watcher, `pack`/`unpack` export, pluggable
  storage adapters.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - FluxState fully CLI-capable + launcher (Priority: P1) — *pre-work*

A user drives the whole FluxState change-log from the terminal: capture a snapshot into a `.flux/` store,
reconstruct the table as-of any date, inspect a cell's timeline, read a row's lifecycle state, materialize
the current/as-of view, generate the demo fixture, and **launch the viewer** — all without writing Python.

**Why this priority**: It is the foundation the rest stands on (the user called it "pre-work before the main
work"). It exposes the history operations the viewer also uses, and it is how the demo fixture is generated
and the viewer is served. Independently valuable even before any UI exists.

**Independent Test**: Run the `flux` CLI against a `.flux/` store and confirm each subcommand returns
results identical to the library functions (`capture`, `travel`, `timeline`, `row-state`, `view`,
`gen-fixture`, `serve`/`view-ui`).

**Acceptance Scenarios**:

1. **Given** a directory and a source table, **When** the user runs the capture command twice on differing
   snapshots, **Then** the `.flux/` store gains exactly one new events file for the change and a re-capture
   of the same snapshot is a no-op (mirrors the library guarantees).
2. **Given** a populated `.flux/` store, **When** the user requests the table "as of" a past date, **Then**
   the CLI prints the reconstructed table with original types (not string-cast), and a date before history
   prints an empty result (not an error).
3. **Given** a populated store, **When** the user asks for a cell's timeline / a row's lifecycle state,
   **Then** the CLI returns the typed `[{date,value}]` series / `{state, resurrected}` exactly as the
   library reconstruction functions do.
4. **Given** the CLI, **When** the user runs the launch/serve command, **Then** the viewer opens against
   the chosen `.flux/` store.

---

### User Story 2 - Time-travel the table (Priority: P1) — *the core viewer*

A user scrubs a date slider and watches every cell snap to its value as-of that moment, with changed cells
showing a daff-style `old → new` transition in place.

**Why this priority**: This is the headline value — replaying a table's whole history is the thing no
two-table diff can do. It is the minimum demonstrable viewer.

**Independent Test**: Load a store, drag the scrubber across the history, and confirm cells update to the
correct as-of values and changed cells show the transition; verify against the CLI/library `as-of` output.

**Acceptance Scenarios**:

1. **Given** a tracked table with history, **When** the user moves the playhead to time T, **Then** every
   visible cell shows its latest value at or before T, typed correctly.
2. **Given** a discrete step (arrow/step/paused), **When** a cell changes across the step, **Then** the cell
   shows `old → new` (old struck, new emphasized) **pinned** until the user moves on; **When** playing or
   dragging, **Then** the transition **lingers then fades** to just the new value.
3. **Given** the time control, **When** the user presses play / step / the arrow keys, **Then** the playhead
   **snaps to actual change events**, not raw calendar days; a `⇄ diff` toggle disables the transition render.
4. **Given** the scrubber track, **Then** it shows a **change-density histogram** (busy vs quiet time), an
   elapsed-time fill, and a knob, with an `as of <date>` readout.

---

### User Story 3 - Inspect cell history & read row lifecycle (Priority: P2)

A user hovers a cell for a quick peek, clicks it to pin its full history, and reads at a glance which rows
are deleted, resurrected, or not-yet-born, and which cells change most.

**Why this priority**: Turns time-travel into an audit tool — the HIPAA/audit-trail use case. Builds on US2
but is independently testable.

**Independent Test**: Hover and click cells across a store containing deletes and a resurrection; confirm the
peek shows the last few values, the inspector shows the full lifecycle incl. `__deleted__`/resurrection with
a "now" marker that tracks the slider, and rows carry the correct lifecycle encoding.

**Acceptance Scenarios**:

1. **Given** a cell, **When** the user hovers, **Then** a lightweight popover shows its last 3 `{date,value}`.
2. **Given** a cell, **When** the user clicks it, **Then** a fixed inspector pins the **full** history
   (sparkline + every event including `__deleted__` and resurrection) with a "now" marker tied to the slider.
3. **Given** a row deleted then returned under the same identity, **Then** it renders a continuous
   `active → deleted → active` trail (deleted = red stripe, resurrected = green edge `✦`, unborn = faded `·`).
4. **Given** any row/cell, **Then** a per-row change-count gutter and a per-cell heat-tint (how often it
   changes) are shown.

---

### User Story 4 - Scale to large tables (Priority: P2)

A user opens a table of 100k+ rows and scrubs/scrolls without lag.

**Why this priority**: "Real" credibility — the viewer must hold up at production size, not just demos.

**Independent Test**: Open a 100k-row store, scroll and scrub; confirm smoothness and that only a small,
roughly constant number of rows are rendered at any time, and only changed cells repaint on a step.

**Acceptance Scenarios**:

1. **Given** a 100k-row table, **When** the user scrolls, **Then** only the visible window (~25–40 rows) is
   rendered and the rendered-node count stays roughly constant.
2. **Given** a scrub step, **When** few cells change, **Then** only those cells repaint; **When** a step
   flips a large fraction of visible cells, **Then** per-cell flashes are suppressed and a bulk indicator is
   shown instead.
3. **Given** a large store, **When** the user time-travels, **Then** only the visible window + a prefetch
   buffer of the as-of slice is loaded (not the whole table), with a bounded cache of fetched windows.

---

### User Story 5 - Filter rows (Priority: P3)

A user narrows the visible rows with simple controls or a SQL-style `WHERE`, and scrubs the filtered set.

**Why this priority**: Useful for focusing an audit; not required for the core demo.

**Independent Test**: Apply a simple filter and an equivalent SQL `WHERE`; confirm identical results, that
meta-fields (`changes`, `deleted`, `resurrected`, `id`) are filterable, that invalid SQL surfaces an inline
error, and that a "showing X of N" count is shown.

**Acceptance Scenarios**:

1. **Given** the filter sidebar, **When** the user sets simple controls (category selects, numeric range,
   min-changes, id-contains) OR types a `WHERE` expression, **Then** both compile to the **same** predicate
   and return identical rows.
2. **Given** an invalid `WHERE`, **Then** an inline error is shown and the prior result is preserved.
3. **Given** a filter, **Then** the default behavior is a **stable row set** the user then scrubs (filter is
   not re-evaluated at each time step).

---

### User Story 6 - Static / print audit view (Priority: P3)

A user exports a non-interactive, print-ready audit render.

**Why this priority**: Compliance artifacts / sharing; nice-to-have after the interactive core.

**Independent Test**: Render the static view of a store; confirm it shows latest value + prior-value ghost
subtext, a lifecycle spark-path column, auto-callouts on the most volatile cells, and prints cleanly.

**Acceptance Scenarios**:

1. **Given** a store, **When** the user opens the static view, **Then** each cell shows its latest value with
   a ghost subtext (prior value + ↑/↓) and a leading lifecycle spark-path, print-formatted.

---

### User Story 7 - End-to-end demo & stress with a realistic dataset (Priority: P1) — *the final acceptance test*

A user generates one realistic 1000-row × 20-column temporal dataset and uses it to both **stress-test** and
**demo** every feature.

**Why this priority**: It is the agreed **final acceptance test** — a single fixture that proves scale and
tells a legible story.

**Independent Test**: Run the fixture generator, open the resulting store in the viewer, and confirm every
lifecycle / diff / heat feature is visibly exercised while the table stays smooth.

**Acceptance Scenarios**:

1. **Given** the generator, **When** it runs, **Then** it produces a 1000×20 store whose **values** come from
   **Faker + seeded `numpy.random`** (names/PCPs/categories/dates from Faker; random floats/ints) and whose
   **history** is produced by the **flux capture** mechanism (the flux library simulates the passage of time,
   not the values).
2. **Given** the dataset, **Then** columns fall into deliberate classes — **immutable** (e.g. `id`,
   `birth_date`, `cohort`, `mrn` — never change), **mutable** (e.g. `risk`, `score`, `last_seen`), and
   **categorical/incremental** (e.g. `status: A→B→C`, `tier`) — across mixed datatypes
   (integer / decimal / text / boolean / timestamp / categorical).
3. **Given** the seeded history (~30–60 capture points), **Then** each step changes only a **sparse** subset
   of cells (~1–5% — "not all cells change"), and the history includes rows **born**, **deleted** (single
   marker), and **resurrected**, plus a few curated **volatile hotspots** so heat/callouts have a story.
4. **Given** the fixture in the viewer, **Then** it scrubs/filters/inspects smoothly and reproducibly (seeded
   → same dataset every run).

### Edge Cases

- **Date before any history** → empty result, never an error (CLI and viewer).
- **Genuine null vs deletion** → a real null cell is distinct from a `__deleted__` marker; the viewer must not
  conflate them.
- **A scrub step that changes a large fraction of visible cells** → suppress per-cell flashes, show a bulk pill.
- **Long values during `old → new`** → cells stay single-line (truncate); full value available in the inspector.
- **Invalid SQL filter** → inline error, prior result preserved.
- **Re-capturing an already-recorded snapshot** (via CLI) → no-op, store byte-identical.
- **Resurrection identity** → the returned row keeps the same `entity_id`; it is NOT a new row.

## Requirements *(mandatory)*

### Functional Requirements — Reconstruction fidelity (the data contract)

- **FR-001**: The viewer and CLI MUST reconstruct table state, cell timelines, and row lifecycle **only** from
  the real `.flux` change-log — never from JSON-in-cell blobs or synthetic in-memory arrays in any production path.
- **FR-002**: Reconstructed values MUST be returned in their **original types** (no string-cast loss), decoded
  via the recorded type tag.
- **FR-003**: The viewer's reconstruction results MUST **match the library's reconstruction ground truth**
  (as-of value, timeline, lifecycle state, change count) for the same store and time — verified by a parity check.
- **FR-004**: A deletion MUST read as a **single lifecycle marker** per vanished record (not one null per
  column); a record deleted then re-added MUST present **one continuous timeline under one identity**.

### Functional Requirements — Time control & rendering

- **FR-005**: Users MUST be able to move a playhead across the full history and have every visible cell snap to
  its as-of value at that point.
- **FR-006**: The time control MUST present a **change-density histogram** track, an elapsed fill, a knob, and an
  `as of <date>` readout, and MUST offer play / pause / loop / step and keyboard control.
- **FR-007**: Stepping/playing MUST **snap to actual change events**, not raw calendar days.
- **FR-008**: A changed cell MUST render an `old → new` transition that is **pinned on discrete steps** and
  **lingers-then-fades on play/drag**, with a toggle to disable it.

### Functional Requirements — History inspection & lifecycle

- **FR-009**: Hovering a cell MUST show a quick peek of its most recent values; clicking MUST pin its full
  history (including deletion/resurrection events) with a "now" marker that tracks the slider.
- **FR-010**: Rows MUST be visually encoded for lifecycle state (active / deleted / resurrected / unborn) and
  per-row change volume; cells MUST be tinted by how often they change.

### Functional Requirements — Scale

- **FR-011**: The viewer MUST render only the visible row window (roughly constant rendered-node count) and
  repaint only changed cells on a time step.
- **FR-012**: For large tables the viewer MUST load only the visible window + a prefetch buffer of the as-of
  slice, with a **bounded** cache of fetched windows (no unbounded memory growth).

### Functional Requirements — Filtering

- **FR-013**: Users MUST be able to filter rows via simple controls and via a SQL-style `WHERE`, both compiling
  to the **same** predicate over tracked columns and meta-fields (`changes`, `deleted`, `resurrected`, `id`);
  invalid expressions MUST surface an inline error; a "showing X of N" count MUST be shown. Default filter
  behavior is a **stable row set** (not re-evaluated per time step).

### Functional Requirements — Static view

- **FR-014**: A non-interactive, print-ready audit view MUST present latest values with a prior-value ghost, a
  lifecycle spark-path, and auto-callouts on the most volatile cells.

### Functional Requirements — CLI (pre-work)

- **FR-015**: FluxState MUST be fully driveable from a `flux` command-line launcher exposing the history
  operations: capture, as-of/time-travel, cell timeline, row lifecycle state, and as-of view materialization —
  with outputs equal to the library functions.
- **FR-016**: The CLI MUST be able to **generate the demo fixture** and **launch/serve the viewer** against a
  chosen `.flux/` store.

### Functional Requirements — Demo / stress fixture

- **FR-017**: The system MUST provide a **seeded, reproducible** generator that produces a 1000-row × 20-column
  store whose cell **values** come from Faker + `numpy.random` and whose **history** is produced by flux
  capture, with deliberate immutable / mutable / categorical column classes across mixed datatypes.
- **FR-018**: The generated history MUST exhibit **sparse** per-step change (~1–5% of cells), include record
  births, deletions, and resurrections, and include curated volatile hotspots — i.e. a legible story at
  stress-test scale. Faker/`numpy` are **demo/test-only**, never runtime dependencies.

### Key Entities

- **Change Event**: one recorded cell change — `(entity_id, timestamp, field, value, type-tag, snapshot_id)`;
  a deletion is a single reserved marker row.
- **Cell Timeline**: the ordered `[{date, value}]` series for one `(record, field)`, reconstructed on demand.
- **Row Lifecycle State**: `{state: active | deleted | unborn, resurrected: bool}` at a given time.
- **Density / Snap model**: the set of actual change-event timestamps (snap points) and a per-time-bucket
  change-count histogram.
- **Demo Fixture**: a seeded 1000×20 temporal store (Faker/numpy values + flux-captured history) used as the
  combined stress test and demo.
- **CLI command surface**: the `flux` launcher exposing capture / travel / timeline / row-state / view /
  gen-fixture / serve.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Scrubbing the date control updates the whole visible table to the correct as-of state with no
  perceptible lag (≤ ~16 ms/frame, ~60fps) on the 1000×20 fixture.
- **SC-002**: At **100k rows**, scrolling and scrubbing stay ~60fps and the rendered-row count stays roughly
  constant (independent of total rows).
- **SC-003**: Viewer reconstruction matches the library ground truth on **100%** of sampled cells/rows/times,
  including deleted and resurrected records (parity check passes).
- **SC-004**: The demo fixture generator is **reproducible** — the same seed yields the identical store every
  run — and produces sparse change (~1–5% of cells per step) with at least one birth, one deletion, and one
  resurrection visibly present.
- **SC-005**: Every interaction in the locked model (event-snap scrub, pinned/fading diff, hover peek, pinned
  inspector, lifecycle encodings, simple+SQL filter, static print view) is demonstrable on the fixture.
- **SC-006**: A user can perform every history operation (capture, time-travel, timeline, lifecycle, view,
  generate fixture, launch viewer) from the CLI without writing code.
- **SC-007**: Filtering returns identical rows for an equivalent simple filter and SQL `WHERE`, and shows an
  accurate "showing X of N".

## Assumptions

- The change-log store and its reconstruction primitives already exist and are correct (shipped in the
  changelog-first pivot, PRD `L001`); this feature consumes them, it does not re-implement history.
- The viewer reads a **single tracked table/view** at a time (multi-table dashboards are out of scope).
- For the demo, the `.flux/` store is read from a locally served location; remote/object-store sourcing is a
  fast-follow concern, not required for acceptance.
- "Filter follows time" is **not** the default — the default is a stable filtered row set the user then scrubs.
- Whole-table point-in-time across very deep history may be slower than a SQL snapshot; mitigations
  (checkpoints) are fast-follow and out of scope here.
- Faker + `numpy` are added as demo/test-only dependencies (alongside the existing test-only DuckDB/pytz);
  the runtime stays lightweight.
