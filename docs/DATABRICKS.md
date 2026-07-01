# FluxState on Databricks — runbook

Track a Databricks **view/table's** cell-level deltas over time — daily, on a schedule, **staying
entirely on Databricks** — and store the change-log in a **tabular** format.

> **Status (read first).** The **`fluxstate[databricks]` sidecar** (a `DeltaBackend` that writes a
> Delta `flux_events` table + optional `flux_mirror`) is **PLANNED** — see the PRD
> [`prd/pluggable_storage_backends_2026-07-01.md`](../prd/pluggable_storage_backends_2026-07-01.md)
> (Constitution Principle I: platform integrations are sidecars, never core). **What works TODAY** is
> **Pattern A** below: run the FluxState *library* inside a scheduled Job, capture on POSIX local disk,
> and sync the `.flux/` store to a Unity Catalog Volume. This doc documents both — clearly labeled.

---

## The core problem (why the bridge exists)

FluxState's local writer commits by **write-temp → atomic `rename` → rewrite `manifest.json`**. That
is reliable on a real POSIX disk. Databricks' *durable* storage is object-store-backed **FUSE**
(UC Volumes / DBFS), where `rename` is **not guaranteed atomic** — so writing the store *directly* to
a Volume risks a torn manifest. The fix in every pattern: **capture on POSIX local disk, persist the
result to durable storage** (a Volume folder, or — with the sidecar — a Delta table whose transaction
log replaces the manifest entirely).

---

## Prerequisites

- A cluster (or serverless) with FluxState installed:
  ```python
  %pip install git+https://github.com/gyasis/fluxstate.git
  ```
- A source **view/table with a stable key column** (the `entity_id` — must match rows across days).
- A durable location: a **UC Volume** (`/Volumes/<cat>/<sch>/<vol>/`) for Pattern A, and/or Delta
  tables for the planned sidecar.

---

## Pattern A — library on a scheduled Job + Volume sync (WORKS TODAY)

A daily Job task (Python notebook). It restores the prior store from the Volume to local disk,
captures today's snapshot, and syncs back. Because captures are **append-only + idempotent**, a
no-change day writes nothing.

```python
import os, shutil
import polars as pl
from fluxstate import FluxState

VIEW         = "catalog.schema.my_view"
KEY          = "id"                                              # stable entity key
DURABLE      = "/Volumes/catalog/schema/flux/my_view.flux"       # durable (UC Volume, FUSE)
LOCAL        = "/local_disk0/my_view.flux"                       # POSIX working copy (ephemeral)

# 1) restore prior store Volume -> local disk (POSIX working area)
if os.path.exists(DURABLE):
    shutil.rmtree(LOCAL, ignore_errors=True)
    shutil.copytree(DURABLE, LOCAL)

# 2) read today's snapshot of the view into Polars
#    (toArrow() is fastest where available; toPandas() is the portable fallback)
try:
    snapshot = pl.from_arrow(spark.table(VIEW).toArrow())
except Exception:
    snapshot = pl.from_pandas(spark.table(VIEW).toPandas())

# 3) capture on local disk (atomic on POSIX; idempotent — no-op if unchanged)
FluxState(snapshot, key_column=KEY, store_path=LOCAL).update_mirror_table()

# 4) sync local -> Volume (durable). Simple: mirror the whole folder.
shutil.rmtree(DURABLE, ignore_errors=True)
shutil.copytree(LOCAL, DURABLE)
```

- **Scale note:** step 4 mirrors the whole `.flux/` folder. The store is append-only, so for large
  histories copy only **new** `events/*.parquet` + the `manifest.json` instead of a full re-copy.
- **Query it** anywhere — the events are glob-readable Parquet:
  ```sql
  SELECT * FROM read_files('/Volumes/catalog/schema/flux/my_view.flux/events/*.parquet');
  ```

### Schedule it (the "cron")

Databricks **Workflows (Jobs)** = the cron. Attach the notebook as a task and give the Job a
**scheduled trigger** (cron syntax), e.g. daily at 02:00 UTC: `0 0 2 * * ?`. (A table-update /
file-arrival trigger is also available if you later want change-driven capture.)

---

## Pattern B/C — the `[databricks]` sidecar: Delta `flux_events` (+ `flux_mirror`) — PLANNED

The target the PRD specs. The sidecar wires FluxState's storage interface to **Delta tables** — no
Volume/FUSE dance, no manifest (Delta's transaction log tracks files), and the change-log is a
**first-class table**:

```python
# PLANNED — fluxstate[databricks]
from fluxstate import FluxState
from fluxstate.databricks import DeltaBackend

fs = FluxState(
    snapshot, key_column="id",
    store=DeltaBackend(
        events="catalog.schema.flux_events",          # narrow EAV change-log (source of truth)
        mirror="catalog.schema.flux_mirror",          # optional: materialized wide current state
        mirror_refresh="each_capture",                # each_capture | on_demand | off
    ),
)
fs.update_mirror_table()     # appends to flux_events (idempotent) + refreshes flux_mirror
```

Distributed capture for very large views will be optional (`applyInPandas`); the default is a
driver-side capture (the diff is a **table-level join**, not a per-row UDF).

---

## Storage: the two-table contract (answers "delta + tabular?")

| Artifact | Shape | Role | Store it? |
|---|---|---|---|
| **`flux_events`** | narrow EAV table `(entity_id, timestamp, field, value, dtype, snapshot_id)` | lean, append-only **source of truth** (history) | **always** |
| **`flux_mirror`** | wide reconstructed **current/as-of** state | the query-friendly "mirror table" shape | **optional** — materialize on a cadence |
| meta | Delta table properties (or a tiny `flux_meta`) | schema union + `key_column` | replaces `manifest.json` in table mode |

Trade-off: `flux_mirror` is a full-table-size duplicate refreshed each run; `flux_events` stays
compact. Keep both for query convenience; keep only `flux_events` + reconstruct-on-read to minimize
space. Your call per dataset.

---

## Reconstruct as-of T in Spark SQL (works over `flux_events`, Delta or Parquet)

The same algorithm the library uses — newest value per `(entity_id, field)` ≤ T, drop entities whose
newest event is a `__deleted__` marker, then pivot field→columns:

```sql
WITH ranked AS (
  SELECT *,
         row_number() OVER (PARTITION BY entity_id, field ORDER BY timestamp DESC) AS rn,
         max_by(field, timestamp) OVER (PARTITION BY entity_id)               AS newest_field
  FROM flux_events
  WHERE timestamp <= TIMESTAMP '2026-07-01T00:00:00Z'
),
live AS (                                   -- drop entities whose newest event is a deletion
  SELECT entity_id, field, value, dtype
  FROM ranked
  WHERE rn = 1 AND field <> '__deleted__'
    AND newest_field <> '__deleted__'
)
SELECT * FROM live;                         -- then PIVOT field -> columns, cast per dtype
```

(For the current state, drop the `WHERE timestamp <= …`. `flux_mirror`, if materialized, is this
already pivoted.)

---

## Gotchas

- **Never write the `.flux/` folder straight to a Volume/DBFS in Pattern A** — FUSE `rename` isn't
  atomic; capture on `/local_disk0`, then sync. (The Delta sidecar removes this entirely.)
- **`/local_disk0` is ephemeral** — it's a working copy; the Volume/Delta table is the durable truth.
- **Stable key column is mandatory** — no stable `entity_id`, no cross-day matching.
- **Idempotency makes no-op days free** — a content-derived `snapshot_id` anti-join means an unchanged
  view adds nothing on its daily run.
- **A UDF is the wrong tool for the diff** — capturing deltas is a table-level join, done driver-side
  (or `applyInPandas`), not a per-row UDF.
- **Faithful recorder:** a format/precision/timezone change in the view **is** a change and will be
  logged (Constitution Principle II). Normalize at read/compare time if you want `82.00 == 82`.

---

## Other platforms

Databricks is the **first** sidecar, not the only one (Constitution Principle I — platform-agnostic).
The same storage interface backs **Snowflake / PostgreSQL / Supabase / LakeBase / generic Lakehouse**
sidecars, and the universal `ObjectStoreBackend` (S3/ADLS/GCS) + `LocalFolderStore` need no sidecar at
all. Design: [`prd/pluggable_storage_backends_2026-07-01.md`](../prd/pluggable_storage_backends_2026-07-01.md).

## See also
[`AGENTS.md`](../AGENTS.md) (storage targets) · [`docs/API.md`](API.md) · the constitution
`.specify/memory/constitution.md`.
