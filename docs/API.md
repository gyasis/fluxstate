# FluxState API Reference

**Feature**: `001-changelog-first-pivot` · **Version**: 2026-06-05

---

## 1. Overview

FluxState is a Python library for tracking row-level changes in tabular data with full
time-travel reconstruction. The `001-changelog-first-pivot` storage pivot replaces the
prior JSON-in-cell mirror with an **append-only change log** stored in a `<name>.flux/`
folder on disk.

Key properties of the pivoted system:

- **Append-only** — every capture writes exactly one new immutable `events/<ts>.parquet`
  file. No existing file is ever rewritten.
- **Reconstruction on read** — the current or any historical state is rebuilt by
  replaying change events; no materialized snapshot is kept separate.
- **Type fidelity** — `int64`, `float64`, `utf8`, `bool`, `datetime[us, UTC]`, and genuine
  `null` are recorded as canonical dtype tags and round-trip correctly through encode/decode.
- **Glob-readable** — `SELECT * FROM '<name>.flux/events/*.parquet'` in DuckDB or Polars
  returns all change events with no FluxState code. No Delta/Iceberg metadata is created.
- **Lightweight** — runtime dependencies are Polars and PyArrow only.

---

## 2. Concepts

### The `.flux/` store layout

```text
<name>.flux/
├── manifest.json          # authoritative commit record
├── events/
│   ├── 20260605T143000000000Z.parquet   # one immutable file per capture
│   ├── 20260605T150000000000Z.parquet
│   └── …
└── checkpoints/           # reserved (empty in P1)
```

`<ts>` is a UTC, lexically-sortable microsecond stamp so a plain glob sorts
chronologically. `<name>` is derived from `store_path` (the `Path.stem`).

### The Change Event schema

Every `events/<ts>.parquet` file has exactly these columns:

| Column | Parquet type | Notes |
|---|---|---|
| `entity_id` | `string` | The key-column value cast to string |
| `timestamp` | `timestamp(us, UTC)` | Capture time, always UTC |
| `field` | `string` | A tracked column name, or `"__deleted__"` |
| `value` | `string` (nullable) | Encoded value; real `null` for deletions and genuine SQL nulls |
| `dtype` | `string` | Canonical dtype tag (see below); `"null"` on deletion rows |
| `snapshot_id` | `string` | 16-hex-char content-derived id for idempotency |

### The `__deleted__` marker

When a row disappears from a snapshot, **one** `__deleted__` marker event is written
(not one null per column). The marker has `value=None` and `dtype="null"`. The entity
is excluded from the reconstructed view until it reappears. When it does reappear, new
SET events are appended under the **same** `entity_id`, giving a continuous
`active → deleted → active` trail without creating a new identity.

### `snapshot_id` idempotency

`snapshot_id` is a 16-character SHA-256 prefix derived deterministically from the
snapshot content (rows sorted by key, columns canonicalized). Re-capturing the identical
snapshot yields the same `snapshot_id`; the `capture` method anti-joins against recorded
`snapshot_id`s and returns a no-op dict without writing anything.

### dtype tags

| Tag | Polars dtype | Notes |
|---|---|---|
| `"int64"` | `pl.Int64` | |
| `"float64"` | `pl.Float64` | `repr()` round-trip preserves precision |
| `"utf8"` | `pl.Utf8` / `pl.String` | |
| `"bool"` | `pl.Boolean` | stored as `"true"` / `"false"` |
| `"datetime[us, UTC]"` | `pl.Datetime("us", "UTC")` | tz-aware ISO 8601 |
| `"null"` | `pl.Null` | deletion-marker rows only |

Genuine SQL `null` in a tracked cell: the `value` column holds a real parquet null
(not the string `"NULL"`); the `dtype` tag is the column's actual type. This
distinguishes a null value from a deletion marker (`dtype="null"`).

---

## 3. Installation

```bash
cd /path/to/fluxstate
uv venv && source .venv/bin/activate
uv pip install -e .
```

Runtime dependencies: **Polars**, **PyArrow**, `orjson`, `numpy`, `humanize`, `tqdm`.
`duckdb` and `pytz` are test-only. No Delta/Iceberg or Snowflake dependencies are
required in P1.

Run the test suite (no Snowflake connection required):

```bash
pytest TESTS/ -q
```

---

## 4. Usage Guide

All examples below use `store_path=` so the store goes to a controlled location. Pass a
`tempfile.TemporaryDirectory()` path to avoid leaving files in the working directory
during exploration.

### 4.1 Capture — append-only and idempotent

```python
import polars as pl
from fluxstate import FluxState

# --- Day 1: first snapshot -------------------------------------------------------
t1 = pl.DataFrame({
    "id":     pl.Series([1, 2, 3], dtype=pl.Int64),
    "risk":   pl.Series([0.4, 0.7, 0.2], dtype=pl.Float64),
    "status": pl.Series(["A", "A", "B"], dtype=pl.Utf8),
})
fs = FluxState(t1, key_column="id", store_path="/tmp/mystore.flux")
result = fs.update_mirror_table()
# result == {"events_added": 6, "snapshot_id": "<hex>", "file": "events/<ts>.parquet", "noop": False}

# --- Day 2: one cell changes; row 3 disappears -----------------------------------
t2 = pl.DataFrame({
    "id":     pl.Series([1, 2], dtype=pl.Int64),
    "risk":   pl.Series([0.4, 0.9], dtype=pl.Float64),
    "status": pl.Series(["A", "A"], dtype=pl.Utf8),
})
fs2 = FluxState(t2, key_column="id", store_path="/tmp/mystore.flux")
fs2.update_mirror_table()
# Exactly one new events file appended; row 3 logged as __deleted__

# --- Re-capture day 2 (idempotent): no new file ----------------------------------
result = fs2.update_mirror_table()
assert result["noop"] is True
assert result["events_added"] == 0
```

### 4.2 Time-travel reconstruction with type fidelity

```python
from datetime import datetime, timezone
import polars as pl
from fluxstate import FluxState

U = lambda *a: datetime(*a, tzinfo=timezone.utc)

# Seed three captures with deterministic timestamps (tests do this too)
store_path = "/tmp/travelstore.flux"
for ts, risk2 in [(U(2026, 1, 1), 0.7), (U(2026, 1, 2), 0.9)]:
    df = pl.DataFrame({"id": [1, 2], "risk": [0.4, risk2]})
    fs = FluxState(df, key_column="id", store_path=store_path)
    fs.update_mirror_table(captured_at=ts)

# Current state as pl.DataFrame
mirror_now = fs.save_mirror_table(output_format="polars")
assert isinstance(mirror_now, pl.DataFrame)

# Current state as pa.Table (zero-copy)
arrow_now = fs.save_mirror_table(output_format="arrow")

# State as of Jan 1 (before Jan 2 update)
state_jan1 = fs.travel("2026-01-01T12:00:00Z")
assert state_jan1.filter(pl.col("id") == 2)["risk"][0] == 0.7   # typed float, not string

# Historical dict: {entity_id: {column: value}}
hist = fs.query_historical_value("2026-01-01T12:00:00Z")
assert hist[2]["risk"] == 0.7            # float64, not "0.7"

# Per-cell timeline: [{date, value}, ...]
timeline = fs.get_timeline(entity_id=2, field="risk")
assert [e["value"] for e in timeline] == [0.7, 0.9]   # typed floats
```

### 4.3 Delete → resurrection continuity

```python
from datetime import datetime, timezone
import polars as pl
from fluxstate import FluxState

U = lambda *a: datetime(*a, tzinfo=timezone.utc)
store_path = "/tmp/resurrection.flux"

# Day 1: all three rows
fs1 = FluxState(
    pl.DataFrame({"id": [1, 2, 3], "risk": [0.4, 0.7, 0.2]}),
    key_column="id", store_path=store_path,
)
fs1.update_mirror_table(captured_at=U(2026, 1, 1))

# Day 2: row 3 vanishes
fs2 = FluxState(
    pl.DataFrame({"id": [1, 2], "risk": [0.4, 0.9]}),
    key_column="id", store_path=store_path,
)
fs2.update_mirror_table(captured_at=U(2026, 1, 2))

# Day 3: row 3 returns
fs3 = FluxState(
    pl.DataFrame({"id": [1, 2, 3], "risk": [0.4, 0.9, 0.5]}),
    key_column="id", store_path=store_path,
)
fs3.update_mirror_table(captured_at=U(2026, 1, 3))

# row_state shows the lifecycle
assert fs3.row_state(entity_id=3, T="now") == {"state": "active", "resurrected": True}
assert fs3.row_state(entity_id=3, T=U(2026, 1, 2)) == {"state": "deleted", "resurrected": False}

# Timeline under ONE entity_id: 0.2 (day1) → 0.5 (day3), deletion in between
timeline = fs3.get_timeline(entity_id=3, field="risk")
assert [e["value"] for e in timeline] == [0.2, 0.5]
```

### 4.4 Multi-format output

```python
import polars as pl
import pyarrow as pa
from fluxstate import FluxState

store_path = "/tmp/formats.flux"
fs = FluxState(pl.DataFrame({"id": [1, 2], "v": [10, 20]}),
               key_column="id", store_path=store_path)
fs.update_mirror_table()

# Polars DataFrame
df = fs.save_mirror_table(output_format="polars")
assert isinstance(df, pl.DataFrame)

# PyArrow Table (zero-copy)
tbl = fs.save_mirror_table(output_format="arrow")
assert isinstance(tbl, pa.Table)

# Write parquet, receive path back
path = fs.save_mirror_table(output_path_parquet="/tmp/out.parquet", output_format="parquet")
assert path == "/tmp/out.parquet"

# Write CSV, receive path back
cpath = fs.save_mirror_table(csv_path="/tmp/out.csv", output_format="csv")
assert cpath == "/tmp/out.csv"

# Legacy positional call still writes parquet (back-compat API-6)
res = fs.save_mirror_table("/tmp/legacy.parquet")
assert not isinstance(res, pl.DataFrame)   # it returned the path, not a DataFrame
```

### 4.5 Direct DuckDB glob query (no FluxState code)

```sql
-- All change events across the store:
SELECT * FROM 'mystore.flux/events/*.parquet';

-- Time-travel in SQL:
SELECT entity_id, field, value
FROM 'mystore.flux/events/*.parquet'
WHERE timestamp <= '2026-01-02T00:00:00Z'
ORDER BY timestamp;
```

---

## 5. API Reference — `FluxState`

`FluxState` is the public facade. Import it as:

```python
from fluxstate import FluxState
```

### `FluxState.__init__`

```python
FluxState(
    table: pl.DataFrame,
    key_column: str | None = None,
    mode: str = "init",
    expect_serialized: bool = False,
    store_path: str | Path | None = None,
)
```

**Parameters**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `table` | `pl.DataFrame` | required | The typed source snapshot. |
| `key_column` | `str \| None` | `None` | Column providing `entity_id`. Defaults to the first column when `None`. |
| `mode` | `str` | `"init"` | `"init"` initializes a fresh in-memory mirror. `"compare"` loads an existing mirror (legacy). Raises `ValueError` on unknown values. |
| `expect_serialized` | `bool` | `False` | `True` when `mode="compare"` and the source table has JSON-serialized cell values (legacy compare path). |
| `store_path` | `str \| Path \| None` | `None` | Path for the `.flux/` store. Defaults to `fluxstate.flux` in the current working directory. Use explicit paths in tests and scripts. |

**Notes**
- `self.store` is the bound `ChangeLogStore` instance.
- The `_source_table` attribute holds the original typed frame (before the internal cast to string that the legacy mirror uses). `capture` always receives the typed frame.

---

### `update_mirror_table`

```python
fs.update_mirror_table(captured_at: datetime | None = None) -> dict
```

Diff the current snapshot against the prior stored state and append one immutable
events file (FR-011 / API-1). O(rows), not O(rows × columns), due to the row-hash
prefilter.

**Parameters**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `captured_at` | `datetime \| None` | `None` | Stamp the capture time deterministically; defaults to `datetime.now(UTC)`. Useful in tests and backfills. |

**Returns** `dict` with:

| Key | Type | Description |
|---|---|---|
| `events_added` | `int` | Number of change events written (`0` on a no-op). |
| `snapshot_id` | `str` | 16-char hex content-derived id. |
| `file` | `str` | Relative path of the new events file, e.g. `"events/20260605T143000Z.parquet"`. Only present when `noop=False`. |
| `noop` | `bool` | `True` when nothing was written (no changes or snapshot already recorded). |

**Guarantees**
- Exactly one new file when ≥1 change; zero new files when nothing changed or the snapshot was already recorded (idempotent, API-1, API-2).
- No prior events file is modified (STORE-2).

---

### `save_mirror_table`

```python
fs.save_mirror_table(
    output_path_parquet: str | None = None,
    csv_path: str | None = None,
    *,
    output_format: str | None = None,
) -> pl.DataFrame | pa.Table | str
```

Return or write the reconstructed mirror view in the requested format (FR-012 / API-3).

**Precedence rule (I1) — `output_format` defaults to `None`, not `"polars"`:**

1. If `output_format` is **explicitly** passed → honor it exactly (table below).
2. Else if `output_path_parquet` is given (and no `output_format`) → write parquet (legacy). Returns the path string.
3. Else if `csv_path` is given (and no `output_format`) → write CSV (legacy). Returns the path string.
4. Else (no path, no format) → return a `pl.DataFrame`.

| `output_format` value | Return type | Side effect |
|---|---|---|
| `"polars"` | `pl.DataFrame` | none |
| `"arrow"` | `pa.Table` | none (zero-copy) |
| `"parquet"` | `str` (path) | writes `output_path_parquet` |
| `"csv"` | `str` (path) | writes `csv_path` |

**Parameters**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `output_path_parquet` | `str \| None` | `None` | Path for parquet output. Required when `output_format="parquet"`. |
| `csv_path` | `str \| None` | `None` | Path for CSV output. Required when `output_format="csv"`. |
| `output_format` | `str \| None` | `None` | One of `"polars"`, `"arrow"`, `"parquet"`, `"csv"`. Keyword-only. |

**Raises**
- `ValueError` — unknown `output_format` (message lists valid options).
- `ValueError` — `output_format="parquet"` with no `output_path_parquet`.
- `ValueError` — `output_format="csv"` with no `csv_path`.

**Back-compat (API-6)**

```python
# Legacy positional call still writes parquet and returns the path (not a DataFrame):
result = fs.save_mirror_table("out.parquet")   # writes the file
```

---

### `travel`

```python
fs.travel(date: str | datetime) -> pl.DataFrame
```

Reconstructed table state **as of** `date` (API-4 / API-6).

**Parameters**

| Parameter | Type | Description |
|---|---|---|
| `date` | `str \| datetime` | ISO 8601 string or `datetime`. Normalized to UTC. |

**Returns** `pl.DataFrame` with original column dtypes. An empty (zero-row) DataFrame
with the table's schema is returned when `date` precedes all history — not an error.

---

### `query_historical_value`

```python
fs.query_historical_value(query_date: str | datetime) -> dict
```

Historical state as of `query_date`, as a nested dict (API-6).

**Returns** `{entity_id: {column: value}}` where values are restored to their original
dtypes (float, int, bool, datetime — not string-cast).

---

### `as_of`

```python
fs.as_of(entity_id: Any, field: str, T: datetime) -> dict | None
```

Thin wrapper around `reconstruct.as_of`. Returns `{"date": datetime, "value": Any}` for
the value of `(entity_id, field)` at/before `T`, decoded to its recorded dtype.
Returns `None` when no event exists at or before `T`.

---

### `get_timeline`

```python
fs.get_timeline(entity_id: Any, field: str | None = None) -> list[dict]
```

Per-cell change history (thin wrapper around `reconstruct.get_timeline`).

- With `field` set → `[{"date": datetime, "value": Any}, ...]` for that cell.
- With `field=None` → every change for the entity as `[{"date": datetime, "field": str, "value": Any}, ...]`.

Values are decoded to their recorded dtypes.

---

### `change_count`

```python
fs.change_count(entity_id: Any) -> int
```

Total number of change events recorded for `entity_id` (thin wrapper around
`reconstruct.change_count`).

---

### `row_state`

```python
fs.row_state(entity_id: Any, T: str | datetime = "now") -> dict
```

Lifecycle state of `entity_id` at `T` (thin wrapper around `reconstruct.row_state`).

**Returns** `{"state": str, "resurrected": bool}` where `state` is one of:

| `state` | Meaning |
|---|---|
| `"unborn"` | No events for this entity at or before `T`. |
| `"active"` | The most recent event at/before `T` is a SET (not a deletion). |
| `"deleted"` | The most recent event at/before `T` is a `__deleted__` marker. |

`resurrected` is `True` when a deletion was followed by a later SET in the chain.

---

### `get_change_statistics`

```python
fs.get_change_statistics() -> dict
```

Change statistics computed over the committed change events and the current
reconstructed view (FR-011 / API-6).

**Returns** `dict` with keys: `total_cells`, `changed_cells`, `percent_changed`,
`mirror_table_size`.

---

### `filter`

```python
fs.filter(
    column_filters: dict | None = None,
    date_range: tuple | None = None,
) -> pl.DataFrame
```

Filter the reconstructed view by column values and/or date (FR-011 / API-6).

- `column_filters` maps a column name to either a value (equality test) or a callable
  predicate `(value) -> bool`.
- `date_range` is a `(lo, hi)` tuple (either bound may be `None`). When provided, the
  view is reconstructed as of `date_range[1]`.

Returns a `pl.DataFrame` of matching rows.

---

### `filter_for_null_values`

```python
fs.filter_for_null_values(
    column_name: str | None = None,
    date_range: tuple | None = None,
) -> pl.DataFrame
```

Rows of the reconstructed view that have a null value (FR-011 / API-6).

- With `column_name` → rows where that column is null.
- Without `column_name` → rows where any non-key column is null.
- `date_range` controls the reconstruction point (same as `filter`).

---

### `load_mirror_table` *(classmethod)*

```python
FluxState.load_mirror_table(parquet_path: str, key_column: str | None = None) -> FluxState
```

Load a mirror table from a parquet file and return a new `FluxState` instance in
`mode="compare"` (API-6 back-compat).

**Parameters**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `parquet_path` | `str` | required | Path to the parquet file. |
| `key_column` | `str \| None` | `None` | Key column; defaults to the first column of the loaded frame. |

---

## 6. API Reference — `ChangeLogStore`

The low-level append-only store. Import it as:

```python
from fluxstate import ChangeLogStore
# or directly:
from changelog import ChangeLogStore
```

### `ChangeLogStore.__init__`

```python
ChangeLogStore(path: str | Path)
```

Binds the store to `path`. Does not create the directory yet (created lazily on the
first write).

**Attributes**

| Attribute | Description |
|---|---|
| `self.path` | `Path` — the `.flux/` root. |
| `self.events_dir` | `Path` — `<path>/events/`. |
| `self.manifest_path` | `Path` — `<path>/manifest.json`. |

---

### `capture`

```python
store.capture(
    df: pl.DataFrame,
    key_column: str,
    captured_at: datetime | None = None,
) -> dict
```

Diff `df` against current state and append one immutable events file (T015).

**Parameters**

| Parameter | Type | Description |
|---|---|---|
| `df` | `pl.DataFrame` | The typed snapshot to capture. |
| `key_column` | `str` | Must be a column in `df`. Raises `ValueError` otherwise. |
| `captured_at` | `datetime \| None` | Stamp the capture time; defaults to now (UTC). |

**Returns** `dict`:

| Key | Type | Present when |
|---|---|---|
| `events_added` | `int` | always |
| `snapshot_id` | `str` | always |
| `file` | `str` | `noop=False` only |
| `noop` | `bool` | always |

**Raises** `ValueError` — `key_column` not in `df.columns`.

---

### `read_manifest`

```python
store.read_manifest() -> dict
```

Return the manifest dict (STORE-4). Returns a fresh empty manifest when the store does
not exist yet. Only files listed under `"events"` in the returned manifest are valid
history; orphan parquet files on disk but absent from the manifest are ignored by all
readers.

---

### `write_manifest`

```python
store.write_manifest(manifest: dict) -> None
```

Atomically write the manifest (temp file → fsync → `os.replace`). This is the commit
point (FR-013). Creates `self.path` if it does not exist.

---

### `list_events`

```python
store.list_events(
    as_of: datetime | None = None,
    window: tuple | None = None,
) -> list[Path]
```

Return manifest-valid event file paths (chronological order) with file-skip pruning (R5).

- `as_of` — drop files whose `ts_min` is strictly after `as_of` (they cannot contain
  relevant events).
- `window` — `(lo, hi)` tuple (either bound `None`); drop files whose `[ts_min, ts_max]`
  range lies entirely outside the window.

Orphan parquet files (not in the manifest) are never returned.

---

### Internal methods (summary)

These are prefixed with `_` and not part of the public contract, but noted here for
completeness:

| Method | Purpose |
|---|---|
| `_materialize_current(pl_schema, key_column, as_of=None)` | Reconstruct the live wide-table state (latest value per cell, typed). Used by `capture` to obtain the prior frame. Returns `None` for an empty store. |
| `_diff(prev, new, key_column, *, timestamp, snap_id)` | Keyed full-outer diff → INSERT/UPDATE/DELETE Change Events. Row-hash prefilter gates the expensive melt. |
| `_append_events(events, snapshot_id, key_column, schema, stamp=None)` | Atomic crash-safe append (temp → fsync → rename) plus manifest commit. |

---

## 7. Module-level helpers (`changelog` module)

These functions are importable directly from `changelog` and are the building blocks of
the store. They are not re-exported from the top-level `fluxstate` package but are stable.

### `dtype_tag`

```python
dtype_tag(dtype: pl.DataType) -> str
```

Canonicalize a Polars dtype to its stored string tag. Single source of truth used by
the schema, the diff, and the codec.

```python
from changelog import dtype_tag
import polars as pl

assert dtype_tag(pl.Int64) == "int64"
assert dtype_tag(pl.Float64) == "float64"
assert dtype_tag(pl.Boolean) == "bool"
assert dtype_tag(pl.Utf8) == "utf8"
assert dtype_tag(pl.Datetime("us", "UTC")) == "datetime[us, UTC]"
```

---

### `tag_to_dtype`

```python
tag_to_dtype(tag: str) -> pl.DataType
```

Inverse of `dtype_tag`: a stored type tag → the Polars dtype. Used by the reader to
rebuild a typed schema from the manifest.

---

### `encode_value`

```python
encode_value(v: Any, dtype: str) -> str | None
```

Encode a Python value to its string cell representation. Returns `None` for a genuine
null (so the parquet `value` column holds a real null, not the string `"NULL"`).

---

### `decode_value`

```python
decode_value(value: str | None, dtype: str) -> Any
```

Decode a stored string cell back to its original typed value. `None` input → `None`
output.

---

### `to_utc`

```python
to_utc(ts: datetime) -> datetime
```

Normalize a timestamp to UTC. tz-aware → convert; tz-naive → declare UTC (no shift).
Every event `timestamp` flows through this.

---

### `change_event_schema`

```python
change_event_schema() -> dict[str, pl.DataType]
```

Return the ordered Polars schema for a Change Event frame (T006). Mirrors the
`events/<ts>.parquet` column spec.

---

### `hash_rows`

```python
hash_rows(df: pl.DataFrame, key_column: str) -> pl.Series
```

Per-row content hash over canonicalized non-key columns (T012). Used as a cheap
prefilter: unchanged rows never reach the melt+compare path. Column order is stabilized
(sorted) so the hash is canonical regardless of input column order.

---

### `snapshot_id`

```python
snapshot_id(df: pl.DataFrame, key_column: str) -> str
```

Content-derived 16-char hex id for a snapshot (T013). Deterministic across processes
for identical content (rows sorted by key, columns sorted). Identical snapshots yield
the same id; this is the key used by `capture`'s idempotency anti-join.

---

## 8. API Reference — `reconstruct` module

Reader/reconstruction primitives. All functions are also available as thin `FluxState`
methods (binding `self.store` automatically).

```python
import reconstruct
# or via FluxState:
# fs.as_of(...), fs.get_timeline(...), etc.
```

### `as_of`

```python
reconstruct.as_of(
    store: ChangeLogStore,
    entity_id: Any,
    field: str,
    T: datetime,
) -> dict | None
```

**Public resolver** (I2 distinction — see note below). Looks up the `(entity_id, field)`
series and returns the latest event at or before `T`, decoded to its dtype.

**Returns** `{"date": datetime, "value": Any}` or `None` when no event exists at/before `T`.

---

### `_as_of` *(internal)*

```python
reconstruct._as_of(history: list[dict], T: datetime) -> dict | None
```

**Series-level resolver** (I2). Operates directly on a pre-built
`[{"date": datetime, "value": Any}, ...]` list for one cell. Distinct from the public
`as_of`, which first fetches the series from the store and then applies this. Performs
a linear scan over the ascending list and returns the last entry with `date <= T`.

This is an internal function; use `as_of` (public) or `fs.as_of(...)` in application
code.

---

### `get_timeline`

```python
reconstruct.get_timeline(
    store: ChangeLogStore,
    entity_id: Any,
    field: str | None = None,
) -> list[dict]
```

Per-cell timeline rebuilt from change events (values decoded to their dtype).

- `field` provided → `[{"date": datetime, "value": Any}, ...]`
- `field=None` → `[{"date": datetime, "field": str, "value": Any}, ...]`

Entries are sorted ascending by date.

---

### `change_count`

```python
reconstruct.change_count(store: ChangeLogStore, entity_id: Any) -> int
```

Total number of change events recorded for `entity_id` across all captures.

---

### `build_mirror_view`

```python
reconstruct.build_mirror_view(
    store: ChangeLogStore,
    T: str | datetime = "now",
) -> pl.DataFrame
```

Reconstruct the full table state as of `T` into a typed `pl.DataFrame` (T020).

For each live `entity_id`, the latest value of each field at/before `T` is resolved and
cast back to its recorded dtype (API-4). Deleted rows are omitted. Returns an empty
(zero-row) DataFrame with the table's schema when `T` precedes all history. Uses
`list_events` file-skip pruning to bound the scan.

`T="now"` (or `T=None`) means current state (no time bound).

---

### `row_state`

```python
reconstruct.row_state(
    store: ChangeLogStore,
    entity_id: Any,
    T: str | datetime = "now",
) -> dict
```

Return `{"state": str, "resurrected": bool}` describing the lifecycle at `T` (FR-005).

| `state` value | Condition |
|---|---|
| `"unborn"` | No events for this entity at/before `T`. |
| `"deleted"` | Most recent event at/before `T` is a `__deleted__` marker. |
| `"active"` | Otherwise. |

`resurrected` is `True` when the lifecycle chain contains a `__deleted__` marker
followed by a later SET event (continuous trail under one `entity_id`).

---

## 9. Store layout & manifest

### Folder tree

```text
<name>.flux/
├── manifest.json
├── events/
│   ├── 20260605T143000000000Z.parquet
│   └── 20260605T150000000000Z.parquet
└── checkpoints/              # empty in P1
```

### `manifest.json` fields

| Field | Type | Description |
|---|---|---|
| `schema_version` | `int` | Manifest format version (currently `1`). |
| `store_name` | `str` | The `<name>` from `<name>.flux/`. |
| `key_column` | `str` | Source column providing `entity_id`. |
| `schema` | `object` | `{column: dtype_tag}` map for the tracked table, e.g. `{"id": "int64", "risk": "float64"}`. |
| `events` | `array` | Ordered list of committed event file entries (see below). |
| `checkpoints` | `array` | Reserved; empty in P1. |

Each entry in `events`:

| Field | Type | Description |
|---|---|---|
| `file` | `str` | Relative path, e.g. `"events/20260605T143000Z.parquet"`. |
| `snapshot_id` | `str` | 16-hex content-derived id (idempotency key). |
| `ts_min` | `str` (ISO 8601) | Earliest event timestamp in the file. Used for file-skip pruning. |
| `ts_max` | `str` (ISO 8601) | Latest event timestamp in the file. |
| `row_count` | `int` | Number of change events in the file. |

**Reader contract**: resolve valid files from `manifest.json` only. A parquet file on
disk but absent from `manifest.json` is an uncommitted/partial write and must be ignored
(STORE-4).

### DuckDB glob query

```sql
-- Read all change events across all captures (no FluxState code required):
SELECT * FROM 'mystore.flux/events/*.parquet';
```

This returns the Change Event schema (6 columns: `entity_id`, `timestamp`, `field`,
`value`, `dtype`, `snapshot_id`). Works with DuckDB-WASM in the browser too.

---

## 10. Migration notes

### From the old JSON-in-cell mirror

The previous API stored change history as JSON arrays inside parquet cells. The pivoted
API is transparent at the call site — existing code calling `update_mirror_table()`,
`save_mirror_table()`, `travel()`, and `query_historical_value()` continues to work
without changes.

**Key behavioral changes** (all additive or internalized):

| Old behavior | New behavior |
|---|---|
| `update_mirror_table()` mutated an in-memory dict mirror | Now appends one immutable parquet file to `<name>.flux/events/`; O(rows) not O(rows × cells). |
| `save_mirror_table()` serialized the in-memory dict | Now reconstructs the typed wide-table view from change events. |
| Values in the reconstructed view were strings | Values now round-trip to their original dtype (`float64`, `int64`, `bool`, `datetime`). |
| No `store_path` parameter | `store_path=` is now an additive keyword; defaults to `fluxstate.flux` in cwd. |
| No `captured_at` parameter | `captured_at=` is an additive keyword on `update_mirror_table`; defaults to now. |
| Snowpark / Snowflake was a dependency | Removed; runtime deps are Polars + PyArrow only. |

### Preserved call signatures (API-6)

These all work unchanged after the pivot:

```python
# positional parquet write
fs.save_mirror_table("out.parquet")

# travel
df = fs.travel("2026-01-01T00:00:00Z")

# historical dict
hist = fs.query_historical_value("2026-01-01T00:00:00Z")

# classmethod load
fs2 = FluxState.load_mirror_table("snapshot.parquet", key_column="id")
```

---

## 11. Invariants & guarantees

### Store invariants (STORE-1..6)

| ID | Invariant |
|---|---|
| STORE-1 | After a capture with changes, exactly one new `events/*.parquet` exists and is listed in the manifest. |
| STORE-2 | No pre-existing events file's bytes change across captures. |
| STORE-3 | `SELECT * FROM '<name>.flux/events/*.parquet'` in DuckDB returns all change events (glob-readable). |
| STORE-4 | A parquet present on disk but not in the manifest is excluded from all reconstruction. |
| STORE-5 | Each events file exposes `min/max(timestamp)` row-group statistics (enables file-skip for time-travel). |
| STORE-6 | No Delta/Iceberg/proprietary metadata directory is created. |

### API invariants (API-1..6)

| ID | Invariant |
|---|---|
| API-1 | A second differing capture appends exactly one events file; no prior file is modified. |
| API-2 | Re-capturing an already-recorded snapshot returns/writes the same view and adds zero events. |
| API-3 | `save_mirror_table(output_format="polars")` → `pl.DataFrame`; `"arrow"` → `pa.Table`; identical contents across formats. |
| API-4 | `travel(T)` / `query_historical_value(T)` return original dtypes (float, int, bool, datetime — not string-cast). |
| API-5 | An entity deleted then re-inserted yields one continuous `active → deleted → active` timeline under one `entity_id`. |
| API-6 | Existing positional calls to `save_mirror_table`, `travel`, and `query_historical_value` behave as before the pivot. |

### Crash safety

The atomic commit protocol (FR-013) guarantees no half-valid commits:

1. Write events parquet to `events/.<ts>.parquet.tmp` → fsync → atomic `os.replace`.
2. Compute new manifest in memory → write to `manifest.json.tmp` → fsync → atomic `os.replace`.

A crash before step 2 leaves an orphan parquet that readers ignore (STORE-4). History is
never corrupted.

---

## 12. Package exports

```python
from fluxstate import FluxState          # public facade
from fluxstate import ChangeLogStore     # low-level store
from fluxstate import MirrorTableValidator, HistoricalRecord, MirrorTableColumn  # validators
```
