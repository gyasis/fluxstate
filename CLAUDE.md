<!-- SPECKIT START -->
Active feature: **003-pluggable-storage-backends** (pluggable StorageBackend + tabular store + platform sidecars).

For technologies, project structure, constraints, and design decisions, read the
current plan and its Phase 0/1 artifacts:
- Plan: `specs/003-pluggable-storage-backends/plan.md`
- Spec: `specs/003-pluggable-storage-backends/spec.md`
- Research: `specs/003-pluggable-storage-backends/research.md`
- Data model: `specs/003-pluggable-storage-backends/data-model.md`
- Contracts: `specs/003-pluggable-storage-backends/contracts/` (storage-backend.md, tables.md)
- Quickstart: `specs/003-pluggable-storage-backends/quickstart.md`

Adds a pluggable **`StorageBackend`** seam so the change-log persists to ANY platform (Constitution
Principle I / G8 — platform-agnostic; platform code only in `sidecars/` + optional extras, never core).
New `storage/` package: `LocalFolderStore` (default, behaviour-identical to today), `ObjectStoreBackend`
(fsspec `[remote]`), `TableBackend` (`flux_events` + optional `flux_mirror`; Parquet default, Delta/Iceberg
opt-in `[table]`). First sidecar = Databricks (`[databricks]`, DeltaBackend). `changelog.py` refactors to
talk to the protocol; `reconstruct.py` is UNCHANGED. Core stays Polars+PyArrow; heavy libs are opt-in
extras. Invariants preserved: faithful-recorder, append-only/immutable/idempotent (`snapshot_id`), type
fidelity (`dtype`), API back-compat, reconstruction parity across backends. Clarify decisions (2026-07-01):
Parquet-default table format, opt-in on-demand+cadence `flux_mirror`, single-writer v1, URI-inference
backend selection.

Prior shipped features: **001-changelog-first-pivot** (change-log substrate) + **002-fluxstate-temporal-viewer**
(Temporal Ghost viewer + `flux` CLI). Keep it lightweight above all.
<!-- SPECKIT END -->
