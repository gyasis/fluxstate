# Phase 1 Data Model: Pluggable Storage Backends

Entities the feature introduces or formalizes. The **change-event schema and reconstruction are
invariant** (from 001); this feature adds the *persistence-layer* entities around them.

## StorageBackend (protocol)

The contract `ChangeLogStore` depends on. Implementations vary; the model does not.

| Member | Kind | Meaning |
|---|---|---|
| `read_meta() -> Meta` | method | load the store descriptor (schema union, key column, catalog if any) |
| `write_meta(meta: Meta)` | method | persist the descriptor **atomically** |
| `read_current_state() -> pl.DataFrame` | method | the reconstructed latest mirror (input to the keyed diff) |
| `append_events(events: pl.DataFrame) -> EventRef` | method | persist one capture's change events; **atomic + idempotent** by `snapshot_id` |
| `read_events(predicate=None) -> pl.DataFrame` | method | events for reconstruction; ts/row-group pushdown when supported |
| `capabilities -> Capabilities` | property | feature flags (below) |

**Capabilities** (flags the store advertises): `is_table` (bool), `supports_atomic_meta` (bool),
`supports_time_pushdown` (bool), `supports_mirror` (bool).

**Invariants**: `append_events` never rewrites prior units (G4); a partial write is not visible until commit
(FR-013); re-appending an already-present `snapshot_id` is a no-op (FR-009).

## Change Event (INVARIANT — from 001)

Row schema `(entity_id, timestamp, field, value, dtype, snapshot_id)`; `field == "__deleted__"` marks a
deletion; `dtype == "null"` + `value is None` distinguishes deletion/null. **Unchanged by this feature** —
only its *container* varies (files vs table).

## flux_events (the change-log as data)

The persisted change-event record. Physical form varies by backend:

| Backend | Physical form of `flux_events` |
|---|---|
| LocalFolderStore | `events/*.parquet` (one immutable file per capture) + `manifest.json` |
| ObjectStoreBackend | same layout on object storage; `manifest.json` via atomic PUT |
| TableBackend (Parquet) | partitioned Parquet **dataset** (append = new part) + `_flux_meta.json` companion |
| TableBackend (Delta/Iceberg, opt-in) | a Delta/Iceberg **table** (append = transaction); meta in table properties |

Logical schema is identical everywhere (the Change Event columns above). Always present.

## flux_mirror (optional, materialized)

The reconstructed **wide** current-or-as-of state — one row per live entity, columns = key + tracked
fields (schema order), typed per `dtype`. **Opt-in**; derivable purely from `flux_events`.

| Attribute | Value |
|---|---|
| Presence | opt-in (off by default) |
| Refresh | on-demand (default) OR cadence/staleness-threshold (optional) |
| Content | `build_mirror_view(flux_events @ T)` — same output as `reconstruct.build_mirror_view` |
| Physical form | second dataset/table alongside `flux_events` (Parquet/Delta/Iceberg per backend) |
| Source of truth | NO — `flux_events` is authoritative; `flux_mirror` is a cache |

## Meta (store descriptor)

Authoritative descriptor a reader trusts. Replaces/*supersedes* `manifest.json` where a real table exists.

| Field | Meaning |
|---|---|
| `schema` | schema union `column → dtype` (carries 002 add/drop/rename churn) |
| `key_column` | the entity key matched across captures |
| `event_catalog` | valid event units + ts ranges + `snapshot_id`s (folder/object backends only; a Delta/Iceberg tx-log supplies this) |
| `mirror` | `{enabled, refresh_mode, cadence_n, watermark}` for `flux_mirror` |
| `schema_version` | store format version |

Physical form: `manifest.json` (folder/object), `_flux_meta.json` companion (Parquet TableBackend), or
table properties (Delta/Iceberg).

## EventRef

Handle returned by `append_events` — identifies the committed unit (`snapshot_id`, physical ref: file/object
key or table transaction id, row_count, ts range). Used for logging/idempotency, not reconstruction.

## Platform Sidecar (e.g. Databricks)

An **optional, separately-installed** package that provides a backend (e.g. `DeltaBackend`) + platform glue
(a scheduled-Job template). Contains **no** change-log or reconstruction logic. Never imported by core.

| Attribute | Value |
|---|---|
| Install | `pip install "fluxstate[databricks]"` (opt-in extra) |
| Provides | a `StorageBackend` impl + a capture/job template |
| Constraint | zero platform imports in core (G8); heavy deps confined to the extra (G1) |

## Backend selection

`select_backend(location, *, store=None, backend=None)`:
`file://`/plain path → LocalFolderStore · `s3://`/`abfss://`/`gs://` → ObjectStoreBackend · explicit
`store=`/`backend=` (or `table://`) → TableBackend / sidecar backend. Default = LocalFolderStore (G3).
