# Data Model: FluxState Temporal Viewer

**Feature**: `002-fluxstate-temporal-viewer` · **Date**: 2026-06-06

The viewer is a **read model** over the shipped change-log. It introduces no new persisted entities — the
on-disk truth is the `<name>.flux/` store (see the changelog-pivot contracts). The entities below are the
reconstructed/derived shapes the CLI and viewer compute and pass around.

---

## Source of truth (shipped — not redefined here)

| Entity | Where | Shape |
|---|---|---|
| **Change Event** | `events/*.parquet` row | `entity_id:str, timestamp:datetime[UTC], field:str, value:str?, dtype:str, snapshot_id:str` |
| **Manifest** | `manifest.json` | `schema_version, store_name, key_column, schema{col→dtype-tag}, events[{file,snapshot_id,ts_min,ts_max,row_count}], checkpoints[]` |
| Deletion marker | a Change Event | `field="__deleted__", value=null, dtype="null"` (one per vanished entity) |

---

## Derived / read entities (computed by reconstruct primitives)

### CellTimeline
The ordered history of one `(entity_id, field)`.
- **Fields**: `entity_id:str`, `field:str`, `points: [{ date: datetime[UTC], value: <typed> }]` (ascending).
- **Source**: `reconstruct.get_timeline(store, entity_id, field)` — values decoded via the dtype tag (FR-002).
- **Rule**: never a JSON-in-cell blob; rebuilt on demand (FR-001).

### AsOfValue
The value of one cell at time T.
- **Fields**: `{ date: datetime[UTC], value: <typed> } | null` (null ⇒ no value at/before T).
- **Source**: `reconstruct.as_of(store, entity_id, field, T)` (public) / `_as_of(history, T)` (series-level, I2).

### MirrorView (as-of table)
The whole table reconstructed at T.
- **Fields**: a typed table of live entities × tracked columns at T (deleted rows omitted).
- **Source**: `reconstruct.build_mirror_view(store, T="now"|datetime)`; `T` before history ⇒ empty (FR + edge case).

### RowLifecycleState
- **Fields**: `{ state: "active"|"deleted"|"unborn", resurrected: bool }` at T.
- **Source**: `reconstruct.row_state(store, entity_id, T)`.
- **Transitions**: `unborn → active` (birth) · `active → deleted` (`__deleted__` marker) · `deleted → active`
  (resurrection, same `entity_id`, `resurrected=true`). One continuous trail per identity (FR-004).

### ChangeCount / Heat
- `change_count(store, entity_id)` → per-row Δ (the gutter). Per-cell heat = events for `(entity_id, field)`
  bucketed into `v1..v4` (volatility tint).

### SnapPoints
- **Fields**: `sorted unique timestamp[]` across all change events — the slider's event-snap targets (FR-007).
- **Derived once** per store load.

### DensityBuckets
- **Fields**: `[{ bucket_start, bucket_end, change_count }]` — histogram of events per time-bucket (the
  scrubber track shading). Derived once.

---

## Viewer-only runtime state (not persisted)

### WindowCache (bounded LRU)
- **Fields**: `key=(rowWindow, T) → reconstructed cells`; **bounded** capacity, LRU eviction (economy-first, FR-012).
- **Rule**: no unbounded growth; evict oldest on insert past capacity.

### ScrubberState
- **Fields**: `T:datetime`, `playing:bool`, `looping:bool`, `diffOn:bool`, `snapIndex:int` (into SnapPoints).
- **Rule**: step/play move `snapIndex` over SnapPoints (event-snap), not raw days.

### FilterState
- **Fields**: `mode: "simple"|"sql"`, simple controls (`status[]`, `pcp[]`, `riskRange`, `minChanges`,
  `idContains`) OR `where:string`; both **compile to the same predicate**; `stableRowSet:bool=true` (default).
- **Derived**: `visibleIds[]` + `showingX`, `totalN`.

---

## Demo Fixture (generated, test/demo-only)

A seeded 1000×20 `<name>.flux/` store produced by `scripts/demo_fixture.py`.

| Aspect | Spec |
|---|---|
| Rows × cols | 1000 × 20 |
| Datatypes | int64, float64, utf8, bool, datetime[us,UTC], categorical (low-card utf8) |
| **Immutable** cols | e.g. `id`, `birth_date`, `cohort`, `mrn` — set at birth, never change |
| **Mutable** cols | e.g. `risk:float`, `score:int`, `last_seen:datetime` — change over time |
| **Categorical/incremental** cols | e.g. `status: A→B→C`, `tier`/`stage` |
| Value source | **Faker** (names/PCPs/categories/dates) + seeded **numpy.random** (floats/ints) |
| Time source | **flux `ChangeLogStore.capture()`** over ~30–60 evolving snapshots |
| Change density | **sparse ~1–5%** of cells per step ("not all cells") |
| Lifecycle | births, deletions (single marker), resurrections, + curated volatile hotspots |
| Reproducibility | **seeded** → identical store every run |

---

## CLI command surface (entity: the `flux` launcher)

| Command | Maps to |
|---|---|
| `flux capture <store> <input>` | `FluxState.update_mirror_table` / `ChangeLogStore.capture` |
| `flux travel <store> --as-of <T>` | `reconstruct.build_mirror_view(store, T)` |
| `flux timeline <store> <id> [--field F]` | `reconstruct.get_timeline` |
| `flux row-state <store> <id> [--as-of T]` | `reconstruct.row_state` |
| `flux view <store> [--as-of T] [--format ...]` | `build_mirror_view` + `save_mirror_table` |
| `flux gen-fixture <store> [--seed N]` | `scripts/demo_fixture.py` |
| `flux serve <store> [--port]` | launch the `viewer/` app over the store |

Full arg/output contract: `contracts/cli.md`.
