// File: viewer/src/lib/reconstruct.ts
//
// JS port of the shipped Python reconstruction primitives (reconstruct.py)
// and the codec in changelog.py, kept honest by a parity test against
// Python-exported ground truth.
//
// Contract: specs/002-fluxstate-temporal-viewer/contracts/viewer-data.md §2.
//
// DESIGN CONTRACT (T005): every function here is PURE over an in-memory array
// of raw change-event records — NOT over DuckDB. A separate `duckdb.ts` fetches
// events and hands an array to these resolvers. This keeps reconstruct.ts
// unit/parity-testable in Node with no browser. We do NOT import duckdb.ts; we
// only keep our record shape aligned with its `ChangeEvent` interface (the one
// difference: over the wire / in the parity fixture, `timestamp` is an ISO-8601
// UTC string rather than a JS `Date`, matching the Python export).

// Reserved field name for a deletion-marker row (one per vanished entity).
// Mirrors changelog.py `DELETED_FIELD` / `NULL_DTYPE`.
const DELETED_FIELD = "__deleted__";
const NULL_DTYPE = "null";

/**
 * One raw change event as consumed by the pure reconstructors.
 *
 * Field-for-field identical to `duckdb.ts`'s `ChangeEvent`, except `timestamp`
 * is an ISO-8601 UTC string (the Python parity export emits ISO strings, and
 * keeping it a string keeps these functions free of `Date`-construction order
 * concerns). `value === null` with a real `dtype` is a genuine null cell;
 * `dtype === "null"` (with `field === "__deleted__"`) is a deletion marker.
 */
export interface RawChangeEvent {
  entity_id: string;
  /** ISO-8601 UTC timestamp string (e.g. "2026-06-06T12:00:00+00:00"). */
  timestamp: string;
  field: string;
  value: string | null;
  dtype: string;
  snapshot_id: string;
}

/**
 * A typed cell value at a point in time (null ⇒ no value / deletion / genuine null).
 * `bigint` carries int64 values beyond float64's exact-integer range (|v| > 2^53-1),
 * which a plain `number` would silently round (parity with Python's arbitrary int).
 */
export type Typed = string | number | bigint | boolean | Date | null;

/** One point in a cell's history (ascending by date). */
export interface TimelinePoint {
  date: Date;
  value: Typed;
  /** Present only when querying all fields (field omitted in getTimeline). */
  field?: string;
}

/** The value of one cell as-of T, or null. */
export interface AsOfValue {
  date: Date;
  value: Typed;
}

/** Lifecycle state of a row at T. */
export interface RowLifecycleState {
  state: "active" | "deleted" | "unborn";
  resurrected: boolean;
}

/** A reconstructed table (whole mirror view) as-of T. */
export interface MirrorView {
  /** Column order: key column first, then tracked fields (schema order). */
  columns: string[];
  /** One object per live entity: column → typed value. */
  rows: Record<string, Typed>[];
}

/** A density-histogram bucket of change events. */
export interface DensityBucket {
  bucketStart: Date;
  bucketEnd: Date;
  changeCount: number;
}

// --------------------------------------------------------------------------- //
// UTC normalization + codec (port of changelog.py)                            //
// --------------------------------------------------------------------------- //

/** Parse an ISO-8601 timestamp string to a `Date` (always UTC instant). */
function parseTs(ts: string): Date {
  return new Date(ts);
}

function norm(dtype: string): string {
  return (dtype ?? "").trim().toLowerCase();
}

/**
 * Inverse of the Python codec (`changelog.decode_value`): decode a stored
 * string cell back to its original typed value.
 *
 * - `value === null` OR `dtype === "null"` → `null` (genuine null OR deletion).
 * - int* → number, float* → number, bool → boolean ("true"/"false"),
 *   datetime* → `Date` (UTC), anything else (utf8/str) → string.
 */
export function decodeValue(value: string | null, dtype: string): Typed {
  const d = norm(dtype);
  if (value === null || d === NULL_DTYPE) {
    return null;
  }
  if (d.startsWith("int")) {
    // Preserve full int64 precision: values beyond float64's exact-integer range
    // (|n| > 2^53-1) lose their low bits as a Number, so fall back to BigInt to
    // match Python's arbitrary-precision int decode (audit F1).
    const n = Number(value);
    return Number.isSafeInteger(n) ? n : BigInt(value);
  }
  if (d.startsWith("float")) {
    return Number(value);
  }
  if (d === "bool" || d === "boolean") {
    return value.trim().toLowerCase() === "true";
  }
  if (d.startsWith("datetime")) {
    // NOTE (audit F2): `Date` is millisecond-resolution, so a datetime value with
    // sub-millisecond microseconds is truncated here. This is an accepted viewer
    // limitation — the whole temporal engine (scrubber/density/inspector) is
    // Date/getTime()-based and parity is normalized to ms (see viewer-data.md VD-3).
    // The store itself retains full µs; only the in-browser display rounds.
    return new Date(value);
  }
  // utf8 / str / string / any other tag → string form.
  return value;
}

// --------------------------------------------------------------------------- //
// internal: time-ordered events for one entity (optionally one field)         //
// --------------------------------------------------------------------------- //

/**
 * Filtered, ascending-by-timestamp events for one entity (and optionally one
 * field), restricted to `timestamp <= T` when `T` is given. Mirrors Python
 * `_entity_series` + the as-of filter in `_scan`.
 *
 * Sort is stable on a tie-broken key (timestamp, then original index) so a
 * deterministic "latest" is selected the same way Polars' stable sort would.
 */
function entitySeries(
  events: RawChangeEvent[],
  entityId: string,
  field: string | null,
  T: Date | null,
): RawChangeEvent[] {
  const tMs = T === null ? null : T.getTime();
  const matched: { e: RawChangeEvent; t: number; i: number }[] = [];
  for (let i = 0; i < events.length; i++) {
    const e = events[i];
    if (e.entity_id !== entityId) continue;
    if (field !== null && e.field !== field) continue;
    const t = parseTs(e.timestamp).getTime();
    if (tMs !== null && t > tMs) continue;
    matched.push({ e, t, i });
  }
  matched.sort((a, b) => (a.t - b.t) || (a.i - b.i));
  return matched.map((m) => m.e);
}

// --------------------------------------------------------------------------- //
// Series-level + public resolvers (port of reconstruct.py T019)               //
// --------------------------------------------------------------------------- //

/**
 * Series-level resolver (Python `_as_of`): latest `{date, value}` at or before
 * `T` from an ascending-by-date history, or `null`.
 */
export function _asOf(history: TimelinePoint[], T: Date): AsOfValue | null {
  const tMs = T.getTime();
  let result: AsOfValue | null = null;
  for (const entry of history) {
    if (entry.date.getTime() <= tMs) {
      result = { date: entry.date, value: entry.value };
    } else {
      break;
    }
  }
  return result;
}

/**
 * Public resolver (Python `as_of`): the value of `(entityId, field)` as-of `T`,
 * decoded — the latest event at or before `T` — or `null`.
 */
export function asOf(
  events: RawChangeEvent[],
  entityId: string,
  field: string,
  T: Date,
): AsOfValue | null {
  const series = entitySeries(events, entityId, field, T);
  if (series.length === 0) return null;
  const last = series[series.length - 1];
  return { date: parseTs(last.timestamp), value: decodeValue(last.value, last.dtype) };
}

/**
 * Per-cell timeline (Python `get_timeline`): decoded `[{date, value}]` for one
 * cell when `field` is set, or every change for the entity as
 * `[{date, value, field}]` (field key added) when `field` is omitted —
 * ascending by date.
 */
export function getTimeline(
  events: RawChangeEvent[],
  entityId: string,
  field?: string,
): TimelinePoint[] {
  const series = entitySeries(events, entityId, field ?? null, null);
  return series.map((e) => {
    const point: TimelinePoint = {
      date: parseTs(e.timestamp),
      value: decodeValue(e.value, e.dtype),
    };
    if (field === undefined) {
      point.field = e.field;
    }
    return point;
  });
}

/** Total number of change events recorded for an entity (Python `change_count`). */
export function changeCount(events: RawChangeEvent[], entityId: string): number {
  return entitySeries(events, entityId, null, null).length;
}

// --------------------------------------------------------------------------- //
// Lifecycle state (port of reconstruct.py row_state, T024)                     //
// --------------------------------------------------------------------------- //

/**
 * Lifecycle state of an entity at `T` (Python `row_state`):
 * `{state: active|deleted|unborn, resurrected}`.
 *
 * Walks the entity's events ≤ T: a `__deleted__` marker sets deleted-pending;
 * a later SET event resurrects (resurrected=true) and clears pending. The
 * state is `deleted` iff the most-recent event is a `__deleted__` marker,
 * `unborn` if there are no events, else `active`.
 */
export function rowState(
  events: RawChangeEvent[],
  entityId: string,
  T: Date,
): RowLifecycleState {
  const series = entitySeries(events, entityId, null, T);
  if (series.length === 0) {
    return { state: "unborn", resurrected: false };
  }

  let resurrected = false;
  let deletedPending = false;
  let lastField: string | null = null;
  for (const e of series) {
    lastField = e.field;
    if (e.field === DELETED_FIELD) {
      deletedPending = true;
    } else {
      if (deletedPending) {
        resurrected = true;
      }
      deletedPending = false;
    }
  }

  const state = lastField === DELETED_FIELD ? "deleted" : "active";
  return { state, resurrected };
}

// --------------------------------------------------------------------------- //
// Materialized mirror view (port of _materialize_current / build_mirror_view) //
// --------------------------------------------------------------------------- //

/**
 * Reconstruct the full table state as-of `T` (Python `build_mirror_view` /
 * `_materialize_current`): the latest value per `(entity, field)` at or before
 * `T`, decoded to its typed value.
 *
 * Entities whose most-recent event (across all fields) is a `__deleted__`
 * marker are EXCLUDED. `schema` is the manifest `{column → dtype-tag}` map;
 * `keyColumn` is decoded from `entity_id`. `T` omitted / `"now"` ⇒ current
 * state (no upper bound). Columns are `[keyColumn, ...trackedFields]` in
 * schema order; a field never set for an entity decodes to `null`.
 */
export function buildMirrorView(
  events: RawChangeEvent[],
  schema: Record<string, string>,
  keyColumn: string,
  T?: Date | "now",
): MirrorView {
  const tMs = T === undefined || T === "now" ? null : (T as Date).getTime();
  const fields = Object.keys(schema).filter((c) => c !== keyColumn);
  const columns = [keyColumn, ...fields];

  // Scoped, ascending-by-(timestamp, index) events ≤ T.
  const scoped: { e: RawChangeEvent; t: number; i: number }[] = [];
  for (let i = 0; i < events.length; i++) {
    const e = events[i];
    const t = parseTs(e.timestamp).getTime();
    if (tMs !== null && t > tMs) continue;
    scoped.push({ e, t, i });
  }
  scoped.sort((a, b) => (a.t - b.t) || (a.i - b.i));

  // Latest event per (entity, field) and latest event per entity.
  const latestCell = new Map<string, RawChangeEvent>(); // key: entity field
  const latestEntity = new Map<string, RawChangeEvent>(); // key: entity
  const order: string[] = []; // first-seen order of entity ids
  for (const { e } of scoped) {
    if (!latestEntity.has(e.entity_id)) order.push(e.entity_id);
    latestCell.set(`${e.entity_id} ${e.field}`, e);
    latestEntity.set(e.entity_id, e);
  }

  const rows: Record<string, Typed>[] = [];
  for (const entityId of order) {
    const last = latestEntity.get(entityId)!;
    if (last.field === DELETED_FIELD) continue; // currently deleted → omit

    const row: Record<string, Typed> = {};
    // Decode the key from entity_id via the key column's dtype tag.
    row[keyColumn] = decodeValue(entityId, schema[keyColumn]);
    for (const f of fields) {
      const cell = latestCell.get(`${entityId} ${f}`);
      row[f] = cell ? decodeValue(cell.value, cell.dtype) : null;
    }
    rows.push(row);
  }

  return { columns, rows };
}

// --------------------------------------------------------------------------- //
// Snap points + density buckets (viewer-derived)                              //
// --------------------------------------------------------------------------- //

/** Sorted unique event timestamps — the slider's event-snap targets (VD-5). */
export function snapPoints(events: RawChangeEvent[]): Date[] {
  const seen = new Set<number>();
  const out: Date[] = [];
  for (const e of events) {
    const d = parseTs(e.timestamp);
    const ms = d.getTime();
    if (!seen.has(ms)) {
      seen.add(ms);
      out.push(d);
    }
  }
  out.sort((a, b) => a.getTime() - b.getTime());
  return out;
}

/**
 * Per-time-bucket event histogram (the scrubber track shading): `n` equal-width
 * buckets spanning [min event ts, max event ts], each `[bucketStart, bucketEnd)`
 * except the last which is inclusive of the max. Empty input ⇒ `[]`.
 */
export function densityBuckets(events: RawChangeEvent[], n: number): DensityBucket[] {
  if (events.length === 0 || n <= 0) return [];

  let min = Infinity;
  let max = -Infinity;
  const times: number[] = [];
  for (const e of events) {
    const ms = parseTs(e.timestamp).getTime();
    times.push(ms);
    if (ms < min) min = ms;
    if (ms > max) max = ms;
  }

  const span = max - min;
  const width = span / n;
  const buckets: DensityBucket[] = [];
  for (let b = 0; b < n; b++) {
    const start = min + width * b;
    const end = b === n - 1 ? max : min + width * (b + 1);
    buckets.push({
      bucketStart: new Date(start),
      bucketEnd: new Date(end),
      changeCount: 0,
    });
  }

  // Degenerate span (all events same ts) → everything in bucket 0.
  for (const ms of times) {
    let idx = width === 0 ? 0 : Math.floor((ms - min) / width);
    if (idx >= n) idx = n - 1; // the max lands in the last bucket
    if (idx < 0) idx = 0;
    buckets[idx].changeCount++;
  }

  return buckets;
}

// --------------------------------------------------------------------------- //
// INDEXED reconstruction (the performance path — same semantics, O(log k))    //
//                                                                             //
// The flat-array functions above linear-scan all events on every call: an     //
// O(E) sweep per cell. With E≈37k and 1000×19 cells that is billions of ops   //
// per scrub step. `buildIndex` does ONE O(E log E) pass to group events into  //
// per-cell, already-sorted timelines; thereafter as-of is a binary search     //
// over the tiny per-cell history (O(log k), k = changes for that one cell).   //
//                                                                             //
// Semantics are IDENTICAL to the flat functions (same decode, stable          //
// tie-break by original event index, deletion-exclusion, resurrection,        //
// genuine-null). The parity gate keeps using the flat functions, so this      //
// path is verified equivalent by construction + the table's diff machinery.   //
// --------------------------------------------------------------------------- //

/** One stored event reduced to what reconstruction needs, with its tie-break key. */
interface IndexedEvent {
  /** Timestamp in epoch ms (parsed once). */
  t: number;
  /** Original position in the source `events` array — the stable-sort tie break. */
  i: number;
  value: string | null;
  dtype: string;
  field: string;
}

/**
 * Pre-built reconstruction index over a raw event array.
 *
 * - `cells`: entity_id → field → ascending-sorted IndexedEvent[] (per-cell history).
 * - `lifecycle`: entity_id → ascending-sorted IndexedEvent[] (ALL fields, for row_state /
 *   currently-deleted exclusion). Sorted by (t, i) — the same stable order Polars uses.
 * - `order`: entity_ids in first-seen order over the (t, i)-sorted stream — the row order
 *   buildMirrorView would produce.
 */
export interface ReconIndex {
  cells: Map<string, Map<string, IndexedEvent[]>>;
  lifecycle: Map<string, IndexedEvent[]>;
  order: string[];
}

/**
 * Build the reconstruction index over `events` in one O(E log E) pass.
 * Call ONCE per event set (memoize on the events reference), then use the
 * `*Indexed` resolvers below for O(log k) as-of queries.
 */
export function buildIndex(events: RawChangeEvent[]): ReconIndex {
  // 1. Decorate with (t, i) and stable-sort once by (t, i).
  const sorted: { ev: IndexedEvent; entity_id: string }[] = new Array(events.length);
  for (let i = 0; i < events.length; i++) {
    const e = events[i];
    sorted[i] = {
      ev: {
        t: parseTs(e.timestamp).getTime(),
        i,
        value: e.value,
        dtype: e.dtype,
        field: e.field,
      },
      entity_id: e.entity_id,
    };
  }
  sorted.sort((a, b) => (a.ev.t - b.ev.t) || (a.ev.i - b.ev.i));

  const cells = new Map<string, Map<string, IndexedEvent[]>>();
  const lifecycle = new Map<string, IndexedEvent[]>();
  const order: string[] = [];

  for (const { ev, entity_id } of sorted) {
    let byField = cells.get(entity_id);
    if (byField === undefined) {
      byField = new Map();
      cells.set(entity_id, byField);
      lifecycle.set(entity_id, []);
      order.push(entity_id); // first-seen in (t, i) order
    }
    let cellHist = byField.get(ev.field);
    if (cellHist === undefined) {
      cellHist = [];
      byField.set(ev.field, cellHist);
    }
    // Both lists are populated in (t, i) order → already ascending-sorted.
    cellHist.push(ev);
    lifecycle.get(entity_id)!.push(ev);
  }

  return { cells, lifecycle, order };
}

/**
 * Binary search: index of the LAST event in an ascending-by-t history with
 * `t <= tMs`, or -1 if none. (Ties on `t` are already ordered by `i`, so the
 * highest matching index is the stable "latest" — identical to the flat scan.)
 */
function lastLEIndex(hist: IndexedEvent[], tMs: number): number {
  let lo = 0;
  let hi = hist.length - 1;
  let ans = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (hist[mid].t <= tMs) {
      ans = mid;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  return ans;
}

/**
 * Indexed equivalent of `asOf`: value of `(entityId, field)` as-of `T`, decoded,
 * or `null`. O(log k) binary search over the per-cell timeline.
 */
export function asOfIndexed(
  index: ReconIndex,
  entityId: string,
  field: string,
  T: Date,
): AsOfValue | null {
  const hist = index.cells.get(entityId)?.get(field);
  if (!hist || hist.length === 0) return null;
  const idx = lastLEIndex(hist, T.getTime());
  if (idx < 0) return null;
  const e = hist[idx];
  return { date: new Date(e.t), value: decodeValue(e.value, e.dtype) };
}

/**
 * Indexed equivalent of `rowState`: lifecycle state of an entity at `T`.
 * Walks only the entity's own (small) event list up to the as-of cutoff.
 */
export function rowStateIndexed(
  index: ReconIndex,
  entityId: string,
  T: Date,
): RowLifecycleState {
  const hist = index.lifecycle.get(entityId);
  if (!hist || hist.length === 0) {
    return { state: "unborn", resurrected: false };
  }
  const tMs = T.getTime();
  const cut = lastLEIndex(hist, tMs);
  if (cut < 0) {
    return { state: "unborn", resurrected: false };
  }

  let resurrected = false;
  let deletedPending = false;
  let lastField: string | null = null;
  for (let i = 0; i <= cut; i++) {
    const e = hist[i];
    lastField = e.field;
    if (e.field === DELETED_FIELD) {
      deletedPending = true;
    } else {
      if (deletedPending) resurrected = true;
      deletedPending = false;
    }
  }
  const state = lastField === DELETED_FIELD ? "deleted" : "active";
  return { state, resurrected };
}

/**
 * Indexed equivalent of `buildMirrorView`: reconstruct the whole table as-of `T`.
 * Iterates entities in first-seen order, binary-searching each cell — O(N · F · log k)
 * total instead of the flat O(E) re-sweep. Currently-deleted entities (most-recent
 * event across all fields is a `__deleted__` marker) are excluded, exactly as the
 * flat version does.
 */
export function buildMirrorViewIndexed(
  index: ReconIndex,
  schema: Record<string, string>,
  keyColumn: string,
  T?: Date | "now",
): MirrorView {
  const tMs =
    T === undefined || T === "now" ? Number.POSITIVE_INFINITY : (T as Date).getTime();
  const fields = Object.keys(schema).filter((c) => c !== keyColumn);
  const columns = [keyColumn, ...fields];

  const rows: Record<string, Typed>[] = [];
  for (const entityId of index.order) {
    const life = index.lifecycle.get(entityId)!;
    const lastIdx = lastLEIndex(life, tMs);
    if (lastIdx < 0) continue; // unborn at T → not present
    if (life[lastIdx].field === DELETED_FIELD) continue; // currently deleted → omit

    const byField = index.cells.get(entityId)!;
    const row: Record<string, Typed> = {};
    row[keyColumn] = decodeValue(entityId, schema[keyColumn]);
    for (const f of fields) {
      const hist = byField.get(f);
      if (!hist) {
        row[f] = null;
        continue;
      }
      const ci = lastLEIndex(hist, tMs);
      row[f] = ci < 0 ? null : decodeValue(hist[ci].value, hist[ci].dtype);
    }
    rows.push(row);
  }

  return { columns, rows };
}

/** Indexed `snapPoints`: sorted unique event timestamps, read from the index. */
export function snapPointsIndexed(index: ReconIndex): Date[] {
  const seen = new Set<number>();
  for (const hist of index.lifecycle.values()) {
    for (const e of hist) seen.add(e.t);
  }
  return [...seen].sort((a, b) => a - b).map((ms) => new Date(ms));
}

/** Indexed `densityBuckets`: same histogram, computed from the index's timestamps. */
export function densityBucketsIndexed(index: ReconIndex, n: number): DensityBucket[] {
  const times: number[] = [];
  let min = Infinity;
  let max = -Infinity;
  for (const hist of index.lifecycle.values()) {
    for (const e of hist) {
      times.push(e.t);
      if (e.t < min) min = e.t;
      if (e.t > max) max = e.t;
    }
  }
  if (times.length === 0 || n <= 0) return [];

  const span = max - min;
  const width = span / n;
  const buckets: DensityBucket[] = [];
  for (let b = 0; b < n; b++) {
    const start = min + width * b;
    const end = b === n - 1 ? max : min + width * (b + 1);
    buckets.push({
      bucketStart: new Date(start),
      bucketEnd: new Date(end),
      changeCount: 0,
    });
  }
  for (const ms of times) {
    let idx = width === 0 ? 0 : Math.floor((ms - min) / width);
    if (idx >= n) idx = n - 1;
    if (idx < 0) idx = 0;
    buckets[idx].changeCount++;
  }
  return buckets;
}

// --------------------------------------------------------------------------- //
// ANOMALY-WATCHER additions (T036) — PURELY ADDITIVE.                          //
//                                                                             //
// Two read-only helpers over the existing ReconIndex / Typed machinery for    //
// the table's anomaly cues. Neither touches the existing functions or their   //
// semantics; the parity gate is unaffected.                                   //
// --------------------------------------------------------------------------- //

/** One immutable-column violation: the first genuine value change of an immutable cell. */
export interface ImmutableViolation {
  /** The offending immutable column. */
  field: string;
  /** Timestamp of the FIRST change (the moment the immutable invariant broke). */
  t: Date;
  /** Prior decoded value (the value before the change). */
  from: Typed;
  /** New decoded value (the value the immutable cell changed to). */
  to: Typed;
}

/** entity_id → its immutable-column violations (only entities with ≥1 violation appear). */
export type ImmutableViolations = Map<string, ImmutableViolation[]>;

/**
 * Compare two `Typed` values for "same decoded value" — the equality used to
 * decide whether an immutable cell actually changed. `Date`s compare by instant;
 * everything else by `===`. (Two `null`s are equal.)
 */
function typedEqual(a: Typed, b: Typed): boolean {
  if (a instanceof Date && b instanceof Date) return a.getTime() === b.getTime();
  // A Date vs non-Date is never equal; primitives + null via ===.
  if (a instanceof Date || b instanceof Date) return false;
  return a === b;
}

/**
 * Scan each entity's per-cell timeline (via the index) for immutable columns that
 * changed value over time. A column listed in `immutableCols` is supposed to be
 * write-once; if its cell takes MORE THAN ONE distinct decoded value across its
 * history, that is a violation.
 *
 * A genuine value change of an immutable field is the violation. Transitions that
 * are merely null-vs-value are NOT violations:
 *  - a deletion marker (`__deleted__`) never lands in a real column's cell history,
 *    so it cannot register; deletions are ignored by construction.
 *  - the leading transition from "no value yet" to the first real value is the
 *    initial write, not a change — only changes BETWEEN two non-null distinct
 *    values (or a real value being replaced by another real value) count.
 *  - a genuine-null cell (value === null with a real dtype) participating in a
 *    null→value or value→null transition is treated as a non-violating
 *    appearance/clearing, not an immutable change.
 *
 * Returns, per offending entity, the list of violations recorded at the FIRST
 * offending change for each column (`{field, t, from, to}`). O(timeline) per cell
 * — walks the already-sorted per-cell history; no flat scans.
 */
export function immutableViolations(
  index: ReconIndex,
  immutableCols: string[],
): ImmutableViolations {
  const out: ImmutableViolations = new Map();
  if (immutableCols.length === 0) return out;

  for (const [entityId, byField] of index.cells) {
    const found: ImmutableViolation[] = [];
    for (const field of immutableCols) {
      const hist = byField.get(field);
      if (!hist || hist.length < 2) continue;

      // Track the last NON-NULL decoded value seen; the first time a different
      // non-null value appears after one was already established, that's the
      // immutable-invariant break. null↔value transitions don't count.
      let prevNonNull: Typed = null;
      let havePrev = false;
      for (const e of hist) {
        const v = decodeValue(e.value, e.dtype);
        if (v === null) continue; // null (deletion/genuine null/clear) → not a change
        if (havePrev && !typedEqual(prevNonNull, v)) {
          found.push({ field, t: new Date(e.t), from: prevNonNull, to: v });
          break; // record only the FIRST change for this column
        }
        prevNonNull = v;
        havePrev = true;
      }
    }
    if (found.length > 0) out.set(entityId, found);
  }

  return out;
}

/**
 * Convenience predicate for the table: is `entityId` flagged as immutable-violating
 * at-or-after time `T`? A violation flags the row from its earliest `t` onward, so
 * this is true iff the entity has any violation whose `t <= T`.
 */
export function isViolatingAt(
  violations: ImmutableViolations,
  entityId: string,
  T: Date,
): boolean {
  const list = violations.get(entityId);
  if (!list || list.length === 0) return false;
  const tMs = T.getTime();
  for (const v of list) {
    if (v.t.getTime() <= tMs) return true;
  }
  return false;
}

/** Direction + signed delta of a value transition (drives ↑/↓ + delta indicators). */
export interface ChangeDirection {
  dir: "up" | "down" | "same" | "na";
  /** `next - prev` for numerics; `null` for non-numeric kinds. */
  delta: number | null;
}

/**
 * Classify a transition `prev → next` for the diff indicators.
 *
 * - NUMBERS: `up` if `next > prev`, `down` if `next < prev`, `same` if equal,
 *   with `delta = next - prev`.
 * - DATES: chronological `up`/`down`/`same` (no numeric delta → `delta = null`).
 * - everything else (string / bool / null / mismatched kinds): `{dir:'na', delta:null}`.
 */
export function changeDirection(prev: Typed, next: Typed): ChangeDirection {
  if (typeof prev === "number" && typeof next === "number") {
    const delta = next - prev;
    const dir = next > prev ? "up" : next < prev ? "down" : "same";
    return { dir, delta };
  }
  if (prev instanceof Date && next instanceof Date) {
    const a = prev.getTime();
    const b = next.getTime();
    const dir = b > a ? "up" : b < a ? "down" : "same";
    return { dir, delta: null };
  }
  return { dir: "na", delta: null };
}
