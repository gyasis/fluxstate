---
name: fluxstate-capture
description: Capture/ingest snapshots into a FluxState `.flux/` store — the WRITE path. Use when the user wants to "record a snapshot", "capture the current state", "start tracking this table", "append today's data", "build a .flux store", "log what changed", or ingest successive versions of a keyed dataset for versioning/audit. Covers the library `update_mirror_table()` and the `flux capture` CLI, idempotency, and how deletes are recorded.
---

# FluxState — capture (write path)

Capture appends **one immutable delta Parquet** of only the changed cells vs the prior state, and
updates `manifest.json`. Re-capturing identical data is a **no-op** (content-derived `snapshot_id`).

## Library

```python
import polars as pl
from fluxstate import FluxState

# Day 1: first snapshot → writes patients.flux/events/<ts>.parquet + manifest.json
t1 = pl.DataFrame({"id": [1, 2, 3], "risk": [0.4, 0.7, 0.2], "status": ["A", "A", "B"]})
FluxState(t1, key_column="id", store_path="patients.flux").update_mirror_table()

# Day 2: one cell changes; row 3 disappears (recorded as a __deleted__ marker)
t2 = pl.DataFrame({"id": [1, 2], "risk": [0.4, 0.9], "status": ["A", "A"]})
fs = FluxState(t2, key_column="id", store_path="patients.flux")
fs.update_mirror_table()      # appends ONE new events file
fs.update_mirror_table()      # idempotent re-run → adds nothing

# optional explicit capture time (else "now", UTC-normalized)
fs.update_mirror_table(captured_at="2026-06-06T00:00:00Z")
```

- **`key_column`** is the entity key that matches a row **across** snapshots — it's what makes the
  per-cell diff native (no hand-rolled "changed" column). Composite keys: pass the key your data uses.
- A **deletion** = the entity present in the prior state but absent now → one `__deleted__` marker
  row. A later re-insert **resurrects** it on the same timeline.
- The diff is **row-hash-prefiltered + keyed**, so it stays O(changed rows) even on wide tables.

## CLI

```bash
flux capture <store.flux> <input.parquet|csv> --key id [--at <ISO>] [--json]
```

- `<input>` is one snapshot (Parquet or CSV). Run it again with the next snapshot to append.
- `--at <ISO>` sets the capture timestamp (default: now, UTC).

## Verify the capture landed

```bash
flux info <store.flux>            # schema, key, #events, ts range
# or inspect raw: DuckDB over the delta files (no FluxState code)
#   SELECT snapshot_id, count(*) FROM '<store.flux>/events/*.parquet' GROUP BY 1;
```

## Notes

- **Faithful recorder:** a format/precision/timezone change IS a change (`1.1`→`1.10` is logged).
- **Type fidelity:** values are stored as canonical text + a `dtype` tag; reads re-cast to the
  original type — don't pre-stringify your input.
- **Storage:** writes to a local path today. To land in Databricks, capture locally then upload the
  immutable `events/*.parquet` to a UC Volume / object store (copy-new-files-only), or point
  `store_path` at a mounted Volume/DBFS path. See `AGENTS.md` → Storage targets.
