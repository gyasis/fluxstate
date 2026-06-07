<script lang="ts">
  // File: viewer/src/App.svelte  (T018 / US2 — MVP integration)
  //
  // App shell: loads the real demo.flux store via DuckDB-WASM, fetches the full
  // change-event set (1000×20 demo — windowing is US4), derives snapPoints +
  // densityBuckets (reconstruct.ts pure fns, Date-typed to match the component
  // prop contracts), and wires <Scrubber> ↔ <Table> for time-travel:
  //   • scrubber.onmove(t, mode) → tracks prevT + mode so the table diffs on scrub
  //   • ⇄ diff toggle bound through to both components
  //
  // The data layer (duckdb.ts / reconstruct.ts) is untouched — this only wires it.

  import { onMount } from "svelte";
  import {
    openStore,
    fetchEvents,
    isLargeStore,
    totalEvents,
    fetchEntityOrder,
    fetchWindowEvents,
    type Store,
    type ChangeEvent,
  } from "./lib/duckdb";
  import {
    snapPoints as deriveSnapPoints,
    densityBuckets as deriveDensity,
    buildIndex,
    decodeValue,
    rowStateIndexed,
    immutableViolations,
    type RawChangeEvent,
    type DensityBucket,
    type ReconIndex,
    type Typed,
  } from "./lib/reconstruct";
  import Scrubber from "./lib/scrubber.svelte";
  import Table from "./lib/table.svelte";
  import WindowedTable from "./lib/windowed-table.svelte";
  import StaticView from "./lib/static-view.svelte";
  import Filter, { type FilterPredicate, type FilterRow } from "./lib/filter.svelte";

  const DELETED_FIELD = "__deleted__";
  /** Immutable (write-once) columns — a value change is a violation (US8). */
  const IMMUTABLE_COLS = ["id", "birth_date", "cohort", "mrn"];
  /** Max distinct values for a string column to offer a simple-mode <select>. */
  const MAX_DISTINCT = 40;

  // The store to open. Defaults to the 1000×20 demo; a `?store=<name>.flux`
  // query param points the viewer at another store (e.g. the 100k stress
  // fixture) WITHOUT changing the demo's default — so the demo at `/` is
  // byte-for-byte unchanged and the large path is opt-in via the URL.
  function resolveStoreUrl(): string {
    if (typeof location !== "undefined") {
      const q = new URLSearchParams(location.search).get("store");
      if (q) return q.startsWith("/") ? q : `/${q}`;
    }
    return "/demo.flux";
  }
  const STORE_URL = resolveStoreUrl();
  const DENSITY_BUCKETS = 60;
  // The table is now INDEXED + VIRTUALIZED (US4): buildIndex makes as-of an
  // O(log k) binary search per cell, the diff is two whole-table views (not
  // per-cell), and only the ~40 rows in the viewport are in the DOM. So we hand
  // the table the FULL event set for all 1000 entities — no cap.

  // ── Load state ─────────────────────────────────────────────────────────────
  let status = $state<"loading" | "ready" | "error">("loading");
  let errorMsg = $state<string>("");

  let storeName = $state<string>("");
  let keyColumn = $state<string>("");
  let schema = $state<Record<string, string>>({});
  let events = $state<RawChangeEvent[]>([]);
  let totalEntities = $state<number>(0);
  let snaps = $state<Date[]>([]);
  let density = $state<DensityBucket[]>([]);

  // ── US4 SCALE: large-store (windowed) path ─────────────────────────────────
  // For a large store the full event set is millions of rows → we DON'T load it
  // into JS. Instead we keep `storeRef` live, fetch the full ORDERED entity-id
  // list once (cheap aggregate), and stream each visible window's events via
  // DuckDB (bounded-LRU). `events` stays EMPTY in this path.
  let isLarge = $state<boolean>(false);
  // NOTE: storeRef is a PLAIN (non-$state) ref on purpose. The Store carries its
  // live DuckDB connection in a module-level WeakMap keyed by the Store OBJECT
  // identity; wrapping it in $state would hand `loadWindow` a Svelte reactive
  // PROXY whose identity differs from the original key → the WeakMap lookup
  // misses and `fetchEvents` throws "store has no live DuckDB connection".
  let storeRef: Store | null = null;
  let entityOrder = $state<string[]>([]);
  let totalEventCount = $state<number>(0);

  // ── Time-travel state (driven by the scrubber) ─────────────────────────────
  let T = $state<Date | undefined>(undefined);
  let prevT = $state<Date | null>(null);
  let mode = $state<"step" | "play">("step");
  let diffOn = $state<boolean>(true);
  let playing = $state<boolean>(false);

  // ── View mode: interactive viewer vs static / print audit view (US6) ───────
  let viewMode = $state<"interactive" | "static">("interactive");

  // ── Left icon-rail flyout state ────────────────────────────────────────────
  // The wide sidebar collapses to a slim icon rail; clicking an icon opens a
  // flyout panel next to the rail (Filter / Snapshots). Default = collapsed
  // (null) so the scrubber + table own the width.
  let activePanel = $state<null | "filter" | "snapshots">(null);
  function togglePanel(p: "filter" | "snapshots"): void {
    activePanel = activePanel === p ? null : p;
  }

  // ── Filter state (US5) ──────────────────────────────────────────────────────
  // The filter compiles to a predicate over per-entity "as-of-now" FilterRow views.
  // We evaluate it ONCE to a STABLE entity_id Set (dev-spec §5: filter is a stable
  // row set you then scrub — NOT re-evaluated per T) and hand that Set to <Table>.
  let predicate = $state<FilterPredicate>(() => true);

  /** Index over the full event set (one O(E log E) build; for filter row views). */
  const filterIndex = $derived.by<ReconIndex | null>(() =>
    events.length ? buildIndex(events) : null,
  );

  /** Per-entity immutable-column violations (the `flagged` meta-field source). */
  const violations = $derived.by(() =>
    filterIndex ? immutableViolations(filterIndex, IMMUTABLE_COLS) : new Map(),
  );

  /** Tracked columns for the filter (schema columns minus the key). */
  const trackedCols = $derived(Object.keys(schema).filter((c) => c !== keyColumn));

  /** Distinct values per low-cardinality string column (simple-mode selects). */
  const distinct = $derived.by<Record<string, string[]>>(() => {
    const out: Record<string, string[]> = {};
    if (!filterIndex) return out;
    for (const c of trackedCols) {
      const d = (schema[c] ?? "").trim().toLowerCase();
      if (!(d === "utf8" || d === "str" || d === "string")) continue;
      const set = new Set<string>();
      let overflow = false;
      for (const byField of filterIndex.cells.values()) {
        const hist = byField.get(c);
        if (!hist) continue;
        for (const e of hist) {
          if (e.value !== null) set.add(e.value);
          if (set.size > MAX_DISTINCT) { overflow = true; break; }
        }
        if (overflow) break;
      }
      if (!overflow) out[c] = [...set].sort();
    }
    return out;
  });

  /** Per-entity "as-of-now" FilterRow views (latest value per tracked col + meta).
   *  "now" = the latest event timestamp (the stable, full-history view). */
  const filterRows = $derived.by<FilterRow[]>(() => {
    if (!filterIndex || !snaps.length) return [];
    const at = snaps[snaps.length - 1];
    const tMs = at.getTime();
    const rows: FilterRow[] = [];
    for (const eid of filterIndex.order) {
      const st = rowStateIndexed(filterIndex, eid, at);
      const byField = filterIndex.cells.get(eid)!;
      const row = { id: eid } as FilterRow;
      let changes = 0;
      for (const c of trackedCols) {
        const hist = byField.get(c);
        if (hist && c !== DELETED_FIELD) changes += hist.length;
        // latest decoded value as a filter-friendly primitive
        let v: Typed = null;
        if (hist && hist.length) v = decodeValue(hist[hist.length - 1].value, hist[hist.length - 1].dtype);
        row[c] = v instanceof Date ? v.toISOString().slice(0, 10) : (v ?? "") as string | number | boolean;
      }
      row.changes = changes;
      row.deleted = st.state === "deleted";
      row.resurrected = st.resurrected;
      row.flagged = violations.has(eid); // meta `flagged` = immutable violation
      rows.push(row);
    }
    return rows;
  });

  /** The STABLE filtered entity_id set (predicate evaluated once over filterRows). */
  const filteredIds = $derived.by<Set<string>>(() => {
    const s = new Set<string>();
    for (const r of filterRows) {
      try {
        if (predicate(r)) s.add(r.id);
      } catch {
        // a bad predicate slipped through → treat as match-all for that row
        s.add(r.id);
      }
    }
    return s;
  });

  /** Slim KPI summary (small-store path) — counts over the as-of-now row views. */
  const kpis = $derived.by(() => {
    let flagged = 0;
    let deleted = 0;
    let resurrected = 0;
    for (const r of filterRows) {
      if (r.flagged) flagged++;
      if (r.deleted) deleted++;
      if (r.resurrected) resurrected++;
    }
    return {
      entities: totalEntities,
      flagged,
      deleted,
      resurrected,
      events: isLarge ? totalEventCount : events.length,
    };
  });

  /** Track prevT + mode on every playhead move so the table can diff on scrub.
   *  T is NOT bound to the scrubber's value (that would double-drive it and
   *  clobber prevT) — onmove is the single source of truth for the playhead. */
  function onScrubMove(t: Date, m: "step" | "play"): void {
    if (T && t.getTime() === T.getTime()) return; // no-op move
    prevT = T ?? null;
    mode = m;
    T = t;
  }

  /** Stream a window of entities' events (DuckDB, bounded-LRU). Large path only. */
  async function loadWindow(ids: string[]): Promise<RawChangeEvent[]> {
    if (!storeRef) return [];
    const evs = await fetchWindowEvents(storeRef, ids);
    return evs as ChangeEvent[] as RawChangeEvent[];
  }

  /** Jump the playhead to a specific snapshot instant (sidebar Snapshots list). */
  function jumpToSnap(d: Date): void {
    onScrubMove(d, "step");
  }

  /** Short label for a snapshot row in the sidebar list. */
  function snapLabel(d: Date): string {
    return d.toLocaleDateString("en-US", {
      month: "short",
      day: "numeric",
      year: "numeric",
    });
  }
  /** The currently-selected snapshot index (drives the active highlight). */
  const activeSnapIdx = $derived.by<number>(() => {
    if (!T) return -1;
    const tMs = T.getTime();
    for (let i = 0; i < snaps.length; i++) {
      if (snaps[i].getTime() === tMs) return i;
    }
    return -1;
  });

  onMount(async () => {
    try {
      const store: Store = await openStore(STORE_URL);
      storeRef = store;
      storeName = store.manifest.store_name;
      keyColumn = store.manifest.key_column;
      schema = store.manifest.schema;
      totalEventCount = totalEvents(store);
      isLarge = isLargeStore(store);

      if (isLarge) {
        // ── LARGE: stream the visible window; never load all events. ──────────
        // snapPoints + density come from CHEAP aggregates (DISTINCT timestamp /
        // manifest row_counts), NOT a full scan. The full ordered entity-id list
        // is one aggregate query (ids only) — the row order to virtualize over.
        snaps = store.snapPoints.map((s) => new Date(s));
        density = store.densityBuckets(DENSITY_BUCKETS).map((b) => ({
          bucketStart: new Date(b.bucketStart),
          bucketEnd: new Date(b.bucketEnd),
          changeCount: b.changeCount,
        }));
        entityOrder = await fetchEntityOrder(store);
        totalEntities = entityOrder.length;
      } else {
        // ── SMALL: load the full event set (the demo path — UNCHANGED). ───────
        const page = await fetchEvents(store);
        // duckdb.ChangeEvent is field-identical to reconstruct.RawChangeEvent.
        events = page.events as ChangeEvent[] as RawChangeEvent[];
        snaps = deriveSnapPoints(events);
        density = deriveDensity(events, DENSITY_BUCKETS);
        totalEntities = new Set(events.map((e) => e.entity_id)).size;
      }

      // Default the playhead to the latest event.
      if (snaps.length) {
        T = snaps[snaps.length - 1];
        prevT = null;
      }
      status = "ready";
    } catch (err) {
      console.error("App: failed to load store", err);
      errorMsg = err instanceof Error ? err.message : String(err);
      status = "error";
    }
  });
</script>

<div class="app">
  <!-- ── Top bar ──────────────────────────────────────────────────────────── -->
  <header class="topbar">
    <div class="brand">
      <span class="mark" aria-hidden="true">
        <svg viewBox="0 0 24 24" width="22" height="22">
          <rect x="2.5" y="2.5" width="19" height="19" rx="5.5" fill="var(--accent)" />
          <path
            d="M7 8.5h10M7 12h7M7 15.5h4"
            stroke="#fff"
            stroke-width="1.9"
            stroke-linecap="round"
          />
        </svg>
      </span>
      <span class="wordmark">FluxState</span>
      {#if status === "ready"}
        <span class="storechip"><code>{storeName}.flux</code></span>
      {/if}
    </div>

    {#if status === "ready" && !isLarge}
      <nav class="viewtabs" aria-label="View mode">
        <button
          type="button"
          class="vtab"
          class:on={viewMode === "interactive"}
          onclick={() => (viewMode = "interactive")}
        >Interactive</button>
        <button
          type="button"
          class="vtab"
          class:on={viewMode === "static"}
          onclick={() => (viewMode = "static")}
        >Audit</button>
      </nav>
    {/if}

    <div class="topright">
      {#if status === "ready" && T}
        <div class="asof-readout" title="Current as-of instant">
          <span class="lbl">as of</span>
          <span class="val"
            >{T.toLocaleDateString("en-US", {
              month: "short",
              day: "numeric",
              year: "numeric",
            })}</span
          >
        </div>
      {/if}
      {#if status === "ready"}
        <div class="metachip" title="Store summary">
          {(isLarge ? totalEventCount : events.length).toLocaleString()} events ·
          {totalEntities.toLocaleString()} entities{#if isLarge}
            <span class="streamed">streamed</span>{/if}
        </div>
      {/if}
      <button class="iconbtn" type="button" title="Help" aria-label="Help">?</button>
      <button class="iconbtn" type="button" title="Settings" aria-label="Settings">
        <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8">
          <circle cx="12" cy="12" r="3" />
          <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
        </svg>
      </button>
    </div>
  </header>

  {#if status === "error"}
    <div class="centerbox">
      <div class="errbox">
        <strong>Failed to load the store.</strong>
        <pre>{errorMsg}</pre>
      </div>
    </div>
  {:else if status === "loading"}
    <div class="centerbox">
      <div class="loading">Instantiating DuckDB-WASM and reading the changelog…</div>
    </div>
  {:else if T && viewMode === "static" && !isLarge}
    <div class="body body--full fx-scroll">
      <StaticView {events} {schema} {keyColumn} {storeName} T="now" />
    </div>
  {:else if T && isLarge}
    <!-- LARGE (windowed) path: scrubber + streamed table. Global filter/sort
         + static/print view need whole-table evaluation across all 100k, so
         the sidebar filter is scoped OUT (we still show the snapshot list). -->
    <div class="body body--rail">
      <nav class="rail" aria-label="Panels">
        <button
          type="button"
          class="railbtn"
          class:on={activePanel === "snapshots"}
          aria-pressed={activePanel === "snapshots"}
          aria-label="Snapshots"
          title="Snapshots"
          onclick={() => togglePanel("snapshots")}
        >
          <svg viewBox="0 0 24 24" width="19" height="19" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="12" cy="12" r="8.5" />
            <path d="M12 7v5l3.5 2" />
          </svg>
        </button>
      </nav>
      {#if activePanel === "snapshots"}
        <aside class="flyout fx-scroll">
          <div class="flyout-head">
            <span class="flyout-title">Snapshots</span>
            <button class="flyout-x" type="button" aria-label="Close panel" onclick={() => (activePanel = null)}>✕</button>
          </div>
          <div class="snaplist">
            {#each snaps as s, i (i)}
              <button
                type="button"
                class="snap"
                class:on={i === activeSnapIdx}
                onclick={() => jumpToSnap(s)}
              >
                <span class="snap-dot" aria-hidden="true"></span>
                <span class="snap-label">{snapLabel(s)}</span>
                {#if i === activeSnapIdx}<span class="snap-now">now</span>{/if}
              </button>
            {/each}
          </div>
        </aside>
      {/if}
      <main class="main fx-scroll">
        <Scrubber
          snapPoints={snaps}
          {density}
          value={T}
          bind:playing
          bind:diffOn
          onmove={onScrubMove}
        />
        <p class="scopenote">
          Streaming the visible window over {totalEntities.toLocaleString()} entities /
          {totalEventCount.toLocaleString()} events via DuckDB-WASM. Scroll + scrub are
          live; global filter, sort, and the audit view are disabled at this scale
          (they require evaluating the whole table).
        </p>
        <WindowedTable
          {entityOrder}
          {schema}
          {keyColumn}
          {T}
          {prevT}
          {diffOn}
          {mode}
          fetchWindow={loadWindow}
        />
      </main>
    </div>
  {:else if T}
    <div class="body body--rail">
      <!-- ── Slim left icon rail ─────────────────────────────────────────── -->
      <nav class="rail" aria-label="Panels">
        <button
          type="button"
          class="railbtn"
          class:on={activePanel === "filter"}
          aria-pressed={activePanel === "filter"}
          aria-label="Filter"
          title="Filter"
          onclick={() => togglePanel("filter")}
        >
          <svg viewBox="0 0 24 24" width="19" height="19" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">
            <path d="M3 5h18l-7 8v5l-4 2v-7L3 5z" />
          </svg>
          {#if filteredIds.size < totalEntities}<span class="raildot" aria-hidden="true"></span>{/if}
        </button>
        <button
          type="button"
          class="railbtn"
          class:on={activePanel === "snapshots"}
          aria-pressed={activePanel === "snapshots"}
          aria-label="Snapshots"
          title="Snapshots"
          onclick={() => togglePanel("snapshots")}
        >
          <svg viewBox="0 0 24 24" width="19" height="19" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="12" cy="12" r="8.5" />
            <path d="M12 7v5l3.5 2" />
          </svg>
        </button>
      </nav>

      <!-- ── Flyout panel (mounts beside the rail when an icon is active) ─── -->
      {#if activePanel}
        <aside class="flyout fx-scroll">
          <div class="flyout-head">
            <span class="flyout-title">{activePanel === "filter" ? "Filter" : "Snapshots"}</span>
            <button class="flyout-x" type="button" aria-label="Close panel" onclick={() => (activePanel = null)}>✕</button>
          </div>
          {#if activePanel === "filter"}
            <Filter
              columns={trackedCols}
              {schema}
              {distinct}
              total={totalEntities}
              shown={filteredIds.size}
              bind:predicate
            />
          {:else}
            <div class="snaplist">
              {#each snaps as s, i (i)}
                <button
                  type="button"
                  class="snap"
                  class:on={i === activeSnapIdx}
                  onclick={() => jumpToSnap(s)}
                >
                  <span class="snap-dot" aria-hidden="true"></span>
                  <span class="snap-label">{snapLabel(s)}</span>
                  {#if i === activeSnapIdx}<span class="snap-now">now</span>{/if}
                </button>
              {/each}
            </div>
          {/if}
        </aside>
      {/if}

      <main class="main fx-scroll">
        <div class="kpis">
          <div class="kpi"><span class="kpi-n">{kpis.entities.toLocaleString()}</span><span class="kpi-l">Entities</span></div>
          <div class="kpi"><span class="kpi-n kpi-flag">{kpis.flagged.toLocaleString()}</span><span class="kpi-l">Flagged</span></div>
          <div class="kpi"><span class="kpi-n kpi-del">{kpis.deleted.toLocaleString()}</span><span class="kpi-l">Deleted</span></div>
          <div class="kpi"><span class="kpi-n kpi-res">{kpis.resurrected.toLocaleString()}</span><span class="kpi-l">Resurrected</span></div>
          <div class="kpi"><span class="kpi-n">{kpis.events.toLocaleString()}</span><span class="kpi-l">Events</span></div>
        </div>
        <Scrubber
          snapPoints={snaps}
          {density}
          value={T}
          bind:playing
          bind:diffOn
          onmove={onScrubMove}
        />
        <Table
          {events}
          {schema}
          {keyColumn}
          {T}
          {prevT}
          {diffOn}
          {mode}
          rowFilter={filteredIds}
        />
      </main>
    </div>
  {/if}
</div>

<style>
  .app {
    display: grid;
    grid-template-rows: auto 1fr;
    height: 100dvh;
    width: 100%;
    background: var(--bg);
    overflow: hidden;
  }

  /* ── Top bar ─────────────────────────────────────────────────────────── */
  .topbar {
    display: flex;
    align-items: center;
    gap: 18px;
    height: 60px;
    padding: 0 22px;
    background: var(--card);
    border-bottom: 1px solid var(--line);
    box-shadow: var(--shadow-sm);
    z-index: 20;
  }
  .brand {
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .mark {
    display: flex;
  }
  .wordmark {
    font-size: 17px;
    font-weight: 700;
    letter-spacing: -0.3px;
    color: var(--ink);
  }
  .storechip {
    margin-left: 4px;
  }
  .storechip code {
    font-family: var(--mono);
    font-size: 12px;
    color: var(--mut);
    background: var(--bg-sunken);
    border: 1px solid var(--line);
    padding: 3px 9px;
    border-radius: var(--r-pill);
  }
  .viewtabs {
    display: flex;
    gap: 4px;
    padding: 3px;
    background: var(--bg-sunken);
    border-radius: var(--r-sm);
  }
  .vtab {
    appearance: none;
    border: none;
    background: transparent;
    color: var(--mut);
    font: inherit;
    font-size: 13px;
    font-weight: 600;
    padding: 6px 16px;
    border-radius: 6px;
    cursor: pointer;
    transition: background 0.15s, color 0.15s;
  }
  .vtab:hover {
    color: var(--ink);
  }
  .vtab.on {
    background: var(--card);
    color: var(--accent);
    box-shadow: var(--shadow-sm);
  }
  .topright {
    margin-left: auto;
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .asof-readout {
    display: flex;
    align-items: center;
    gap: 7px;
    font-size: 12.5px;
    background: var(--accent-bg);
    border: 1px solid var(--accent-border);
    border-radius: var(--r-sm);
    padding: 5px 12px;
  }
  .asof-readout .lbl {
    font-size: 9.5px;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: var(--accent);
    font-weight: 700;
  }
  .asof-readout .val {
    font-weight: 600;
    color: var(--ink);
    font-variant-numeric: tabular-nums;
  }
  .metachip {
    font-size: 12px;
    color: var(--mut);
    font-variant-numeric: tabular-nums;
  }
  .metachip .streamed {
    margin-left: 4px;
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: var(--teal);
    background: var(--teal-bg);
    padding: 1px 7px;
    border-radius: var(--r-pill);
  }
  .iconbtn {
    width: 32px;
    height: 32px;
    border-radius: var(--r-sm);
    border: 1px solid var(--line);
    background: var(--card);
    color: var(--mut);
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 14px;
    font-weight: 700;
    transition: background 0.15s, color 0.15s, border-color 0.15s;
  }
  .iconbtn:hover {
    background: var(--hover);
    color: var(--ink);
    border-color: var(--line-strong);
  }

  /* ── Body layout ─────────────────────────────────────────────────────── */
  /* Slim icon rail (fixed) + optional flyout (auto width) + main (1fr). When
     no panel is active the rail is the only chrome → scrubber+table own the
     width. */
  .body--rail {
    display: grid;
    grid-template-columns: 52px auto minmax(0, 1fr);
    /* Pin the single content row to the full height of this track EXPLICITLY
       (this element is the 1fr row of .app) instead of relying on implicit-row
       stretch. minmax(0,1fr) lets .main → .tablewrap → .scroller's flex:1 chain
       resolve to a real height so the virtualized rows render — guards against
       the "table body collapses to 0" regression class. */
    grid-template-rows: minmax(0, 1fr);
    min-height: 0;
    overflow: hidden;
  }
  /* Audit body: a flex column so its single child <StaticView>'s .sheet can
     fill the available height (height:100%) and let only the table scroll. The
     body itself clips (min-height:0) so the page doesn't overflow the viewport;
     @media print releases this (see static-view.svelte) so the full audit
     paginates. */
  .body--full {
    display: flex;
    flex-direction: column;
    min-height: 0;
    overflow: hidden;
    padding: 20px 24px 24px;
  }

  /* The slim vertical icon rail. */
  .rail {
    grid-column: 1;
    width: 52px;
    background: var(--card);
    border-right: 1px solid var(--line);
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 6px;
    padding: 10px 0;
  }
  .railbtn {
    position: relative;
    width: 38px;
    height: 38px;
    border-radius: var(--r-sm);
    border: 1px solid transparent;
    background: transparent;
    color: var(--mut);
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: background 0.15s, color 0.15s, border-color 0.15s;
  }
  .railbtn:hover {
    background: var(--hover);
    color: var(--ink);
  }
  .railbtn.on {
    background: var(--accent-bg);
    color: var(--accent);
    border-color: var(--accent-border);
  }
  .raildot {
    position: absolute;
    top: 6px;
    right: 6px;
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: var(--accent);
    box-shadow: 0 0 0 2px var(--card);
  }

  /* Flyout panel beside the rail (mounts only when a panel is active). */
  .flyout {
    grid-column: 2;
    width: 270px;
    background: var(--card);
    border-right: 1px solid var(--line);
    overflow-y: auto;
    padding: 12px 14px 28px;
    display: flex;
    flex-direction: column;
    gap: 12px;
    box-shadow: var(--shadow-sm);
  }
  .flyout-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  .flyout-title {
    font-size: 13px;
    font-weight: 700;
    color: var(--ink);
  }
  .flyout-x {
    width: 26px;
    height: 26px;
    border-radius: var(--r-sm);
    border: 1px solid var(--line);
    background: var(--card);
    color: var(--mut);
    cursor: pointer;
    font-size: 12px;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: background 0.15s, color 0.15s;
  }
  .flyout-x:hover {
    background: var(--hover);
    color: var(--ink);
  }

  /* The main content column: KPI cards + scrubber size to content; the table
     (Table / WindowedTable, both flex:1) fills the remaining height so only it
     scrolls. A flex column with min-height:0 so it bounds to the viewport (the
     scrubber/KPIs stay fixed, the table scrolls) instead of overflowing. */
  .main {
    grid-column: 3; /* ALWAYS the 1fr track — even when the flyout (col 2) is absent */
    display: flex;
    flex-direction: column;
    overflow: hidden;
    padding: 20px 24px 24px;
    min-width: 0;
    min-height: 0;
  }

  /* Slim KPI / metric cards row above the scrubber. */
  .kpis {
    display: flex;
    gap: 10px;
    margin-bottom: 16px;
    flex-wrap: wrap;
  }
  .kpi {
    flex: 1 1 0;
    min-width: 96px;
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: var(--r);
    padding: 11px 14px;
    display: flex;
    flex-direction: column;
    gap: 2px;
    box-shadow: var(--shadow-sm);
  }
  .kpi-n {
    font-size: 19px;
    font-weight: 700;
    color: var(--ink);
    font-variant-numeric: tabular-nums;
    letter-spacing: -0.4px;
  }
  .kpi-l {
    font-size: 10.5px;
    text-transform: uppercase;
    letter-spacing: 0.6px;
    color: var(--mut);
    font-weight: 600;
  }
  .kpi-flag {
    color: var(--del);
  }
  .kpi-del {
    color: var(--mod);
  }
  .kpi-res {
    color: var(--add);
  }

  /* Snapshots list — styled like the reference's selectable day list. */
  .snaplist {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .snap {
    appearance: none;
    border: 1px solid transparent;
    background: transparent;
    border-radius: var(--r-sm);
    padding: 8px 10px;
    font: inherit;
    font-size: 12.5px;
    color: var(--text);
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: 9px;
    text-align: left;
    transition: background 0.12s, border-color 0.12s;
  }
  .snap:hover {
    background: var(--hover);
  }
  .snap.on {
    background: var(--active-bg);
    border-color: var(--accent-border);
    color: var(--ink);
    font-weight: 600;
  }
  .snap-dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: var(--dim);
    flex: none;
  }
  .snap.on .snap-dot {
    background: var(--accent);
    box-shadow: 0 0 0 3px var(--accent-bg);
  }
  .snap-label {
    font-variant-numeric: tabular-nums;
  }
  .snap-now {
    margin-left: auto;
    font-size: 9px;
    text-transform: uppercase;
    letter-spacing: 0.6px;
    font-weight: 700;
    color: #fff;
    background: var(--accent);
    padding: 1px 7px;
    border-radius: var(--r-pill);
  }

  .scopenote {
    margin: 14px 0 0;
    font-size: 12px;
    color: var(--mut);
    background: var(--accent-bg);
    border: 1px solid var(--accent-border);
    border-radius: var(--r-sm);
    padding: 9px 13px;
  }

  /* ── Loading / error ─────────────────────────────────────────────────── */
  .centerbox {
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 60px 24px;
  }
  .loading {
    color: var(--mut);
    font-size: 14px;
  }
  .errbox {
    border: 1px solid var(--del-border);
    border-radius: var(--r);
    padding: 16px 18px;
    background: var(--del-bg);
    color: var(--del);
    max-width: 640px;
    box-shadow: var(--shadow);
  }
  .errbox pre {
    margin: 8px 0 0;
    white-space: pre-wrap;
    font-size: 12px;
  }

  @media (max-width: 720px) {
    .flyout {
      position: absolute;
      top: 60px;
      bottom: 0;
      left: 52px;
      z-index: 30;
      box-shadow: var(--shadow-lg);
    }
  }
</style>
