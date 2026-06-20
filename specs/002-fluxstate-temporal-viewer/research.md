# Research: FluxState Temporal Viewer

**Feature**: `002-fluxstate-temporal-viewer` · **Date**: 2026-06-06

The design space was already explored and **locked** in `docs/viewer/fluxstate-viewer-dev-spec.md` (per the
user: do NOT re-derive). This file consolidates the decisions that govern the build; there were no open
`NEEDS CLARIFICATION` items in the spec — defaults were recorded in the spec's Assumptions.

---

## R1 — Reconstruction is reused, never re-implemented

**Decision**: The viewer + CLI reconstruct **only** through the shipped primitives — `reconstruct.as_of` /
`_as_of`, `get_timeline`, `change_count`, `row_state`, `build_mirror_view`, over `ChangeLogStore`. The JS
viewer ports these to `reconstruct.ts` but is held to the Python behavior by a **parity test**.
**Rationale**: one source of truth for as-of / lifecycle; prevents the UI from drifting from the engine (the
"feed our data the correct way" constraint). **Alternatives rejected**: a second, viewer-only history model
(drift + double-maintenance); JSON-in-cell (the very thing the P1 pivot removed).

## R2 — In-browser data access = DuckDB-WASM over `events/*.parquet`

**Decision**: The viewer loads the change-log with **DuckDB-WASM**, querying the manifest-valid
`events/*.parquet` (the store is glob-readable — STORE-3). As-of slices are `WHERE timestamp <= T`; large
tables use **keyset pagination** over the visible window + a prefetch buffer; fetched windows are held in a
**bounded LRU** (economy-first). **Rationale**: zero server, portable, parquet is the native format; matches
the dev-spec §4.3 "crush-before-render ≤5K at runtime" pattern. **Alternatives rejected**: ship the whole
table to JS (blows up at 100k+); a custom WASM reader (DuckDB already does it); a backend query service
(adds an ops surface; the store is already glob-readable). **Open**: glob `events/*.parquet` directly vs read
the manifest's file list first (STORE-4 says the manifest is authoritative; prefer manifest-driven list).

## R3 — Reconstruction parity (JS ↔ Python) is the cross-boundary contract test

**Decision**: A Python harness (`TESTS/test_parity_export.py`) exports reconstruction ground truth
(`contracts/parity.schema.json` shape) for a set of (entity, field, T) probes over a known store; the viewer
test (`reconstruct.parity.test.ts`) asserts the JS port returns identical results (incl. deleted /
resurrected). **Rationale**: makes "the viewer matches the library" (FR-003 / SC-003) a runnable gate, not a
hope. **Alternatives rejected**: trusting manual visual checks; re-querying Python from JS at runtime (couples
the browser to a Python process — defeats the portable, DuckDB-WASM design).

## R4 — Stack: Svelte 5 + Vite + DuckDB-WASM, no heavy grid lib

**Decision**: Per dev-spec §6 — Svelte 5 + Vite shell; DuckDB-WASM for data; **hand-rolled virtualization**
(~25–40 absolutely-positioned rows over a sized spacer; recycle on scroll, ~80 LoC); inline SVG sparklines;
`requestAnimationFrame` on slider/scroll. **Rationale**: keeps the lightweight constitution; avoids
ag-grid-class dependency tail. **Alternatives rejected**: ag-grid/tanstack-table (heavy, fights unified
virtualization + custom diff render); a charting lib for sparklines (inline SVG suffices).

## R5 — Demo/stress fixture: Faker+numpy values, flux capture for time (user correction)

**Decision**: `scripts/demo_fixture.py` generates each time-step's `pl.DataFrame` with **Faker** (names, PCPs,
categorical states, dates) + **seeded `numpy.random`** (floats, ints), then feeds the evolving snapshots
through **`ChangeLogStore.capture()`** to materialize the temporal history. The flux library does **not**
invent values — it simulates the *passage of time*. Columns are partitioned into **immutable / mutable /
categorical** classes; per-step change is **sparse (~1–5%)**; history includes births, deletions,
resurrections, and curated volatile hotspots. **Seeded → reproducible.** **Rationale**: realistic, legible,
reproducible data that is simultaneously the stress test (1000×20) and the demo story. Faker + numpy are
**demo/test-only** deps (like duckdb/pytz), never runtime. **Alternatives rejected**: flux generating values
(category error — flux records change, it doesn't synthesize data); pure `numpy.random` everywhere
(unrealistic names/categories, weak demo); hand-authored JSON (not reproducible, not real change-log).

## R6 — CLI is the pre-work launcher (stdlib argparse)

**Decision**: `flux_cli.py` exposes `capture`, `travel`, `timeline`, `row-state`, `view`, `gen-fixture`,
`serve` (launch the viewer over a chosen store) — text + `--json` output, thin wrappers over the library.
Stdlib `argparse` (no new runtime dep). Registered as a console entry point in `pyproject.toml`.
**Rationale**: the user-requested foundational pre-work; makes the whole store driveable without code and is
how the fixture is generated + the viewer served. **Alternatives rejected**: Click/Typer (extra runtime dep
vs the lightweight constitution); no CLI (forces code for every op — fails US1).

## R7 — Defaults carried from the spec (no clarification needed)

- **Filter = stable row set** the user then scrubs (not re-evaluated per T) — dev-spec §5 default.
- **Single tracked table** per viewer instance (no multi-table dashboard).
- **Store served locally** for the demo (remote/object-store sourcing is fast-follow).
- **Checkpoint compaction** for very deep history is **out of scope** (fast-follow); acceptance is the 1000×20
  fixture + a 100k synthetic scroll/scrub, both within live-reconstruction range.
