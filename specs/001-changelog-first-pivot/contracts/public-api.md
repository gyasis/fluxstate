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

### `save_mirror_table(output_path_parquet=None, csv_path=None, *, output_format="polars")`
**Additive `output_format` keyword.** Returns/writes the reconstructed mirror view.
| `output_format` | Result |
|---|---|
| `"polars"` (default) | returns `pl.DataFrame` |
| `"arrow"` | returns `pa.Table` (zero-copy) |
| `"parquet"` | writes parquet (uses `output_path_parquet`); returns path |
| `"csv"` | writes CSV (uses `csv_path`); returns path |
- **Back-compat**: existing positional `output_path_parquet` / `csv_path` calls still write files as before.
- **Error**: unknown `output_format` → `ValueError` listing valid options.

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

```python
as_of(entity_id, field, T) -> {"date": datetime, "value": Any} | None
row_state(entity_id, T)     -> {"state": "active"|"deleted"|"unborn", "resurrected": bool}
get_timeline(entity_id, field=None) -> list[{"date": datetime, "value": Any}]
change_count(entity_id)     -> int
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
