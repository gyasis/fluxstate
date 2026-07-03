# Phase 0 Research: Pluggable Storage Backends

All four spec-level unknowns were resolved in `/speckit-clarify` (Session 2026-07-01). This document
records the technical decisions the design depends on. Format per item: **Decision / Rationale /
Alternatives considered**.

## R1 — `StorageBackend` interface shape

**Decision**: A minimal `Protocol` (`storage/base.py`) the engine talks to:
`read_meta()` / `write_meta(meta)`; `read_current_state() -> pl.DataFrame` (the reconstructed latest mirror,
used by the keyed diff); `append_events(events: pl.DataFrame) -> EventRef` (atomic, idempotent by
`snapshot_id`); `read_events(predicate=None) -> pl.DataFrame` (with ts/row-group pushdown where the backend
supports it); plus **capability flags** (`is_table`, `supports_atomic_meta`, `supports_time_pushdown`,
`supports_mirror`). `changelog.ChangeLogStore` depends only on this protocol.

**Rationale**: keeps the change-log model + `reconstruct.py` engine-generic (G8); the diff only needs
"prior current state" + "append events"; capability flags let the store adapt without backend `isinstance`
checks.

**Alternatives**: an ABC base class (heavier, forces inheritance) — rejected for a `Protocol` (duck-typed,
lets a sidecar implement it without importing core internals). A fat interface exposing file paths —
rejected (leaks the folder model into non-folder backends).

## R2 — LocalFolderStore = extract, don't rewrite

**Decision**: Move today's `changelog.py` writer/reader (temp-write → fsync → atomic `rename` → update
`manifest.json`; `events/*.parquet`) verbatim behind `LocalFolderStore` implementing the protocol. It is the
default backend.

**Rationale**: G3 back-compat + SC-002 (zero regression). The existing test suite becomes the regression
gate for this backend; no on-disk format change.

**Alternatives**: reimplement — rejected (needless risk to a shipped format).

## R3 — Object-store atomicity (the FUSE problem)

**Decision**: `ObjectStoreBackend` uses **fsspec** (`s3fs`/`adlfs`/`gcsfs`, and the local/memory FS for
tests). Event files are **immutable object PUTs**. The metadata commit is a **single-object atomic PUT** of
`manifest.json` (object stores guarantee atomic single-object PUT), **never** a multi-step rename.

**Rationale**: resolves the Databricks/UC-Volume FUSE non-atomic-rename hazard from `docs/DATABRICKS.md`;
single-object PUT is atomic on S3/ADLS/GCS, so remote commit is actually *safer* than FUSE.

**Alternatives**: write-temp + rename on the object store — rejected (rename is copy+delete, non-atomic,
can tear). A lock object — deferred (single-writer v1, R6).

## R4 — TableBackend layout: Parquet dataset default; Delta/Iceberg opt-in

**Decision**: `flux_events` is a **partitioned Parquet dataset** by default (append = new file part;
partition by capture/`snapshot_id` for prune), read as one table via glob. Store metadata (schema union,
key column) lives in a small companion object `_flux_meta.json` beside the dataset (no per-file manifest —
the dataset *is* the event catalog). **Delta (delta-rs)** and **Iceberg (pyiceberg)** are opt-in formats
selected via config; when used, metadata maps to **table properties** and the transaction log replaces both
the manifest and the companion. `flux_mirror` (opt-in) is a second table/dataset written from a full
reconstruction.

**Rationale**: Clarify Q1 — zero lock-in + no new core/extra dep for the common case (G1/G2); Delta/Iceberg
give ACID + native catalogs where wanted. Companion-meta keeps the Parquet path dependency-free while
preserving the "authoritative descriptor" contract.

**Alternatives**: Delta-by-default (adds a heavy dep to the table path) — rejected per Clarify Q1. A single
wide table only — rejected (loses the lean append-only source of truth).

## R5 — `flux_mirror` materialization + refresh

**Decision**: `flux_mirror` is **opt-in (off by default)**. Refresh modes: **on-demand** (explicit
`refresh_mirror()` / at query time — the default) and an **optional cadence/staleness-threshold** mode
(refresh every N captures or when an event-count/`snapshot_id` delta crosses a threshold). A tiny counter/
watermark in `_flux_meta.json` (or table props) drives the cadence check. `flux_mirror` is always fully
derivable from `flux_events`.

**Rationale**: Clarify Q2 (hybrid on-demand + cadence) — bounds materialize cost while allowing "keep it
warm" pipelines; `flux_events` stays the single source of truth.

**Alternatives**: eager-every-capture (pays a full-table write each run) — rejected as the default;
never-materialize (loses the query-friendly shape) — rejected (US2 wants it).

## R6 — Concurrency: single-writer v1

**Decision**: **Single writer per store** for v1; documented as a contract. No lock is added. Idempotent
`snapshot_id` still prevents *duplicate* captures, but interleaved multi-writer append is out of scope.

**Rationale**: Clarify Q3 + G6 (YAGNI); the motivating use case is one scheduled job owning one store.
Multi-writer locking (a lock object / Delta's optimistic concurrency) is a documented fast-follow.

**Alternatives**: advisory lock now / optimistic — deferred (extra surface without a present need).

## R7 — Backend selection (public API)

**Decision**: `select_backend(location, *, store=None, backend=None)` — infer from the location URI: a plain
path / `file://` → `LocalFolderStore`; `s3://` / `abfss://` / `gs://` → `ObjectStoreBackend`; an explicit
`store=`/`backend=` object (or a `table://`-style target) → `TableBackend` or a sidecar backend. `FluxState`
/ `ChangeLogStore` accept the existing `store_path` (unchanged local path → folder) **plus** an optional
`store=`/`backend=` param. No change to existing call sites.

**Rationale**: Clarify Q4 + G3 — least friction, additive, existing local calls untouched; power users pass
an explicit backend for tables/sidecars.

**Alternatives**: explicit-object-only (changes common-case ergonomics); config/env-only (hidden contract) —
both rejected per Clarify Q4.

## R8 — Databricks sidecar

**Decision**: `fluxstate[databricks]` provides a `DeltaBackend` (a `TableBackend` bound to Delta:
`flux_events` Delta table + optional `flux_mirror` Delta table) and a **scheduled-Job / notebook template**:
`spark.table(view).toArrow()` → Polars → capture → append to the Delta `flux_events`; optional
`applyInPandas` for very large views (default is driver-side — the diff is a table-level join, not a
per-row UDF). Heavy deps (`deltalake`, `databricks-sdk`, runtime `pyspark`) confined to this extra.

**Rationale**: FR-008 + the live use case; proves G8 (platform code is additive, in a sidecar). Matches the
already-drafted `docs/DATABRICKS.md`.

**Alternatives**: reimplement the change-log in PySpark — rejected (forks logic, violates G8/N1).

## R9 — Schema evolution across backends

**Decision**: schema union + drop/rename churn (from 002) is represented in **store metadata** (companion
`_flux_meta.json` or table properties), so reconstruction reads a dropped column as absent-from-drop-point
and a rename as old-empty/new-filled — identical to the folder-manifest behaviour. Verified by the parity
suite over the 002 `schema_churn` fixture across backends.

**Rationale**: FR + edge case; keeps 002 behaviour invariant regardless of backend.

**Alternatives**: per-backend bespoke handling — rejected (parity risk).
