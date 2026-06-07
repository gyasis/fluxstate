<script lang="ts">
  // File: viewer/src/lib/windowed-table.svelte  (T025 / US4 — SCALE path)
  //
  // The STREAMED, windowed as-of table for LARGE stores (100k+ entities, millions
  // of events). It does NOT receive a full event array — that would OOM JS. Instead:
  //
  //   1. It virtualizes over the FULL ORDERED ENTITY-ID LIST (`entityOrder`, ~100k
  //      strings — cheap, fetched once by the parent via a DuckDB aggregate). The
  //      spacer is sized to the full entity count → the scrollbar + scroll range
  //      are correct for 100k rows while only ~40 rows are ever in the DOM.
  //
  //   2. For the VISIBLE window (firstVisible..+visibleCount + overscan) it asks
  //      `fetchWindow(ids)` (DuckDB, per-entity, bounded-LRU cached) for JUST those
  //      entities' full event history, then `buildIndex` over that small set and
  //      reconstructs as-of T with the SAME pure primitives as the demo table.
  //
  //   3. On scroll the window's entity-id set changes → a new fetch (LRU-cached);
  //      on scrub (T change) the SAME window index is reused (no re-query) — as-of
  //      and the prev↔now diff are binary searches over the per-window timelines.
  //
  // SCOPE (honest, dev-spec §4.3): features that need whole-table evaluation across
  // all 100k (global SQL filter / global sort) are NOT offered in this path — the
  // header shows columns but is not click-to-sort, and there is no Filter pane. The
  // MUST-haves ARE here: scroll, scrub (as-of), diff-on-scrub, lifecycle (deleted/
  // resurrected) + immutable-violation flag encodings on the visible window, and a
  // constant DOM. The demo's load-all `table.svelte` is untouched.

  import {
    buildIndex,
    decodeValue,
    rowStateIndexed,
    immutableViolations,
    isViolatingAt,
    changeDirection,
    type ReconIndex,
    type RawChangeEvent,
    type RowLifecycleState,
    type Typed,
  } from "./reconstruct";
  import Inspector from "./inspector.svelte";

  const DELETED_FIELD = "__deleted__";
  const IMMUTABLE_COLS = ["id", "birth_date", "cohort", "mrn"];

  // ── Props ────────────────────────────────────────────────────────────────
  interface Props {
    /** FULL ordered entity-id list (the row order to virtualize over). */
    entityOrder: string[];
    /** Manifest schema map: column → dtype-tag (key column included). */
    schema: Record<string, string>;
    /** The key (entity-id) column name. */
    keyColumn: string;
    /** Current as-of instant. */
    T: Date;
    /** Previous as-of instant (the step we came FROM) — drives the diff. */
    prevT?: Date | null;
    /** ⇄ diff toggle: when false, cells snap plainly with no old→new. */
    diffOn?: boolean;
    /** How we arrived at T: "step" → pin the diff; "play" → linger then fade. */
    mode?: "step" | "play";
    /**
     * Fetch the full event history for a set of entity_ids (DuckDB-backed,
     * bounded-LRU cached by the parent/store). The table calls this for the
     * VISIBLE window only — never the whole table.
     */
    fetchWindow: (entityIds: string[]) => Promise<RawChangeEvent[]>;
  }

  let {
    entityOrder,
    schema,
    keyColumn,
    T,
    prevT = null,
    diffOn = true,
    mode = "step",
    fetchWindow,
  }: Props = $props();

  const DWELL_MS = 2600;
  const FADE_MS = 750;

  // Virtualization geometry (identical to the demo table for visual parity).
  const ROW_H = 33;
  const HEAD_H = 35;
  const VIEWPORT_FALLBACK = 560; // px — used until the scroller is measured (clientHeight 0)
  const OVERSCAN = 8;
  // Scroll-container height is set EXPLICITLY in JS (window bottom − scroller top,
  // min 200px) rather than flex-filled — the flex chain collapsed to 0 in some
  // real browsers. Matches the deterministic approach in table.svelte.
  const MIN_SCROLLER_H = 200;
  let scrollerEl = $state<HTMLDivElement | undefined>(undefined);
  let viewportH = $state(0);

  $effect(() => {
    const el = scrollerEl;
    if (!el || typeof window === "undefined") return;
    const measure = () => {
      const top = el.getBoundingClientRect().top;
      const h = Math.max(MIN_SCROLLER_H, Math.floor(window.innerHeight - top - 12));
      if (h !== viewportH) viewportH = h;
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(document.body);
    window.addEventListener("resize", measure);
    return () => {
      ro.disconnect();
      window.removeEventListener("resize", measure);
    };
  });

  const fields = $derived(Object.keys(schema).filter((c) => c !== keyColumn));
  const columns = $derived([keyColumn, ...fields]);

  // ── Virtual window over the FULL entity list ───────────────────────────────
  let scrollTop = $state(0);
  const totalRows = $derived(entityOrder.length);
  const spacerH = $derived(totalRows * ROW_H);
  const firstVisible = $derived(
    Math.max(0, Math.floor((scrollTop - HEAD_H) / ROW_H) - OVERSCAN),
  );
  const visibleCount = $derived(
    Math.ceil((viewportH || VIEWPORT_FALLBACK) / ROW_H) + OVERSCAN * 2,
  );
  /** The entity_ids currently in (or near) the viewport — the fetch window. */
  const windowIds = $derived(
    entityOrder.slice(firstVisible, firstVisible + visibleCount),
  );
  /** Stable key for the window so the loader only re-fetches on a real change. */
  const windowKey = $derived(windowIds.join(","));

  function onScroll(e: Event): void {
    scrollTop = (e.currentTarget as HTMLElement).scrollTop;
  }

  // ── Load the window's events + build a per-window index ────────────────────
  // Keyed on windowKey so scrolling triggers a (LRU-cached) re-fetch but a pure
  // scrub (T change) reuses the loaded window. While a fetch is in flight we keep
  // the previous index so the rows don't blank out (smooth scroll).
  let windowEvents = $state<RawChangeEvent[]>([]);
  let loadedKey = $state<string>("");
  let loading = $state(false);

  $effect(() => {
    const key = windowKey;
    const ids = windowIds;
    if (key === loadedKey) return;
    let cancelled = false;
    loading = true;
    fetchWindow(ids)
      .then((evs) => {
        if (cancelled) return;
        windowEvents = evs;
        loadedKey = key;
        loading = false;
      })
      .catch((err) => {
        if (cancelled) return;
        console.error("windowed-table: fetchWindow failed", err);
        loading = false;
      });
    return () => {
      cancelled = true;
    };
  });

  /** Per-window reconstruction index — built only over the ~40-entity window. */
  const index = $derived.by<ReconIndex>(() => buildIndex(windowEvents));

  /** Immutable-column violations within the window (the red-flag source). */
  const violationsLocal = $derived.by(() =>
    immutableViolations(index, IMMUTABLE_COLS),
  );

  // ── Per-cell / per-row change frequency (heat-tint + Δ gutter) over window ──
  interface FreqMaps {
    cell: Map<string, Map<string, number>>;
    row: Map<string, number>;
  }
  const freq = $derived.by<FreqMaps>(() => {
    const cell = new Map<string, Map<string, number>>();
    const row = new Map<string, number>();
    for (const [eid, byField] of index.cells) {
      let total = 0;
      const fm = new Map<string, number>();
      for (const [f, hist] of byField) {
        if (f === DELETED_FIELD) continue;
        fm.set(f, hist.length);
        total += hist.length;
      }
      cell.set(eid, fm);
      row.set(eid, total);
    }
    return { cell, row };
  });

  function heatClass(n: number): "" | "v1" | "v2" | "v3" | "v4" {
    if (n >= 8) return "v4";
    if (n >= 5) return "v3";
    if (n >= 3) return "v2";
    if (n >= 1) return "v1";
    return "";
  }

  /** Last-known value for (entity, field) at ≤ T via binary search. O(log k). */
  function lastLE(entityId: string, field: string, tMs: number): Typed {
    const hist = index.cells.get(entityId)?.get(field);
    if (!hist || hist.length === 0) return null;
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
    if (ans < 0) return null;
    return decodeValue(hist[ans].value, hist[ans].dtype);
  }

  interface VisibleRow {
    /** Absolute index in the full entity list (drives the absolute top). */
    abs: number;
    id: string;
    /** Loaded? false ⇒ the window event fetch for this row hasn't arrived. */
    loaded: boolean;
    state: RowLifecycleState["state"] | "unborn";
    resurrected: boolean;
    cells: Record<string, Typed>;
  }

  /** Build the VISIBLE rows as-of T from the per-window index. */
  function buildVisible(at: Date): VisibleRow[] {
    const tMs = at.getTime();
    const out: VisibleRow[] = [];
    for (let k = 0; k < windowIds.length; k++) {
      const eid = windowIds[k];
      const abs = firstVisible + k;
      const hasEvents = index.cells.has(eid);
      if (!hasEvents) {
        // Window events not loaded yet for this id → render a placeholder row.
        out.push({
          abs,
          id: eid,
          loaded: false,
          state: "active",
          resurrected: false,
          cells: {},
        });
        continue;
      }
      const st = rowStateIndexed(index, eid, at);
      const cells: Record<string, Typed> = {};
      cells[keyColumn] = decodeValue(eid, schema[keyColumn]);
      for (const f of fields) cells[f] = lastLE(eid, f, tMs);
      out.push({ abs, id: eid, loaded: true, state: st.state, resurrected: st.resurrected, cells });
    }
    return out;
  }

  const visibleRows = $derived(buildVisible(T));

  /** Map id → as-of-prevT cell values (for the diff). Built over the SAME window. */
  const prevById = $derived.by(() => {
    const m = new Map<string, Record<string, Typed>>();
    if (!prevT) return m;
    const tMs = prevT.getTime();
    for (const eid of windowIds) {
      if (!index.cells.has(eid)) continue;
      const cells: Record<string, Typed> = {};
      cells[keyColumn] = decodeValue(eid, schema[keyColumn]);
      for (const f of fields) cells[f] = lastLE(eid, f, tMs);
      m.set(eid, cells);
    }
    return m;
  });

  function flaggedAt(entityId: string): boolean {
    return isViolatingAt(violationsLocal, entityId, T);
  }

  function fmt(v: Typed | undefined): string {
    if (v === undefined || v === null) return "ø";
    if (v instanceof Date) return v.toISOString().slice(0, 10);
    if (typeof v === "boolean") return v ? "true" : "false";
    return String(v);
  }

  function fmtDelta(delta: number): string {
    const sign = delta >= 0 ? "+" : "";
    const s = Number.isInteger(delta) ? String(delta) : String(Number(delta.toFixed(4)));
    return ` ${sign}${s}`;
  }

  function cellDir(
    id: string,
    field: string,
    curCells: Record<string, Typed>,
  ): { dir: "up" | "down" | "same" | "na"; deltaLabel: string } | null {
    const prevRow = prevById.get(id);
    if (!prevRow) return null;
    const cd = changeDirection(prevRow[field] ?? null, curCells[field] ?? null);
    return { dir: cd.dir, deltaLabel: cd.delta !== null ? fmtDelta(cd.delta) : "" };
  }

  // ── Diff state, keyed by cell (delta-only repaint) ─────────────────────────
  // Same non-reactive plain store + published snapshot pattern as the demo table.
  interface DiffState {
    old: string;
    faded: boolean;
    pinT: number;
    settleTimer?: ReturnType<typeof setTimeout>;
    endTimer?: ReturnType<typeof setTimeout>;
  }
  const diffStore: Record<string, DiffState> = {};
  let diffs = $state<Record<string, DiffState>>({});
  let flashing = $state<Record<string, number>>({});

  function cellKey(id: string, field: string): string {
    return `${id}|${field}`;
  }
  function clearDiff(key: string): void {
    const d = diffStore[key];
    if (d) {
      if (d.settleTimer) clearTimeout(d.settleTimer);
      if (d.endTimer) clearTimeout(d.endTimer);
    }
    delete diffStore[key];
  }
  function publish(): void {
    diffs = { ...diffStore };
  }

  // Compute transitions whenever T changes relative to prevT. We diff ONLY the
  // VISIBLE rows (the window) — exactly the cells in the DOM — so the repaint is
  // delta-only and bounded by the window size, not the 100k table.
  $effect(() => {
    const curT = T;
    const fromT = prevT;
    const rows = visibleRows;
    const prevMap = prevById;
    const diffEnabled = diffOn;
    const moveMode = mode;

    if (!fromT) return;

    const nextFlash: Record<string, number> = {};
    const now = Date.now();
    for (const r of rows) {
      if (!r.loaded) continue;
      const id = r.id;
      const prevRow = prevMap.get(id);
      for (const f of fields) {
        const beforeStr = prevRow ? fmt(prevRow[f]) : "ø";
        const afterStr = fmt(r.cells[f]);
        const key = cellKey(id, f);
        if (beforeStr !== afterStr) {
          nextFlash[key] = now;
          if (diffEnabled) {
            clearDiff(key);
            const d: DiffState = { old: beforeStr, faded: false, pinT: curT.getTime() };
            if (moveMode === "play") {
              d.settleTimer = setTimeout(() => {
                if (diffStore[key]) {
                  diffStore[key].faded = true;
                  publish();
                }
              }, DWELL_MS);
              d.endTimer = setTimeout(() => {
                clearDiff(key);
                publish();
              }, DWELL_MS + FADE_MS);
            }
            diffStore[key] = d;
          } else {
            clearDiff(key);
          }
        } else if (diffStore[key] && diffStore[key].pinT !== curT.getTime()) {
          clearDiff(key);
        }
      }
    }
    flashing = nextFlash;
    publish();
  });

  $effect(() => () => {
    for (const k of Object.keys(diffStore)) clearDiff(k);
  });

  // ── Inspector hover + pin (reads the per-window index) ─────────────────────
  interface PeekTarget {
    entityId: string;
    field: string;
    x: number;
    y: number;
  }
  let peek = $state<PeekTarget | null>(null);
  let pinned = $state<{ entityId: string; field: string } | null>(null);

  function onCellHover(e: MouseEvent, id: string, field: string): void {
    peek = { entityId: id, field, x: e.clientX, y: e.clientY };
  }
  function onCellLeave(): void {
    peek = null;
  }
  function onCellClick(id: string, field: string): void {
    pinned = { entityId: id, field };
  }
  function isPinned(id: string, field: string): boolean {
    return pinned !== null && pinned.entityId === id && pinned.field === field;
  }
</script>

<div class="tablewrap" style="--rowh:{ROW_H}px; --cols:{columns.length}">
  <div class="scroller" data-large="true" bind:this={scrollerEl} style="height:{viewportH}px" onscroll={onScroll}>
    <div class="grid" style="width:max(100%, calc(var(--gutw) + var(--cols) * var(--colmin)))">
      <div class="head">
        <div class="hrow">
          <div class="hcell gut" aria-hidden="true"><span class="hlabel">Δ</span></div>
          {#each columns as c (c)}
            <div class="hcell"><span class="hlabel">{c}</span></div>
          {/each}
        </div>
      </div>
      <div class="spacer" style="height:{spacerH}px">
        {#each visibleRows as r (r.id)}
          {@const flagged = r.loaded && flaggedAt(r.id)}
          {@const cellFreq = freq.cell.get(r.id)}
          <div
            class="trow"
            class:deleted={r.state === "deleted"}
            class:resurrected={r.resurrected}
            class:flagged
            class:unloaded={!r.loaded}
            style="top:{r.abs * ROW_H}px"
          >
            <div class="td gut">
              <div class="cellpad gcount"><b>{r.loaded ? (freq.row.get(r.id) ?? 0) : ""}</b></div>
            </div>
            <div class="td idcell">
              <div class="cellpad">
                {#if flagged}<span class="warn" title="immutable-column violation">⚠</span>{/if}{r.loaded ? fmt(r.cells[keyColumn]) : r.id}{#if r.resurrected}<span class="phoenix">✦</span>{/if}
              </div>
            </div>
            {#each fields as f (f)}
              {@const key = cellKey(r.id, f)}
              {@const diff = r.loaded ? diffs[key] : undefined}
              {@const flashed = flashing[key]}
              {@const heat = r.loaded ? heatClass(cellFreq?.get(f) ?? 0) : ""}
              {@const dirc = diff ? cellDir(r.id, f, r.cells) : null}
              <div
                class="td cell {heat}"
                class:flash={flashed}
                class:pinned={isPinned(r.id, f)}
                role="gridcell"
                tabindex="-1"
                onmousemove={(e) => r.loaded && onCellHover(e, r.id, f)}
                onmouseleave={onCellLeave}
                onclick={() => r.loaded && onCellClick(r.id, f)}
                onkeydown={(e) => {
                  if ((e.key === "Enter" || e.key === " ") && r.loaded) onCellClick(r.id, f);
                }}
              >
                <div class="cellpad">
                  {#if !r.loaded}
                    <span class="skel"></span>
                  {:else if diff}
                    <span class="d-old" class:fade={diff.faded}>{diff.old}</span>
                    <span class="d-arr" class:fade={diff.faded}>→</span>
                    <span class="d-new">{fmt(r.cells[f])}</span>
                    {#if dirc && (dirc.dir === "up" || dirc.dir === "down")}
                      <span class="d-dir {dirc.dir}">{dirc.dir === "up" ? "↑" : "↓"}{dirc.deltaLabel}</span>
                    {/if}
                  {:else}
                    <span class="val">{fmt(r.cells[f])}</span>
                  {/if}
                </div>
              </div>
            {/each}
          </div>
        {/each}
      </div>
    </div>
  </div>
</div>

<Inspector {index} {schema} {T} {peek} bind:pinned />

<style>
  /* Geometry + visual encodings mirror table.svelte so the two scales look
     identical; this component only differs in its DATA path (windowed). */
  .tablewrap {
    --colmin: 140px;
    --gutw: 56px;
    margin-top: 14px;
    background: var(--card, #fff);
    border: 1px solid var(--line, #e7e9ee);
    border-radius: 12px;
    overflow: hidden;
    font-size: 13px;
    /* Height comes from the child .scroller's explicit JS-set height — not flex
       (see table.svelte for rationale). Plain block so it can't collapse it. */
    flex: none;
  }
  .grid {
    position: relative;
  }
  .hrow,
  .trow {
    display: grid;
    grid-template-columns: var(--gutw) repeat(var(--cols), minmax(0, 1fr));
    width: 100%;
  }
  .head {
    position: sticky;
    top: 0;
    z-index: 1;
    width: 100%;
    background: var(--bg-sunken, #eef0f3);
    border-bottom: 1px solid var(--line, #e7e9ee);
  }
  .hcell {
    border: none;
    border-right: 1px solid var(--line, #e7e9ee);
    background: transparent;
    color: var(--mut, #64748b);
    font-size: 10.5px;
    text-transform: uppercase;
    letter-spacing: 0.7px;
    font-weight: 600;
    font-family: inherit;
    padding: 8px 12px;
    white-space: nowrap;
    text-align: left;
    display: flex;
    align-items: center;
  }
  .hlabel {
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .scroller {
    /* Height set explicitly inline (JS-measured); not flex-filled. */
    height: 200px;
    overflow: auto;
    position: relative;
  }
  .spacer {
    position: relative;
    width: 100%;
  }
  .trow {
    position: absolute;
    left: 0;
    height: var(--rowh, 33px);
    border-bottom: 1px solid var(--line, #eef0f3);
    transition: background 0.1s;
  }
  .trow:hover .td {
    background: var(--hover, #f3f5f8);
  }
  .td {
    border-right: 1px solid var(--line, #eef0f3);
    overflow: hidden;
  }
  .cellpad {
    padding: 7px 12px;
    height: var(--rowh, 33px);
    display: flex;
    align-items: center;
    gap: 6px;
    position: relative;
    white-space: nowrap;
    box-sizing: border-box;
  }
  .val {
    font-family: var(--mono, ui-monospace, monospace);
    font-variant-numeric: tabular-nums;
    color: var(--ink, #0f172a);
  }
  .idcell {
    font-family: var(--mono, ui-monospace, monospace);
    font-weight: 600;
    color: var(--text, #334155);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }
  .idcell .phoenix {
    color: var(--add, #16a34a);
    margin-left: 5px;
    font-size: 11px;
  }
  .hcell.gut,
  .td.gut {
    text-align: center;
  }
  .td.gut .gcount {
    justify-content: center;
    font-size: 11px;
    color: var(--mut, #64748b);
    font-variant-numeric: tabular-nums;
    font-family: var(--mono, ui-monospace, monospace);
  }
  .td.gut .gcount b {
    color: var(--accent, #2563eb);
  }
  .td.cell.v1 {
    background: rgba(37, 99, 235, 0.05);
  }
  .td.cell.v2 {
    background: rgba(37, 99, 235, 0.1);
  }
  .td.cell.v3 {
    background: rgba(217, 119, 6, 0.13);
  }
  .td.cell.v4 {
    background: rgba(220, 38, 38, 0.14);
  }
  .td.cell {
    transition: background 0.2s;
    cursor: pointer;
  }
  .td.cell:hover {
    outline: 1.5px solid var(--accent, #2563eb);
    outline-offset: -1.5px;
  }
  .td.cell.pinned {
    outline: 2px solid var(--mod, #d97706);
    outline-offset: -2px;
  }
  .trow.deleted .td {
    background: repeating-linear-gradient(
      45deg,
      #fdeaea,
      #fdeaea 9px,
      #f9d8d8 9px,
      #f9d8d8 18px
    );
    color: #9a4a42;
  }
  .trow.deleted .val,
  .trow.deleted .d-new,
  .trow.deleted .d-old {
    text-decoration: line-through;
    color: #9a4a42;
  }
  .trow.deleted .idcell {
    color: var(--del, #dc2626);
  }
  .trow.resurrected {
    box-shadow: inset 3px 0 0 var(--add, #16a34a);
  }
  .trow.flagged {
    box-shadow: inset 4px 0 0 var(--del, #dc2626);
  }
  .trow.flagged:not(.deleted) .td {
    background: rgba(220, 38, 38, 0.06);
  }
  .trow.flagged.resurrected {
    box-shadow:
      inset 4px 0 0 var(--del, #dc2626),
      inset 7px 0 0 var(--add, #16a34a);
  }
  .idcell .warn {
    color: var(--del, #dc2626);
    margin-right: 5px;
    font-size: 12px;
    font-weight: 700;
  }
  .d-dir {
    margin-left: 6px;
    font-weight: 700;
    font-family: var(--mono, ui-monospace, monospace);
    font-variant-numeric: tabular-nums;
    font-size: 12px;
    white-space: nowrap;
  }
  .d-dir.up {
    color: var(--add, #2e7d4f);
  }
  .d-dir.down {
    color: var(--del, #b23b2c);
  }
  @keyframes flash {
    0% {
      background: rgba(251, 191, 36, 0.55);
    }
    100% {
      background: transparent;
    }
  }
  .td.cell.flash {
    animation: flash 1.1s ease-out;
  }
  .d-old {
    color: var(--del, #b23b2c);
    text-decoration: line-through;
    opacity: 0.85;
    display: inline-block;
    max-width: 240px;
    overflow: hidden;
    white-space: nowrap;
    vertical-align: bottom;
    transition:
      opacity 0.55s ease,
      max-width 0.55s ease,
      margin 0.55s ease;
  }
  .d-arr {
    color: var(--mut, #6c655a);
    padding: 0 5px;
    display: inline-block;
    transition:
      opacity 0.55s ease,
      padding 0.55s ease;
  }
  .d-new {
    color: var(--add, #2e7d4f);
    font-weight: 700;
    font-family: var(--mono, ui-monospace, monospace);
    font-variant-numeric: tabular-nums;
  }
  .d-old.fade {
    opacity: 0;
    max-width: 0;
    margin: 0;
  }
  .d-arr.fade {
    opacity: 0;
    padding: 0;
  }
  /* A not-yet-loaded window row: faint skeleton bar in each cell. */
  .trow.unloaded {
    opacity: 0.55;
  }
  .skel {
    display: inline-block;
    height: 9px;
    width: 60%;
    border-radius: 3px;
    background: linear-gradient(90deg, #ece8df, #f5f2ea, #ece8df);
  }
</style>
