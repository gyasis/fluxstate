# Implementation Plan: Changelog-First Storage Pivot

**Branch**: `001-changelog-first-pivot` | **Date**: 2026-06-05 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `specs/001-changelog-first-pivot/spec.md`

## Summary

Make an **append-only change-log** the source of truth for FluxState's cell-level history.
Each capture computes a Polars **keyed join-diff with a row-hash prefilter** against prior
state, appends a single immutable `events/<ts>.parquet`, and updates a `manifest.json` — no
full-table rewrite. Values carry a `dtype` tag (ending string-cast loss) and timestamps are
UTC-normalized; a **full-outer join** records `__deleted__` and resurrection events under one
`entity_id`; a `snapshot_id` anti-join makes re-capture idempotent. The old JSON-in-cell mirror
table becomes a **reconstructed view** over the change-log so `save_mirror_table`, `travel`, and
`query_historical_value` keep working, and `save_mirror_table` gains
`output_format ∈ {polars, arrow, parquet, csv}`. Store stays lightweight and glob-readable
(`SELECT * FROM '<name>.flux/events/*.parquet'`) — no Delta/Iceberg. The 7 stale tests are
rewritten to the new API and decoupled from Snowflake; change-log/delete/resurrection/idempotency
tests are added.

## Technical Context

**Language/Version**: Python ≥3.10 (existing pyproject `python = "^3.10"`)
**Primary Dependencies**: polars ^1.5, pyarrow ^17, orjson ^3.10, numpy ^2.1, humanize, tqdm — all already declared; **no new runtime dependency** in P1 (daff is renderer-only / audit-bundle, fast-follow; no Delta/Iceberg)
**Storage**: `<name>.flux/` folder on local filesystem — `manifest.json` (schema + valid file list) + `events/*.parquet` (immutable, append = new file) + `checkpoints/` (reserved, populated in fast-follow). Atomic commit = write parquet → fsync → update manifest. Remote/S3/Snowflake-stage adapter is **out of P1 scope**.
**Testing**: pytest over `TESTS/`; new tests feed Polars `DataFrame`s directly (decoupled from live Snowflake/Snowpark used by the legacy `Unit_*` tests)
**Target Platform**: pure-Python library, OS-agnostic; the produced change-log parquet must also be readable by **DuckDB-WASM** in-browser (the Viewer consumer)
**Project Type**: single project — Python library (flat module layout, packaged via poetry)
**Performance Goals**: capture is O(rows) (preserve P0 dict-map win); wide-table safe at ~300 columns × ~500k rows via the row-hash prefilter (only ~1–5% of rows reach the melt); time-travel skips whole files via parquet row-group `min/max(ts)` metadata
**Constraints**: lightweight above all (no heavy table-format dep tail); parquet immutable (never rewrite an events file); glob-readable store (no opaque container); all timestamps UTC; capture atomic (manifest is the commit point); existing public API signatures preserved
**Scale/Scope**: one tracked table/view; designed up to ~10M rows before the documented DROP-trigger to dbt-snapshot/Streams applies (see design-decision §"DROP triggers")

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

The project constitution (`.specify/memory/constitution.md`) is an **unratified template** (placeholders
only). In its absence, the de-facto constitution is the PRD's locked principle — **"lightweight above all"** —
plus the design-decision record. Gates derived from those:

| Gate | Rule | Status |
|---|---|---|
| **G1 Lightweight** | No heavyweight table-format dependency (no Delta/Iceberg); P1 adds zero new runtime deps | ✅ PASS — reuses polars/pyarrow only |
| **G2 Portability** | Store is glob-readable parquet + plain-JSON manifest; no opaque container | ✅ PASS — `events/*.parquet` + `manifest.json` |
| **G3 API back-compat** | `save_mirror_table` / `travel` / `query_historical_value` keep working for existing callers | ✅ PASS — mirror becomes a reconstructed view; signatures preserved (additive `output_format`) |
| **G4 Append-only / immutable** | Never rewrite an events parquet; commit via manifest | ✅ PASS by design |
| **G5 Test coverage** | Capture, reconstruction, delete/resurrection, idempotency, type-fidelity covered; 7 stale tests green | ✅ PASS (planned) — enforced in `/speckit.tasks` |
| **G6 Simplicity / YAGNI** | Defer compaction, streaming reconstruction, pack/unpack, storage adapters | ✅ PASS — explicitly fast-follow |

No violations → **Complexity Tracking left empty**. No unresolved gates blocking Phase 0.

## Project Structure

### Documentation (this feature)

```text
specs/001-changelog-first-pivot/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/           # Phase 1 output
│   ├── public-api.md         # FluxState library public surface (the back-compat contract)
│   ├── changelog-store.md    # .flux/ folder layout + events parquet schema
│   └── manifest.schema.json  # manifest.json shape
├── checklists/
│   └── requirements.md  # spec quality checklist (from /speckit.specify)
└── tasks.md             # /speckit.tasks output (NOT created here)
```

### Source Code (repository root)

The repo is a **flat Python package** (`__init__.py` at root re-exports `FluxState` and the
validators). The pivot keeps that layout and adds two focused modules; `fluxstate.py` becomes a
thin facade that delegates to them.

```text
__init__.py              # re-exports FluxState (+ validators); add ChangeLogStore export
fluxstate.py             # FACADE: keeps save_mirror_table/travel/query_historical_value/
                         #   update_mirror_table signatures; delegates to changelog + reconstruct
changelog.py             # NEW — ChangeLogStore: keyed join-diff + row-hash prefilter,
                         #   full-outer-join delete/resurrection, snapshot_id idempotency,
                         #   append events/<ts>.parquet (row-group min/max ts), manifest commit
reconstruct.py           # NEW — reconstruction primitives over the change-log:
                         #   as_of / row_state / get_timeline / change_count + build mirror view,
                         #   save_mirror_table(output_format) [LazyFrame internal]
mirror_validator.py      # EXISTING — validator strictness DEFERRED (left as-is in P1)

TESTS/
├── Unit_1.py … Unit_7.py    # REWRITE to new API; decouple from Snowpark (feed pl.DataFrame)
├── Unit_8.py                # rewrite/retire per content (audit later)
├── test_changelog.py        # NEW — append-only, no-rewrite, manifest commit
├── test_idempotency.py      # NEW — snapshot_id anti-join (re-capture = no-op)
├── test_lifecycle.py        # NEW — delete + resurrection continuity (one entity_id)
├── test_reconstruct.py      # NEW — as_of/get_timeline ground-truth + dtype restore
└── test_output_formats.py   # NEW — polars/arrow/parquet/csv parity
```

**Structure Decision**: Single flat Python library (Option 1, flattened to match the existing
root-level module layout). Two new root modules (`changelog.py`, `reconstruct.py`) keep concerns
separated (write vs read) while `fluxstate.py` stays the public facade — the smallest change that
preserves the back-compat contract and the "lightweight" constitution.

## Complexity Tracking

> No Constitution Check violations — section intentionally empty.

## Phase 0 — Research

See [research.md](./research.md). The design is locked by the PRD + design-decision record, so
Phase 0 resolves the few genuine unknowns (test decoupling from Snowflake, row-hash prefilter
mechanics, parquet row-group metadata for file-skip, atomic manifest commit, arrow zero-copy
output) and records them as Decision / Rationale / Alternatives.

## Phase 1 — Design & Contracts

See [data-model.md](./data-model.md), [contracts/](./contracts/), [quickstart.md](./quickstart.md).
Entities and the change-log schema come straight from the design-decision record and the Viewer
data contract; the public-API contract pins the back-compat surface.
