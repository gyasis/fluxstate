# FluxState Visualizations — the "Temporal Ghost" viewer

> **FluxState is a faithful recorder of what the source emits, not a semantic-equality engine.**
> The visualizations below all reconstruct *from the real change-log* (no synthetic data),
> and they render exactly what was recorded — change is shown as change.

This is the catalogue of every **visual surface** FluxState ships, what each one shows, the data
it needs, and how it behaves. It is the reference for embedding these surfaces elsewhere (e.g.
[pharos](./PHAROS_INTEGRATION.md)).

The viewer is **Svelte 5 + Vite + DuckDB-WASM**. It reads a `<name>.flux/` store directly in the
browser and reconstructs any point in time via a **JS port of the Python `reconstruct.py`**
(`viewer/src/lib/reconstruct.ts`), kept honest by a Python↔JS **parity test**
(`viewer/tests/reconstruct.parity.test.ts`). No heavy grid library — all rendering is hand-rolled
SVG + virtualized DOM.

---

## 0. The mental model: time-travel over a change-log

The store is an **append-only log of cell changes** (`events/*.parquet` + `manifest.json`). Nothing
is ever rewritten. Any historical table state is *reconstructed on read* by replaying the latest
value of each `(entity, field)` at-or-before a chosen time **T**. The viewer's whole job is to let a
human (or another app) **scrub T** and *see* what changed, when, and how.

Two data paths, chosen automatically by store size:

| Path | When | How it loads | Components |
|---|---|---|---|
| **Small** (demo) | store fits in memory | loads the full event set once, builds an in-memory index | `table.svelte` |
| **Large** (scale) | 100k+ entities / millions of events | streams only the visible window via DuckDB, bounded-LRU cached | `windowed-table.svelte` |

Both paths reconstruct with the **same pure primitives** in `reconstruct.ts`, so behavior is
identical; only the data-fetch strategy differs.

---

## 1. The Scrubber — the "Temporal Ghost" capsule  ·  `lib/scrubber.svelte`

**What it shows.** A horizontal capsule whose **track *is* a change-density histogram** (inline-SVG
segments, shaded by how many changes happened in each time bucket). A round knob is the playhead;
an elapsed-time fill sits to its left. An `as of <date>` readout shows the current T.

**Interactions.** `▶ Play / ⏸`, `‹ ›` step, `⟳` loop, and a `⇄ diff` toggle. The control is
**event-snapped**: stepping and Play jump only to timestamps where something actually changed
(`snapPoints`) — never to empty days. Play advances over snap points via `requestAnimationFrame`.

**Data it needs (pure-presentation props):**
- `snapPoints: Date[]` — the unique event timestamps (the only legal playhead positions).
- `density: DensityBucket[]` — `{ bucketStart: Date, bucketEnd: Date, changeCount }` for the track.
- bindable `value: Date` (current T), `playing`, `diffOn`; a `step(mode)` callback.

It owns **no** data access — the parent shell (`App.svelte`) derives `snapPoints` + `density` from
the store (cheap aggregates on the large path) and feeds them in.

---

## 2. The As-Of Table — in-cell diff rendering  ·  `lib/table.svelte`

**What it shows.** The reconstructed table **as-of T**: each cell is the value of `(entity, field)`
at T. The signature behavior is the **in-cell diff** when you scrub across a change:

```
~~old~~ → **new**      (old struck through in red, new in bold green) + an amber cell flash
```

**Pin vs. fade** (the "ghost"):
- **Discrete** moves (`‹ ›`, paused) → the `old → new` transition is **pinned** until you move on.
- **Play / drag** → the transition **lingers, then fades** to just the new value.
- The `⇄ diff` toggle disables diffing entirely → a plain as-of snapshot.

**Direction & precision encodings:**
- Numeric changes can carry a `↑ / ↓` direction arrow (via `changeDirection`) — including for very
  large `int64` values that decode to **BigInt** (`|v| > 2^53−1`).
- `int64` beyond the float64 safe range decodes to BigInt (no precision loss).
- Non-finite floats (`inf`/`-inf`/`nan`) render as `±Infinity`/`NaN` (faithful to the store).

**Performance:** an O(E log E) index is built **once** per event set; each scrub step builds two
whole-table views (`prevT`, `T`) and the per-cell `old→new` is a map lookup for **visible cells
only**; only the ~30–45 rows in the viewport are in the DOM (virtualized over a sized spacer). The
scroll container height is set **deterministically in JS** (measured), never via a fragile flex
chain — see the UI-verification note in §8.

---

## 3. The Windowed Table — the scale path  ·  `lib/windowed-table.svelte`

**What it shows.** The same as-of table, but for **100k+ entities / millions of events** that can't
fit in JS memory. Visually identical to §2; the difference is purely how data arrives.

**How it works:**
1. Virtualizes over the **full ordered entity-id list** (`entityOrder`, fetched once via a DuckDB
   aggregate) — so the scrollbar/scroll range are correct for the full row count while only ~40 rows
   are ever in the DOM.
2. For the visible window it calls `fetchWindow(ids)` (DuckDB, per-entity, **bounded-LRU cached**)
   for just those entities' history, indexes that small set, and reconstructs as-of T.
3. Scrolling changes the window → a new (cached) fetch; **scrubbing reuses the same window index**
   (no re-query) — as-of and prev↔now diff are binary searches over per-window timelines.

**Honest scope:** features needing whole-table evaluation across all 100k (global SQL filter, global
sort) are **not** offered on this path — they'd require scanning everything. Per-window diff/scrub is
full-fidelity.

---

## 4. The Inspector — cell-history peek + pin  ·  `lib/inspector.svelte`

**What it shows.** Drill into a single cell's history.
- **Hover (peek):** a lightweight popover with the cell's **last 3** `{date, value}` (newest first).
- **Click (pin):** a panel with the cell's **full history**:
  - an inline-SVG **sparkline** (numeric polyline; otherwise event-tick marks),
  - **every** event — including `__deleted__` ("row deleted") and the resurrection SET that follows
    ("row resurrected"), merged + sorted,
  - a **"now" marker** tied to the slider T: events at `t ≤ T` are highlighted, later events dimmed
    as `future`. As you scrub, the marker moves.

**Data it needs:** reads the **shared `ReconIndex`** (per-cell timeline + lifecycle) via `getTimeline`
and per-entity walks — O(log k), never a flat per-event scan. The table drives it (passes hover
coords + the pinned `(entity, field)`).

---

## 5. The Filter — simple + SQL, one predicate  ·  `lib/filter.svelte`

**What it shows.** A collapsible pane with **two modes that compile to the same predicate**:
- **Simple:** selects/range over tracked columns + `min-changes` + `id-contains` + a `flagged`
  (immutable-violation) toggle + `deleted` / `resurrected` toggles.
- **SQL:** a `WHERE` expression over tracked columns + meta-fields (`changes`, `deleted`,
  `resurrected`, `flagged`, `id`). Invalid syntax → inline error, prior result preserved.

Both emit one `(row: FilterRow) => boolean`. The parent evaluates it **once** over the as-of-now row
views to get a **stable entity-id set**, then scrubs *that* set — the filter is **not** re-evaluated
per T (so a row doesn't pop in/out as you time-travel). The SQL mode is a string-substitution shim in
the demo; production swaps in DuckDB's real `WHERE` parser.

---

## 6. The Static Audit View — print-ready  ·  `lib/static-view.svelte`

**What it shows.** A **non-interactive, print-ready** as-of-now audit for a compliance reviewer:
- every cell shows its **latest value** with a **ghost subtext** (prior value + `↑/↓` for numerics),
- a leading **lifecycle spark-path** column (tiny inline SVG of each row's active/deleted/resurrected
  trail + change density),
- **auto-callouts** on the most **volatile** rows and the **immutable-violation** rows.

**Performance:** virtualized like the live table; heavy per-row shaping runs only for the visible
window. For **print**, `beforeprint`/`afterprint` flips a flag that renders **every** row (not just
the window) so the printed audit is complete.

---

## 7. Anomaly signals — immutable violations & change direction (US8)

Two cross-cutting visual signals layered onto the surfaces above:
- **Immutable-column violations** — a column expected never to change that *did* is flagged (red
  badge / `flagged` filter facet / static-view callout). Computed by `immutableViolations` over the
  index.
- **Change direction** — numeric (and BigInt) transitions carry `↑ / ↓ / =` via `changeDirection`,
  surfaced as in-cell arrows (table) and ghost-subtext arrows (static view).

---

## 8. Shared contracts the visuals depend on

All surfaces are downstream of three things; embed-targets should treat these as the contract:

| Contract | Where | What it guarantees |
|---|---|---|
| **Reconstruction primitives** | `reconstruct.ts` (`asOf`, `getTimeline`, `buildMirrorView`, `rowState`, `densityBuckets`, `snapPoints`, `immutableViolations`, `changeDirection`, `decodeValue`) | JS reconstruction is **byte-parity** with Python `reconstruct.py` (parity-tested). |
| **Data access** | `duckdb.ts` (`fetchEvents`, `fetchWindow`, `fetchEntityOrder`, manifest/parquet read) | reads the real `.flux/` store; large-store windowing + bounded-LRU cache. |
| **Parity gate** | `reconstruct.parity.test.ts` ⇄ `TESTS/test_parity_export.py` | every probe (deleted, resurrected, genuine-null, big-int, **same-timestamp row order**) matches Python. |

**Reconstruction guarantees worth knowing when displaying values:**
- **Row order is deterministic** — mirror-view rows are key-sorted on both sides (VD-6), so
  same-timestamp entities render in identical order in Python and the viewer.
- **Datetime display is ms-resolution** — the viewer engine is `Date`-based; the **store keeps full
  µs** (recorder vs. view, VD-3). Datetime *value* cells render as `YYYY-MM-DD` in the table.
- **Faithfulness** — `1.1` vs `1.10`, a tz-representation change, etc. show **as changes**; the
  viewer never canonicalizes to decide values are "really equal."

**Verifying these surfaces:** logic/data is covered by the parity + interaction tests (headless).
**Pixel layout is NOT certifiable headless** — flex/grid collapse, scrollbar gutter, sticky-header
seams differ in a real browser. The load-bearing scroll height is therefore set **deterministically
in JS** (measured `window.innerHeight - top`), with a CSS fallback + hard minimum, so the table body
can never collapse to zero. Verify visual changes in a **real headed browser**, not headless.

---

## 9. File map (quick reference)

| Surface | File | LOC |
|---|---|---|
| Shell / wiring (small↔large branch, derives snap/density) | `viewer/src/App.svelte` | ~974 |
| As-of diff table | `viewer/src/lib/table.svelte` | ~984 |
| Windowed (scale) table | `viewer/src/lib/windowed-table.svelte` | ~749 |
| Scrubber (Temporal Ghost capsule) | `viewer/src/lib/scrubber.svelte` | ~447 |
| Inspector (peek + pin history) | `viewer/src/lib/inspector.svelte` | ~472 |
| Filter (simple + SQL) | `viewer/src/lib/filter.svelte` | ~682 |
| Static audit (print) | `viewer/src/lib/static-view.svelte` | ~956 |
| JS reconstruction (parity with Python) | `viewer/src/lib/reconstruct.ts` | ~810 |
| DuckDB-WASM data access | `viewer/src/lib/duckdb.ts` | ~610 |

The original locked design prototypes live in `docs/viewer/` (`fluxstate-temporal-ghost-prototype.html`,
`fluxstate-5k-stress-test.html`, `fluxstate-daff-audit-render.html`, `fluxstate-viewer-dev-spec.md`).
