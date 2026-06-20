// File: viewer/src/lib/static-view.ts  (T029 support — dev-spec §2.5)
//
// PURE data-shaping for the static / print audit view. Kept out of the .svelte
// component so it is unit-testable in Node with no browser (T028). Everything
// here is a pure function over a pre-built `ReconIndex` (the perf path — no flat
// scans) + the manifest schema; the component just renders the shapes.
//
// What the static view needs, per row, as-of T ("now" by default):
//   • latest decoded value per cell;
//   • a GHOST subtext = the prior value + a direction arrow (↑/↓ for numeric)
//     via changeDirection — i.e. the second-most-recent value of that cell;
//   • a LIFECYCLE SPARK-PATH: a compact per-row summary of its lifecycle over
//     time (active / deleted / resurrected) plus its change density, ready to
//     render as a tiny inline SVG;
//   • CALLOUTS: the most volatile cells/rows (highest change count) and the
//     immutable-violation rows (reused from immutableViolations).
//
// VIRTUALIZATION (T029 perf pass): rendering 1000 (and eventually 100k) rows
// eagerly is a heavy DOM + a heavy shaping pass. The component now virtualizes:
// it computes only the visible window's StaticRow shapes on demand. To support
// that, the heavy per-row shaping lives in a STANDALONE pure function
// (`shapeStaticRow`) and a cheap `prepareStaticView` does just the row-order +
// total-count + callout work (callouts only need a per-row change COUNT, which
// is read straight off the index — no value decode). `buildStaticView` (the
// eager whole-view shape) is kept for the unit test + the print render-all path,
// and is now just `prepareStaticView` + a `shapeRow` over every planned row.

import {
  decodeValue,
  changeDirection,
  rowStateIndexed,
  type ReconIndex,
  type Typed,
  type ChangeDirection,
  type ImmutableViolations,
} from "./reconstruct";

const DELETED_FIELD = "__deleted__";

/** A single cell in the static render: latest value + optional prior ghost. */
export interface StaticCell {
  field: string;
  /** Latest decoded value as-of T (null ⇒ no value / deletion / genuine null). */
  value: Typed;
  /** The prior decoded value of this cell (the ghost subtext), or null if none. */
  ghost: Typed | null;
  /** Direction of prior→latest (drives the ↑/↓ arrow); null when there's no ghost. */
  dir: ChangeDirection | null;
  /** Number of recorded changes for this cell ≤ T (for the heat / volatility cue). */
  changes: number;
}

/** One spark-path segment: a lifecycle phase over a contiguous run of events. */
export type SparkKind = "active" | "deleted" | "resurrected";

export interface SparkSegment {
  kind: SparkKind;
  /** Event index where this phase starts (0-based within the row's history ≤ T). */
  from: number;
  /** Event index where this phase ends (inclusive). */
  to: number;
}

/** A row in the static render. */
export interface StaticRow {
  id: string;
  /** Decoded key value. */
  key: Typed;
  cells: StaticCell[];
  /** Lifecycle state as-of T. */
  state: "active" | "deleted";
  resurrected: boolean;
  /** True if this row has an immutable-column violation at/ before T. */
  flagged: boolean;
  /** Total change events for the row ≤ T (the change-density figure). */
  changeCount: number;
  /** Lifecycle spark-path segments for the inline SVG (chronological). */
  spark: SparkSegment[];
}

/** A surfaced callout for the audit page. */
export interface Callout {
  kind: "volatile" | "violation";
  entityId: string;
  /** Human-readable headline. */
  title: string;
  /** Supporting detail. */
  detail: string;
}

/** The fully-shaped static view ready for the component to render. */
export interface StaticView {
  columns: string[];
  rows: StaticRow[];
  callouts: Callout[];
  /** True when `rows` was capped below the true row total (for the print note). */
  capped: boolean;
  /** Total live+deleted rows born at T before capping. */
  totalRows: number;
}

/**
 * The CHEAP plan for the virtualized render. Holds everything that's cheap to
 * compute up front (column order, the capped list of entity ids to render, total
 * count, the auto-callouts) plus a `shapeRow(eid)` closure the component calls
 * lazily for only the rows in the viewport. No per-cell value decode happens
 * until `shapeRow` is invoked.
 */
export interface StaticViewPlan {
  columns: string[];
  /** Entity ids to render, in row order, already capped to `maxRows`. */
  order: string[];
  callouts: Callout[];
  /** True when `order` was capped below `totalRows`. */
  capped: boolean;
  /** Total live+deleted rows born at T (before capping). */
  totalRows: number;
  /** Lazily shape ONE row to a full StaticRow (heavy work; visible window only). */
  shapeRow: (entityId: string) => StaticRow;
}

/** Binary search: index of the last event with t <= tMs, or -1. */
function lastLEIndex(hist: { t: number }[], tMs: number): number {
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
 * Build the lifecycle spark-path for one entity from its (t,i)-sorted lifecycle
 * events up to and including index `cut`. Walks the events once, coalescing
 * contiguous runs into phases:
 *   • a `__deleted__` marker opens (or extends) a `deleted` phase;
 *   • a SET after a deletion opens a `resurrected` phase (the comeback);
 *   • any other SET is `active`.
 * Returns chronological segments keyed by event index so the SVG can lay them
 * left-to-right with the change density implied by the segment widths.
 */
function buildSpark(
  life: { t: number; field: string }[],
  cut: number,
): SparkSegment[] {
  const segs: SparkSegment[] = [];
  let deletedPending = false;
  for (let i = 0; i <= cut; i++) {
    const isDel = life[i].field === DELETED_FIELD;
    let kind: SparkKind;
    if (isDel) {
      kind = "deleted";
      deletedPending = true;
    } else if (deletedPending) {
      kind = "resurrected";
      deletedPending = false;
    } else {
      kind = "active";
    }
    const last = segs[segs.length - 1];
    if (last && last.kind === kind && last.to === i - 1) {
      last.to = i; // extend the current run
    } else {
      segs.push({ kind, from: i, to: i });
    }
  }
  return segs;
}

/**
 * Cheap per-row tracked-field change count ≤ T (the change-density figure used
 * by the callouts and the Δ gutter). Reads only the per-cell history LENGTHS via
 * a binary search per field — no value decode. `__deleted__` markers never land
 * in a tracked field's history so they're naturally excluded.
 */
function rowChangeCount(
  index: ReconIndex,
  entityId: string,
  fields: string[],
  tMs: number,
): number {
  const byField = index.cells.get(entityId);
  if (!byField) return 0;
  let total = 0;
  for (const f of fields) {
    const hist = byField.get(f);
    if (!hist || !hist.length) continue;
    const ci = lastLEIndex(hist, tMs);
    if (ci >= 0) total += ci + 1;
  }
  return total;
}

/**
 * Shape ONE entity into a full StaticRow as-of T. This is the heavy per-row work
 * (decode every cell's latest + prior value, classify the direction, build the
 * spark-path); the virtualized component calls it ONLY for rows in the viewport.
 *
 * @param index       pre-built ReconIndex
 * @param schema      manifest column → dtype-tag map
 * @param keyColumn   entity-id / key column name
 * @param fields      tracked fields (schema order, key excluded) — passed in to
 *                    avoid re-deriving per row
 * @param violations  immutableViolations result (for the `flagged` flag)
 * @param entityId    the entity to shape
 * @param tMs         as-of cutoff in epoch ms (Infinity for "now")
 * @param asOfDate    a Date at/after all events for "now", else T (for rowState)
 * @param isNow       whether T was "now"
 */
export function shapeStaticRow(
  index: ReconIndex,
  schema: Record<string, string>,
  keyColumn: string,
  fields: string[],
  violations: ImmutableViolations,
  entityId: string,
  tMs: number,
  asOfDate: Date,
  isNow: boolean,
): StaticRow {
  const life = index.lifecycle.get(entityId)!;
  const cut = lastLEIndex(life, tMs);

  // rowStateIndexed gives us state + resurrected as-of T. (For "now" we pass a
  // date at/after all events.)
  const stT = isNow ? new Date(life[cut].t) : asOfDate;
  const st = rowStateIndexed(index, entityId, stT);
  const state: "active" | "deleted" = st.state === "deleted" ? "deleted" : "active";

  const byField = index.cells.get(entityId)!;
  let changeCount = 0;
  const cells: StaticCell[] = [];
  for (const f of fields) {
    const hist = byField.get(f);
    let value: Typed = null;
    let ghost: Typed | null = null;
    let dir: ChangeDirection | null = null;
    let changes = 0;
    if (hist && hist.length) {
      const ci = lastLEIndex(hist, tMs);
      if (ci >= 0) {
        changes = ci + 1; // events for this cell ≤ T
        value = decodeValue(hist[ci].value, hist[ci].dtype);
        if (ci >= 1) {
          ghost = decodeValue(hist[ci - 1].value, hist[ci - 1].dtype);
          dir = changeDirection(ghost, value);
        }
      }
    }
    changeCount += changes;
    cells.push({ field: f, value, ghost, dir, changes });
  }

  return {
    id: entityId,
    key: decodeValue(entityId, schema[keyColumn]),
    cells,
    state,
    resurrected: st.resurrected,
    flagged: violations.has(entityId),
    changeCount,
    spark: buildSpark(life, cut),
  };
}

/**
 * CHEAP plan for the virtualized audit render (T029 perf pass).
 *
 * Walks entities once to find those born ≤ T (row order, capped to `maxRows`),
 * computes the per-row change count cheaply (history lengths, no decode) to rank
 * the volatility callouts, and returns a `shapeRow` closure for the heavy
 * per-row shaping the component does lazily for the visible window only.
 *
 * Pure over the pre-built index. Currently-deleted rows are KEPT visible (an
 * audit must show the deletion), but UNBORN rows (no events ≤ T) are omitted.
 */
export function prepareStaticView(
  index: ReconIndex,
  schema: Record<string, string>,
  keyColumn: string,
  violations: ImmutableViolations,
  T: Date | "now" = "now",
  maxRows = 1000,
): StaticViewPlan {
  const tMs = T === "now" ? Number.POSITIVE_INFINITY : T.getTime();
  const isNow = T === "now";
  const asOfDate = isNow ? new Date(tMs === Infinity ? 0 : tMs) : T;
  const fields = Object.keys(schema).filter((c) => c !== keyColumn);
  const columns = [keyColumn, ...fields];

  const order: string[] = [];
  let totalRows = 0;
  // entity_id → cheap change count, for the volatility ranking (born rows only).
  const changeCounts = new Map<string, number>();

  for (const eid of index.order) {
    const life = index.lifecycle.get(eid)!;
    const cut = lastLEIndex(life, tMs);
    if (cut < 0) continue; // unborn at T → omit

    totalRows++;
    changeCounts.set(eid, rowChangeCount(index, eid, fields, tMs));
    if (order.length < maxRows) order.push(eid);
  }

  const shapeRow = (entityId: string): StaticRow =>
    shapeStaticRow(
      index,
      schema,
      keyColumn,
      fields,
      violations,
      entityId,
      tMs,
      asOfDate,
      isNow,
    );

  return {
    columns,
    order,
    callouts: buildCalloutsFromCounts(order, changeCounts, violations),
    capped: totalRows > order.length,
    totalRows,
    shapeRow,
  };
}

/**
 * Shape the whole static / print audit view as-of T (default "now").
 *
 * Eager whole-view shape: `prepareStaticView` for the plan, then `shapeRow` over
 * EVERY planned row. Kept for the unit test (T028) and the component's print
 * render-all path (US6/T028 — print must emit ALL rows, not just the virtual
 * window). For the on-screen virtualized render the component uses
 * `prepareStaticView` directly and shapes only the visible window.
 *
 * @param index         pre-built ReconIndex over the full event set
 * @param schema        manifest column → dtype-tag map (key column included)
 * @param keyColumn     the entity-id / key column name
 * @param violations    immutableViolations(index, immutableCols) result
 * @param T             as-of instant, or "now" (default)
 * @param maxRows       cap on rendered rows (default 1000 — full demo store)
 */
export function buildStaticView(
  index: ReconIndex,
  schema: Record<string, string>,
  keyColumn: string,
  violations: ImmutableViolations,
  T: Date | "now" = "now",
  maxRows = 1000,
): StaticView {
  const plan = prepareStaticView(index, schema, keyColumn, violations, T, maxRows);
  const rows = plan.order.map((eid) => plan.shapeRow(eid));
  return {
    columns: plan.columns,
    rows,
    callouts: plan.callouts,
    capped: plan.capped,
    totalRows: plan.totalRows,
  };
}

/**
 * Auto-callouts from a pre-computed per-row change-count map (the cheap path used
 * by `prepareStaticView` — no StaticRow shaping needed):
 *   • EVERY flagged immutable-violation row;
 *   • the top-volatility non-flagged rows (highest change count) — up to `topN`.
 */
export function buildCalloutsFromCounts(
  order: string[],
  changeCounts: Map<string, number>,
  violations: ImmutableViolations,
  topN = 3,
): Callout[] {
  const out: Callout[] = [];
  const seen = new Set<string>();

  // Violation callouts first (highest audit priority).
  for (const id of order) {
    if (!violations.has(id)) continue;
    const v = violations.get(id);
    const cols = v ? v.map((x) => x.field).join(", ") : "";
    out.push({
      kind: "violation",
      entityId: id,
      title: `Immutable-column violation · ${id}`,
      detail: cols
        ? `write-once column(s) changed: ${cols}`
        : "an immutable column changed value",
    });
    seen.add(id);
  }

  // Then the most volatile non-flagged rows.
  const byVol = order
    .filter((id) => !seen.has(id) && (changeCounts.get(id) ?? 0) > 0)
    .sort((a, b) => (changeCounts.get(b) ?? 0) - (changeCounts.get(a) ?? 0))
    .slice(0, topN);
  for (const id of byVol) {
    out.push({
      kind: "volatile",
      entityId: id,
      title: `High change density · ${id}`,
      detail: `${changeCounts.get(id) ?? 0} change events recorded`,
    });
  }

  return out;
}

/**
 * Auto-callouts for the audit page (kept for the existing buildStaticView path
 * + the unit test):
 *   • the top-volatility rows (highest changeCount) — up to `topN`;
 *   • EVERY flagged immutable-violation row.
 * Volatility callouts that are also flagged are folded into the violation
 * callout (one row → one callout, violation taking priority).
 *
 * NOTE: the detail strings here append the lifecycle suffix (· resurrected / ·
 * deleted) because they have the shaped StaticRow available; the cheap
 * `buildCalloutsFromCounts` path (used by the virtualized render) omits that
 * suffix since it deliberately avoids shaping the row.
 */
export function buildCallouts(
  rows: StaticRow[],
  violations: ImmutableViolations,
  topN = 3,
): Callout[] {
  const out: Callout[] = [];
  const seen = new Set<string>();

  // Violation callouts first (highest audit priority).
  for (const r of rows) {
    if (!r.flagged) continue;
    const v = violations.get(r.id);
    const cols = v ? v.map((x) => x.field).join(", ") : "";
    out.push({
      kind: "violation",
      entityId: r.id,
      title: `Immutable-column violation · ${r.id}`,
      detail: cols
        ? `write-once column(s) changed: ${cols}`
        : "an immutable column changed value",
    });
    seen.add(r.id);
  }

  // Then the most volatile non-flagged rows.
  const byVol = [...rows]
    .filter((r) => !seen.has(r.id) && r.changeCount > 0)
    .sort((a, b) => b.changeCount - a.changeCount)
    .slice(0, topN);
  for (const r of byVol) {
    out.push({
      kind: "volatile",
      entityId: r.id,
      title: `High change density · ${r.id}`,
      detail: `${r.changeCount} change events recorded${
        r.resurrected ? " · resurrected" : r.state === "deleted" ? " · deleted" : ""
      }`,
    });
  }

  return out;
}
