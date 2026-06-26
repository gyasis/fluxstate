# FluxState Viewer — Developer Spec

**Status:** reference prototype + build spec · **Date:** 2026-06-05
**Owner:** Gyasi · **Skin:** provisional (daff-style light editorial) — visual polish expected, *interaction model is locked*

> This document IS the FluxState Viewer definition. The two HTML prototypes are the
> executable reference; this spec is what a developer builds against. "We can skin
> better" — the look may change; the interaction model, data contract, and scaling
> architecture below are the spec.

---

## 0. Reference artifacts (same folder)

| File | What it proves |
|---|---|
| `fluxstate-temporal-ghost-prototype.html` | The full interaction model on a 14-row demo (the design reference) |
| `fluxstate-5k-stress-test.html` | Scale: 5,000 rows virtualized + delta-on-scrub + SQL/simple filter |
| `fluxstate-daff-audit-render.html` | The daff-style audit render + cross-time/resurrection concept |
| `fluxstate-design-decision.md` | Why FluxState is kept (changelog-first pivot) + the data contract |

Serve locally: `python3 -m http.server` in this folder.

---

## 1. What the Viewer is

An interactive viewer over **one FluxState-tracked table/view** that lets a user
**time-travel** the table (scrub a date slider → every cell snaps to its as-of value),
**see transitions** (daff-style `old → new` in-cell during scrub), inspect any cell's
**full change history**, and **filter** rows — at thousands-to-millions of rows.

Core mental model: **"daff aligns two tables; FluxState remembers all of them."**
The slider replays the whole history; deleted rows stripe out; **resurrected** rows
(same `entity_id` returns after deletion) show a continuous `active → deleted → active`
trail no two-table diff can produce.

---

## 2. Interaction model (LOCKED — "Temporal Ghost")

### 2.1 The time control = a capsule scrubber whose track is the change-density histogram
- Rounded capsule track (~20px tall). Behind it: **same-height segments shaded by amount
  of change** per time-bucket (faint = quiet, solid = busy) — the user sees *where the
  action is in time*.
- **Fill** to the left of the knob = elapsed time. **Round knob** = the playhead.
- **Event-snapping:** `←/→`, `‹/›`, and Play jump to actual change events (`SNAPS`), not raw days.
- Controls: **▶ Play / ⏸**, **‹ ›** step, **⟳ loop** (replay), `as of <date>` readout.

### 2.2 As-of rendering + diff-on-scrub
- Each cell shows `asOf(cellHistory, T)`.
- When a cell changes across a scrub step, render the **daff transition in-cell**:
  `~~old~~ → **new**` (old struck red, new green-bold) + an amber cell flash.
- **Pinned vs fade:** discrete steps (`←/→`, `‹/›`, paused) **pin** the `old → new` until
  the user moves on; **Play/drag** let it **linger then fade** to just the new value.
- `⇄ diff` toggle disables it (plain snap).

### 2.3 Hover + pin
- **Hover** a cell → lightweight popover: last 3 `{date,value}`.
- **Click** a cell → pin its **full history** (sparkline + every event incl.
  `__deleted__`/`resurrected`, with a "now" marker tied to the slider) in a fixed inspector.

### 2.4 Row lifecycle encodings
- **Heat-tint** cell bg = how often that cell changes (`v1..v4`).
- **Deleted** row → red diagonal stripe + strikethrough.
- **Resurrected** row → green left-edge + `✦`.
- **Not-yet-born** row → faded/`·`.
- **Δ gutter** = per-row change count.

### 2.5 Static / print view
- Non-interactive: latest value + ghost subtext (prior value + ↑/↓), leading lifecycle
  spark-path column, auto-callouts on the most volatile cells. `@media print` ready.

### 2.6 Compare strategy — 2-capture A/B (added)
A store with **exactly two captures** is an **A/B comparison**, not a time series — two discrete states
cannot be a continuous play-scrubber. The viewer picks a **strategy** from the data **state**: `compare`
for 2 captures, the `timeseries` "Temporal Ghost" (§2.1–2.5) for >2. The timeseries model is unchanged.

In `compare`:
- The time control becomes a **discrete 3-chapter stepper** — **Before · Δ changes · After** — *not* a
  draggable/playing track. No play, no loop, no timer. Each chapter is a **crisp step (no fade)**.
- **Before / After** = each capture's as-of state (clean snapshot, diff off).
- **Δ changes** = the data-change state, reusing the §2.2 diff-on-scrub engine at `prev=Before,
  value=After`: **ADD** → `ø→new`, **CHG** → `old→new` (its change-fade is **preserved/pinned** on Δ —
  landing on Δ must not strip it), **DROP** → the §2.4 deleted encoding (stripe/strikethrough). A footer
  tallies **+added · −dropped · ~changed**.
- Membership uses lifecycle **state** (`active`); an **unborn** row (no events at Before) counts as ADD,
  not changed.

Applies to **this repo's `viewer/`** and to Pharos's `FluxQuickTable.svelte`. Reference implementation:
Pharos #238 (verified on a synthetic 2-capture demo + a real A/B store).

---

## 3. Data contract

The Viewer consumes the **FluxState change-log** (see `fluxstate-design-decision.md`),
NOT JSON-in-cell mirror tables.

**Change-log row:** `entity_id, timestamp(UTC), field, value, dtype, snapshot_id`
**Lifecycle events:** `__deleted__` (value=null) and resurrection (a later `active`),
keyed by `entity_id` (a full outer join produces these — never a left join).

### Reconstruction primitives (the only logic the UI needs)
```
asOf(history, T)        → latest {date,value} with t ≤ T        (binary-searchable)
rowState(row, T)        → { state: active|deleted|unborn, resurrected:bool }
changeCount(row)        → Σ events across fields (volatility / heat)
SNAPS                   → sorted unique event timestamps (slider snap points)
density buckets         → histogram of events per time-bucket (computed once)
```
A cell timeline = `[{date,value}, …]` is reconstructed on demand
(`get_timeline(id, field)`), never stored as JSON-in-cell.

---

## 4. Scaling architecture (the part that makes it real)

Three independent layers, each windowed to the viewport → cost is **O(viewport), not O(table)**:

1. **Row virtualization** — render only the ~25–40 rows in view (absolute-positioned rows
   over a sized spacer; recycle on scroll). Constant DOM regardless of total rows.
   *(5k prototype: ~25 nodes live, paints in ~1–3ms.)*
2. **Delta-only updates** — on a scrub step compute `asOf(prevT)` vs `asOf(T)` per visible
   cell; repaint only changed cells (changes are sparse). Add **change-throttling**: if a
   step flips a large fraction of visible cells, skip per-cell flashes, show a bulk pill.
3. **Streamed windowed data** — don't load the whole table. Fetch only the visible rows +
   a prefetch buffer; refetch the as-of slice on time-move. **In production this is
   DuckDB-WASM querying the change-log parquet in-browser** (keyset pagination +
   `WHERE date <= T`), the Pharos "crush-before-render ≤5K at runtime" pattern. Bounded
   LRU cache of fetched windows (economy-first; no unbounded growth).

**Checkpoint compaction:** periodic materialized snapshots so whole-table point-in-time
reconstruction starts from the nearest checkpoint, not t=0 — prevents O(N) time-travel as
history deepens.

**Live tail (optional):** if FluxState watches a live source, subscribe to the change-log
tail; the density strip + visible cells update in real time (the "drop-in watcher" pitch).

---

## 5. Filtering

Sidebar (collapsible), two modes — both compile to the **same `WHERE`**:
- **Simple:** STATUS / PCP selects, RISK range, min-changes, ID-contains.
- **Advanced · SQL:** a `WHERE` expression box.
  Columns: the tracked columns. Meta: `changes`, `deleted`, `resurrected`, `id`.
  Ops: `= != < > <= >= AND OR LIKE 'x%'`, string literals in quotes.
  Evaluated on each row's as-of-now view; invalid syntax → inline error.

**Production:** the typed `WHERE` becomes the **DuckDB-WASM predicate** that decides which
windowed rows stream in (§4.3). The prototype evaluates it in JS as a stand-in.
**Open option:** "filter follows time" (re-evaluate the predicate at the current slider T)
vs the current "filter is a stable row set you then scrub." Default = stable set.

---

## 6. Recommended stack (aligns with Pharos)

- **Svelte 5 + Vite** (Pharos app shell). Viewer is a component over a change-log source.
- **DuckDB-WASM** for in-browser change-log querying (as-of slice, WHERE, keyset paging).
- **No heavy grid lib** — the virtualization is ~80 lines; avoid ag-grid-class deps (keep it lightweight, P1).
- Sparkline: inline SVG (no chart lib needed); Vega-Lite only if richer per-cell charts are wanted later.
- Vanilla render loop with `requestAnimationFrame` on slider input + scroll.

---

## 7. Build milestones / acceptance

| # | Milestone | Acceptance |
|---|---|---|
| M1 | Reconstruction lib | `asOf` / `rowState` / `changeCount` unit-tested incl. deleted + resurrected |
| M2 | Capsule scrubber + as-of render | scrub updates table; density strip + fill + knob; event-snap; play/loop/step/keys |
| M3 | Diff-on-scrub | pinned on step, fade on play/drag; `⇄` toggle |
| M4 | Hover peek + click inspector | full history incl. lifecycle events; "now" marker tracks slider |
| M5 | Virtualization | 100k synthetic rows scroll + scrub at 60fps; ~constant DOM node count |
| M6 | Streamed data via DuckDB-WASM | window + as-of slice fetched by SQL; bounded LRU; prefetch buffer |
| M7 | Filter (simple + SQL) | both compile to the DuckDB `WHERE`; invalid SQL handled; "showing X of N" |
| M8 | Static / print view | print-ready audit render with lifecycle trails + callouts |

---

## 8. Known trade-offs (honest)
- **Whole-table point-in-time at scale** (e.g. "all 50k patients as of date T, weekly") is
  the one query where SCD2/dbt-snapshot beats this; mitigate with checkpoints, or fall back
  to a SQL snapshot for that report. (See `fluxstate-design-decision.md` §"DROP triggers".)
- **`old → new` width growth** during diff — keep cells `nowrap`; long values truncate with
  the full value in the inspector.
- The prototype's SQL filter is JS-evaluated and string-substituted onto column names — fine
  for the demo; production uses DuckDB's real parser, not this shim.
