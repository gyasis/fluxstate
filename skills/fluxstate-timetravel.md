---
name: fluxstate-timetravel
description: Read/reconstruct history from a FluxState `.flux/` store — the READ path. Use when the user wants "the table as of <date>", "time-travel this data", "what was this cell/row back then", "the history of this value", "when did X change", "is this row deleted or resurrected", "reconstruct the state at T", or to inspect/query a `.flux` store (including direct DuckDB glob). Covers `travel` / `timeline` / `row-state` / `view` / `info` and the `reconstruct.py` primitives.
---

# FluxState — time-travel (read path)

Nothing is stored pre-materialized; every state is **reconstructed on read** from the delta log,
with values restored to their original types.

## The as-of algorithm (what every read does)

Newest value per `(entity_id, field)` with `timestamp ≤ T` → drop entities whose newest event is a
`__deleted__` marker → pivot field→column → cast text→type per `dtype` → sort by key. (Re-expressible
in Spark/DuckDB SQL if you need it outside Python.)

## Library

```python
from fluxstate import FluxState
fs = FluxState(df=None, key_column="id", store_path="patients.flux")  # open existing store

current    = fs.save_mirror_table(output_format="polars")   # current view (or arrow/parquet/csv)
as_of      = fs.travel("2026-06-05T00:00:00Z")              # whole-table state as of a past point
historical = fs.query_historical_value("2026-06-05T00:00:00Z")  # {entity_id: {col: value}}, typed
timeline   = fs.get_timeline(entity_id=2, field="risk")     # [{date, value}, …] per-cell history
state      = fs.row_state(entity_id=3, T="now")             # {"state": active|deleted|unborn, "resurrected": bool}
n          = fs.change_count(entity_id=3)                    # #change events for an entity
```

Pure primitives (take a `ChangeLogStore` explicitly) live in `reconstruct.py`:
`as_of`, `get_timeline`, `row_state`, `build_mirror_view`.

## CLI (`--json` everywhere)

```bash
flux travel  <store.flux> --as-of <ISO|now>                         # reconstruct table as-of T (typed)
flux timeline <store.flux> <id> [--field risk]                      # per-cell history [{date,value}]
flux row-state <store.flux> <id> [--as-of now]                      # {state, resurrected}
flux view    <store.flux> [--as-of T] [--format polars|arrow|parquet|csv] [--out <path>]
flux info    <store.flux>                                           # schema, key, #events, ts range
```

## Direct DuckDB / Polars (no FluxState code)

The store is plain, glob-readable Parquet — reconstruct or audit it from any engine:

```sql
-- all raw change events
SELECT * FROM '<store.flux>/events/*.parquet' ORDER BY timestamp;

-- as-of T (sketch): newest value per (entity_id, field) ≤ T, then pivot + drop __deleted__ in your query
SELECT entity_id, field, value, dtype
FROM (
  SELECT *, row_number() OVER (PARTITION BY entity_id, field ORDER BY timestamp DESC) rn
  FROM '<store.flux>/events/*.parquet' WHERE timestamp <= TIMESTAMP '<T>'
) WHERE rn = 1;
```

## Notes

- `travel()` **before any history → empty result, not an error.**
- `__deleted__` rows and `dtype="null"` are lifecycle markers — respect them when hand-writing SQL.
- Always treat `manifest.json`'s file list as authoritative (an unlisted stray parquet isn't history).
