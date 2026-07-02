# Feature Specification: Pluggable Storage Backends + Tabular Store + Platform Sidecars

**Feature Branch**: `003-pluggable-storage-backends`
**Created**: 2026-07-01
**Status**: Draft
**Input**: User description: "Pluggable storage backends + tabular store + platform sidecars (Databricks first). A pluggable StorageBackend interface in the core so FluxState persists to ANY data platform (Constitution Principle I / G8 — platform-agnostic, never coded for one platform; platform integrations are optional sidecars, never core). Backends: LocalFolderStore (default, unchanged), ObjectStoreBackend (s3/abfss/gcs/UC Volume), TableBackend (events as a first-class tabular table). Tabular contract: flux_events (narrow EAV, append-only, always) + optional materialized flux_mirror (wide reconstructed current/as-of). Platform sidecars as optional extras, Databricks first. Preserve invariants: faithful-recorder, append-only/immutable/idempotent, type fidelity, API back-compat, reconstruction parity."

Source PRD: `prd/pluggable_storage_backends_2026-07-01.md`. Builds on shipped **001** (change-log
substrate) and **002** (temporal viewer). Governed by the Constitution (`.specify/memory/constitution.md`),
esp. **Principle I / G8 — Platform-Agnostic**.

## Clarifications

### Session 2026-07-01

- Q: Off-platform, what format should the TableBackend use for `flux_events`? → A: **Plain partitioned Parquet dataset by default** (zero lock-in, no new dependency); **Delta (delta-rs) / Iceberg (pyiceberg)** available as **opt-in** formats.
- Q: What is the default refresh policy for the optional `flux_mirror`? → A: **Opt-in (off by default)**; when enabled it supports **on-demand refresh (default)** AND an **optional cadence/staleness-threshold** mode; eager-every-capture is NOT the default.
- Q: What concurrency writer model should v1 support? → A: **Single-writer per store** (documented); concurrent multi-writer append with locking is a fast-follow.
- Q: How is a backend selected (public API surface)? → A: **URI/scheme inference** (local path → folder; `s3://`·`abfss://`·`gs://` → object-store) **plus an explicit `store=`/`backend=` override** for table/sidecar targets; existing local-path calls are unchanged.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Existing local stores keep working, storage is now swappable (Priority: P1)

A user who already keeps `.flux/` stores on their local disk upgrades FluxState. Every existing store
still captures and reconstructs exactly as before — but persistence now goes through a **pluggable
storage layer**, so the *same* capture/query calls can later target a different location by selecting a
backend, with zero change to the change-log model or reconstruction.

**Why this priority**: This is the foundation and the back-compat guardrail (Constitution G3). Without a
clean storage seam that preserves today's behavior, no other backend can be added safely.

**Independent Test**: Point FluxState at an existing `.flux/` folder and run the full capture →
reconstruct → timeline → row-state flow; results are identical to the pre-feature version and the
existing test suite stays green.

**Acceptance Scenarios**:

1. **Given** a `.flux/` store created before this feature, **When** a user reconstructs any as-of state,
   **Then** the result is identical to the prior FluxState version (no migration required).
2. **Given** the default configuration, **When** a user captures a snapshot, **Then** persistence uses
   the local-folder backend and writes the same `manifest.json` + `events/*.parquet` layout as today.
3. **Given** a capture in progress that is interrupted before commit, **When** the store is next read,
   **Then** it reflects only fully-committed captures (no torn/partial state).

---

### User Story 2 - Store the change-log as a first-class tabular table (Priority: P1)

A user prefers to keep the history as a **table**, not a folder of files. They select the table backend;
each capture appends changed cells to a narrow **`flux_events`** table (the append-only source of truth),
and they can optionally materialize a wide **`flux_mirror`** table holding the reconstructed current (or
as-of) state to query directly.

**Why this priority**: This is the owner's core stated preference ("I'd rather store it in a tabular
format… store both the delta file and the tabular file"). It unlocks direct SQL over the change-log and
removes the fragile file-manifest for platforms that offer real tables.

**Independent Test**: Capture two snapshots into the table backend; query `flux_events` directly as a
table and confirm the changed cells are present; enable `flux_mirror` and confirm it equals the
reconstructed current state.

**Acceptance Scenarios**:

1. **Given** the table backend, **When** a user captures a snapshot, **Then** only the changed cells are
   appended to `flux_events` and no existing rows are rewritten.
2. **Given** `flux_mirror` is enabled, **When** a capture completes, **Then** `flux_mirror` reflects the
   reconstructed current state per the configured refresh policy.
3. **Given** the table backend, **When** the same snapshot is captured twice, **Then** the second capture
   is a no-op (idempotent) and `flux_events` is unchanged.
4. **Given** the table backend, **When** a user reconstructs an as-of state, **Then** the result is
   identical to the same store reconstructed under the local-folder backend (parity).

---

### User Story 3 - Persist a store to any object store (Priority: P2)

A pipeline author keeps stores in cloud object storage (S3 / ADLS / GCS / a UC Volume) rather than local
disk. They select the object-store backend by pointing the store location at a remote URI; capture and
reconstruction behave identically to local, and the base install pulls no heavyweight table-format deps.

**Why this priority**: Universality across "everywhere else" (Constitution G8) without needing a
per-platform sidecar; the common cloud-pipeline case.

**Independent Test**: Point a store at a remote object-store URI, capture a snapshot, and reconstruct it
from a fresh process reading only that remote location.

**Acceptance Scenarios**:

1. **Given** a remote object-store location, **When** a user captures a snapshot, **Then** the store's
   metadata commit is atomic and prior event objects are never rewritten.
2. **Given** a store on object storage, **When** a different process reconstructs an as-of state, **Then**
   it reads only the remote store and returns results identical to a local copy.

---

### User Story 4 - Databricks daily delta capture on a schedule (Priority: P2)

A Databricks user tracks a **view's** daily deltas entirely on Databricks. Using the Databricks sidecar,
a scheduled job reads the view, captures its deltas into a Delta **`flux_events`** table (and optionally
refreshes **`flux_mirror`**), and does nothing on days when the view is unchanged.

**Why this priority**: The concrete live use case that motivated this feature, and the first proof that a
platform integration is a *sidecar* (never core).

**Independent Test**: Run the sidecar's scheduled-job template against a keyed view twice with unchanged
data; `flux_events` gains rows on the first run and none on the second (idempotent), and the as-of
reconstruction matches the view snapshots.

**Acceptance Scenarios**:

1. **Given** a keyed Databricks view and the sidecar, **When** the scheduled job runs, **Then** the view's
   changed cells are appended to a Delta `flux_events` table without leaving Databricks.
2. **Given** the sidecar installed, **When** a user installs the base package only, **Then** no Databricks
   or Delta dependency is present (the sidecar is a separate optional extra).
3. **Given** an unchanged view, **When** the daily job runs again, **Then** the capture is a no-op.

---

### User Story 5 - Identical reconstruction across every backend (Priority: P3)

Any user (or maintainer) gets **the same history answers** regardless of where/how the store is persisted —
local folder, object store, or table — so a store can move between backends without changing meaning.

**Why this priority**: The correctness guarantee that makes "pluggable storage" trustworthy; enforced as a
merge gate rather than a user-facing feature.

**Independent Test**: Run the reconstruction-parity suite over the same seeded change-log persisted through
each backend; all backends produce identical as-of / timeline / row-state / mirror outputs.

**Acceptance Scenarios**:

1. **Given** one seeded change-log written through each backend, **When** the parity suite runs, **Then**
   every backend yields identical reconstruction results.

---

### Edge Cases

- **Object-store non-atomic rename**: metadata commit MUST use an atomic mechanism (e.g., single-object
  put or a transaction), never a multi-step rename that can tear on FUSE/object storage.
- **Schema evolution** (add / drop / rename columns across captures): each backend MUST represent the
  churn so reconstruction still reads a dropped column as absent from its drop point and a rename as
  old-empty/new-filled (parity with the folder manifest behavior from 002).
- **Concurrent writers** to one store: v1 assumes a single writer per store; simultaneous captures are out
  of scope and MUST be documented, not silently corrupt the store.
- **Interrupted capture** on any backend: a partially-written capture MUST NOT become visible.
- **Empty / first capture** and **all-deleted** states MUST reconstruct without error.
- **`flux_mirror` staleness**: with a non-eager refresh policy, `flux_mirror` MAY lag `flux_events`; the
  refresh policy and any staleness MUST be explicit, and `flux_events` is always the source of truth.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The change-log engine MUST perform all persistence through a single **pluggable storage
  interface**; the change-event model and reconstruction MUST remain independent of where/how bytes are
  stored.
- **FR-002**: A **local-folder backend** MUST be the default and MUST behave identically to the current
  implementation (`manifest.json` + immutable `events/*.parquet`); pre-existing stores MUST remain
  readable with no migration (Constitution G3).
- **FR-003**: An **object-store backend** MUST persist a store to remote object storage (S3 / ADLS / GCS /
  UC Volume) with capture and reconstruction identical to local, committing metadata **atomically**.
- **FR-004**: A **table backend** MUST store the change-log as a first-class **`flux_events`** table
  (narrow, append-only) and MUST NOT rewrite existing rows/files on capture. Off-platform (no managed
  lakehouse), the table backend MUST **default to a plain partitioned Parquet dataset** (no new
  dependency); **Delta (delta-rs) and Iceberg (pyiceberg)** MUST be available as **opt-in** table formats.
- **FR-005**: The table backend MUST support an **optional materialized `flux_mirror`** table (the
  reconstructed current or as-of state), which MUST be **opt-in (off by default)**. When enabled, it MUST
  support **on-demand refresh (the default)** and an **optional cadence/staleness-threshold refresh mode**;
  `flux_mirror` MUST be derivable purely from `flux_events`, which remains the source of truth.
- **FR-006**: Store metadata (schema union, key column, and — where needed — the valid-event catalog) MUST
  be persisted per backend (e.g., table properties or a companion), replacing `manifest.json` where a real
  table renders it redundant.
- **FR-007**: Platform integrations MUST ship as **optional, separately-installable sidecars/extras** and
  MUST NOT add platform SDKs or heavyweight table-format libraries to the **core** dependency tree
  (Constitution G1/G8); no platform-specific logic may live in core.
- **FR-008**: A **Databricks sidecar** MUST be provided as the first platform integration: capture a keyed
  view's deltas into a Delta `flux_events` table (+ optional `flux_mirror`) via a schedulable job, entirely
  on the platform.
- **FR-009**: Capture MUST remain **idempotent** across all backends (a content-derived snapshot identity
  makes re-capturing already-recorded data a no-op).
- **FR-010**: Values MUST round-trip at full **type fidelity** across all backends; deletions MUST use the
  single `__deleted__` marker; timestamps MUST be UTC-normalized.
- **FR-011**: Reconstruction (as-of / timeline / row-state / mirror) MUST return **identical results across
  all backends**, enforced by a reconstruction-parity test (Constitution G6/parity).
- **FR-012**: Existing public API signatures MUST be **preserved**; backend selection MUST be **additive**
  and MUST default to the local-folder backend. Backend selection MUST work by **store-location URI/scheme
  inference** (a local path → folder backend; `s3://` / `abfss://` / `gs://` → object-store backend) with
  an **explicit `store=`/`backend=` override** for table and platform-sidecar targets; existing local-path
  calls MUST continue to work unchanged.
- **FR-013**: Every backend's capture MUST be **atomic**: a partial/interrupted write MUST NOT corrupt the
  store or become visible before commit.
- **FR-014**: **Faithful-recorder** semantics MUST be preserved — no semantic-equality normalization is
  applied at capture; format / precision / timezone differences ARE recorded as changes (Constitution
  Principle II).
- **FR-015**: Adding support for a **new data platform** MUST require only a new sidecar over the storage
  interface, with **no change to the core engine** (Constitution G8) — verified by the Databricks sidecar
  being additive.

### Key Entities *(include if feature involves data)*

- **Storage backend**: the pluggable persistence contract the engine talks to (read metadata, read/append
  events, read current state for diffing, commit atomically); implementations vary, the model does not.
- **`flux_events`**: the narrow, append-only change-event record `(entity_id, timestamp, field, value,
  dtype, snapshot_id)` — the lean source of truth, expressible as files or a first-class table.
- **`flux_mirror`**: an optional, materialized wide table of the reconstructed current (or as-of) state —
  the query-friendly "mirror" shape, always derivable from `flux_events`.
- **Store metadata**: schema union (column → dtype) + key column (+ event catalog where a folder manifest
  is used); the authoritative descriptor a reader trusts.
- **Platform sidecar**: an optional, separately-installed package that wires the storage interface to a
  specific platform's native store; contains no change-log or reconstruction logic.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A change-log can be persisted to at least **three distinct storage targets** (local folder,
  remote object store, first-class table) using the **same** capture/query calls apart from selecting the
  target.
- **SC-002**: **100% back-compat** — every store created before this feature reconstructs correctly with no
  migration, and the pre-existing test suite passes with zero regressions.
- **SC-003**: **Reconstruction parity is 100%** — for the same input, as-of / timeline / row-state / mirror
  results are identical across all backends.
- **SC-004**: Installing the **base package pulls no platform-specific or heavyweight table-format
  dependency** — the base dependency footprint is unchanged from today; platform/table support arrives only
  via opt-in extras.
- **SC-005**: A daily scheduled capture of a platform view records **only changed cells** and is a **no-op
  when nothing changed**, so storage growth is proportional to change volume, not table-size × captures.
- **SC-006**: Supporting a **new data platform** requires **only a new sidecar** — the core engine is
  untouched (demonstrated by the Databricks sidecar being purely additive).

## Assumptions

- **Off-platform table format** (DECIDED, Session 2026-07-01): the table backend **defaults to a plain
  partitioned Parquet dataset** (zero lock-in, no new dependency, aligns G1/G2); **Delta (delta-rs) and
  Iceberg (pyiceberg)** are **opt-in** formats used where a platform offers them or a user selects them.
- **`flux_mirror` refresh** (DECIDED): materialization is **opt-in (off by default)**; when enabled it
  supports **on-demand refresh (default)** plus an **optional cadence/staleness-threshold** mode
  (eager-every-capture is not the default). `flux_events` is always the source of truth.
- **Concurrency** (DECIDED): v1 assumes a **single writer per store** (documented); concurrent
  multi-writer append with locking is fast-follow, not in scope.
- **Backend selection** (DECIDED): chosen by **store-location URI/scheme inference** (local path → folder;
  `s3://`/`abfss://`/`gs://` → object-store) with an **explicit `store=`/`backend=` override** for
  table/sidecar targets; existing local-path calls are unchanged (back-compat G3).
- **Databricks is the first sidecar**; Snowflake / PostgreSQL / Supabase / LakeBase / generic-Lakehouse
  sidecars are fast-follow over the identical interface and are **out of scope** for this feature.
- Builds on the **shipped 001 change-log substrate** and **002 viewer parity discipline**; the
  reconstruction algorithm itself is unchanged.
- Sources expose a **stable key column** so rows match across captures (an existing FluxState requirement).
- The universal `ObjectStoreBackend` + `LocalFolderStore` cover "everywhere else"; a sidecar is only needed
  when a platform's *native table* (e.g., Delta) or *scheduler* is the target.
