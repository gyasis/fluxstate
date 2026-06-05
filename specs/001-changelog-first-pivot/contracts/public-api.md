# Contract: FluxState Public API (back-compat surface)

**Feature**: `001-changelog-first-pivot` · **Date**: 2026-06-05

The library's external interface. Existing signatures MUST keep working (FR-011); the change-log
pivot is transparent to callers. New capability is **additive**. Types are logical (Polars/PyArrow).

---

## Preserved entry points (back-compat — MUST NOT break callers)

### `FluxState(table, key_column=None, mode="init", expect_serialized=False)`
Unchanged constructor surface. `table` is a Polars `DataFrame` (the snapshot source). `key_column`
provides `entity_id`.

### `update_mirror_table()`
**Behavior change, signature preserved.** Now performs a capture: keyed join-diff (row-hash
prefiltered) vs prior state → append one `events/<ts>.parquet` → commit manifest. O(rows); no
full-table rewrite. Idempotent on re-capture of the same snapshot.
- **Post-condition**: exactly one new events file when ≥1 change; zero new files when nothing changed or snapshot already recorded.

### `save_mirror_table(output_path_parquet=None, csv_path=None, *, output_format=None)`
**Additive `output_format` keyword.** Returns/writes the reconstructed mirror view.

**Precedence rule (resolves the back-compat vs default conflict — I1):** `output_format`
defaults to `None`, NOT `"polars"`. Resolution order:
1. If `output_format` is **explicitly** passed → honor it exactly (table below).
2. Else if `output_path_parquet` is given (and no `output_format`) → **write parquet** (legacy behavior).
3. Else if `csv_path` is given (and no `output_format`) → **write CSV** (legacy behavior).
4. Else (no path, no format) → return a `pl.DataFrame` (the convenient default).

| explicit `output_format` | Result |
|---|---|
| `"polars"` | returns `pl.DataFrame` |
| `"arrow"` | returns `pa.Table` (zero-copy) |
| `"parquet"` | writes parquet to `output_path_parquet`; returns path |
| `"csv"` | writes CSV to `csv_path`; returns path |

- **Back-compat (API-6)**: a legacy positional call `save_mirror_table("out.parquet")` still
  **writes a parquet file** (rule 2) — it does NOT silently return a DataFrame.
- **Error**: explicit unknown `output_format` → `ValueError` listing valid options; a file format
  requested without its path → `ValueError`.

### `travel(date)`
Returns the reconstructed table state **as of** `date` (UTC-compared). Built on `as_of` over the
change-log. `date` before any history → empty result (not an error).

### `query_historical_value(query_date)`
Returns historical value(s) as of `query_date`, reconstructed from the change-log; numeric/datetime
values restored to original `dtype` (no string-cast).

### `load_mirror_table(parquet_path, key_column=None)` *(classmethod)*
Preserved. Loads an existing store/exported view.

### `get_change_statistics()`, `filter(...)`, `filter_for_null_values(...)`
Preserved signatures; now computed over the reconstructed view / change-log.

---

## New reconstruction primitives (additive)

Exposed for the Viewer + auditors (see data-model.md). Pure reads over the change-log.

**Exposure surface (U2):** the primitives are implemented as functions in `reconstruct.py` and
**re-exported as thin `FluxState` methods** so both call styles work — `fs.get_timeline(...)`
(as in quickstart.md) and `reconstruct.get_timeline(store, ...)`. The `FluxState` method binds the
store; the module function takes the store explicitly.

**`as_of` — two distinct functions (I2), do not conflate:**
```python
# Internal series-level primitive (binary-searchable over one cell's ordered history):
reconstruct._as_of(history: list[{date, value}], T) -> {"date": datetime, "value": Any} | None
# Public resolver (looks up the (entity_id, field) series, then applies _as_of):
as_of(entity_id, field, T)          -> {"date": datetime, "value": Any} | None
```

```python
row_state(entity_id, T)             -> {"state": "active"|"deleted"|"unborn", "resurrected": bool}
get_timeline(entity_id, field=None) -> list[{"date": datetime, "value": Any}]
change_count(entity_id)             -> int
```
- All `value`s are cast back to their recorded `dtype`.
- `T` / `date` inputs normalized to UTC before comparison.
- `get_timeline` rebuilds the per-cell series lazily; never reads a JSON-in-cell blob.

---

## Invariants (testable)

| ID | Invariant |
|---|---|
| API-1 | A second differing capture appends exactly one events file; no prior file is modified. |
| API-2 | Re-capturing an already-recorded snapshot returns/writes the same view and adds zero events. |
| API-3 | `save_mirror_table(output_format="polars")` → `pl.DataFrame`; `"arrow"` → `pa.Table`; identical contents across formats. |
| API-4 | `travel(T)` / `query_historical_value(T)` equal ground-truth state at `T`, with original dtypes. |
| API-5 | An entity deleted then re-inserted yields one continuous `active → deleted → active` timeline under one `entity_id`. |
| API-6 | Existing positional calls to `save_mirror_table` / `travel` / `query_historical_value` behave as before the pivot. |
