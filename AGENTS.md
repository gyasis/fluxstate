# FluxState — agent orientation

> Fast, accurate context for a coding agent about to work with FluxState. Read this first;
> deep detail is in [`docs/API.md`](docs/API.md). The usable-from-any-session skills are flat
> files in [`skills/`](skills/) (drop them into `~/.claude/skills/`).

## What it is (one paragraph)

FluxState is a Python library for **cell-level Change Data Capture with temporal versioning**
over *any* keyed tabular data. You feed it successive typed snapshots of a table; it stores only
the **cell-level diffs** as an append-only, glob-readable Parquet change-log, and lets you
reconstruct any column / cell / row / whole-table state **as of any timestamp**, with exact types
and full delete/resurrect lineage. **Guiding principle:** it is a *faithful recorder of what the
source emits*, not a semantic-equality engine — `1.1` → `1.10` **is** a change; it never
canonicalizes values to decide they're "really equal."

## The storage model — two file kinds (know this)

A store is a folder `<name>.flux/`:

```
<name>.flux/
├── manifest.json          # the INDEX: schema union (col→dtype), key_column,
│                          # and the list of event files w/ ts_min/ts_max + snapshot_id
└── events/
    ├── <ts>.parquet       # a DELTA file: one capture's changed cells (immutable)
    └── <ts>.parquet       # append = a NEW file; existing files are NEVER rewritten
```

- **Change event schema:** `(entity_id, timestamp, field, value, dtype, snapshot_id)`. One row =
  one changed cell at a capture. A deletion is a single `__deleted__` marker row (not a null per
  column), so delete→re-insert keeps one continuous timeline.
- **Why two kinds / why it's lean:** the old model stored a full "mirror table" with the audit
  trail JSON-in-cell (grew with table-size × captures, whole-file rewrites). The delta model grows
  only with **how much data changes** — unchanged cells cost nothing, Parquet is append-only, and
  the current/as-of "mirror table" is a **view reconstructed on read**. Lossless (canonical text +
  `dtype` re-cast) and idempotent (content-derived `snapshot_id` anti-join → re-capture is a no-op).
- **No lock-in:** plain Parquet, no Delta/Iceberg metadata. `SELECT * FROM
  '<name>.flux/events/*.parquet'` works in DuckDB / DuckDB-WASM / Polars with zero FluxState code.

## The as-of reconstruction (the one algorithm to know)

To get the table state at time T: **take the newest value per `(entity_id, field)` with
`timestamp ≤ T`, drop entities whose newest event is a `__deleted__` marker, pivot field→column,
cast text→type per the `dtype` tag, sort by key.** This is what `travel()` / `flux travel` /
`reconstruct.as_of` do, and it's trivially re-expressible in SQL (Spark/DuckDB) if you need it
outside Python.

## Library API (`from fluxstate import FluxState`)

| Method | Behavior |
|---|---|
| `FluxState(df, key_column, store_path=...)` | bind a snapshot to a store (key = the entity id across captures) |
| `update_mirror_table(captured_at=None)` | capture: keyed join-diff vs prior → append one immutable events file (idempotent) |
| `save_mirror_table(*, output_format=)` | reconstructed **current** view; `output_format ∈ polars\|arrow\|parquet\|csv` |
| `travel(date)` | reconstructed state **as of** `date` (UTC); before history → empty, not an error |
| `query_historical_value(query_date)` | `{entity_id: {column: value}}` as of a date, typed |
| `get_timeline(entity_id, field=None)` | per-cell history `[{date, value}]`, typed |
| `row_state(entity_id, T="now")` | `{state: active\|deleted\|unborn, resurrected: bool}` |
| `change_count(entity_id)` | number of change events for an entity |

Reconstruction primitives also exist as pure functions in `reconstruct.py`
(`as_of`, `get_timeline`, `row_state`, `build_mirror_view`) taking a `ChangeLogStore`.

## CLI (`flux …` — thin wrappers over the library; `--json` everywhere)

```bash
flux capture <store.flux> <input.parquet|csv> --key id [--at <ISO>]   # append-only capture
flux travel  <store.flux> --as-of <ISO|now>                           # reconstruct as-of T (typed)
flux timeline <store.flux> <id> [--field risk]                        # per-cell history
flux row-state <store.flux> <id> [--as-of now]                        # {state, resurrected}
flux view    <store.flux> [--as-of T] [--format polars|arrow|parquet|csv] [--out …]
flux info    <store.flux>                                             # schema, key, #events, ts range
flux gen-fixture <store.flux> [--rows N --cols N --steps N --seed 42 --violations K]
flux serve   <store.flux> [--port 5173] [--no-open]                  # launch the Temporal Viewer
```

## Storage backends (feature 003 — pluggable, platform-agnostic)

Persistence goes through a pluggable `StorageBackend` seam (Constitution G8). The core stays
Polars + PyArrow; each backend beyond local is an **opt-in extra**. Select by `store_path` URI
(inference) or pass an explicit backend via `store=`:

| Backend (`from storage import …`) | Persists as | Target | Extra |
|---|---|---|---|
| `LocalFolderStore` (default) | `manifest.json` + `events/*.parquet` | local disk | — (core) |
| `ObjectStoreBackend` | same layout, atomic single-object meta PUT | `s3://` `abfss://` `gs://` UC Volume `memory://` | `[remote]` (fsspec) |
| `TableBackend` | **`flux_events`** table (+ optional **`flux_mirror`**) | Parquet dataset (default) · Delta/Iceberg (opt-in) | `[table]` for Delta/Iceberg |
| `DeltaBackend` (`from sidecars.databricks import DeltaBackend`) | Delta `flux_events` (+ `flux_mirror`) | Databricks | `[databricks]` |

```python
from fluxstate import FluxState
from storage import TableBackend
FluxState(df, key_column="id", store_path="patients.flux").update_mirror_table()        # local (default)
FluxState(df, key_column="id", store_path="s3://bucket/patients.flux").update_mirror_table()  # object store
FluxState(df, key_column="id", store=TableBackend(events="warehouse/flux_events",
          mirror="warehouse/flux_mirror")).update_mirror_table()                        # tabular
```

Any platform integration is an optional **sidecar** (Databricks is the first — `docs/DATABRICKS.md`);
Snowflake / Postgres / Supabase / LakeBase / Lakehouse follow the same interface. **Reconstruction is
byte-identical across all backends** (a parity test is the merge gate). The store stays plain
Parquet — glob-readable by DuckDB / Polars / Spark with no FluxState code; `flux_events` can be a
first-class table you query directly, and `flux_mirror` is the optional wide "current state" view.

## Gotchas

- **Faithful recorder, not semantic-equality:** format/timezone/precision changes ARE changes.
  (A consumer that wants "82.00 == 82" normalizes at read time — e.g. the viewer's `≈` toggle.)
- **`dtype` tag is load-bearing:** it re-casts stored text back to the real type. Don't drop it.
- **Idempotency is content-derived** (`snapshot_id`): re-capturing identical data adds no file.
- **`__deleted__` is a real row,** not an absence — it's how lifecycle/resurrection works.
- **Glob order:** always read via the `manifest.json` file list (it's authoritative); a stray
  parquet in `events/` not listed in the manifest is not valid history.

## Where to go next

- **Deep API + runnable examples:** [`docs/API.md`](docs/API.md)
- **The viewer surfaces:** [`docs/VISUALIZATIONS.md`](docs/VISUALIZATIONS.md)
- **Embedding in Pharos:** [`docs/PHAROS_INTEGRATION.md`](docs/PHAROS_INTEGRATION.md)
- **Design/spec:** `specs/001-changelog-first-pivot/`, `specs/002-fluxstate-temporal-viewer/`
- **Agent skills (flat, copy into `~/.claude/skills/`):** [`skills/`](skills/)
