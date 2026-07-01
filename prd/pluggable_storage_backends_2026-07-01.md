---
tags: [repo:fluxstate]
---

# FluxState — Pluggable Storage Backends + Tabular Store + Platform Sidecars (Databricks first)

> **Constitution Principle I (G8) — Platform-Agnostic:** FluxState must work on ALL data platforms
> (Databricks, Snowflake, PostgreSQL, Lakehouse, LakeBase, Supabase, object stores, local) and must
> NEVER be coded for one. Every platform integration is an **optional sidecar/extra**, never core.
> Databricks is the **first** sidecar (the live use case) — not the design center. Any new platform
> is a new sidecar over the same storage interface; the core engine is never touched.

**Ephemeral PRD** — delete when: the pluggable-storage feature is merged to fluxstate `master`
AND the full test suite passes green across all backends (local folder + object-store + table).

- **Status:** DRAFT (design locked in discussion 2026-07-01; ready for `/speckit-specify` → dev-kid)
- **Created:** 2026-07-01
- **Trigger:** The `001-changelog-first-pivot` spec explicitly scoped **remote/staged storage out of
  P1** ("Store location for P1 is a local change-log folder; remote/staged storage … is a fast-follow
  Storage Adapter and out of P1 scope"). A concrete use case now needs it: track a **Databricks view's**
  daily deltas via a scheduled Job, keeping everything on Databricks, with a strong preference to store
  the change-log in a **tabular** format (and optionally the materialized mirror too).
- **Related:** repo `gyasis/fluxstate`; builds on shipped `001-changelog-first-pivot` (the change-log
  substrate: `changelog.py` + `reconstruct.py` + `<name>.flux/` store) and `002-fluxstate-temporal-viewer`.
  Prior PRD: `prd/fluxstate_changelog_pivot_2026-06-05.md`. Agent orientation: `AGENTS.md`.

**Branch:** master (PRD authored on master; `/speckit-specify` will cut `003-pluggable-storage`)
**Repo:** fluxstate
**Owner_path:** /home/gyasis/Documents/code/fluxstate
**Branch_at_creation:** master

---

## 1. Context

Today FluxState persists a store to the **local filesystem only**: `store_path` is a local `Path`,
and `changelog.py` writes via `write_parquet` → temp file → atomic `rename` → rewrite `manifest.json`.
That is rock-solid on POSIX disk but blocks three things the project now needs:

1. **Universality across all data / engines.** FluxState's value is being *engine-portable* — it must
   persist to wherever data lives (local disk, S3/ADLS/GCS object stores, a lakehouse table), not just
   a POSIX folder. It must **NOT become Databricks-specific**; Databricks is one first-class target,
   not the only one.
2. **Databricks as a real target.** A scheduled Databricks Job needs to read a view, capture its
   deltas, and persist the store **entirely on Databricks** (UC Volume / Delta). The blocker is that
   the durable Databricks storage is object-store-backed **FUSE** (Volumes/DBFS), where FluxState's
   atomic `rename` of `manifest.json` is not guaranteed atomic.
3. **Tabular storage preference.** The owner would rather store the change-log as a **first-class
   table** than a folder-of-files. FluxState's events are *already* a flat table
   (`entity_id, timestamp, field, value, dtype, snapshot_id`); promoting that to a real table (Delta /
   Iceberg / partitioned Parquet dataset) — and optionally materializing the wide **mirror** table too
   — is a natural fit and removes the `manifest.json` rename problem (a real table's transaction log
   tracks files).

**Key reframe (design anchor):** *delta vs tabular is not either/or.* The **events** are the lean,
append-only source of truth AND are themselves tabular. The **mirror** (reconstructed wide state) is an
optional, materialized, query-friendly duplicate. Both are tabular; storing both is supported and is the
owner's stated preference where space allows.

## 2. Goals / Non-Goals

**Goals**
- G1 — A **pluggable `StorageBackend` interface** in the core: the change-log *model* + *reconstruction*
  stay engine-agnostic; only **where/how bytes persist** varies by backend.
- G2 — Ship **three backends**: `LocalFolderStore` (default, unchanged behavior), `ObjectStoreBackend`
  (fsspec: s3/abfss/gcs/UC Volume), `TableBackend` (events as a single tabular dataset/table).
- G3 — A **tabular storage contract**: `flux_events` (narrow EAV, append-only, always) + optional
  materialized `flux_mirror` (wide reconstructed current/as-of). Store-level metadata (schema union,
  `key_column`) lives in table properties or a tiny `flux_meta` companion — no `manifest.json` rename.
- G4 — **Platform sidecars** shipped as **optional extras**, one per platform, over the same storage
  interface — **Databricks first** (`fluxstate[databricks]`: `TableBackend` → Delta `flux_events` +
  optional `flux_mirror`, a scheduled-Job/notebook template, optional `applyInPandas` capture).
  Snowflake / PostgreSQL / LakeBase / Supabase / generic-lakehouse sidecars follow the identical
  pattern (fast-follow); **no platform logic ever lives in core** (Constitution G8).
- G5 — **Backend parity:** reconstruction (`as_of` / `timeline` / `row_state` / mirror) returns
  identical results regardless of backend, enforced by a parity test.

**Non-Goals**
- N1 — NOT reimplementing reconstruction per engine. One engine-agnostic algorithm; backends only
  supply bytes/rows.
- N2 — NOT making Databricks the only (or a privileged) target; NOT requiring Delta/Iceberg.
- N3 — NOT adding heavy deps to the **core** dependency tree. `deltalake` / `pyiceberg` / `fsspec` /
  `databricks-sdk` are **optional extras**; `pip install fluxstate` stays Polars + PyArrow only.
- N4 — NOT changing the change-event schema, idempotency (`snapshot_id`), type fidelity (`dtype`), or
  faithful-recorder semantics. Those are invariant across backends.

## 3. Design (the shape to spec)

### 3.1 `StorageBackend` interface (core, universal)
A minimal protocol that `ChangeLogStore` talks to instead of the filesystem directly. Roughly:
- `read_meta() -> Meta` / `write_meta(Meta)` — schema union + `key_column` (+ event catalog if the
  backend needs one; a real-table backend derives it from the table).
- `read_current_state() -> pl.DataFrame` — the reconstructed latest mirror, for the keyed diff.
- `append_events(events: pl.DataFrame) -> EventRef` — persist one capture's change events **atomically**
  (immutable object PUT / Delta append / new parquet part). Idempotent by `snapshot_id`.
- `read_events(predicate=None) -> pl.DataFrame | Iterator` — for reconstruction (with ts/row-group
  pushdown where possible).
- Capability flags (`supports_atomic_meta`, `is_table`, `supports_time_pushdown`) so the store adapts.

The existing local writer/reader is refactored to be the first implementation; **no behavior change**
for existing `.flux/` folder users (regression-gated).

### 3.2 Backends
| Backend | Persists as | Atomicity | Deps | Use |
|---|---|---|---|---|
| `LocalFolderStore` (default) | `manifest.json` + `events/*.parquet` | temp+rename (POSIX) | none (core) | laptop / local pipelines; portable default |
| `ObjectStoreBackend` | same layout on s3/abfss/gcs/Volume | **atomic single-object PUT** for meta; immutable event PUTs | `fsspec` + store libs (`[remote]`) | cloud pipelines; any object store |
| `TableBackend` | **one tabular table**: `flux_events` (+ optional `flux_mirror`) | table transaction log (Delta/Iceberg) or dataset append | `deltalake`/`pyiceberg` OR plain partitioned Parquet (`[table]`) | lakehouse / "store it as a table" |

### 3.3 Platform sidecars (optional extras — Databricks first, then any platform)
Each sidecar wires the core storage interface to a platform's **native store**, and does nothing else
(no re-implemented change-log/reconstruction, no platform assumptions leaking into core — G8).

- **`fluxstate[databricks]` (first / live use case):** `TableBackend` → **Delta** (`flux_events` +
  optional `flux_mirror`); a **scheduled-Job / notebook template** (`spark.table(view)` → capture →
  append to `flux_events` + refresh `flux_mirror`), fully on Databricks (UC Volume for any file spill,
  Delta for tables); optional **distributed capture** (`applyInPandas`) for large views (default is a
  driver-side capture — the diff is a table-level join, not a per-row UDF). Heavy/native deps
  (`databricks-sdk`, `deltalake`) confined to this extra.
- **Follow-on sidecars (same pattern, fast-follow):** `[snowflake]` (stage/table + Task schedule),
  `[postgres]` / `[supabase]` (table + `pg_cron`/scheduled function), `[lakehouse]` (Iceberg/Delta),
  LakeBase, etc. Each is an extra with its platform SDK isolated; the core never imports any of them.
- **The universal fallbacks need no sidecar:** `ObjectStoreBackend` (any S3/ADLS/GCS/Volume) and
  `LocalFolderStore` already cover "everywhere else" out of the box.

### 3.4 Tabular storage contract (answers "store delta + tabular?")
| Artifact | Shape | Role | Stored |
|---|---|---|---|
| `flux_events` | narrow EAV table | lean append-only **source of truth** (history) | **always** |
| `flux_mirror` | wide reconstructed table (current or as-of) | the "old mirror table" shape, query-friendly | **optional** — materialize on a configurable cadence |
| meta | table properties or `flux_meta` companion | schema union + `key_column` | replaces `manifest.json` in table mode |
Trade-off (documented, per-dataset choice): `flux_mirror` is a full-table-size duplicate refreshed each
run; `flux_events` stays compact. Keep both for query convenience; keep only events + reconstruct-on-read
to minimize space.

## 4. User stories (for `/speckit-specify`)

- **US1 — Universal remote store.** As a pipeline author, I point `store_path` at `s3://…` / `abfss://…`
  (or a UC Volume) and `capture` / `travel` / `view` work identically to local, with no core dep bloat.
- **US2 — Databricks-native daily delta capture.** As a Databricks user, a scheduled Job reads a view
  daily, captures its deltas into a Delta `flux_events` table (+ optional `flux_mirror`), staying
  entirely on Databricks — idempotent on no-change days.
- **US3 — Store the change-log as a table.** As any user, I store the change-log as a first-class table
  (Delta/Iceberg/Parquet dataset) and query `flux_events` (and `flux_mirror`) directly with SQL.
- **US4 — Laptop unchanged.** As a local user, `pip install fluxstate` stays Polars+PyArrow, the local
  folder store remains the default, and my existing `.flux/` stores keep working (regression-gated).
- **US5 — Backend parity.** As a maintainer, I get identical reconstruction across every backend, proven
  by a parity test (the `001/002` parity discipline extended to backends).

## 5. Tasks (indicative — SpecKit/dev-kid will refine)

**P1 — core interface + backends**
- [ ] Define `StorageBackend` protocol + capability flags; refactor `ChangeLogStore` to depend on it.
- [ ] `LocalFolderStore` = the refactored existing writer/reader (zero behavior change; regression test).
- [ ] `ObjectStoreBackend` (fsspec) — atomic meta PUT, immutable event PUTs, ts pushdown where possible.
- [ ] `TableBackend` — events as a table (`flux_events`), meta as table props / `flux_meta`; optional
      `flux_mirror` materialization + refresh policy.
- [ ] Optional-dependency extras: `[remote]`, `[table]`, `[databricks]`; keep core deps unchanged.
- [ ] Backend-parity test (US5) across local/object/table.

**P1 — Databricks sidecar**
- [ ] `fluxstate[databricks]`: Delta wiring for `TableBackend`; scheduled-Job/notebook template; optional
      `applyInPandas` capture.
- [ ] `docs/DATABRICKS.md` runbook (Job schedule, Volume/Delta layout, the two-table contract, gotchas).

**Fast-follow (NOT P1)**
- [ ] Iceberg backend variant; delta-rs off-Databricks default for `[table]`.
- [ ] Concurrency/locking for concurrent Jobs appending to one store.
- [ ] Checkpoint compaction interplay with the table backend.

## 6. Open questions (resolve during `/speckit-clarify`)

- Q1 — Meta in **table properties** vs a **`flux_meta` companion table**? (governance / portability trade.)
- Q2 — `flux_mirror` refresh: **every capture** vs **on-demand** vs **cadence/threshold**? Default?
- Q3 — **Concurrency**: two Jobs/writers appending to one store — locking, or single-writer contract?
- Q4 — Off-Databricks `TableBackend` default: **Delta (delta-rs)** vs **Iceberg (pyiceberg)** vs **plain
  partitioned Parquet dataset**? (Parquet-dataset keeps zero-lock-in; Delta/Iceberg add ACID + deps.)
- Q5 — **Schema evolution** (the `002` churn behavior: add/drop/rename columns) across backends — how does
  the table backend represent it vs the folder manifest?
- Q6 — Does the object-store backend need its own **atomic-write test matrix** (S3 vs ADLS vs GCS vs UC
  Volume FUSE) to certify meta atomicity?

## 7. Subagent Log

| Spawn | Name | Purpose | Result |
|---|---|---|---|
| — | (none yet) | design captured inline from the 2026-07-01 discussion | — |
