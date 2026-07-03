---
description: "Task list for 003-pluggable-storage-backends"
---

# Tasks: Pluggable Storage Backends + Tabular Store + Platform Sidecars

**Input**: Design documents from `specs/003-pluggable-storage-backends/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/
**Tests**: INCLUDED — the spec (FR-011, SC-002/003) and Constitution G5 require regression + cross-backend
parity tests.

**Organization**: grouped by user story (US1–US5) for independent implementation + testing.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: parallelizable (different files, no incomplete dependency)
- **[Story]**: US1..US5 (user-story phases only)
- Paths are repo-root-relative (single-project library + CLI, per plan.md)

---

## Phase 1: Setup (Shared Infrastructure)

- [ ] T001 Create the `storage/` package skeleton (`storage/__init__.py`, `storage/base.py`) per plan.md structure
- [ ] T002 Create the `sidecars/databricks/` package skeleton (`sidecars/databricks/__init__.py`) — MUST never be imported by core (G8)
- [ ] T003 [P] Add `[project.optional-dependencies]` to `pyproject.toml`: `remote` (fsspec, s3fs, adlfs, gcsfs), `table` (deltalake, pyiceberg), `databricks` (deltalake, databricks-sdk) — core deps (Polars, PyArrow) UNCHANGED (G1/SC-004)

---

## Phase 2: Foundational (Blocking Prerequisites)

**⚠️ CRITICAL**: No user story can begin until Phase 2 is complete — this is the storage seam everything uses.

- [ ] T004 Define the `StorageBackend` Protocol + `Capabilities` flags (`is_table`, `supports_atomic_meta`, `supports_time_pushdown`, `supports_mirror`) in `storage/base.py` per `contracts/storage-backend.md`
- [ ] T005 [P] Define `Meta` (store descriptor — schema union + `key_column` + mirror policy; FR-006), `EventRef`, and the invariant Change-Event schema constants in `storage/base.py` per `data-model.md`
- [ ] T006 Implement `select_backend(location, *, store=None, backend=None)` URI-scheme inference in `storage/__init__.py` per contract §Selection; missing-extra → actionable error naming the extra
- [ ] T007 Refactor `changelog.ChangeLogStore` to persist/read exclusively via the `StorageBackend` protocol (no behavior change yet), and wire the additive `store=`/`backend=` parameter through `fluxstate.FluxState` (default = local; G3)

---

## Phase 3: User Story 1 — Local stores unchanged, storage swappable (Priority: P1) 🎯 MVP

**Goal**: Existing `.flux/` stores behave identically, now through the pluggable seam (the default backend).
**Independent test**: Run capture → reconstruct → timeline → row-state on a pre-existing `.flux/` folder; results identical to pre-feature and the full existing suite is green.

- [ ] T008 [US1] Extract today's writer/reader into `storage/local_folder.py:LocalFolderStore` implementing `StorageBackend` (manifest.json + immutable events/*.parquet; temp→fsync→atomic rename) — behavior-identical (SB-1..SB-5)
- [ ] T009 [US1] Make `LocalFolderStore` the backend `select_backend` resolves for local paths / `file://` (default; G3, FR-002)
- [ ] T010 [P] [US1] Regression test `TESTS/test_local_folder.py`: capture / reconstruct / delete-resurrect / idempotency / type-fidelity preserved on a `.flux/` folder; **faithful-recorder (FR-014)** — assert a format / precision / timezone change (e.g. `82.00`→`82`, `MARGARET`→`Margaret`) is recorded AS a change (no semantic-equality normalization at capture)
- [ ] T011 [US1] Run the full existing `TESTS/` suite green against the refactor — zero regressions (SC-002)

**Checkpoint**: MVP — FluxState works exactly as before, on a swappable backend.

---

## Phase 4: User Story 2 — Change-log as a first-class table (Priority: P1)

**Goal**: Store history as a table: `flux_events` (always) + optional materialized `flux_mirror`.
**Independent test**: Capture two snapshots via TableBackend; query `flux_events` as a table; enable `flux_mirror` and confirm it equals the reconstructed current state; reconstruction matches the local backend.

- [ ] T012 [US2] Implement `storage/table.py:TableBackend` — `flux_events` as a partitioned Parquet dataset (append = new part; `_flux_meta.json` companion via atomic PUT persisting the store descriptor; no rewrite) (FR-004, FR-006, R4, FE-1..4, M-1..3)
- [ ] T013 [US2] Implement optional `flux_mirror` in `TableBackend` — opt-in; on-demand refresh (default) + cadence/staleness-threshold mode; derived via `reconstruct.build_mirror_view` (FR-005, R5, FM-1..2)
- [ ] T014 [P] [US2] Add opt-in Delta (delta-rs) + Iceberg (pyiceberg) format paths in `TableBackend` (meta → table properties), guarded by the `[table]` extra (R4)
- [ ] T015 [US2] Wire `FluxState(store=TableBackend(...))` + a `refresh_mirror()` surface in `fluxstate.py`
- [ ] T016 [P] [US2] Tests `TESTS/test_table_backend.py`: `flux_events` append / idempotency / no-rewrite; `flux_mirror` on-demand + cadence; parity vs local
- [ ] T017 [P] [US2] Test: constructing `TableBackend(format="delta")` without `[table]` raises an actionable `pip install "fluxstate[table]"` error

**Checkpoint**: tabular storage works end-to-end (owner's core preference).

---

## Phase 5: User Story 3 — Persist to any object store (Priority: P2)

**Goal**: Stores live on S3 / ADLS / GCS / UC Volume with capture+reconstruct identical to local.
**Independent test**: Point a store at a remote URI, capture, and reconstruct from a fresh process reading only that location.

- [ ] T018 [US3] Implement `storage/object_store.py:ObjectStoreBackend` (fsspec) — immutable event object PUTs + **atomic single-object manifest/meta PUT** persisting the store descriptor (never rename) (FR-003, FR-006, R3, SB-4, M-2)
- [ ] T019 [US3] Resolve `s3://` / `abfss://` / `gs://` in `select_backend` → `ObjectStoreBackend`, guarded by the `[remote]` extra
- [ ] T020 [P] [US3] Tests `TESTS/test_object_store.py`: capture+reconstruct over the fsspec memory/local FS; atomic-meta + no-rewrite; cross-process read; parity vs local

**Checkpoint**: universal remote storage without a per-platform sidecar.

---

## Phase 6: User Story 4 — Databricks daily delta capture (Priority: P2)

**Goal**: A scheduled Databricks Job captures a view's deltas into a Delta `flux_events` (+ optional `flux_mirror`), entirely on Databricks.
**Independent test**: Run the sidecar capture template against a keyed view twice unchanged; rows added on run 1, none on run 2 (idempotent); reconstruction matches the view snapshots.

- [ ] T021 [US4] Implement `sidecars/databricks/__init__.py:DeltaBackend` (a `TableBackend` bound to Delta `flux_events` + optional `flux_mirror`; store descriptor → Delta table properties), depending ONLY on the `[databricks]` extra (FR-008, FR-006, G8/SB-7)
- [ ] T022 [US4] Implement `sidecars/databricks/job_template.py` — scheduled-Job capture: `spark.table(view).toArrow()` → Polars → capture → append; optional `applyInPandas` path (R8)
- [ ] T023 [P] [US4] Test: assert the CORE import graph pulls NO databricks/delta module (base install has zero platform dep) (SC-004, G8)
- [ ] T024 [P] [US4] Test the sidecar capture idempotency / no-op-on-unchanged against a local Delta table (deltalake) standing in for Spark; **assert storage grows proportional to changed cells (an unchanged re-capture appends ~0; a 3-changed-cell capture appends ~3 event rows)** (SC-005)
- [ ] T025 [US4] Update `docs/DATABRICKS.md` — flip the sidecar section from PLANNED → shipped `DeltaBackend` usage

**Checkpoint**: first platform sidecar proven; adding a platform = adding a sidecar (FR-015).

---

## Phase 7: User Story 5 — Identical reconstruction across every backend (Priority: P3)

**Goal**: Same history answers regardless of backend.
**Independent test**: Run the parity suite over one seeded change-log persisted through each backend; all identical.

- [ ] T026 [US5] Implement `TESTS/test_storage_parity.py` — reconstruct `as_of` / `timeline` / `row_state` / `mirror` over the SAME seeded change-log through Local / Object / Table backends; assert byte-identical, **including explicit type-fidelity round-trip (values restored to their original dtypes), `__deleted__` lifecycle, and UTC timestamps across every backend** (FR-010, FR-011, SC-003)
- [ ] T027 [P] [US5] Extend the parity suite over the 002 `schema_churn` fixture (add / drop / rename) across all backends (R9)

**Checkpoint**: pluggable storage is trustworthy — parity is the merge gate.

---

## Phase 8: Polish & Cross-Cutting

- [ ] T028 [P] Update `README.md` + `AGENTS.md` storage sections: backends, extras, URI selection (docs ship with the feature)
- [ ] T029 [P] Validate `specs/003-pluggable-storage-backends/quickstart.md` end-to-end (each backend snippet runs)
- [ ] T030 [P] Finalize `pyproject.toml` extras + a `pip install "fluxstate[...]"` smoke check; confirm base footprint unchanged (SC-004)
- [ ] T031 Verify the `flux` CLI still works unchanged against a local store (no CLI change required)
- [ ] T032 Final gate: full `TESTS/` suite + parity green; Constitution re-check G1–G8 (G8 first)

---

## Dependencies & Execution Order

- **Phase 1 (Setup)** → **Phase 2 (Foundational, BLOCKING)** → user stories.
- **US1 (P1)** depends only on Phase 2; it is the MVP.
- **US2 (P1)**, **US3 (P2)** depend on Phase 2 + the parity baseline from US1's LocalFolderStore; they are independent of each other.
- **US4 (P2)** depends on US2 (DeltaBackend extends TableBackend).
- **US5 (P3)** depends on the backends it compares (US1 + whichever of US2/US3 are done — runs incrementally as backends land).
- **Phase 8 (Polish)** last.

## Parallel Opportunities

- Phase 1: T003 ∥ T001/T002.
- Phase 2: T005 ∥ T004 (same file — sequence if needed); T006 after T004/T005.
- US1: T010 ∥ implementation.
- US2: T014, T016, T017 ∥.
- US4: T023, T024 ∥.
- US5: T027 ∥ T026 once a 2nd backend exists.
- Polish: T028, T029, T030 ∥.

## Implementation Strategy

- **MVP = US1** (Phases 1–3): the swappable seam + LocalFolderStore + zero regression. Shippable alone.
- **Increment 2 = US2** (tabular table) — the owner's core preference.
- **Increment 3 = US3 + US4** (object store + Databricks sidecar) — universality + the live use case.
- **Increment 4 = US5 + Polish** — parity gate + docs.
- Each increment keeps the full suite + parity green before moving on.
