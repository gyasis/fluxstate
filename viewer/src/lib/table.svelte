<script lang="ts">
  // File: viewer/src/lib/table.svelte  (T017 / US2 — dev-spec §2.2; US4 perf pass)
  //
  // The as-of table. Cells render the value of `(entity, field)` as-of T.
  // When a cell changes across a scrub step we render the daff transition IN-CELL:
  //   ~~old~~ → **new**   (old struck red, new green-bold) + an amber cell flash.
  // Pinned-vs-fade (dev-spec §2.2):
  //   • discrete steps (←/→, ‹/›, paused)  → PIN the old→new until the user moves on
  //   • Play/drag (mode === "play")        → linger, then fade to just the new value
  // A `⇄ diff` toggle (diffOn=false) disables diffing entirely → plain snap.
  //
  // PERFORMANCE (US4):
  //   1. INDEX: an O(E log E) `buildIndex(events)` is computed ONCE per event set
  //      (memoized on the events reference). Every reconstruction below is then a
  //      binary search over a tiny per-cell timeline — NOT an O(37k) flat scan.
  //   2. DIFF via TWO WHOLE-TABLE VIEWS: per scrub step we build
  //      viewPrev = buildMirrorViewIndexed(index, …, prevT) and
  //      viewNow  = buildMirrorViewIndexed(index, …, T) ONCE each, then the per-cell
  //      old→new is a cheap map lookup for the VISIBLE cells only — no per-cell asOf.
  //   3. VIRTUALIZATION: only the ~30–45 rows in the viewport are in the DOM
  //      (absolute-positioned over a sized spacer; recycled on scroll). Rendered
  //      node count stays ~constant regardless of total row count.
  //
  // PURE PRESENTATION: receives raw events + schema/keyColumn + current T, previous T,
  // the diff toggle, and the move `mode` as PROPS. We do NOT touch duckdb.ts here.

  import {
    buildIndex,
    decodeValue,
    rowStateIndexed,
    immutableViolations,
    isViolatingAt,
    changeDirection,
    type MirrorView,
    type ReconIndex,
    type RawChangeEvent,
    type RowLifecycleState,
    type ImmutableViolations,
    type Typed,
  } from "./reconstruct";
  import Inspector from "./inspector.svelte";

  const DELETED_FIELD = "__deleted__";
  /** Immutable (write-once) columns — a value change in any of these is a violation. */
  const IMMUTABLE_COLS = ["id", "birth_date", "cohort", "mrn"];

  // ── Props ────────────────────────────────────────────────────────────────
  interface Props {
    /** Raw change events for the row set (parent fetches via duckdb.ts). */
    events: RawChangeEvent[];
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
    /**
     * How we arrived at T:
     *   "step" → discrete (←/→ ‹/›, paused) → PIN the diff
     *   "play" → Play/loop or drag          → linger then fade
     */
    mode?: "step" | "play";
    /**
     * Optional row filter (US5): when set, only entity_ids present in this Set are
     * rendered. `null`/undefined ⇒ no filter (render all entities). The filter is a
     * STABLE row set the parent evaluates once — the table just narrows by it.
     */
    rowFilter?: Set<string> | null;
    /**
     * Bindable out: the immutable-column violations map for the current event set.
     * The parent (App.svelte) reads it to build the `flagged` filter predicate so the
     * violations are computed ONCE, here, off the same memoized index.
     */
    violations?: ImmutableViolations;
  }

  let {
    events,
    schema,
    keyColumn,
    T,
    prevT = null,
    diffOn = true,
    mode = "step",
    rowFilter = null,
    violations = $bindable(new Map()),
  }: Props = $props();

  // Linger/fade timing for play/drag (mirrors the prototype's DWELL/FADE).
  const DWELL_MS = 2600;
  const FADE_MS = 750;

  // Virtualization geometry.
  const ROW_H = 33; // px — matches .cellpad min-height + borders
  const HEAD_H = 35; // px — sticky header height (.hcell padding 9px + label + borders)
  const VIEWPORT_FALLBACK = 560; // px — used until the scroller is measured (clientHeight 0)
  const OVERSCAN = 8; // extra rows rendered above/below the viewport
  // The scroll container's height is set EXPLICITLY (JS-deterministic), not via a
  // flex-fill chain. Relying on `flex:1; min-height:0` up through App.svelte's
  // grid/flex layout was fragile — it resolved to a real height in headless
  // Chromium but COLLAPSED TO 0 in some real browsers (the table body vanished,
  // only the header showed). We instead MEASURE the scroller's top against
  // window.innerHeight and set its height directly, with a hard minimum so rows
  // always render regardless of how the ancestor layout resolves.
  const MIN_SCROLLER_H = 200; // px — never let the table collapse below ~6 rows
  let scrollerEl = $state<HTMLDivElement | undefined>(undefined);
  let viewportH = $state(0);

  // Measure + set the scroller height = (window bottom − scroller top), clamped to
  // a minimum. Re-runs on window resize and any layout change (ResizeObserver on
  // <body>) so it tracks the real viewport live. This is the single source of the
  // table's height — CSS no longer flexes it.
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

  // ── Reconstruction index (built ONCE per event set) ───────────────────────
  // Memoized on the `events` reference: the O(E log E) build runs only when the
  // parent hands a new array, NOT on every scrub / render.
  const index = $derived.by<ReconIndex>(() => buildIndex(events));

  // ── Immutable-column violations (US8 anomaly watcher) ──────────────────────
  // Computed ONCE per event set off the index: entity_id → [{field, t, from, to}].
  // The immutable columns (id/birth_date/cohort/mrn) are write-once; a value change
  // in any of them is a violation. We publish this into the bindable `violations`
  // prop so the parent's filter can use the SAME map without recomputing.
  const violationsLocal = $derived.by<ImmutableViolations>(() =>
    immutableViolations(index, IMMUTABLE_COLS),
  );
  $effect(() => {
    violations = violationsLocal;
  });

  // ── Derived table state ───────────────────────────────────────────────────
  /** Tracked field columns in schema order (key column excluded from cells). */
  const fields = $derived(Object.keys(schema).filter((c) => c !== keyColumn));
  const columns = $derived([keyColumn, ...fields]);

  // ── Per-cell + per-row change frequency (heat-tint + Δ gutter) ─────────────
  // Computed ONCE per event set off the index (entity_id → field → count for
  // heat buckets; entity_id → Σ field changes for the gutter). __deleted__
  // markers are NOT counted as cell changes (they're lifecycle, not a value).
  interface FreqMaps {
    cell: Map<string, Map<string, number>>; // entity → field → #changes
    row: Map<string, number>; // entity → Σ field changes
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

  /** Heat bucket class for a per-cell change count (mirrors prototype volClass). */
  function heatClass(n: number): "" | "v1" | "v2" | "v3" | "v4" {
    if (n >= 8) return "v4";
    if (n >= 5) return "v3";
    if (n >= 3) return "v2";
    if (n >= 1) return "v1";
    return "";
  }

  // ── Temporal view as-of T — INCLUDES deleted + unborn rows ─────────────────
  // dev-spec §2.4: deleted rows stay VISIBLE (struck, striped) so the deletion
  // is seen in the temporal view; resurrected rows get the green ✦ edge; unborn
  // rows are omitted until born (the prototype omits not-yet-born rows entirely).
  // buildMirrorViewIndexed OMITS deleted entities, so we reconstruct our own view
  // here off the SAME index (binary-search per cell — no flat scan, no perf
  // regression): for every entity born at T we emit a row carrying its lifecycle
  // state + resurrected flag and its last-known cell values (active OR deleted).
  interface TemporalRow {
    id: string;
    cells: Record<string, Typed>;
    state: RowLifecycleState["state"]; // active | deleted (unborn omitted)
    resurrected: boolean;
  }
  interface TemporalView {
    rows: TemporalRow[];
  }

  /** Last-known value for (entity, field) at ≤ T via binary search over the
   *  index's already-sorted per-cell history. O(log k); no flat scan. */
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

  function buildTemporalView(at: Date): TemporalView {
    const tMs = at.getTime();
    const rows: TemporalRow[] = [];
    for (const eid of index.order) {
      if (rowFilter && !rowFilter.has(eid)) continue; // US5 filter narrows the row set
      const st = rowStateIndexed(index, eid, at);
      if (st.state === "unborn") continue; // not yet in the dataset → omit
      const cells: Record<string, Typed> = {};
      cells[keyColumn] = decodeValue(eid, schema[keyColumn]);
      for (const f of fields) {
        cells[f] = lastLE(eid, f, tMs);
      }
      rows.push({ id: eid, cells, state: st.state, resurrected: st.resurrected });
    }
    return { rows };
  }

  const viewT = $derived(buildTemporalView(T));

  /** MirrorView-shaped projection (rows = cell maps) for sorting/diff reuse. */
  const viewRaw = $derived<MirrorView>({
    columns: [keyColumn, ...fields],
    rows: viewT.rows.map((r) => r.cells),
  });

  /** entity_id → lifecycle meta (state + resurrected) for the visible render. */
  const metaById = $derived.by(() => {
    const m = new Map<string, { state: TemporalRow["state"]; resurrected: boolean }>();
    for (const r of viewT.rows) m.set(r.id, { state: r.state, resurrected: r.resurrected });
    return m;
  });

  /** Is `entityId` immutable-violating at-or-before the current T? Drives the red flag. */
  function flaggedAt(entityId: string): boolean {
    return isViolatingAt(violationsLocal, entityId, T);
  }

  /** Format a numeric delta with a sign (e.g. +0.13 / -2). */
  function fmtDelta(delta: number): string {
    const sign = delta >= 0 ? "+" : "";
    // Trim trailing zeros from floats; keep ints as-is.
    const s = Number.isInteger(delta) ? String(delta) : String(Number(delta.toFixed(4)));
    return ` ${sign}${s}`;
  }

  /**
   * Direction + signed-delta label for a cell that just transitioned on scrub.
   * Reads the RAW typed prev value (from the prevT view) and the RAW typed new
   * value (from the current sorted view) — not the formatted diff strings — so
   * numeric ↑/↓ + delta is exact. Non-numeric transitions return dir "na".
   */
  function cellDir(
    id: string,
    field: string,
  ): { dir: "up" | "down" | "same" | "na"; deltaLabel: string } | null {
    const prevRow = prevById.get(id);
    const curRow = byId.get(id);
    if (!prevRow || !curRow) return null;
    const cd = changeDirection(prevRow[field] ?? null, curRow[field] ?? null);
    return {
      dir: cd.dir,
      deltaLabel: cd.delta !== null ? fmtDelta(cd.delta) : "",
    };
  }

  // ── Column sorting ──────────────────────────────────────────────────────
  // Click a header to sort by that column: natural → asc → desc → natural.
  // The FULL row set is sorted once per (sort change | T change); the virtual
  // window then slices the already-sorted rows — sorting is NOT per-scroll.
  let sortCol = $state<string | null>(null);
  let sortDir = $state<"asc" | "desc" | null>(null);

  function onSort(col: string): void {
    if (sortCol !== col) {
      sortCol = col;
      sortDir = "asc";
    } else if (sortDir === "asc") {
      sortDir = "desc";
    } else {
      // desc → off (natural order)
      sortCol = null;
      sortDir = null;
    }
  }

  /**
   * Type-aware comparator over decoded `Typed` cell values. Numbers compare
   * numerically, Dates chronologically, booleans false<true, strings lexically.
   * Nulls/undefined ALWAYS sort last, in both asc and desc directions.
   */
  function compareTyped(a: Typed | undefined, b: Typed | undefined): number {
    const aNull = a === null || a === undefined;
    const bNull = b === null || b === undefined;
    if (aNull && bNull) return 0;
    if (aNull) return 1; // nulls last
    if (bNull) return -1;
    if (a instanceof Date && b instanceof Date) {
      return a.getTime() - b.getTime();
    }
    if (typeof a === "number" && typeof b === "number") {
      return a - b;
    }
    if (typeof a === "boolean" && typeof b === "boolean") {
      return a === b ? 0 : a ? 1 : -1;
    }
    // Mixed or string: fall back to lexical on the string form.
    const as = String(a);
    const bs = String(b);
    return as < bs ? -1 : as > bs ? 1 : 0;
  }

  /** Sorted whole-table view. Re-sorts only when (viewRaw | sortCol | sortDir)
   *  change — once per scrub step or sort toggle, never per scroll. */
  const view = $derived.by<MirrorView>(() => {
    if (!sortCol || !sortDir) return viewRaw;
    const col = sortCol;
    const sign = sortDir === "asc" ? 1 : -1;
    const sorted = [...viewRaw.rows].sort(
      (ra, rb) => sign * compareTyped(ra[col], rb[col]),
    );
    return { columns: viewRaw.columns, rows: sorted };
  });
  /** Whole-table view as-of prevT (for diffing) — built ONCE, not per cell.
   *  Uses the SAME temporal view (includes deleted rows) so a row that flips
   *  active↔deleted across a step still has its prior values for the diff. */
  const viewPrev = $derived<MirrorView | null>(
    prevT
      ? { columns: [keyColumn, ...fields], rows: buildTemporalView(prevT).rows.map((r) => r.cells) }
      : null,
  );

  /** prevT view as a Map<entityId, row> for O(1) old-value lookup. */
  const prevById = $derived.by(() => {
    const m = new Map<string, Record<string, Typed>>();
    if (viewPrev) for (const r of viewPrev.rows) m.set(rowKeyId(r), r);
    return m;
  });

  /** Current (as-of-T) view as a Map<entityId, row> for O(1) new-value lookup
   *  (raw typed values — used by the direction indicator). */
  const byId = $derived.by(() => {
    const m = new Map<string, Record<string, Typed>>();
    for (const r of view.rows) m.set(rowKeyId(r), r);
    return m;
  });

  /** Decode an entity_id back to its typed key (string id form). */
  function rowKeyId(row: Record<string, Typed>): string {
    const k = row[keyColumn];
    return k === null ? "" : String(k);
  }

  /** Format a typed cell value for display (null → ø sentinel). */
  function fmt(v: Typed | undefined): string {
    if (v === undefined || v === null) return "ø";
    if (v instanceof Date) return v.toISOString().slice(0, 10);
    if (typeof v === "boolean") return v ? "true" : "false";
    return String(v);
  }

  // ── Virtual window ────────────────────────────────────────────────────────
  let scrollTop = $state(0);
  const totalRows = $derived(view.rows.length);
  const spacerH = $derived(totalRows * ROW_H);
  // The header is sticky inside the SAME scroll container and the body spacer
  // sits BELOW it, so the spacer's content offset is (scrollTop - HEAD_H).
  // Clamp ≥0 so rows aren't dropped while the header is still fully in view.
  const firstVisible = $derived(
    Math.max(0, Math.floor((scrollTop - HEAD_H) / ROW_H) - OVERSCAN),
  );
  const visibleCount = $derived(
    Math.ceil((viewportH || VIEWPORT_FALLBACK) / ROW_H) + OVERSCAN * 2,
  );
  /** The slice of rows currently mounted, with their absolute index. */
  const windowRows = $derived(
    view.rows
      .slice(firstVisible, firstVisible + visibleCount)
      .map((row, k) => ({ row, abs: firstVisible + k })),
  );

  function onScroll(e: Event): void {
    scrollTop = (e.currentTarget as HTMLElement).scrollTop;
  }

  // ── Hover + pin → Inspector (dev-spec §2.3) ────────────────────────────────
  // Hover sets a peek target (entity+field+cursor); the Inspector reads the
  // index for the last-3 popover. Click pins a cell's full history (bindable so
  // the close control clears it and we can outline the pinned cell here).
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

  // ── Diff state, keyed by cell ─────────────────────────────────────────────
  // One entry per cell currently showing an old→new transition. Pinned entries
  // persist until T moves past `pinT`; fading entries auto-settle via timers.
  // Keyed `${entityId}|${field}` so the virtualized render reuses it on recycle.
  interface DiffState {
    old: string;
    faded: boolean;
    pinT: number;
    settleTimer?: ReturnType<typeof setTimeout>;
    endTimer?: ReturnType<typeof setTimeout>;
  }
  // CRITICAL: the diff bookkeeping lives in PLAIN (non-reactive) objects so the
  // diff $effect can freely read+write them without forming a reactive
  // read/write cycle (which, over 1000 entities, would re-trigger the effect
  // repeatedly and freeze the thread). We mutate `diffStore`/`flashStore` during
  // computation, then publish ONE immutable snapshot into the reactive `diffs`/
  // `flashing` $state for the template to render.
  const diffStore: Record<string, DiffState> = {};
  let diffs = $state<Record<string, DiffState>>({});
  let flashing = $state<Record<string, number>>({});

  function cellKey(id: string, field: string): string {
    return `${id}|${field}`;
  }

  /** Drop a diff from the plain store + cancel its timers (no reactive read). */
  function clearDiff(key: string): void {
    const d = diffStore[key];
    if (d) {
      if (d.settleTimer) clearTimeout(d.settleTimer);
      if (d.endTimer) clearTimeout(d.endTimer);
    }
    delete diffStore[key];
  }

  /** Publish the current plain store into the reactive snapshot for rendering. */
  function publish(): void {
    diffs = { ...diffStore };
  }

  // Compute transitions whenever T changes relative to prevT — using the TWO
  // whole-table views (viewNow + viewPrev) instead of per-cell asOf. We diff
  // EVERY entity's cells (so a pinned diff is correct even when its row later
  // scrolls into view), but each comparison is now a cheap map lookup AND all
  // diff bookkeeping is on the non-reactive `diffStore` — so this effect depends
  // ONLY on (T, prevT, view, prevById, diffOn, mode), never on its own writes.
  $effect(() => {
    const curT = T;
    const fromT = prevT;
    const nowRows = view.rows;
    const prevMap = prevById;
    const diffEnabled = diffOn;
    const moveMode = mode;

    if (!fromT) return; // first render — nothing to diff against.

    const nextFlash: Record<string, number> = {};
    const now = Date.now();
    for (const row of nowRows) {
      const id = rowKeyId(row);
      const prevRow = prevMap.get(id);
      for (const f of fields) {
        const beforeStr = prevRow ? fmt(prevRow[f]) : "ø";
        const afterStr = fmt(row[f]);
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

  // Tear down any pending timers on unmount.
  $effect(() => () => {
    for (const k of Object.keys(diffStore)) clearDiff(k);
  });
</script>

<div class="tablewrap" style="--rowh:{ROW_H}px; --cols:{columns.length}">
  <div class="scroller" bind:this={scrollerEl} style="height:{viewportH}px" onscroll={onScroll}>
    <div class="grid" style="width:max(100%, calc(var(--gutw) + var(--cols) * var(--colmin)))">
    <div class="head">
      <div class="hrow">
        <div class="hcell gut" aria-hidden="true"><span class="hlabel">Δ</span></div>
        {#each columns as c (c)}
          <button
            type="button"
            class="hcell"
            class:sorted={sortCol === c}
            aria-label={`Sort by ${c}${
              sortCol === c
                ? sortDir === "asc"
                  ? " (ascending)"
                  : " (descending)"
                : ""
            }`}
            onclick={() => onSort(c)}
          >
            <span class="hlabel">{c}</span>
            <span class="sortind"
              >{sortCol === c ? (sortDir === "asc" ? "▲" : "▼") : ""}</span
            >
          </button>
        {/each}
      </div>
    </div>
    <div class="spacer" style="height:{spacerH}px">
      {#each windowRows as { row, abs } (rowKeyId(row))}
        {@const id = rowKeyId(row)}
        {@const meta = metaById.get(id)}
        {@const cellFreq = freq.cell.get(id)}
        {@const flagged = flaggedAt(id)}
        <div
          class="trow"
          class:deleted={meta?.state === "deleted"}
          class:resurrected={meta?.resurrected}
          class:flagged
          style="top:{abs * ROW_H}px"
        >
          <div class="td gut">
            <div class="cellpad gcount"><b>{freq.row.get(id) ?? 0}</b></div>
          </div>
          <div class="td idcell">
            <div class="cellpad">
              <span class="idval">{fmt(row[keyColumn])}</span>
              {#if flagged}<span class="pill pill-flag" title="immutable-column violation"
                  >⚠ violation</span
                >{/if}
              {#if meta?.state === "deleted"}<span class="pill pill-del">deleted</span>{/if}
              {#if meta?.resurrected}<span class="pill pill-res">✦ resurrected</span>{/if}
            </div>
          </div>
          {#each fields as f (f)}
            {@const key = cellKey(id, f)}
            {@const diff = diffs[key]}
            {@const flashed = flashing[key]}
            {@const heat = heatClass(cellFreq?.get(f) ?? 0)}
            {@const dirc = diff ? cellDir(id, f) : null}
            <div
              class="td cell {heat}"
              class:flash={flashed}
              class:pinned={isPinned(id, f)}
              role="gridcell"
              tabindex="-1"
              onmousemove={(e) => onCellHover(e, id, f)}
              onmouseleave={onCellLeave}
              onclick={() => onCellClick(id, f)}
              onkeydown={(e) => {
                if (e.key === "Enter" || e.key === " ") onCellClick(id, f);
              }}
            >
              <div class="cellpad">
                {#if diff}
                  <span class="d-old" class:fade={diff.faded}>{diff.old}</span>
                  <span class="d-arr" class:fade={diff.faded}>→</span>
                  <span class="d-new">{fmt(row[f])}</span>
                  {#if dirc && (dirc.dir === "up" || dirc.dir === "down")}
                    <span class="d-dir {dirc.dir}"
                      >{dirc.dir === "up" ? "↑" : "↓"}{dirc.deltaLabel}</span
                    >
                  {/if}
                {:else}
                  <span class="val">{fmt(row[f])}</span>
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
  .tablewrap {
    --colmin: 140px; /* per-column minimum track width before horizontal scroll */
    --gutw: 56px; /* fixed Δ-gutter column width */
    margin-top: 14px;
    background: var(--card, #fff);
    border: 1px solid var(--line, #e7e9ee);
    border-radius: 12px;
    overflow: hidden; /* clip the rounded corners; scroll lives in .scroller */
    font-size: 13px;
    /* Height is NO LONGER flex-driven (that collapsed to 0 in some real
       browsers). The wrap sizes to its child .scroller, whose height is set
       explicitly in JS (window bottom − scroller top, min 200px). The wrap is a
       plain block so it can never collapse the scroller. */
    flex: none;
  }
  /* The .grid is ONE fixed-width content box (width = max(100% of the scroller,
     cols * colmin), set inline). The sticky header and every virtualized row
     are width:100% of THIS box and use the IDENTICAL explicit grid template
     `repeat(var(--cols), 1fr)`. Because 1fr resolves against the same fixed box
     width with the same track count — and is CONTENT-INDEPENDENT (no
     grid-auto-columns / min-content sizing) — header and body columns line up
     EXACTLY, regardless of which rows the sort/scrub put in view. When
     cols*colmin > viewport the box overflows → horizontal scroll (no clipping);
     when it fits, 1fr fills the full width (no empty whitespace). */
  .grid {
    position: relative;
  }
  /* header + rows share the SAME explicit grid template → identical track
     widths, independent of cell content. */
  .hrow,
  .trow {
    display: grid;
    /* A fixed-width Δ gutter, then minmax(0, 1fr) data tracks: the 0 min makes
       each data track purely a fraction of the REMAINING .grid box width —
       content can't expand a track — so header and body tracks are byte-for-byte
       equal regardless of cell text, and the gutter aligns identically. */
    grid-template-columns: var(--gutw) repeat(var(--cols), minmax(0, 1fr));
    width: 100%;
  }
  .head {
    /* sticky inside .scroller: pins to the top on vertical scroll while sharing
       the SINGLE horizontal scroll + content width with the body → exact
       column alignment. */
    position: sticky;
    top: 0;
    z-index: 1;
    width: 100%;
    background: var(--bg-sunken, #eef0f3);
    border-bottom: 1px solid var(--line, #e7e9ee);
  }
  .hcell {
    /* now a real <button> — reset native chrome, keep the grid cell look. */
    appearance: none;
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
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 6px;
    user-select: none;
  }
  .hcell:hover {
    background: var(--hover, #f3f5f8);
    color: var(--ink, #0f172a);
  }
  .hcell.sorted {
    background: var(--accent-bg, #eff5ff);
    color: var(--accent, #2563eb);
  }
  .hlabel {
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .sortind {
    font-size: 9px;
    line-height: 1;
    color: var(--accent, #2563eb);
  }
  .scroller {
    /* THE single scroll container — both axes live here so the sticky header
       and the virtualized body share one content width AND one horizontal
       offset. Its HEIGHT is set EXPLICITLY inline (JS-measured: window bottom −
       scroller top, min 200px) — NOT flex-filled — so the body can never
       collapse to 0 the way the flex chain did in some real browsers. */
    height: 200px; /* fallback before JS measures; overridden by inline style */
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
    /* width:100% of the .grid box (set on .hrow/.trow shared rule) so every
       row's columns line up with the sticky header across the full horizontal
       scroll range. */
    height: var(--rowh, 33px);
    border-bottom: 1px solid var(--line, #eef0f3);
    transition: background 0.1s;
  }
  /* Subtle whole-row hover highlight (reference look). Sits under the per-cell
     outline; lifecycle washes (deleted/flagged) still win via their own .td bg. */
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

  /* ── Lifecycle / flag pills (clean minimal badges) ─────────────────────── */
  .pill {
    display: inline-flex;
    align-items: center;
    font-size: 9.5px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.4px;
    padding: 1px 7px;
    border-radius: var(--r-pill, 999px);
    line-height: 1.5;
    white-space: nowrap;
    border: 1px solid transparent;
  }
  .pill-flag {
    color: var(--del, #dc2626);
    background: var(--del-bg, #fdeaea);
    border-color: var(--del-border, #f6c6c6);
  }
  .pill-del {
    color: var(--mod, #d97706);
    background: var(--mod-bg, #fdf2e3);
    border-color: var(--mod-border, #f3d9a8);
  }
  .pill-res {
    color: var(--add, #16a34a);
    background: var(--add-bg, #e7f6ec);
    border-color: var(--add-border, #bbe6c8);
  }

  /* ── Δ gutter (per-row change count) ───────────────────────────────────── */
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

  /* ── Heat-tint cell background by per-cell change frequency (v1..v4) ────── */
  /* Restrained blue→amber ramp: quiet cells barely tinted, busy cells warmer. */
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

  /* ── Row lifecycle encodings (dev-spec §2.4) ───────────────────────────── */
  /* Deleted at T → red diagonal stripe + strikethrough (row stays VISIBLE so
     the deletion is seen in the temporal view). The stripe wins over heat-tint. */
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
  /* Resurrected → green left-edge accent (the ✦ is rendered in the id cell). */
  .trow.resurrected {
    box-shadow: inset 3px 0 0 var(--add, #16a34a);
  }
  /* Immutable-violation (US8) → SOLID red left-edge bar + faint red wash + ⚠.
     Deliberately distinct from:
       • deleted (red 45° DIAGONAL STRIPE pattern + strikethrough)
       • resurrected (green left edge + ✦)
     The flag uses a 4px SOLID red edge (not a stripe, not green) and a flat
     red-tint background — so a flagged row reads clearly even when it is ALSO
     active/deleted/resurrected (the encodings layer rather than clash). */
  .trow.flagged {
    box-shadow: inset 4px 0 0 var(--del, #dc2626);
  }
  /* Flagged-only background wash. The deleted DIAGONAL stripe (set on .deleted
     .td above, later in source / more specific) still wins for a flagged+deleted
     row, so the stripe + the solid red edge coexist without the wash fighting it. */
  .trow.flagged:not(.deleted) .td {
    background: rgba(178, 59, 44, 0.06);
  }
  /* If a flagged row is also resurrected, layer BOTH edge accents (red outer +
     green inner) so neither signal is lost. */
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
  /* Direction indicator on a numeric diff: green ↑ for increase, red ↓ for
     decrease, with the signed delta. */
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
  /* daff transition: old struck red, new green-bold. */
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
</style>
