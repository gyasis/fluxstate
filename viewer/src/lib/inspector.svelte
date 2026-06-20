<script lang="ts">
  // File: viewer/src/lib/inspector.svelte  (T020 / US3 — dev-spec §2.3)
  //
  // Cell-history inspection, reproducing the prototype's PEEK + INSPECTOR.
  //
  //   • HOVER (peek)  → a lightweight fixed popover with the cell's last 3
  //     {date,value} (newest first), positioned by the cursor. Pure presentation.
  //   • CLICK (pin)   → a fixed inspector panel with the cell's FULL history:
  //       - an inline-SVG sparkline (numeric polyline, else event-tick marks)
  //       - every event, INCLUDING `__deleted__` ("row deleted") and the
  //         resurrection SET that follows ("row resurrected"), merged + sorted
  //       - a "now" marker tied to the slider T: events at t ≤ T are highlighted
  //         (dot ring), events after T are dimmed `future`. As T scrubs, the
  //         marker moves (the panel re-derives off the reactive `T` prop).
  //     A close/unpin control clears the pin.
  //
  // PERFORMANCE: everything reads the SHARED ReconIndex (per-cell timeline +
  // lifecycle) via O(log k) / single-entity walks — NEVER the flat per-event
  // scan. The peek uses `getTimeline` over the index's already-sorted history;
  // the inspector reads the same per-cell + lifecycle lists.
  //
  // The table drives this: it passes hover coordinates + the pinned (entity,field)
  // up; the inspector reads the index for the detail. The pin is BINDABLE so the
  // table can render the `.pinned` outline on the matching cell and the close
  // button can clear it.

  import {
    decodeValue,
    changeDirection,
    type ReconIndex,
    type Typed,
  } from "./reconstruct";

  const DELETED_FIELD = "__deleted__";

  // ── Props ────────────────────────────────────────────────────────────────
  interface PeekTarget {
    entityId: string;
    field: string;
    /** Cursor client coords (for popover placement). */
    x: number;
    y: number;
  }
  interface PinTarget {
    entityId: string;
    field: string;
  }

  interface Props {
    /** Shared reconstruction index (built once by the table's parent). */
    index: ReconIndex;
    /** Manifest schema map — column → dtype tag (for decoding cell values). */
    schema: Record<string, string>;
    /** Current as-of instant — drives the inspector "now" marker. */
    T: Date;
    /** Hover target (null ⇒ no peek). */
    peek?: PeekTarget | null;
    /** Pinned cell (bindable so the close control / table outline stay in sync). */
    pinned?: PinTarget | null;
  }

  let {
    index,
    schema,
    T,
    peek = null,
    pinned = $bindable(null),
  }: Props = $props();

  // ── Decoded timeline for a (entity, field) cell ───────────────────────────
  interface Pt {
    date: Date;
    value: Typed;
  }
  function cellTimeline(entityId: string, field: string): Pt[] {
    const hist = index.cells.get(entityId)?.get(field);
    if (!hist) return [];
    return hist.map((e) => ({
      date: new Date(e.t),
      value: decodeValue(e.value, e.dtype),
    }));
  }

  function fmtVal(v: Typed): string {
    if (v === null || v === undefined) return "ø";
    if (v instanceof Date) return v.toISOString().slice(0, 10);
    if (typeof v === "boolean") return v ? "true" : "false";
    return String(v);
  }
  function fmtDate(d: Date): string {
    return d.toISOString().slice(0, 10);
  }

  // ── PEEK (hover) — last 3 events, newest first ─────────────────────────────
  const peekRows = $derived.by(() => {
    if (!peek) return null;
    const tl = cellTimeline(peek.entityId, peek.field);
    if (tl.length === 0) return { title: "", rows: [], extra: 0 };
    const last3 = tl.slice(-3).reverse();
    return {
      title: `${peek.entityId} · ${peek.field}`,
      rows: last3.map((p) => ({ date: fmtDate(p.date), value: fmtVal(p.value) })),
      extra: Math.max(0, tl.length - 3),
    };
  });

  // Popover placement: nudge it away from the right/bottom edges.
  const peekPos = $derived.by(() => {
    if (!peek) return { left: 0, top: 0 };
    let x = peek.x + 14;
    let y = peek.y + 14;
    if (typeof window !== "undefined") {
      if (x + 250 > window.innerWidth) x = peek.x - 250;
      if (y + 130 > window.innerHeight) y = peek.y - 130;
    }
    return { left: x, top: y };
  });

  // ── INSPECTOR (pinned) — full history incl. lifecycle, with "now" marker ───
  type EvKind = "set" | "del" | "res";
  interface InspEvent {
    t: number;
    date: string;
    label: string;
    kind: EvKind;
    /** For numeric SET events: direction vs the previous numeric value + signed delta. */
    dir?: "up" | "down";
    deltaLabel?: string;
  }

  /** Format a numeric delta with an explicit sign (e.g. +0.13 / -2). */
  function fmtDelta(delta: number): string {
    const sign = delta >= 0 ? "+" : "";
    const s = Number.isInteger(delta) ? String(delta) : String(Number(delta.toFixed(4)));
    return `${sign}${s}`;
  }

  /** Merge per-cell SETs with lifecycle del/resurrection events, sorted by time. */
  const insp = $derived.by(() => {
    if (!pinned) return null;
    const { entityId, field } = pinned;
    const tl = cellTimeline(entityId, field);

    // Lifecycle: walk the entity's full event list to label each __deleted__ as
    // "row deleted" and the FIRST SET after a deletion as "row resurrected".
    const life = index.lifecycle.get(entityId) ?? [];
    const lifeEvents: InspEvent[] = [];
    let pendingDel = false;
    for (const e of life) {
      if (e.field === DELETED_FIELD) {
        lifeEvents.push({
          t: e.t,
          date: fmtDate(new Date(e.t)),
          label: "row deleted",
          kind: "del",
        });
        pendingDel = true;
      } else if (pendingDel) {
        lifeEvents.push({
          t: e.t,
          date: fmtDate(new Date(e.t)),
          label: "row resurrected",
          kind: "res",
        });
        pendingDel = false;
      }
    }

    const setEvents: InspEvent[] = tl.map((p, i) => {
      const ev: InspEvent = {
        t: p.date.getTime(),
        date: fmtDate(p.date),
        label: fmtVal(p.value),
        kind: "set" as const,
      };
      // Numeric direction cue vs the previous point (matches the table's ↑/↓).
      if (i > 0) {
        const cd = changeDirection(tl[i - 1].value, p.value);
        if ((cd.dir === "up" || cd.dir === "down") && cd.delta !== null) {
          ev.dir = cd.dir;
          ev.deltaLabel = fmtDelta(cd.delta);
        }
      }
      return ev;
    });

    const events = [...setEvents, ...lifeEvents].sort((a, b) => a.t - b.t);

    // Sparkline geometry (inline SVG). Numeric history → polyline; else ticks.
    const W = 278;
    const H = 46;
    const nums = tl.map((p) =>
      typeof p.value === "number" ? p.value : Number.NaN,
    );
    const allNum = tl.length > 1 && nums.every((n) => !Number.isNaN(n));
    const minT = tl.length ? tl[0].date.getTime() : 0;
    const maxT = tl.length ? tl[tl.length - 1].date.getTime() : 1;
    const spanT = Math.max(1, maxT - minT);

    let sparkline: { polyline?: string; dots: { x: number; y: number }[] } = {
      dots: [],
    };
    if (allNum) {
      const mn = Math.min(...nums);
      const mx = Math.max(...nums);
      const range = mx - mn || 1;
      const pts = tl.map((p, i) => {
        const x = ((p.date.getTime() - minT) / spanT) * W;
        const y = H - 6 - ((nums[i] - mn) / range) * (H - 12);
        return { x, y };
      });
      sparkline = {
        polyline: pts.map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" "),
        dots: pts,
      };
    } else {
      const pts = tl.map((p) => {
        const x = ((p.date.getTime() - minT) / spanT) * W;
        return { x, y: H - 8 };
      });
      sparkline = { dots: pts };
    }

    const totalRow = (index.lifecycle.get(entityId) ?? []).filter(
      (e) => e.field !== DELETED_FIELD,
    ).length;

    return {
      title: `${entityId} · ${field}`,
      sub: `${tl.length} change events · ${totalRow} total on row`,
      events,
      sparkline,
      W,
      H,
    };
  });

  /** "now" cutoff in ms — re-derives whenever T scrubs, moving the marker. */
  const nowMs = $derived(T.getTime());

  function unpin(): void {
    pinned = null;
  }
</script>

{#if peek && peekRows && peekRows.rows.length}
  <div class="peek" style="left:{peekPos.left}px; top:{peekPos.top}px">
    <div class="pt">{peekRows.title}</div>
    {#each peekRows.rows as r (r.date + r.value)}
      <div class="ev"><span class="d">{r.date}</span><span>{r.value}</span></div>
    {/each}
    {#if peekRows.extra > 0}
      <div class="ev">
        <span class="d">+{peekRows.extra} earlier — click to pin</span>
      </div>
    {/if}
  </div>
{/if}

{#if pinned && insp}
  <div class="inspector show">
    <div class="ihead">
      <div>
        <div class="it">{insp.title}</div>
        <div class="is">{insp.sub}</div>
      </div>
      <button class="iclose" type="button" aria-label="Unpin history" onclick={unpin}
        >×</button
      >
    </div>
    <div class="ibody">
      <div class="isparkbox">
        <svg width={insp.W} height={insp.H} aria-hidden="true">
          {#if insp.sparkline.polyline}
            <polyline
              fill="none"
              stroke="#2e5d7d"
              stroke-width="2"
              points={insp.sparkline.polyline}
            />
            {#each insp.sparkline.dots as d (d.x + ',' + d.y)}
              <circle cx={d.x.toFixed(1)} cy={d.y.toFixed(1)} r="2.5" fill="#9a6b12" />
            {/each}
          {:else}
            {#each insp.sparkline.dots as d (d.x)}
              <line
                x1={d.x.toFixed(1)}
                y1="8"
                x2={d.x.toFixed(1)}
                y2={insp.H - 8}
                stroke="#9a6b12"
                stroke-width="2"
              />
              <circle cx={d.x.toFixed(1)} cy={insp.H - 8} r="2.5" fill="#2e5d7d" />
            {/each}
          {/if}
        </svg>
      </div>
      <div class="ievents">
        {#each insp.events as ev (ev.kind + ev.t + ev.label)}
          <div class="ievent" class:future={ev.t > nowMs}>
            <span
              class="idot"
              class:del={ev.kind === "del"}
              class:res={ev.kind === "res"}
              class:now={ev.t <= nowMs}
            ></span>
            <span class="id">{ev.date}</span>
            <span class="iv">
              {#if ev.kind === "del"}
                <span class="tagdel">{ev.label}</span>
              {:else if ev.kind === "res"}
                <span class="tagres">{ev.label}</span>
              {:else}
                {ev.label}{#if ev.dir}<span class="idir {ev.dir}"
                    >{ev.dir === "up" ? "↑" : "↓"}{ev.deltaLabel}</span
                  >{/if}
              {/if}
            </span>
          </div>
        {/each}
      </div>
    </div>
  </div>
{/if}

<style>
  /* mini popover (hover) — matches the prototype .peek */
  .peek {
    position: fixed;
    background: #fff;
    border: 1px solid var(--line, #dcd6c8);
    border-radius: 8px;
    padding: 8px 10px;
    font-size: 11.5px;
    box-shadow: 0 10px 30px rgba(0, 0, 0, 0.18);
    z-index: 50;
    pointer-events: none;
    max-width: 240px;
  }
  .peek .pt {
    color: var(--mut, #6c655a);
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.6px;
    margin-bottom: 4px;
    font-weight: 600;
  }
  .peek .ev {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    padding: 1px 0;
    font-variant-numeric: tabular-nums;
    font-family: var(--mono, ui-monospace, monospace);
  }
  .peek .ev .d {
    color: var(--dim, #b8b0a0);
  }

  /* History Inspector (pinned) — matches the prototype .inspector */
  .inspector {
    position: fixed;
    right: 18px;
    bottom: 18px;
    width: 308px;
    background: var(--card, #fff);
    border: 1px solid var(--mod, #9a6b12);
    border-radius: 12px;
    box-shadow: 0 18px 50px rgba(0, 0, 0, 0.25);
    z-index: 40;
    overflow: hidden;
  }
  .ihead {
    background: #f2efe7;
    padding: 10px 13px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    border-bottom: 1px solid var(--line, #dcd6c8);
  }
  .ihead .it {
    font-weight: 700;
    font-size: 13px;
    font-family: var(--mono, ui-monospace, monospace);
  }
  .ihead .is {
    color: var(--mut, #6c655a);
    font-size: 11px;
  }
  .iclose {
    background: none;
    border: none;
    color: var(--mut, #6c655a);
    font-size: 17px;
    cursor: pointer;
    line-height: 1;
  }
  .ibody {
    padding: 12px 13px;
    max-height: 320px;
    overflow: auto;
  }
  .isparkbox {
    background: #faf8f2;
    border: 1px solid var(--line, #dcd6c8);
    border-radius: 8px;
    padding: 8px;
    margin-bottom: 10px;
  }
  .ievent {
    display: grid;
    grid-template-columns: 14px 80px 1fr;
    gap: 8px;
    align-items: center;
    padding: 4px 0;
    border-top: 1px solid var(--line, #dcd6c8);
    font-size: 12px;
  }
  .ievent:first-child {
    border-top: none;
  }
  .idot {
    width: 9px;
    height: 9px;
    border-radius: 50%;
    background: #2e5d7d;
  }
  .idot.del {
    background: var(--del, #b23b2c);
  }
  .idot.res {
    background: var(--add, #2e7d4f);
  }
  .idot.now {
    box-shadow: 0 0 0 3px rgba(46, 93, 125, 0.22);
  }
  .ievent .id {
    color: var(--dim, #b8b0a0);
    font-variant-numeric: tabular-nums;
    font-size: 11px;
    font-family: var(--mono, ui-monospace, monospace);
  }
  .ievent .iv {
    font-variant-numeric: tabular-nums;
    font-family: var(--mono, ui-monospace, monospace);
  }
  .ievent.future {
    opacity: 0.4;
  }
  .tagdel {
    color: var(--del, #b23b2c);
    font-size: 10.5px;
    font-weight: 600;
  }
  .tagres {
    color: var(--add, #2e7d4f);
    font-size: 10.5px;
    font-weight: 600;
  }
  .idir {
    margin-left: 6px;
    font-weight: 700;
    font-size: 11px;
  }
  .idir.up {
    color: var(--add, #2e7d4f);
  }
  .idir.down {
    color: var(--del, #b23b2c);
  }
</style>
