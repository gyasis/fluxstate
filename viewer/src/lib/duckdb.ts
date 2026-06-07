// File: viewer/src/lib/duckdb.ts
//
// DuckDB-WASM data access over the real <name>.flux/ store: load the
// manifest, register the manifest-valid events/*.parquet ONLY (orphan parquet
// ignored, STORE-4), fetch RAW ChangeEvent rows with manifest-driven file-skip
// pruning (R5 / VD-1) + keyset pagination, and a bounded LRU window cache
// (VD-4: no unbounded growth).
//
// DESIGN CONTRACT (composes with reconstruct.ts): this module ONLY
// fetches/returns raw ChangeEvent arrays + manifest-derived metadata. It does
// NOT do as-of / lifecycle / decode logic — reconstruct.ts (pure over these
// arrays) owns that. We do NOT import reconstruct.ts here.
//
// Contract: specs/002-fluxstate-temporal-viewer/contracts/viewer-data.md §1 + §4
//           (VD-1 / VD-4 / VD-5) and the changelog-store / manifest contracts
//           of feature 001.

import * as duckdb from "@duckdb/duckdb-wasm";

// ───────────────────────────────────────────────────────────────────────────
// Types (kept in sync with the stubs that reconstruct.ts imports).
// ───────────────────────────────────────────────────────────────────────────

/** One change event row as read from events/*.parquet (RAW — undecoded). */
export interface ChangeEvent {
  entity_id: string;
  /** UTC. Emitted from DuckDB as an ISO-8601 string (e.g. "2026-06-05T14:30:00Z"). */
  timestamp: string;
  field: string;
  /** Raw string payload; `null` for a `__deleted__` marker or a genuine SQL null. */
  value: string | null;
  /** Dtype tag — `"null"` on a deletion marker row. */
  dtype: string;
  snapshot_id: string;
}

/** Manifest entry describing one immutable events file (ts range for pruning). */
export interface ManifestEvent {
  file: string;
  snapshot_id: string;
  ts_min: Date;
  ts_max: Date;
  row_count: number;
}

/** The parsed manifest.json — the authoritative file list + schema. */
export interface Manifest {
  schema_version: number;
  store_name: string;
  key_column: string;
  schema: Record<string, string>;
  events: ManifestEvent[];
  checkpoints: unknown[];
}

/** Selector for a raw-event fetch. All clauses are AND-ed. */
export interface FetchSpec {
  /** Restrict to these entity_ids (omitted ⇒ all). */
  entityIds?: string[];
  /** Restrict to these fields (omitted ⇒ all, incl. `__deleted__`). */
  fields?: string[];
  /** As-of cutoff: adds `WHERE timestamp <= asOf` + manifest file-skip pruning. */
  asOf?: Date;
  /** Keyset window over the ordered event stream (visible window + prefetch). */
  rowWindow?: RowWindow;
}

/**
 * A keyset page request over the totally-ordered event stream. Ordering key is
 * `(timestamp, entity_id, field, snapshot_id)` ascending — deterministic and
 * index-friendly. `after` is the cursor of the last row already seen (exclusive).
 */
export interface RowWindow {
  /** Page size: visible rows + prefetch buffer. NEVER the whole table. */
  limit: number;
  /** Exclusive keyset cursor; null/undefined ⇒ start from the first row. */
  after?: RowCursor | null;
}

/** Opaque-ish keyset cursor — the ordering tuple of the last emitted row. */
export interface RowCursor {
  timestamp: string;
  entity_id: string;
  field: string;
  snapshot_id: string;
}

/** A page of raw events + the cursor to fetch the next page (null at end). */
export interface EventPage {
  events: ChangeEvent[];
  nextCursor: RowCursor | null;
}

/**
 * Bounded LRU cache for fetched event windows (economy-first, VD-4).
 * No unbounded growth — evict the least-recently-used entry on insert past
 * capacity. Keyed by a stringified `(rowWindow, T)`.
 */
export interface WindowCache {
  readonly capacity: number;
  get(key: string): ChangeEvent[] | undefined;
  set(key: string, value: ChangeEvent[]): void;
  has(key: string): boolean;
  readonly size: number;
}

/**
 * An opened store handle: the live DuckDB-WASM connection + the manifest + a
 * bounded window cache + manifest-derived metadata (snapPoints, densityBuckets).
 */
export interface Store {
  manifest: Manifest;
  /** Bounded LRU window cache: key=(rowWindow, T) → raw event rows. */
  cache: WindowCache;
  /**
   * Sorted unique event timestamps (ISO UTC) across the whole store — the
   * scrubber's event-snap targets (VD-5 / FR-007). Derived once on open.
   */
  snapPoints: string[];
  /**
   * Histogram of events per equal-width time bucket between the store's first
   * and last event (the scrubber track shading). Derived from the manifest
   * `events[]` ts ranges + row_counts — no table scan.
   */
  densityBuckets(n: number): DensityBucket[];
  /** Tear down the DuckDB-WASM connection + worker. */
  close(): Promise<void>;
}

/** A density-histogram bucket of change events (manifest-derived). */
export interface DensityBucket {
  bucketStart: string;
  bucketEnd: string;
  changeCount: number;
}

// ───────────────────────────────────────────────────────────────────────────
// US4 SCALE — large-store detection + windowed (per-entity) event fetch.
//
// At ~100k entities the event set is millions of rows → loading it ALL into JS
// OOMs/freezes (the 100k stress fixture is ~2.77M events). The fix (dev-spec
// §4.3 / VD-1 / VD-4): keep events in DuckDB; for the VISIBLE row window
// (~40 entities + overscan) fetch ONLY those entities' full history via
// DuckDB, build a small index over just them, reconstruct as-of T. The full
// ORDERED entity-id list (cheap — no event payload) is one aggregate query;
// snapPoints + density are already aggregate-derived (no table scan).
// ───────────────────────────────────────────────────────────────────────────

/**
 * Heuristics for switching to the windowed path. Below these the viewer keeps
 * the load-ALL path so the 1000×20 demo is byte-for-byte unchanged.
 *  - > ~50k entities  (manifest first-snapshot row_count is a tight proxy), OR
 *  - > ~200k total events.
 */
export const LARGE_ENTITY_THRESHOLD = 50_000;
export const LARGE_EVENT_THRESHOLD = 200_000;

/** Total committed events across all manifest files (no scan — manifest sum). */
export function totalEvents(store: Store): number {
  return store.manifest.events.reduce((s, e) => s + e.row_count, 0);
}

/**
 * Is this a "large" store that should use the streamed windowed path? Decided
 * purely from the manifest (no scan): the first snapshot's row_count is the
 * birth-event count ≈ the initial entity population, and the summed event count
 * is the JS-memory risk. Either crossing its threshold ⇒ windowed.
 */
export function isLargeStore(store: Store): boolean {
  const firstSnap = store.manifest.events[0]?.row_count ?? 0;
  return (
    firstSnap > LARGE_ENTITY_THRESHOLD || totalEvents(store) > LARGE_EVENT_THRESHOLD
  );
}

/**
 * The FULL ordered entity-id list — the row order the table virtualizes over in
 * the windowed path. Ordered by `(MIN(timestamp), entity_id)` so it matches the
 * "first-seen" order `buildMirrorView` / the index produce (births appear when
 * they're born; the tie-break by id is deterministic). This is ONE aggregate
 * query returning only ids (≈ a few MB at 100k) — NEVER the event payload.
 */
export async function fetchEntityOrder(store: Store): Promise<string[]> {
  const internals = STORE_INTERNALS.get(store);
  if (!internals) {
    throw new Error("fetchEntityOrder: store has no live DuckDB connection (closed?)");
  }
  const { conn, files } = internals;
  if (files.length === 0) return [];
  const fileList = files.map((f) => sqlStr(f)).join(", ");
  const res = await conn.query(
    `SELECT entity_id
       FROM read_parquet([${fileList}])
      GROUP BY entity_id
      ORDER BY MIN(timestamp), entity_id`,
  );
  return res
    .toArray()
    .map((r: { entity_id: unknown }) => String((r as { entity_id: unknown }).entity_id));
}

/**
 * Fetch the FULL event history for a window of entities (the visible rows +
 * overscan), memoized in the store's bounded LRU (VD-4). No as-of cutoff and no
 * row LIMIT here: the window is a SMALL, bounded set of entities (~40–60), so
 * pulling their whole histories is cheap, and reconstruct.ts needs the full
 * per-cell timelines to answer as-of T for ANY T without re-querying on scrub.
 * On scroll the entity set changes → a different cache key → a re-query that the
 * LRU keeps bounded.
 */
export async function fetchWindowEvents(
  store: Store,
  entityIds: string[],
): Promise<ChangeEvent[]> {
  if (entityIds.length === 0) return [];
  const page = await fetchEvents(store, { entityIds });
  return page.events;
}

// ───────────────────────────────────────────────────────────────────────────
// Bounded LRU window cache (VD-4).
// ───────────────────────────────────────────────────────────────────────────

/**
 * Construct a bounded LRU window cache. Backed by a Map (insertion-ordered);
 * on `get` we re-insert to mark recency, and on `set` past capacity we delete
 * the oldest key. Capacity is clamped to ≥1.
 */
export function createWindowCache(capacity: number): WindowCache {
  const cap = Math.max(1, Math.floor(capacity));
  const map = new Map<string, ChangeEvent[]>();

  return {
    get capacity() {
      return cap;
    },
    get size() {
      return map.size;
    },
    has(key: string): boolean {
      return map.has(key);
    },
    get(key: string): ChangeEvent[] | undefined {
      const v = map.get(key);
      if (v === undefined) return undefined;
      // Touch: move to most-recently-used position.
      map.delete(key);
      map.set(key, v);
      return v;
    },
    set(key: string, value: ChangeEvent[]): void {
      if (map.has(key)) map.delete(key);
      map.set(key, value);
      // Evict least-recently-used while over capacity.
      while (map.size > cap) {
        const oldest = map.keys().next().value;
        if (oldest === undefined) break;
        map.delete(oldest);
      }
    },
  };
}

/** Stable cache key for a fetch over a (rowWindow, asOf) pair. */
export function windowCacheKey(spec: FetchSpec): string {
  const w = spec.rowWindow;
  const after = w?.after
    ? `${w.after.timestamp}|${w.after.entity_id}|${w.after.field}|${w.after.snapshot_id}`
    : "";
  return JSON.stringify({
    asOf: spec.asOf ? spec.asOf.toISOString() : null,
    limit: w?.limit ?? null,
    after,
    entityIds: spec.entityIds ? [...spec.entityIds].sort() : null,
    fields: spec.fields ? [...spec.fields].sort() : null,
  });
}

// ───────────────────────────────────────────────────────────────────────────
// DuckDB-WASM lifecycle.
// ───────────────────────────────────────────────────────────────────────────

/** Lazily-created singleton DuckDB-WASM db (one wasm worker per page). */
let dbPromise: Promise<duckdb.AsyncDuckDB> | null = null;

/**
 * Instantiate DuckDB-WASM with the jsDelivr bundle set, picking the best bundle
 * for the host (eh/mvp/coi) via `selectBundle`. The worker script is loaded as
 * a blob URL so it works regardless of how Vite serves the package asset.
 */
async function getDuckDB(): Promise<duckdb.AsyncDuckDB> {
  if (dbPromise) return dbPromise;
  dbPromise = (async () => {
    const bundles = duckdb.getJsDelivrBundles();
    const bundle = await duckdb.selectBundle(bundles);
    const workerUrl = URL.createObjectURL(
      new Blob([`importScripts("${bundle.mainWorker}");`], {
        type: "text/javascript",
      }),
    );
    const worker = new Worker(workerUrl);
    const logger = new duckdb.ConsoleLogger(duckdb.LogLevel.WARNING);
    const db = new duckdb.AsyncDuckDB(logger, worker);
    await db.instantiate(bundle.mainModule, bundle.pthreadWorker);
    URL.revokeObjectURL(workerUrl);
    return db;
  })();
  return dbPromise;
}

// ───────────────────────────────────────────────────────────────────────────
// Manifest parsing + helpers.
// ───────────────────────────────────────────────────────────────────────────

/**
 * Join a store base URL with a store-relative path and resolve to an ABSOLUTE
 * URL. DuckDB-WASM opens registered files from inside its web-worker via
 * `XMLHttpRequest.open()`, which has NO document base — a root-relative URL like
 * `/demo.flux/events/x.parquet` throws "Invalid URL" there. Resolving against
 * `location.href` (when available) yields an absolute `http(s)://host/…` URL the
 * worker can open. In a non-DOM context (Node tests) we fall back to the plain
 * string join.
 */
function joinUrl(base: string, rel: string): string {
  const b = base.endsWith("/") ? base.slice(0, -1) : base;
  const r = rel.startsWith("/") ? rel.slice(1) : rel;
  const joined = `${b}/${r}`;
  if (typeof location !== "undefined" && location.href) {
    try {
      return new URL(joined, location.href).href;
    } catch {
      return joined;
    }
  }
  return joined;
}

function parseManifest(raw: unknown): Manifest {
  const m = raw as Record<string, unknown>;
  const eventsRaw = Array.isArray(m.events) ? m.events : [];
  const events: ManifestEvent[] = eventsRaw.map((e) => {
    const ev = e as Record<string, unknown>;
    return {
      file: String(ev.file),
      snapshot_id: String(ev.snapshot_id),
      ts_min: new Date(String(ev.ts_min)),
      ts_max: new Date(String(ev.ts_max)),
      row_count: Number(ev.row_count),
    };
  });
  return {
    schema_version: Number(m.schema_version ?? 1),
    store_name: String(m.store_name ?? ""),
    key_column: String(m.key_column ?? ""),
    schema: (m.schema as Record<string, string>) ?? {},
    events,
    checkpoints: Array.isArray(m.checkpoints) ? m.checkpoints : [],
  };
}

/**
 * Pick the manifest event files whose `[ts_min, ts_max]` range can contribute
 * rows under the as-of cutoff (R5 file-skip): a file is skipped iff its entire
 * range lies strictly after `asOf`. With no cutoff, all manifest files survive.
 */
function pruneFiles(events: ManifestEvent[], asOf?: Date): ManifestEvent[] {
  if (!asOf) return events;
  const cutoff = asOf.getTime();
  return events.filter((e) => e.ts_min.getTime() <= cutoff);
}

/** SQL string literal escaping (single quotes doubled). */
function sqlStr(s: string): string {
  return `'${s.replace(/'/g, "''")}'`;
}

// ───────────────────────────────────────────────────────────────────────────
// openStore — load manifest, register manifest-valid parquet ONLY (STORE-4),
// derive snapPoints + densityBuckets.
// ───────────────────────────────────────────────────────────────────────────

/**
 * Open a `.flux` store at `storeUrl` (the URL of the `<name>.flux/` folder).
 * Loads `manifest.json`, registers ONLY the manifest-listed `events/*.parquet`
 * with DuckDB-WASM via HTTP (orphan parquet on disk is never registered →
 * STORE-4), and derives `snapPoints` + a `densityBuckets(n)` helper.
 */
export async function openStore(storeUrl: string): Promise<Store> {
  const manifestResp = await fetch(joinUrl(storeUrl, "manifest.json"));
  if (!manifestResp.ok) {
    throw new Error(
      `openStore: failed to load manifest.json (${manifestResp.status}) at ${storeUrl}`,
    );
  }
  const manifest = parseManifest(await manifestResp.json());

  const db = await getDuckDB();
  const conn = await db.connect();
  // Pin the session timezone to UTC so strftime() over the TIMESTAMPTZ event
  // column renders absolute UTC instants (not the host's local wall-clock).
  // NOTE: do NOT use `AT TIME ZONE 'UTC'` to do this — that operator converts a
  // TIMESTAMPTZ to a plain TIMESTAMP, and strftime(TIMESTAMP, …) has no matching
  // overload in the duckdb-wasm build (Binder Error → "null function" worker
  // trap). Setting the session tz keeps the column TIMESTAMPTZ and renders UTC.
  await conn.query("SET TimeZone='UTC'");

  // Register ONLY manifest-listed event files. Each gets a stable virtual
  // filename so the glob view below reads exactly the committed set (STORE-4).
  const registered: string[] = [];
  for (const e of manifest.events) {
    const vname = e.file; // e.g. "events/20260605T143000Z.parquet"
    await db.registerFileURL(
      vname,
      joinUrl(storeUrl, e.file),
      duckdb.DuckDBDataProtocol.HTTP,
      false,
    );
    registered.push(vname);
  }

  // Derive snapPoints once: sorted unique event timestamps across the store.
  // Empty store ⇒ empty array (no scan needed).
  let snapPoints: string[] = [];
  if (registered.length > 0) {
    const fileList = registered.map((f) => sqlStr(f)).join(", ");
    const res = await conn.query(
      `SELECT DISTINCT strftime(timestamp, '%Y-%m-%dT%H:%M:%S.%gZ') AS ts
         FROM read_parquet([${fileList}])
        ORDER BY ts`,
    );
    snapPoints = res
      .toArray()
      .map((r: { ts: unknown }) => String((r as { ts: unknown }).ts));
  }

  const cache = createWindowCache(DEFAULT_CACHE_CAPACITY);

  const store: Store = {
    manifest,
    cache,
    snapPoints,
    densityBuckets(n: number): DensityBucket[] {
      return manifestDensityBuckets(manifest.events, n);
    },
    async close(): Promise<void> {
      await conn.close();
    },
  };

  // Stash the connection + registered file list on a non-enumerable slot so
  // fetchEvents can reach them without widening the public Store surface.
  STORE_INTERNALS.set(store, { conn, files: registered });
  return store;
}

const DEFAULT_CACHE_CAPACITY = 32;

/** Per-Store private handle (connection + registered file list). */
interface StoreInternals {
  conn: duckdb.AsyncDuckDBConnection;
  files: string[];
}
const STORE_INTERNALS = new WeakMap<Store, StoreInternals>();

/**
 * Manifest-derived density histogram: split [firstEvent, lastEvent] into `n`
 * equal-width buckets and attribute each file's `row_count` to the bucket of
 * its `ts_min`. No parquet scan — purely manifest metadata.
 */
function manifestDensityBuckets(
  events: ManifestEvent[],
  n: number,
): DensityBucket[] {
  const buckets = Math.max(1, Math.floor(n));
  if (events.length === 0) return [];
  let lo = Infinity;
  let hi = -Infinity;
  for (const e of events) {
    lo = Math.min(lo, e.ts_min.getTime());
    hi = Math.max(hi, e.ts_max.getTime());
  }
  if (!isFinite(lo) || !isFinite(hi)) return [];
  const span = Math.max(1, hi - lo);
  const width = span / buckets;
  const counts = new Array<number>(buckets).fill(0);
  for (const e of events) {
    const idx = Math.min(
      buckets - 1,
      Math.floor((e.ts_min.getTime() - lo) / width),
    );
    counts[idx] += e.row_count;
  }
  return counts.map((changeCount, i) => ({
    bucketStart: new Date(lo + i * width).toISOString(),
    bucketEnd: new Date(lo + (i + 1) * width).toISOString(),
    changeCount,
  }));
}

// ───────────────────────────────────────────────────────────────────────────
// fetchEvents — RAW ChangeEvent rows, file-skip pruned + keyset paginated.
// ───────────────────────────────────────────────────────────────────────────

/**
 * Fetch a page of RAW change events from the manifest-valid parquet, honoring:
 *  - `entityIds` / `fields`            → WHERE IN (…) filters
 *  - `asOf`                            → WHERE timestamp <= asOf  + R5 file-skip
 *  - `rowWindow`                       → keyset pagination (visible + prefetch)
 *
 * Returns RAW rows + a `nextCursor`; decode/as-of/lifecycle is reconstruct.ts's
 * job. The result for the requested window is memoized in the store's bounded
 * LRU cache (VD-4). Out-of-range files are pruned via manifest ranges and never
 * scanned (VD-1 / R5).
 */
export async function fetchEvents(
  store: Store,
  spec: FetchSpec = {},
): Promise<EventPage> {
  const key = windowCacheKey(spec);
  const cached = store.cache.get(key);
  if (cached) {
    return { events: cached, nextCursor: deriveCursor(cached) };
  }

  const internals = STORE_INTERNALS.get(store);
  if (!internals) {
    throw new Error("fetchEvents: store has no live DuckDB connection (closed?)");
  }
  const { conn } = internals;

  // R5 file-skip: prune manifest files whose whole range is after asOf.
  const survivors = pruneFiles(store.manifest.events, spec.asOf).map(
    (e) => e.file,
  );
  if (survivors.length === 0) {
    store.cache.set(key, []);
    return { events: [], nextCursor: null };
  }
  const fileList = survivors.map((f) => sqlStr(f)).join(", ");

  const where: string[] = [];
  if (spec.asOf) {
    where.push(`timestamp <= TIMESTAMPTZ ${sqlStr(spec.asOf.toISOString())}`);
  }
  if (spec.entityIds && spec.entityIds.length > 0) {
    where.push(`entity_id IN (${spec.entityIds.map(sqlStr).join(", ")})`);
  }
  if (spec.fields && spec.fields.length > 0) {
    where.push(`field IN (${spec.fields.map(sqlStr).join(", ")})`);
  }

  // Keyset pagination over the deterministic order key
  // (timestamp, entity_id, field, snapshot_id) ascending.
  const w = spec.rowWindow;
  if (w?.after) {
    const c = w.after;
    where.push(
      `(timestamp, entity_id, field, snapshot_id) > ` +
        `(TIMESTAMPTZ ${sqlStr(c.timestamp)}, ${sqlStr(c.entity_id)}, ` +
        `${sqlStr(c.field)}, ${sqlStr(c.snapshot_id)})`,
    );
  }
  const whereSql = where.length > 0 ? `WHERE ${where.join(" AND ")}` : "";
  const limitSql = w?.limit ? `LIMIT ${Math.max(1, Math.floor(w.limit))}` : "";

  const sql = `
    SELECT
      entity_id,
      strftime(timestamp, '%Y-%m-%dT%H:%M:%S.%gZ') AS timestamp,
      field,
      CAST(value AS VARCHAR) AS value,
      dtype,
      snapshot_id
    FROM read_parquet([${fileList}])
    ${whereSql}
    ORDER BY timestamp, entity_id, field, snapshot_id
    ${limitSql}`;

  const table = await conn.query(sql);
  const events: ChangeEvent[] = table.toArray().map((r) => {
    const row = r as Record<string, unknown>;
    const value = row.value;
    return {
      entity_id: String(row.entity_id),
      timestamp: String(row.timestamp),
      field: String(row.field),
      value: value === null || value === undefined ? null : String(value),
      dtype: String(row.dtype),
      snapshot_id: String(row.snapshot_id),
    };
  });

  store.cache.set(key, events);
  return { events, nextCursor: deriveCursor(events) };
}

/**
 * Next-page cursor = the order tuple of the last row in this page, or null when
 * the page is empty (end of stream).
 */
function deriveCursor(events: ChangeEvent[]): RowCursor | null {
  if (events.length === 0) return null;
  const last = events[events.length - 1];
  return {
    timestamp: last.timestamp,
    entity_id: last.entity_id,
    field: last.field,
    snapshot_id: last.snapshot_id,
  };
}
