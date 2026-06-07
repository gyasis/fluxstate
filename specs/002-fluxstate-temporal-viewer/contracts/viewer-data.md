# Contract: Viewer ↔ Change-Log data access + reconstruction parity

**Feature**: `002-fluxstate-temporal-viewer` · **Date**: 2026-06-06

This is the **"feed our data the correct way"** contract: how the viewer reads the real store and how its JS
reconstruction is kept identical to the Python engine.

---

## 1. Data source — DuckDB-WASM over the real store

- The viewer reads the **manifest-valid** `events/*.parquet` of a `<name>.flux/` store via **DuckDB-WASM**
  in-browser. The manifest (`manifest.json`) is authoritative — only its listed event files are read
  (orphan parquet ignored, STORE-4).
- **As-of slice**: `SELECT … WHERE timestamp <= :T`. File-skip pruning uses the manifest's per-file
  `[ts_min, ts_max]` (R5) so out-of-range files are never scanned.
- **Windowing**: keyset pagination over the visible row window + a prefetch buffer; results held in a
  **bounded LRU** (no unbounded growth). NEVER load the whole table.
- **No JSON-in-cell, no synthetic arrays** in any production path (FR-001). Values are decoded to their
  original type via the `dtype` tag (FR-002).

## 2. Primitive mapping — JS port ↔ shipped Python (1:1)

| Viewer primitive (`reconstruct.ts`) | Python ground truth (`reconstruct.py`) |
|---|---|
| `asOf(history, T)` | `_as_of(history, T)` (series-level) |
| `asOf(store, id, field, T)` | `as_of(store, id, field, T)` |
| `getTimeline(store, id, field?)` | `get_timeline(store, id, field)` |
| `changeCount(store, id)` | `change_count(store, id)` |
| `rowState(store, id, T)` | `row_state(store, id, T)` → `{state, resurrected}` |
| `buildMirrorView(store, T)` | `build_mirror_view(store, T)` |
| `snapPoints(store)` | sorted unique event `timestamp`s |
| `densityBuckets(store, n)` | per-time-bucket event histogram |

**Decode/lifecycle rules the JS port MUST honor:**
- A genuine null cell (`value=null`, real dtype) is distinct from a `__deleted__` marker (`dtype="null"`).
- A deletion is ONE marker per entity (not one null per column).
- A resurrected entity keeps the SAME `entity_id`; its timeline is continuous `active → deleted → active`.
- `asOf(T)` = latest event at or **before** T (null/None if none); `T` is UTC-normalized before compare.

## 3. Parity gate (the contract test)

- Python (`TESTS/test_parity_export.py`) builds a known store (incl. a delete + a resurrection + a genuine
  null), runs a grid of probes `(entity_id, field, T)` + whole-view at several `T`, and writes the results as
  `parity.schema.json`-shaped JSON.
- The viewer test (`viewer/tests/reconstruct.parity.test.ts`) loads that JSON and asserts `reconstruct.ts`
  returns **identical** `as_of` / `timeline` / `row_state` / `change_count` / `build_mirror_view` for every
  probe. **Any mismatch fails CI** (FR-003 / SC-003).

## 4. Invariants (testable)

| ID | Invariant |
|---|---|
| VD-1 | The viewer reconstructs only from the real change-log (no JSON-in-cell / synthetic) — verified by reading the same store the CLI/library read. |
| VD-2 | JS reconstruction == Python ground truth on 100% of parity probes, incl. deleted + resurrected + genuine-null. |
| VD-3 | Values render in original type (numeric/datetime not string-cast). |
| VD-4 | Only the visible window + prefetch is fetched; the window cache is bounded (LRU eviction observed at capacity). |
| VD-5 | `snapPoints` are exactly the unique event timestamps; the scrubber never snaps to a non-event day. |
