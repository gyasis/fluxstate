---
name: fluxstate-storage
description: Choose and use a FluxState storage backend — where/how the change-log persists. Use when the user wants to "store the flux change-log in a table", "put the store on s3 / adls / gcs / a UC Volume", "capture into Databricks / Delta", "use flux with Snowflake/Postgres/a lakehouse", "make flux_events a real table", "store both the delta and a tabular mirror", or asks which backend/extra to install. FluxState is platform-agnostic (feature 003): the change-log model is fixed; only persistence is pluggable via `StorageBackend`.
---

# FluxState — storage backends

Persistence goes through a pluggable **`StorageBackend`** (feature 003). The change-event model and
reconstruction are invariant and **byte-identical across every backend** (a parity test is the merge
gate). The **core stays Polars + PyArrow**; every non-local backend is an **opt-in extra** — no
platform SDK is ever imported by the core (Constitution G8). Platform integrations are **sidecars**
(Databricks is the first; Snowflake/Postgres/Supabase/LakeBase/Lakehouse follow the same interface).

## The backends

| `from storage import …` | Persists as | Target | Install |
|---|---|---|---|
| `LocalFolderStore` (default) | `manifest.json` + `events/*.parquet` (byte-identical to classic `.flux/`) | local disk | core |
| `ObjectStoreBackend` | same layout, **atomic single-object meta PUT** (never rename — FUSE-safe) | `s3://` `abfss://` `gs://` UC Volume `memory://` | `pip install "fluxstate[remote]"` (fsspec) |
| `TableBackend` | **`flux_events`** table (+ optional materialized **`flux_mirror`**) | Parquet dataset (default) · Delta · Iceberg | core for Parquet; `"fluxstate[table]"` for Delta/Iceberg |
| `DeltaBackend` (`from sidecars.databricks import DeltaBackend`) | Delta `flux_events` (+ `flux_mirror`) + scheduled-Job template | Databricks | `pip install "fluxstate[databricks]"` |

## How to select a backend

Two ways — both preserve the existing API (`store_path=` local calls are unchanged):

```python
from fluxstate import FluxState
from storage import TableBackend

# 1) URI inference via store_path (local path -> folder; s3://·abfss://·gs:// -> object store)
FluxState(df, key_column="id", store_path="patients.flux").update_mirror_table()             # local (default)
FluxState(df, key_column="id", store_path="s3://bucket/patients.flux").update_mirror_table()  # object store ([remote])

# 2) explicit backend via store=  (tables / sidecars)
FluxState(df, key_column="id", store=TableBackend(
    events="warehouse/flux_events",       # Parquet dataset (default) | format="delta"|"iceberg" (opt-in)
    mirror="warehouse/flux_mirror",       # optional wide "current state"; opt-in
    mirror_refresh="on_demand",           # on_demand (default) | cadence:N | off
)).update_mirror_table()
```

- `storage.select_backend(location, *, store=None, backend=None)` is the resolver behind the URI
  inference. Selecting a backend whose extra isn't installed raises an actionable error naming the extra.
- **Reconstruction is identical everywhere:** `fs.travel(T)`, `fs.get_timeline(...)`, `fs.row_state(...)`,
  `fs.save_mirror_table(...)` behave the same regardless of backend.

## The two tabular artifacts (answer to "store delta + a table?")

- **`flux_events`** — the narrow, append-only change-log = the lean **source of truth**. Always present.
  Directly queryable: `SELECT * FROM flux_events` (Parquet dataset / Delta / Iceberg table).
- **`flux_mirror`** — the optional, materialized **wide reconstructed current (or as-of) state** — the
  query-friendly "old mirror table" shape. Opt-in; refresh `on_demand` (default) or `cadence:N`.
  Fully derivable from `flux_events` (a cache, not a second source of truth).

## Databricks (the first platform sidecar)

```python
from fluxstate import FluxState
from sidecars.databricks import DeltaBackend      # needs fluxstate[databricks]
snapshot = pl.from_arrow(spark.table("cat.sch.my_view").toArrow())
FluxState(snapshot, key_column="id", store=DeltaBackend(
    events="cat.sch.flux_events", mirror="cat.sch.flux_mirror")).update_mirror_table()  # idempotent; no-op on unchanged days
```
Runbook: `docs/DATABRICKS.md` (scheduled Job, Volume/Delta layout, Spark-SQL as-of reconstruction).

## Notes / gotchas

- **Idempotent everywhere:** an unchanged re-capture adds nothing (content `snapshot_id`); storage grows
  with *change volume*, not table-size × captures.
- **Object stores:** the meta commit is a single atomic PUT (never a temp+rename), so it's safe on
  FUSE/UC Volumes where rename isn't atomic.
- **`format="iceberg"`** needs `sqlalchemy` (bundled in `[table]`) for pyiceberg's local SqlCatalog.
- **Adding a new platform = adding a sidecar** over the same interface — never touch the core (G8).
- Deep reference: `AGENTS.md` (backends table + the as-of algorithm) · `docs/DATABRICKS.md` · the design
  contracts in `specs/003-pluggable-storage-backends/contracts/`.
