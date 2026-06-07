# Implementation Plan: FluxState Temporal Viewer ("Temporal Ghost")

**Branch**: `002-fluxstate-temporal-viewer` | **Date**: 2026-06-06 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `specs/002-fluxstate-temporal-viewer/spec.md`

## Summary

Build the **FluxState Temporal Viewer** — an interactive, time-travelling table UI — by reproducing the
LOCKED "Temporal Ghost" prototype (dev-spec §2/§3) *behaviorally* and wiring it to the **real `.flux`
change-log** through the already-shipped reconstruction primitives (`reconstruct.py` / `ChangeLogStore`).
Two precursor deliverables come first: (1) **a `flux` CLI** that makes the library fully driveable from the
terminal (capture / travel / timeline / row-state / view / gen-fixture / serve), and (2) a **seeded
1000×20 demo+stress fixture** whose *values* come from Faker + `numpy.random` and whose *history* comes from
flux `capture()`. Correctness is anchored by a **JS↔Python reconstruction parity check**; the runtime stays
lightweight (no Delta/Iceberg, no heavy grid lib; bounded caches).

## Technical Context

**Language/Version**: Python ≥3.10 (existing lib + new CLI + fixture + parity export); TypeScript / Svelte 5 (viewer)
**Primary Dependencies**: runtime unchanged (Polars, PyArrow); **viewer** = Svelte 5 + Vite + **DuckDB-WASM** (in-browser change-log querying); **CLI** = stdlib `argparse` (lightweight, no new runtime dep); **fixture** = Faker + numpy (demo/test-only); **no heavy grid lib** (virtualization hand-rolled, ~80 LoC)
**Storage**: the shipped append-only `<name>.flux/` store (`manifest.json` + immutable `events/*.parquet`) — glob-readable by DuckDB(-WASM)
**Testing**: pytest (CLI, fixture, parity-export); Vitest (viewer JS reconstruction unit + parity vs Python ground-truth JSON); Playwright (headed) for interaction + perf smoke (optional gate)
**Target Platform**: CLI on Linux/macOS; viewer in evergreen browsers with DuckDB-WASM
**Project Type**: hybrid — existing Python **library + CLI** at repo root + a new web **frontend** (`viewer/`)
**Performance Goals**: viewer ~60 fps on the 1000×20 fixture; **100k rows** scroll+scrub at ~60 fps with ~constant rendered-node count; CLI sub-second on the demo store
**Constraints**: lightweight (no Delta/Iceberg, no heavy grid lib); **bounded LRU** window cache (economy-first, no unbounded growth); type fidelity (decode via dtype tag); **JS reconstruction MUST match Python** ground truth; read a **single tracked table** at a time
**Scale/Scope**: acceptance fixture 1000 rows × 20 cols, ~30–60 capture points; stress target 100k rows; 7 user stories (US1 CLI pre-work, US2–US6 viewer, US7 fixture)

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

> **The project constitution (`.specify/memory/constitution.md`) is the unfilled SpecKit template** (placeholder
> principles). No concrete gates are defined. The de-facto constitution applied here is the project ethos
> **"lightweight above all"** plus the template's example principles, which this feature already honors:

| De-facto principle | This plan |
|---|---|
| Library-First | Viewer + CLI build on the existing `changelog.py`/`reconstruct.py` library; no logic re-implemented |
| **CLI Interface** | US1 delivers a `flux` CLI exposing every history op (text/JSON out) — *this is the user-requested pre-work* |
| Test-First | Parity tests + per-story acceptance written before/with implementation (TDD as in P1) |
| Integration Testing | JS↔Python reconstruction parity is the cross-boundary contract test |
| Observability / Simplicity | Hand-rolled virtualization (no grid lib), bounded caches, no new runtime deps |

**Gate result: PASS** (no violations; nothing to justify in Complexity Tracking). *Recommendation (non-blocking):*
fill the real constitution via `/speckit.constitution` so future gates are enforceable.

## Project Structure

### Documentation (this feature)

```text
specs/002-fluxstate-temporal-viewer/
├── plan.md              # This file
├── research.md          # Phase 0 — decisions (stack, reconstruction-parity strategy, data source)
├── data-model.md        # Phase 1 — entities (Change Event, Cell Timeline, Lifecycle, Density/Snaps, Fixture, CLI)
├── quickstart.md        # Phase 1 — run the CLI, generate the fixture, launch the viewer
├── contracts/
│   ├── cli.md            # the `flux` command surface (args → output)
│   ├── viewer-data.md     # how the viewer reads the store + the reconstruction primitive mapping (parity contract)
│   └── parity.schema.json # JSON shape the Python side exports as reconstruction ground truth for JS to assert against
└── tasks.md             # Phase 2 (/speckit.tasks — NOT created here)
```

### Source Code (repository root)

```text
# Existing Python library (unchanged contract) + new CLI/fixture
changelog.py                 # writer/diff/store (shipped)
reconstruct.py               # as_of/get_timeline/row_state/build_mirror_view (shipped)
fluxstate.py                 # facade (shipped)
flux_cli.py                  # NEW — `flux` CLI (argparse): capture/travel/timeline/row-state/view/gen-fixture/serve
scripts/
└── demo_fixture.py          # NEW — Faker + numpy.random values → ChangeLogStore.capture() → 1000×20 .flux store
TESTS/
├── test_cli.py              # NEW — `flux` subcommands == library outputs (US1)
├── test_demo_fixture.py     # NEW — seeded/reproducible; sparse change; ≥1 birth/delete/resurrection (US7)
└── test_parity_export.py    # NEW — Python exports reconstruction ground truth (parity.schema.json) for the viewer

# New web frontend — the viewer (Svelte 5 + Vite + DuckDB-WASM)
viewer/
├── src/
│   ├── lib/
│   │   ├── reconstruct.ts   # JS port of the reconstruction primitives (parity-tested vs Python)
│   │   ├── duckdb.ts        # DuckDB-WASM: load events/*.parquet, as-of slice (WHERE ts<=T), keyset paging, bounded LRU
│   │   ├── scrubber.svelte  # capsule scrubber + density histogram + event-snap (US2)
│   │   ├── table.svelte     # virtualized rows + as-of render + diff-on-scrub + lifecycle encodings (US2/US3/US4)
│   │   ├── inspector.svelte # hover peek + click full-history inspector (US3)
│   │   ├── filter.svelte    # simple + SQL WHERE → same DuckDB predicate (US5)
│   │   └── static-view.svelte # print/audit render (US6)
│   └── routes/+page.svelte  # app shell over a change-log source
├── tests/
│   ├── reconstruct.parity.test.ts # asserts JS reconstruction == Python ground-truth JSON (US2/US3 / SC-003)
│   └── interaction.spec.ts        # Playwright: scrub/diff/inspect/filter + perf smoke (SC-001/002/005)
└── vite.config.ts
```

**Structure Decision**: Hybrid. The existing flat Python package at repo root is the "backend"/library; it
gains a `flux_cli.py` launcher and a `scripts/demo_fixture.py` generator (no change to the shipped library
contract). The viewer is a self-contained Svelte 5 + Vite app under `viewer/` that reads the `.flux` store
**directly in-browser via DuckDB-WASM** and reconstructs through a JS port of `reconstruct.py` kept honest by
a parity test against Python-exported ground truth. Python deps stay as-is (Faker/numpy added to the
test/demo dev group only, alongside the existing test-only duckdb/pytz).

## Complexity Tracking

> No Constitution Check violations — section intentionally empty.
