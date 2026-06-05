# Contract: Change-Log Store (`<name>.flux/` folder)

**Feature**: `001-changelog-first-pivot` · **Date**: 2026-06-05

The on-disk contract for the append-only store. MUST stay glob-readable
(`SELECT * FROM '<name>.flux/events/*.parquet'`) by DuckDB / DuckDB-WASM / Polars — no opaque
container, no Delta/Iceberg.

---

## Folder layout

```text
<name>.flux/
├── manifest.json          # authoritative: schema + valid file list + per-file ts range + checkpoints
├── events/
│   ├── <ts1>.parquet      # immutable — one capture's Change Events
│   ├── <ts2>.parquet
│   └── …                  # append = a NEW file; existing files are never rewritten
└── checkpoints/           # reserved (fast-follow); empty in P1
```
`<ts>` is a UTC, lexically-sortable stamp (so a plain glob sorts chronologically).

---

## `events/<ts>.parquet` schema

Columns (see data-model.md "Change Event"):

| Column | Parquet type | Required |
|---|---|---|
| `entity_id` | string | yes |
| `timestamp` | timestamp (UTC) | yes |
| `field` | string | yes — a tracked column name, OR the reserved `"__deleted__"` for a deletion marker row |
| `value` | string (nullable) | yes — `null` for a `"__deleted__"` marker or a genuine SQL null (distinguished by `dtype`) |
| `dtype` | string | yes — `"null"` on a deletion marker row |
| `snapshot_id` | string | yes |

**Write requirements**
- Each file's row-group statistics MUST carry `min(timestamp)` / `max(timestamp)` (native parquet
  stats) so readers can skip whole files during time-travel (R5).
- Files are write-once; a committed file is never mutated (FR-002).
- Within a file, `(entity_id, field, snapshot_id)` is unique.

---

## `manifest.json` (the commit point)

See `manifest.schema.json` for the formal shape. Essentials:
- `schema_version`, the tracked-table `schema` (column → dtype), `key_column`.
- `events`: ordered list of `{ file, snapshot_id, ts_min, ts_max, row_count }` — **only** these
  files are valid history. A parquet on disk but absent here is an uncommitted/partial capture and
  MUST be ignored by readers.
- `checkpoints`: list (empty in P1).

**Atomic commit protocol** (FR-013)
1. Write events parquet to `events/.<ts>.parquet.tmp` → fsync → atomic `rename` to `events/<ts>.parquet`.
2. Compute the new manifest in memory; write to `manifest.json.tmp` → fsync → atomic `rename` over `manifest.json`.
3. A crash before step 2 leaves an orphan parquet that readers ignore (history unchanged) — never a half-valid commit.

---

## Reader contract

- Resolve valid files from `manifest.json` only.
- For `asOf(T)` / window queries, prune files whose `[ts_min, ts_max]` lies entirely after `T` /
  outside the window using manifest ranges (and/or parquet row-group stats), then scan the rest.
- Reconstruction is built on a Polars LazyFrame over the surviving files (streaming-ready; internal).

---

## Invariants (testable)

| ID | Invariant |
|---|---|
| STORE-1 | After a capture with changes, exactly one new `events/*.parquet` exists and is listed in the manifest. |
| STORE-2 | No pre-existing events file's bytes change across captures. |
| STORE-3 | `SELECT * FROM '<name>.flux/events/*.parquet'` in DuckDB returns all change events (glob-readable). |
| STORE-4 | A parquet present on disk but not in the manifest is excluded from all reconstruction. |
| STORE-5 | Each events file exposes `min/max(timestamp)` row-group stats. |
| STORE-6 | No Delta/Iceberg/proprietary metadata directory is created. |
