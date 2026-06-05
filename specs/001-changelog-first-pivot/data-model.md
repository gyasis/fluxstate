# Phase 1 Data Model: Changelog-First Storage Pivot

**Feature**: `001-changelog-first-pivot` · **Date**: 2026-06-05

Source: spec.md Key Entities + `docs/viewer/fluxstate-design-decision.md` (change-log schema) +
the Viewer data contract. All persisted timestamps are **UTC**.

---

## Entity: Change Event  *(the atomic unit of history)*

One row in `events/<ts>.parquet`. Records a single field's value for one record at one point in time.

| Field | Type (logical) | Notes |
|---|---|---|
| `entity_id` | string (or source key type, rendered stable) | The record's stable identity. Ties all events — including delete/resurrect — for one logical row together. From `key_column`. |
| `timestamp` | datetime, **UTC** | Event time. tz-aware inputs converted to UTC; tz-naive declared UTC. Drives `asOf`/window pruning. |
| `field` | string | The tracked column this event is for. A **deletion** is encoded as a single marker row with the reserved field name `field = "__deleted__"` (not one row per column). |
| `value` | string | The new value, rendered. `null` for a deletion marker row. Cast back to `dtype` on read. |
| `dtype` | string (type tag) | Original Polars dtype, e.g. `int64`, `float64`, `utf8`, `bool`, `datetime[us, UTC]`, `null`. Enables lossless restore. |
| `snapshot_id` | string (hash) | Identifies the capture/batch that produced this event. Basis for idempotency anti-join. |

**Validation rules**
- `entity_id` MUST be non-null and stable across snapshots (same logical row ⇒ same id).
- `timestamp` MUST be UTC; events for one `(entity_id, field)` form a time-ordered series.
- A deletion event is **exactly one marker row**: `field = "__deleted__"`, `value = null`,
  `dtype = "null"`, keyed by `entity_id`, produced only by the full-outer-join "in OLD, not NEW"
  branch. Reconstruction reads this marker (not per-field nulls) to set `row_state = deleted`.
  `"__deleted__"` is a reserved field name and MUST NOT collide with a real tracked column.
- `dtype = null` (or `value = null`) represents a genuine SQL null — never the literal string `"NULL"`.
- Idempotency key: `(entity_id, field, snapshot_id)` is unique within the store.

**State transitions (per `entity_id`, derived — not stored as a status column)**
```
unborn ──first event──▶ active ──"in OLD not NEW"──▶ deleted ──reappears in NEW──▶ active (RESURRECTED)
                                                          └────────── stays deleted (no later event) ──────────┘
```
- INSERT: id in NEW, never seen ⇒ initial SET events.
- CHANGE: id in both, cell differs ⇒ normal SET event for that field.
- DELETE: id in OLD, not in NEW ⇒ `__deleted__` event (value=null).
- RESURRECTION: id in NEW, previously seen **and last state deleted** ⇒ SET events appended to the **same** `entity_id` (continuous trail, no new identity).

---

## Entity: Snapshot  *(a capture input)*

The point-in-time dataset handed to a capture. Not persisted as such; it is diffed into Change Events.

| Field | Type | Notes |
|---|---|---|
| (table) | `pl.DataFrame` | The current source state (FluxState is "Polars on anything" — source may originate from Snowflake, CSV, etc., but enters as a DataFrame). |
| `key_column` | string | Column providing `entity_id`. |
| `snapshot_id` | string (hash) | Content-derived identity of this snapshot; makes re-capture idempotent. |
| `captured_at` | datetime, UTC | The `timestamp` stamped on this run's events. |

**Validation rules**
- `key_column` MUST exist and be unique within the snapshot.
- Column order is canonicalized before hashing/diffing (reorder ⇒ no spurious change).

---

## Entity: Change-Log Store  *(`<name>.flux/` folder)*

The append-only home of all history; authoritative for every historical query. See
`contracts/changelog-store.md` for the on-disk contract.

| Member | Type | Notes |
|---|---|---|
| `manifest.json` | file | Source of truth: schema + ordered valid `events` file list + checkpoints + per-file ts range. Commit point. |
| `events/<ts>.parquet` | files (immutable) | Each = one capture's Change Events. Row-group `min/max(timestamp)` stamped for file-skip. Append = new file; never rewritten. |
| `checkpoints/` | dir (reserved) | Materialized point-in-time snapshots — **populated in fast-follow**, not P1. |

**Validation rules**
- A reader trusts ONLY files listed in `manifest.json`; orphan parquet (not in manifest) = partial/failed capture, ignored.
- An events parquet, once committed, is immutable (FR-002, FR-014).
- Commit is atomic: parquet rename → manifest rewrite via temp + atomic rename (FR-013).

---

## Entity: Record Identity  *(`entity_id`)*

The stable key tying all events for one logical record together across snapshots, including across
a delete→resurrect cycle. Sourced from `key_column`. The single reason FluxState can show a
continuous `active → deleted → active` trail that a two-table diff cannot.

---

## Derived View: Reconstructed Mirror View  *(replaces stored JSON-in-cell mirror)*

Computed on demand from the change-log; NOT independently stored. Feeds `save_mirror_table`,
`travel`, `query_historical_value`, and the multi-format output.

**Reconstruction primitives** (the only read logic; see `contracts/public-api.md`):
| Primitive | Returns | Definition |
|---|---|---|
| `as_of(history, T)` | latest `{date, value}` with `t ≤ T` | binary-searchable over an `(entity_id, field)` series |
| `row_state(entity_id, T)` | `{ state: active\|deleted\|unborn, resurrected: bool }` | from the lifecycle transition chain |
| `get_timeline(entity_id, field)` | `[{date, value}, …]` | per-cell timeline, rebuilt lazily (never stored as JSON-in-cell) |
| `change_count(entity_id)` | int | Σ events across fields (volatility/heat) |
| `SNAPS` | sorted unique event timestamps | slider snap points (viewer) |

**Mirror view** at time `T` = for each live `entity_id`, each `field` resolved via `as_of(., T)`,
cast back to `dtype`, with deleted rows marked/omitted per `row_state`.

---

## Relationships

```
Change-Log Store ──contains──▶ events/*.parquet ──rows are──▶ Change Event
Snapshot ──diffed-against-prior-into──▶ Change Event(s)        (keyed join-diff + row-hash prefilter)
Change Event ──keyed-by──▶ Record Identity (entity_id)
Reconstructed Mirror View ──derived-from──▶ Change-Log Store   (as_of / row_state / get_timeline)
manifest.json ──lists/commits──▶ events/*.parquet              (reader trusts manifest only)
```
