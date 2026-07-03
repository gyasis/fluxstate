---
name: fluxstate
description: Overview + router for FluxState, the cell-level CDC / temporal-versioning library for tabular data (a `.flux/` store = manifest.json + immutable delta Parquet). Use when the user says "fluxstate", "track changes over time", "cell-level history", "time-travel a table", "audit trail for this data", "diff two versions of a dataset", "what changed between these snapshots", "store the change-log in a table / on s3 / in Databricks", or wants versioned/temporal storage of a keyed table. Routes to fluxstate-capture (write), fluxstate-timetravel (read/reconstruct), fluxstate-compare (A/B + viewer), or fluxstate-storage (pick a backend: local/object-store/table/Databricks).
---

# FluxState — overview & router

FluxState records **cell-level changes** of any keyed table as an append-only, glob-readable
Parquet change-log, and reconstructs any state **as of any timestamp** with exact types and full
delete/resurrect lineage. It's a *faithful recorder of what the source emits* — not a
semantic-equality engine (`1.1` → `1.10` is a change).

## Mental model (read `AGENTS.md` for the full version)

A store is a folder `<name>.flux/` with **two file kinds**:
- `manifest.json` — the index: schema union (`col→dtype`), `key_column`, event-file list + ts ranges.
- `events/<ts>.parquet` — **delta files**: each capture appends one immutable Parquet of only the
  changed cells, as rows `(entity_id, timestamp, field, value, dtype, snapshot_id)`.

Reconstruction (as-of T) = newest value per `(entity_id, field)` with `timestamp ≤ T`, drop entities
whose newest event is a `__deleted__` marker, pivot field→column, cast per `dtype`, sort by key.

## Which skill to use

| Goal | Skill |
|---|---|
| Ingest snapshots / build or append a store | **`fluxstate-capture`** |
| Read history — as-of state, per-cell timeline, row lifecycle, store info | **`fluxstate-timetravel`** |
| Compare two versions (A/B) or open the interactive viewer | **`fluxstate-compare`** |
| Choose/use a **storage backend** (local · object store · table · Databricks) | **`fluxstate-storage`** |

## Setup (once per repo/machine)

```bash
git clone https://github.com/gyasis/fluxstate.git && cd fluxstate
uv sync            # or: pip install -e .
uv run flux --help
```

## Fast facts an agent needs

- **Library:** `from fluxstate import FluxState`. **CLI:** `flux <capture|travel|timeline|row-state|view|info|gen-fixture|serve>` (`--json` everywhere).
- **Idempotent:** re-capturing identical data is a no-op (content-derived `snapshot_id`).
- **Lossless + typed:** values re-cast from stored text via the `dtype` tag.
- **No lock-in:** `SELECT * FROM '<name>.flux/events/*.parquet'` in DuckDB / Polars, no FluxState code.
- **Pluggable storage backends** (platform-agnostic, G8): **local folder** (default) · **object store**
  (`s3`/`abfss`/`gcs`/UC Volume, `[remote]`) · a **first-class table** (`flux_events` + optional
  `flux_mirror`; Parquet default, Delta/Iceberg opt-in, `[table]`) · **Databricks** `DeltaBackend`
  (`[databricks]`). Select by `store_path` URI inference or pass `store=<backend>`. Reconstruction is
  **byte-identical across every backend**. Core stays Polars+PyArrow; platform libs are opt-in extras.
  → **`fluxstate-storage`** (details) and `AGENTS.md`.
