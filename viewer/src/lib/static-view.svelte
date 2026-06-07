<script lang="ts">
  // File: viewer/src/lib/static-view.svelte  (T029 — dev-spec §2.5)
  //
  // The NON-INTERACTIVE, print-ready audit render. Hand this to a compliance
  // officer: a full as-of-now table where every cell shows its LATEST value with
  // a GHOST subtext (the prior value + a ↑/↓ direction arrow for numerics), a
  // leading LIFECYCLE SPARK-PATH column (tiny inline SVG summarizing each row's
  // active/deleted/resurrected trail + change density), and AUTO-CALLOUTS on the
  // most volatile rows and the immutable-violation rows.
  //
  // PERFORMANCE (T029 perf pass): the audit could have 1000 (eventually 100k)
  // rows. We DON'T render them all:
  //   1. VIRTUALIZATION — only the ~30–50 rows in the viewport (+ overscan) are
  //      in the DOM, absolute-positioned over a sized spacer; recycled on scroll.
  //      Rendered node count stays ~constant regardless of total row count.
  //   2. LAZY SHAPING — the heavy per-row shaping (decode latest+ghost+dir, build
  //      the spark-path) runs ONLY for the visible window, via the plan's
  //      `shapeRow`. The cheap `prepareStaticView` does just row-order + counts +
  //      callouts up front. Scrolling stays smooth.
  //   3. PRINT RENDER-ALL — virtualization renders only the window, but the print
  //      audit must emit EVERY row (US6/T028). `beforeprint`/`afterprint` flip a
  //      `printing` flag that swaps the virtual window for a render-all pass so
  //      `@media print` still produces the full multi-page audit.
  //
  // All data shaping is in the PURE `static-view.ts` (unit-tested in T028); this
  // file only renders the shapes + handles virtualization/hover/print.
  //
  // Styling matches the app's cool design tokens (app.css): card sheet, blue
  // accent, daff add/del/mod cues, grouped callouts, and a `@media print` block
  // that strips chrome to black-on-white.

  import {
    buildIndex,
    immutableViolations,
    getTimeline,
    changeDirection,
    type RawChangeEvent,
    type ImmutableViolations,
    type Typed,
  } from "./reconstruct";
  import {
    prepareStaticView,
    type StaticRow,
    type SparkSegment,
  } from "./static-view";

  /** Immutable (write-once) columns — a value change in any is a violation (US8). */
  const IMMUTABLE_COLS = ["id", "birth_date", "cohort", "mrn"];
  /** Render cap for the audit table (counted-but-not-rendered beyond this). */
  const MAX_ROWS = 1000;

  // Virtualization geometry.
  const ROW_H = 44; // px — a 2-line audit row (latest + ghost) + borders
  const HEAD_H = 30; // px — sticky header height
  const VIEWPORT_FALLBACK = 560; // px — used until the .tablebox is measured (clientHeight 0)
  const OVERSCAN = 8; // extra rows rendered above/below the viewport
  // Real scroll-container (.tablebox) height — measured via bind:clientHeight so
  // the audit window adapts to the actual viewport instead of a fixed 600px cap.
  let viewportH = $state(0);

  interface Props {
    events: RawChangeEvent[];
    schema: Record<string, string>;
    keyColumn: string;
    storeName?: string;
    /** As-of instant; "now" (default) renders the current state. */
    T?: Date | "now";
  }
  let { events, schema, keyColumn, storeName = "", T = "now" }: Props = $props();

  const index = $derived.by(() => buildIndex(events));
  const violations = $derived.by<ImmutableViolations>(() =>
    immutableViolations(index, IMMUTABLE_COLS),
  );
  // The CHEAP plan: row order (capped) + total + callouts + a lazy shapeRow.
  const plan = $derived.by(() =>
    prepareStaticView(index, schema, keyColumn, violations, T, MAX_ROWS),
  );

  // ── Virtual window ─────────────────────────────────────────────────────────
  let scrollTop = $state(0);
  let printing = $state(false);
  const totalRendered = $derived(plan.order.length);
  const spacerH = $derived(totalRendered * ROW_H);
  // The header is sticky inside the SAME scroll container; the body spacer sits
  // BELOW it, so the spacer content offset is (scrollTop - HEAD_H). Clamp ≥0.
  const firstVisible = $derived(
    Math.max(0, Math.floor((scrollTop - HEAD_H) / ROW_H) - OVERSCAN),
  );
  const visibleCount = $derived(
    Math.ceil((viewportH || VIEWPORT_FALLBACK) / ROW_H) + OVERSCAN * 2,
  );

  /**
   * The entity ids currently mounted, with their absolute row index. When
   * `printing` we render ALL planned rows (print needs every row); otherwise
   * just the viewport window. Each id is shaped LAZILY here via plan.shapeRow —
   * so only the rendered rows pay the per-row decode cost.
   */
  const windowRows = $derived.by<{ row: StaticRow; abs: number }[]>(() => {
    const ids = printing
      ? plan.order
      : plan.order.slice(firstVisible, firstVisible + visibleCount);
    const base = printing ? 0 : firstVisible;
    return ids.map((id, k) => ({ row: plan.shapeRow(id), abs: base + k }));
  });

  function onScroll(e: Event): void {
    scrollTop = (e.currentTarget as HTMLElement).scrollTop;
  }

  // Print render-all: flip `printing` around the print so every row is in the
  // DOM for the paginated output, then flip back to the virtual window. We
  // listen to BOTH signals so it's robust:
  //   • beforeprint/afterprint — fired by the browser's real Ctrl+P / print dialog;
  //   • matchMedia("print") change — fired when print MEDIA is toggled (covers
  //     headless `page.pdf()` / print-emulation paths that DON'T fire beforeprint).
  $effect(() => {
    if (typeof window === "undefined") return;
    const on = () => (printing = true);
    const off = () => (printing = false);
    window.addEventListener("beforeprint", on);
    window.addEventListener("afterprint", off);
    const mql = window.matchMedia?.("print");
    const onMql = (e: MediaQueryListEvent) => (printing = e.matches);
    mql?.addEventListener?.("change", onMql);
    return () => {
      window.removeEventListener("beforeprint", on);
      window.removeEventListener("afterprint", off);
      mql?.removeEventListener?.("change", onMql);
    };
  });

  /** Format a typed value for display (null → ø). */
  function fmt(v: Typed | null | undefined): string {
    if (v === undefined || v === null) return "ø";
    if (v instanceof Date) return v.toISOString().slice(0, 10);
    if (typeof v === "boolean") return v ? "true" : "false";
    return String(v);
  }

  /** Direction glyph for a cell's prior→latest transition. */
  function arrow(dir: StaticRow["cells"][number]["dir"]): string {
    if (!dir) return "";
    if (dir.dir === "up") return "↑";
    if (dir.dir === "down") return "↓";
    return "";
  }

  // ── Spark-path geometry ────────────────────────────────────────────────────
  const SPARK_W = 78;
  const SPARK_H = 16;
  const segColor: Record<SparkSegment["kind"], string> = {
    active: "var(--add)",
    deleted: "var(--del)",
    resurrected: "var(--accent)",
  };

  /** Lay spark segments left→right proportional to their event span. */
  function sparkBars(row: StaticRow): {
    x: number;
    w: number;
    kind: SparkSegment["kind"];
  }[] {
    const segs = row.spark;
    if (!segs.length) return [];
    const total = segs[segs.length - 1].to - segs[0].from + 1;
    const unit = SPARK_W / Math.max(1, total);
    return segs.map((s) => ({
      x: (s.from - segs[0].from) * unit,
      w: Math.max(2, (s.to - s.from + 1) * unit - 1),
      kind: s.kind,
    }));
  }

  // ── Hover → full change history (LAZY, per-cell retrieval) ──────────────────
  // dev-spec §2.3: hover = retrieval. On hovering a data cell we read the cell's
  // FULL timeline ON DEMAND (getTimeline over the same indexed history the
  // interactive inspector uses) and render every {date, value} with the ↑/↓
  // direction between steps. Nothing is pre-rendered for un-hovered cells.
  interface HoverTarget {
    entityId: string;
    field: string;
    x: number;
    y: number;
  }
  interface HistStep {
    date: string;
    value: string;
    dir: "up" | "down" | "" ;
    deltaLabel: string;
  }
  let hover = $state<HoverTarget | null>(null);

  function fmtDate(d: Date): string {
    return d.toISOString().slice(0, 10);
  }
  function fmtDelta(delta: number): string {
    const sign = delta >= 0 ? "+" : "";
    const s = Number.isInteger(delta) ? String(delta) : String(Number(delta.toFixed(4)));
    return `${sign}${s}`;
  }

  /** Shape the hovered cell's full change history (lazy — only on hover). */
  const hoverHist = $derived.by(() => {
    const h = hover;
    if (!h) return null;
    const tl = getTimeline(events, h.entityId, h.field);
    const steps: HistStep[] = tl.map((p, i) => {
      let dir: "up" | "down" | "" = "";
      let deltaLabel = "";
      if (i > 0) {
        const cd = changeDirection(tl[i - 1].value, p.value);
        if ((cd.dir === "up" || cd.dir === "down")) {
          dir = cd.dir;
          if (cd.delta !== null) deltaLabel = fmtDelta(cd.delta);
        }
      }
      return { date: fmtDate(p.date), value: fmt(p.value), dir, deltaLabel };
    });
    // Find the violation (if any) for this immutable cell.
    const viol = violations.get(h.entityId)?.find((v) => v.field === h.field);
    return {
      title: `${h.entityId} · ${h.field}`,
      // Newest first reads better in a peek.
      steps: steps.reverse(),
      immutable: IMMUTABLE_COLS.includes(h.field),
      writeOnce: steps.length <= 1,
      violation: viol
        ? `illegal change ${fmt(viol.from)} → ${fmt(viol.to)} on ${fmtDate(viol.t)}`
        : null,
    };
  });

  /** Popover placement: nudge away from the right/bottom edges. */
  const hoverPos = $derived.by(() => {
    const h = hover;
    if (!h) return { left: 0, top: 0 };
    let x = h.x + 14;
    let y = h.y + 14;
    if (typeof window !== "undefined") {
      if (x + 270 > window.innerWidth) x = h.x - 270;
      if (y + 220 > window.innerHeight) y = h.y - 220;
    }
    return { left: Math.max(8, x), top: Math.max(8, y) };
  });

  function onCellEnter(e: MouseEvent, id: string, field: string): void {
    hover = { entityId: id, field, x: e.clientX, y: e.clientY };
  }
  function onCellMove(e: MouseEvent): void {
    if (hover) {
      hover = { ...hover, x: e.clientX, y: e.clientY };
    }
  }
  function onCellLeave(): void {
    hover = null;
  }
</script>

<div class="sheet" class:printing>
  <header class="audit-head">
    <h1>FluxState · Audit Render</h1>
    <p class="sub">
      {#if storeName}<code>{storeName}.flux</code> · {/if}as-of
      {T === "now" ? "now (latest snapshot)" : T.toISOString().slice(0, 19) + "Z"} ·
      {plan.totalRows.toLocaleString()} rows · self-contained, hand to a compliance officer
    </p>
    <div class="rule"></div>
  </header>

  <!-- ── Auto-callouts ─────────────────────────────────────────────────── -->
  {#if plan.callouts.length}
    {@const violationCallouts = plan.callouts.filter((c) => c.kind === "violation")}
    {@const volatiles = plan.callouts.filter((c) => c.kind === "volatile")}
    <h2>Audit callouts</h2>
    {#if violationCallouts.length}
      <div class="callout-group">
        <span class="cg-label viol">⚠ Immutable violations · {violationCallouts.length}</span>
        <div class="callouts">
          {#each violationCallouts as c (c.kind + c.entityId)}
            <div class="callout violation">
              <span class="ctag">{keyColumn} {c.entityId}</span>
              <b>{c.detail}</b>
            </div>
          {/each}
        </div>
      </div>
    {/if}
    {#if volatiles.length}
      <div class="callout-group">
        <span class="cg-label vol">▲ High change density · {volatiles.length}</span>
        <div class="callouts">
          {#each volatiles as c (c.kind + c.entityId)}
            <div class="callout volatile">
              <span class="ctag">{keyColumn} {c.entityId}</span>
              <b>{c.detail}</b>
            </div>
          {/each}
        </div>
      </div>
    {/if}
  {/if}

  <h2>Table — {keyColumn} keyed · latest value + prior ghost</h2>
  {#if plan.capped}
    <p class="note">
      Showing the first {plan.order.length.toLocaleString()} of
      {plan.totalRows.toLocaleString()} rows (render cap).
    </p>
  {/if}

  <div class="legend">
    <span class="lg"><span class="sw spark"></span> lifecycle spark-path (active / deleted / resurrected)</span>
    <span class="lg"><span class="ghost-ex">ghost</span> prior value <span class="up">↑</span>/<span class="down">↓</span></span>
    <span class="lg"><span class="sw del"></span> deleted at T</span>
    <span class="lg"><span class="sw flag"></span> immutable violation</span>
    <span class="lg hovertip">hover a cell → full change history</span>
  </div>

  <div
    class="tablebox"
    style="--rowh:{ROW_H}px; --cols:{plan.columns.length}"
    bind:clientHeight={viewportH}
    onscroll={onScroll}
  >
    <div class="grid">
      <!-- Sticky header row. -->
      <div class="head">
        <div class="hrow">
          <div class="hcell lifecol">lifecycle</div>
          <div class="hcell gut">Δ</div>
          {#each plan.columns as c (c)}
            <div class="hcell">{c}</div>
          {/each}
        </div>
      </div>

      <!-- Virtualized body: a sized spacer + absolute-positioned visible rows.
           When printing we render ALL rows (no absolute positioning so the print
           flow paginates naturally). -->
      <div class="spacer" class:flow={printing} style={printing ? "" : `height:${spacerH}px`}>
        {#each windowRows as { row, abs } (row.id)}
          <div
            class="trow"
            class:r-del={row.state === "deleted"}
            class:r-res={row.resurrected}
            class:r-flag={row.flagged}
            style={printing ? "" : `top:${abs * ROW_H}px`}
            data-entity={row.id}
          >
            <div class="td lifecol">
              <svg
                width={SPARK_W}
                height={SPARK_H}
                viewBox={`0 0 ${SPARK_W} ${SPARK_H}`}
                class="spark"
                role="img"
                aria-label={`lifecycle ${row.state}${row.resurrected ? " (resurrected)" : ""}`}
              >
                <line x1="0" y1={SPARK_H / 2} x2={SPARK_W} y2={SPARK_H / 2} class="spark-base" />
                {#each sparkBars(row) as b (b.x)}
                  <rect
                    x={b.x}
                    y={SPARK_H / 2 - 3}
                    width={b.w}
                    height="6"
                    rx="1.5"
                    fill={segColor[b.kind]}
                  />
                {/each}
              </svg>
            </div>
            <div class="td gut" data-d={row.changeCount}>{row.changeCount}</div>
            <div class="td keycell">
              {#if row.flagged}<span class="warn" title="immutable-column violation">⚠</span>{/if}{fmt(
                row.key,
              )}{#if row.resurrected}<span class="phoenix" title="resurrected">✦</span>{/if}
            </div>
            {#each row.cells as cell (cell.field)}
              <div
                class="td cell"
                class:has-ghost={cell.ghost !== null}
                role="gridcell"
                tabindex="-1"
                onmouseenter={(e) => onCellEnter(e, row.id, cell.field)}
                onmousemove={onCellMove}
                onmouseleave={onCellLeave}
              >
                <span class="latest">{fmt(cell.value)}</span>
                {#if cell.ghost !== null}
                  <span class="ghost"
                    >{fmt(cell.ghost)}<span class="dir {cell.dir?.dir ?? 'na'}"
                      >{arrow(cell.dir)}</span
                    ></span
                  >
                {/if}
              </div>
            {/each}
          </div>
        {/each}
      </div>
    </div>
  </div>

  <div class="foot">
    <span>Static audit render · reconstructed on demand from the FluxState change-log</span>
    <span>{storeName ? storeName + ".flux" : "store"}</span>
  </div>
</div>

<!-- ── Hover history popover (lazy, per-cell retrieval) ───────────────────── -->
{#if hover && hoverHist}
  <div class="histpop" style="left:{hoverPos.left}px; top:{hoverPos.top}px">
    <div class="hp-title">{hoverHist.title}</div>
    {#if hoverHist.violation}
      <div class="hp-viol">⚠ {hoverHist.violation}</div>
    {/if}
    {#if hoverHist.steps.length === 0}
      <div class="hp-empty">no value recorded</div>
    {:else if hoverHist.writeOnce}
      <div class="hp-once">
        {hoverHist.immutable ? "write-once (immutable) — no changes" : "no changes — write-once"}
      </div>
      <div class="hp-step">
        <span class="hp-date">{hoverHist.steps[0].date}</span>
        <span class="hp-val">{hoverHist.steps[0].value}</span>
      </div>
    {:else}
      <div class="hp-steps">
        {#each hoverHist.steps as s, i (s.date + i + s.value)}
          <div class="hp-step">
            <span class="hp-date">{s.date}</span>
            <span class="hp-val">{s.value}</span>
            {#if s.dir}<span class="hp-dir {s.dir}"
                >{s.dir === "up" ? "↑" : "↓"}{s.deltaLabel}</span
              >{/if}
          </div>
        {/each}
      </div>
      <div class="hp-foot">{hoverHist.steps.length} change events · newest first</div>
    {/if}
  </div>
{/if}

<style>
  .sheet {
    max-width: none;
    margin: 0;
    background: var(--card, #fff);
    color: var(--ink, #0f172a);
    padding: 24px 28px 28px;
    border: 1px solid var(--line, #e7e9ee);
    border-radius: var(--r, 12px);
    box-shadow: var(--shadow-sm, 0 1px 3px rgba(0, 0, 0, 0.06));
    line-height: 1.45;
    /* A flex column that fills the audit body: the head/callouts/legend/footer
       size to content and .tablebox (flex:1) fills the remaining height so the
       table scrolls within the viewport rather than the whole page. */
    display: flex;
    flex-direction: column;
    height: 100%;
    min-height: 0;
  }
  h1 {
    font-size: 24px;
    margin: 0 0 2px;
    letter-spacing: -0.3px;
  }
  .sub {
    color: var(--mut, #64748b);
    font-size: 12.5px;
    margin: 0;
  }
  .sub code,
  .foot {
    font-family: var(--mono, ui-monospace, monospace);
  }
  .rule {
    height: 2px;
    background: var(--accent, #2563eb);
    margin: 12px 0 16px;
    border-radius: 2px;
  }
  h2 {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 1.3px;
    margin: 22px 0 8px;
    color: var(--mut, #64748b);
    border-bottom: 1px solid var(--line, #e7e9ee);
    padding-bottom: 5px;
  }
  .note {
    font-size: 12px;
    color: var(--mut, #64748b);
    font-style: italic;
    margin: 0 0 8px;
  }

  /* ── Callouts ─────────────────────────────────────────────────────────── */
  .callout-group {
    margin: 6px 0 14px;
  }
  .cg-label {
    display: inline-block;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.3px;
    margin: 0 0 7px;
    padding: 2px 11px;
    border-radius: var(--r-pill, 999px);
  }
  .cg-label.viol {
    color: var(--del, #dc2626);
    background: var(--del-bg, #fdeaea);
    border: 1px solid var(--del-border, #f6c6c6);
  }
  .cg-label.vol {
    color: var(--mod, #d97706);
    background: var(--mod-bg, #fdf2e3);
    border: 1px solid var(--mod-border, #f3d9a8);
  }
  .callouts {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(230px, 1fr));
    gap: 8px;
    margin: 0;
  }
  .callout {
    border-left: 3px solid var(--accent, #2563eb);
    background: var(--accent-bg, #eff5ff);
    border-radius: var(--r-sm, 6px);
    padding: 8px 12px;
    font-size: 12.5px;
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 7px;
  }
  .callout.violation {
    border-left-color: var(--del, #dc2626);
    background: var(--del-bg, #fdeaea);
  }
  .callout.volatile {
    border-left-color: var(--mod, #d97706);
    background: var(--mod-bg, #fdf2e3);
  }
  .ctag {
    font-family: var(--mono, ui-monospace, monospace);
    font-size: 9.5px;
    font-weight: 700;
    letter-spacing: 0.5px;
    color: var(--mut, #64748b);
  }
  .callout.violation .ctag {
    color: var(--del, #dc2626);
  }
  .callout.volatile .ctag {
    color: var(--mod, #d97706);
  }

  /* ── Legend ───────────────────────────────────────────────────────────── */
  .legend {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin: 6px 0 8px;
  }
  .lg {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 11px;
    color: var(--mut, #64748b);
    border: 1px solid var(--line, #e7e9ee);
    background: var(--card, #fff);
    padding: 3px 9px;
    border-radius: var(--r-sm, 6px);
  }
  .lg.hovertip {
    color: var(--accent, #2563eb);
    background: var(--accent-bg, #eff5ff);
    border-color: var(--accent-border, #bfd3fb);
    font-weight: 600;
  }
  .sw {
    width: 13px;
    height: 13px;
    border-radius: 3px;
    border: 1px solid rgba(0, 0, 0, 0.15);
  }
  .sw.del {
    background: var(--del-bg, #fdeaea);
  }
  .sw.flag {
    background: var(--card, #fff);
    box-shadow: inset 3px 0 0 var(--del, #dc2626);
  }
  .sw.spark {
    background: linear-gradient(
      90deg,
      var(--add, #16a34a) 0 45%,
      var(--del, #dc2626) 45% 70%,
      var(--accent, #2563eb) 70% 100%
    );
  }
  .ghost-ex {
    color: var(--mut, #64748b);
    font-size: 10px;
    font-style: italic;
  }
  .up {
    color: var(--add, #16a34a);
  }
  .down {
    color: var(--del, #dc2626);
  }

  /* ── Virtualized table ────────────────────────────────────────────────── */
  /* The .tablebox is THE single scroll container (both axes). The sticky header
     and every virtualized row share one content box (.grid) and use the SAME
     explicit grid template so columns line up exactly regardless of which rows
     the virtual window mounts. */
  .tablebox {
    --lifew: 92px;
    --gutw: 38px;
    --colmin: 130px;
    /* Flex-fill the remaining height of .sheet (which fills .body--full): the
       header / callouts / legend / footer keep their natural height, and the
       table scroll container takes everything left, adapting to the viewport.
       min-height:0 lets it shrink on a short screen instead of overflowing. */
    flex: 1;
    min-height: 0;
    overflow: auto;
    border: 1px solid var(--line, #e7e9ee);
    border-radius: var(--r, 12px);
    background: var(--card, #fff);
    position: relative;
  }
  .grid {
    position: relative;
    width: max(100%, calc(var(--lifew) + var(--gutw) + var(--cols) * var(--colmin)));
  }
  .hrow,
  .trow {
    display: grid;
    grid-template-columns:
      var(--lifew) var(--gutw) repeat(var(--cols), minmax(0, 1fr));
    width: 100%;
  }
  .head {
    position: sticky;
    top: 0;
    z-index: 2;
    width: 100%;
    background: var(--bg-sunken, #eef0f3);
    border-bottom: 1px solid var(--line, #e7e9ee);
  }
  .hcell {
    border-right: 1px solid var(--line, #e7e9ee);
    color: var(--mut, #64748b);
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.6px;
    font-weight: 600;
    padding: 8px 10px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .hcell.lifecol,
  .hcell.gut {
    text-align: center;
  }
  .spacer {
    position: relative;
    width: 100%;
  }
  /* When printing we render every row in normal flow (no absolute positioning). */
  .spacer.flow {
    height: auto !important;
  }
  .trow {
    position: absolute;
    left: 0;
    height: var(--rowh, 44px);
    border-bottom: 1px solid var(--line, #eef0f3);
    font-family: var(--mono, ui-monospace, monospace);
    font-size: 12px;
  }
  .spacer.flow .trow {
    position: static;
    top: auto !important;
  }
  .td {
    border-right: 1px solid var(--line, #eef0f3);
    padding: 4px 10px;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    justify-content: center;
    white-space: nowrap;
    box-sizing: border-box;
  }
  .td.lifecol {
    align-items: center;
    justify-content: center;
  }
  .td.gut {
    align-items: center;
    justify-content: center;
    color: var(--mut, #64748b);
    font-variant-numeric: tabular-nums;
  }
  .td.gut[data-d="0"] {
    color: var(--dim, #cbd5e1);
  }
  .keycell {
    font-weight: 600;
    color: var(--text, #334155);
    flex-direction: row !important;
    align-items: center;
    gap: 3px;
  }
  .keycell .warn {
    color: var(--del, #dc2626);
    font-weight: 700;
  }
  .keycell .phoenix {
    color: var(--add, #16a34a);
  }

  /* Latest value + ghost subtext (prior value, smaller / muted, with arrow). */
  .latest {
    color: var(--ink, #0f172a);
    font-variant-numeric: tabular-nums;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .ghost {
    display: block;
    font-size: 10px;
    color: var(--mut, #64748b);
    font-style: italic;
    margin-top: 1px;
    font-variant-numeric: tabular-nums;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .dir {
    font-style: normal;
    font-weight: 700;
    margin-left: 3px;
  }
  .dir.up {
    color: var(--add, #16a34a);
  }
  .dir.down {
    color: var(--del, #dc2626);
  }
  .td.cell {
    cursor: help;
    transition: outline 0.1s;
  }
  .td.cell.has-ghost {
    background: var(--mod-bg, #fdf2e3);
  }
  .td.cell:hover {
    outline: 1.5px solid var(--accent, #2563eb);
    outline-offset: -1.5px;
  }

  /* Spark-path. */
  .spark {
    display: inline-block;
    vertical-align: middle;
  }
  .spark-base {
    stroke: var(--line, #e7e9ee);
    stroke-width: 1;
  }

  /* ── Row lifecycle / flag encodings ───────────────────────────────────── */
  .trow.r-del .td {
    background: var(--del-bg, #fdeaea);
    color: #b91c1c;
  }
  .trow.r-del .latest {
    text-decoration: line-through;
    color: #b91c1c;
  }
  .trow.r-res .keycell {
    box-shadow: inset 3px 0 0 var(--add, #16a34a);
  }
  .trow.r-flag .keycell {
    box-shadow: inset 3px 0 0 var(--del, #dc2626);
  }
  .trow.r-flag.r-res .keycell {
    box-shadow: inset 3px 0 0 var(--del, #dc2626), inset 6px 0 0 var(--add, #16a34a);
  }

  .foot {
    margin-top: 18px;
    border-top: 2px solid var(--accent, #2563eb);
    padding-top: 8px;
    font-size: 10.5px;
    color: var(--mut, #64748b);
    display: flex;
    justify-content: space-between;
  }

  /* ── Hover history popover ─────────────────────────────────────────────── */
  .histpop {
    position: fixed;
    z-index: 60;
    background: var(--card, #fff);
    border: 1px solid var(--accent-border, #bfd3fb);
    border-radius: var(--r-sm, 8px);
    padding: 9px 11px;
    box-shadow: var(--shadow-lg, 0 18px 50px rgba(0, 0, 0, 0.25));
    pointer-events: none;
    max-width: 270px;
    font-size: 11.5px;
  }
  .hp-title {
    font-family: var(--mono, ui-monospace, monospace);
    font-size: 10.5px;
    font-weight: 700;
    letter-spacing: 0.4px;
    color: var(--accent, #2563eb);
    margin-bottom: 5px;
  }
  .hp-viol {
    color: var(--del, #dc2626);
    background: var(--del-bg, #fdeaea);
    border-radius: 4px;
    padding: 3px 6px;
    font-size: 10.5px;
    font-weight: 600;
    margin-bottom: 5px;
  }
  .hp-empty,
  .hp-once {
    color: var(--mut, #64748b);
    font-style: italic;
    font-size: 10.5px;
    margin-bottom: 3px;
  }
  .hp-steps {
    display: flex;
    flex-direction: column;
    gap: 1px;
    max-height: 200px;
    overflow: hidden;
  }
  .hp-step {
    display: flex;
    align-items: baseline;
    gap: 9px;
    font-family: var(--mono, ui-monospace, monospace);
    font-variant-numeric: tabular-nums;
    padding: 1px 0;
  }
  .hp-date {
    color: var(--dim, #94a3b8);
    font-size: 10.5px;
    flex: none;
    width: 76px;
  }
  .hp-val {
    color: var(--ink, #0f172a);
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .hp-dir {
    font-weight: 700;
    flex: none;
  }
  .hp-dir.up {
    color: var(--add, #16a34a);
  }
  .hp-dir.down {
    color: var(--del, #dc2626);
  }
  .hp-foot {
    margin-top: 5px;
    padding-top: 4px;
    border-top: 1px solid var(--line, #e7e9ee);
    color: var(--mut, #64748b);
    font-size: 10px;
  }

  /* ── Print: clean, page-friendly, black-on-white, render-all ──────────── */
  @media print {
    :global(body) {
      background: #fff !important;
    }
    /* The app shell clamps to one viewport (`.app { height:100svh; overflow:hidden }`)
       and its scroll containers cap height — that would crush the audit to a single
       page. Release those clamps so the full audit flows across print pages. */
    :global(.app) {
      height: auto !important;
      overflow: visible !important;
      display: block !important;
    }
    :global(.body--full) {
      overflow: visible !important;
      padding: 0 !important;
    }
    :global(.topbar) {
      display: none !important;
    }
    .sheet {
      box-shadow: none;
      border: none;
      margin: 0;
      max-width: none;
      padding: 0;
      background: #fff;
    }
    .tablebox {
      overflow: visible;
      max-height: none;
      border: none;
      border-radius: 0;
    }
    /* In print the spacer is in render-all (flow) mode: rows are static, the
       header stops being sticky, and the spacer height collapses to content. */
    .head {
      position: static;
    }
    .spacer {
      height: auto !important;
    }
    .trow {
      position: static;
      top: auto !important;
      page-break-inside: avoid;
    }
    .legend {
      page-break-after: avoid;
    }
    /* Keep the daff color cues but let them print (color-adjust). */
    .callout,
    .td.cell.has-ghost,
    .trow.r-del .td {
      -webkit-print-color-adjust: exact;
      print-color-adjust: exact;
    }
    .histpop {
      display: none;
    }
  }
</style>
