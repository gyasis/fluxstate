# Implementation Plan: Pluggable Storage Backends + Tabular Store + Platform Sidecars

**Branch**: `003-pluggable-storage-backends` | **Date**: 2026-07-01 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `specs/003-pluggable-storage-backends/spec.md`

## Summary

Introduce a **pluggable `StorageBackend` interface** so the FluxState change-log engine persists through a
single seam instead of directly to the local filesystem. Ship three backends — **LocalFolderStore**
(default, behaviour-identical to today), **ObjectStoreBackend** (fsspec: s3/abfss/gcs/UC Volume), and
**TableBackend** (the change-log as a first-class table: `flux_events` always + optional materialized
`flux_mirror`, default Parquet dataset with Delta/Iceberg opt-in) — and a first **Databricks sidecar** as
an optional extra. The change-event model and `reconstruct.py` stay untouched; only *where/how bytes
persist* becomes swappable. Platform code lives ONLY in sidecars/extras (Constitution G8); core deps stay
Polars + PyArrow.

## Technical Context

**Language/Version**: Python ≥ 3.10
**Primary Dependencies**: **core** — Polars, PyArrow (unchanged). **Optional extras** — `[remote]`: fsspec
(+ s3fs / adlfs / gcsfs); `[table]`: deltalake and/or pyiceberg (Parquet dataset path needs no extra dep);
`[databricks]`: deltalake + databricks-sdk (+ pyspark at runtime on the platform).
**Storage**: today `<name>.flux/` folder (`manifest.json` + `events/*.parquet`). Adds: object-store
location (same layout, remote); first-class table (`flux_events` narrow EAV + optional `flux_mirror` wide),
default a partitioned Parquet dataset, Delta/Iceberg opt-in.
**Testing**: pytest (`TESTS/`), hermetic (in-memory `pl.DataFrame`s); NEW cross-backend
reconstruction-parity suite; DuckDB used test-only to prove glob-readability.
**Target Platform**: cross-platform Python library + `flux` CLI; the Databricks sidecar runs on Databricks
(Spark + Delta).
**Project Type**: single-project library + CLI.
**Performance Goals**: capture stays O(changed rows) (existing row-hash-prefiltered keyed diff); reconstruction
parity is **byte-identical** across backends; **zero regression** in the existing suite.
**Constraints**: core imports **no** platform SDK / heavy table-format lib (G1/G8); every backend commit is
**atomic** and **append-only/immutable** (G4); public API preserved (G3).
**Scale/Scope**: single-table stores; large-store windowing thresholds (first snapshot > 50k entities OR
> 200k events) are reused from 001/002; single-writer per store (v1).

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Gate | Assessment |
|---|---|
| **G8 Platform-Agnostic** (checked first) | ✅ PASS — storage is an interface; local/object/table backends are engine-generic; **all** platform code (Databricks/Delta) lives in `sidecars/` + optional extras; core imports zero platform SDK. Adding a platform = adding a sidecar (FR-015). |
| **G7 Faithful Recorder** | ✅ PASS — capture/diff semantics unchanged; no semantic-equality normalization added (FR-014). |
| **G1 Lightweight** | ✅ PASS — core stays Polars+PyArrow; fsspec/deltalake/pyiceberg/databricks-sdk are **opt-in extras** only (FR-007, SC-004). |
| **G2 Portable / Glob-readable** | ✅ PASS — folder + object-store keep glob-readable Parquet; TableBackend default is a plain Parquet dataset; Delta/Iceberg are open formats, opt-in (FR-004). |
| **G3 API back-compat** | ✅ PASS — backend selection is additive (URI inference + explicit override); local default unchanged; pre-existing stores read with no migration (FR-002, FR-012, SC-002). |
| **G4 Append-only/Immutable/Idempotent** | ✅ PASS — every backend appends immutable units, commits atomically, dedups by `snapshot_id` (FR-004, FR-009, FR-013). |
| **G5 Test coverage** | ✅ PASS (planned) — capture/reconstruct/delete-resurrect/idempotency/type-fidelity + **cross-backend parity** (FR-011, SC-003). |
| **G6 Simplicity / YAGNI** | ✅ PASS — single-writer v1, `flux_mirror` opt-in, only the Databricks sidecar now; concurrency-locking / compaction / other sidecars are fast-follow. |

**Result: no violations.** Complexity Tracking left empty.

## Project Structure

### Documentation (this feature)

```text
specs/003-pluggable-storage-backends/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/           # Phase 1 output (storage-backend, tables, backend-selection)
├── checklists/
│   └── requirements.md  # from /speckit-specify + /speckit-clarify
└── tasks.md             # Phase 2 output (/speckit-tasks — NOT created here)
```

### Source Code (repository root)

```text
fluxstate.py                 # facade — additive backend selection wiring (G3)
changelog.py                 # REFACTOR: depend on the StorageBackend protocol, not the FS directly
reconstruct.py               # UNCHANGED (pure over an events array)
storage/                     # NEW — the pluggable storage layer (core)
├── __init__.py              # public surface + select_backend() (URI inference + explicit override)
├── base.py                  # StorageBackend Protocol, capability flags, Meta / EventRef types
├── local_folder.py          # LocalFolderStore — refactor of today's writer/reader (default)
├── object_store.py          # ObjectStoreBackend (fsspec)                — [remote] extra
└── table.py                 # TableBackend: flux_events (+ opt flux_mirror); Parquet default,
                             #   Delta/Iceberg opt-in                       — [table] extra
sidecars/                    # NEW — optional platform integrations (NEVER imported by core; G8)
└── databricks/              # fluxstate[databricks]
    ├── __init__.py          # DeltaBackend wiring over TableBackend
    └── job_template.py      # scheduled-Job / notebook capture template
TESTS/
├── test_local_folder.py     # back-compat / regression (existing behaviour preserved)
├── test_object_store.py     # NEW  ([remote]; fsspec memory/local + optional live)
├── test_table_backend.py    # NEW  ([table]; flux_events + flux_mirror; Parquet/Delta)
├── test_storage_parity.py   # NEW  — reconstruction parity across ALL backends (G5/G6)
└── (existing capture / reconstruct / delete-resurrect / idempotency / type-fidelity tests)
pyproject.toml               # add [project.optional-dependencies]: remote, table, databricks
docs/DATABRICKS.md           # already drafted — update the sidecar section when it lands
```

**Structure Decision**: single-project library + CLI. A new **`storage/`** package holds the
`StorageBackend` protocol + the three backends; **`sidecars/`** holds optional platform integrations that
core never imports (shipped as extras). `reconstruct.py` stays pure and unchanged; `changelog.py` is
refactored to write/read through the protocol, with `LocalFolderStore` as the extracted default so existing
`.flux/` behaviour is bit-for-bit preserved.

## Complexity Tracking

> No Constitution Check violations — section intentionally empty.
