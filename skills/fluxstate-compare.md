---
name: fluxstate-compare
description: Compare two versions of a dataset (A/B) or change-over-time with FluxState, and launch the interactive Temporal Viewer. Use when the user wants to "diff two versions", "A/B compare these datasets", "what changed between dev and prod", "before vs after", "show me the changes over time visually", "open the flux viewer", "scrub the timeline of this table", or a visual before→after of a keyed table. Covers building a 2-snapshot store, the compare reconstruction, and `flux serve`.
---

# FluxState — compare (A/B & change-over-time) + viewer

A `.flux/` store with **2 captures** is an **A/B compare** (before → after); with **>2** it's a
continuous change-over-time timeline. Either way the diff is native — same `key_column` ties rows
across captures, so per-cell changes, adds, and drops fall out of the reconstruction.

## Build a 2-snapshot store to compare

```python
from fluxstate import FluxState
# capture A (before), then capture B (after) into the SAME store
FluxState(df_a, key_column="id", store_path="ab.flux").update_mirror_table(captured_at="2026-06-01T00:00:00Z")
FluxState(df_b, key_column="id", store_path="ab.flux").update_mirror_table(captured_at="2026-06-02T00:00:00Z")
```

Or from the CLI, one snapshot at a time:

```bash
flux capture ab.flux before.parquet --key id --at 2026-06-01T00:00:00Z
flux capture ab.flux after.parquet  --key id --at 2026-06-02T00:00:00Z
```

## Read the diff (headless)

```bash
flux travel ab.flux --as-of 2026-06-01T00:00:00Z --json   # state A (before)
flux travel ab.flux --as-of now --json                    # state B (after)
flux timeline ab.flux <id>                                 # per-cell before→after for one entity
```

Classify each entity by comparing its `row_state`/value at A vs B: **added** (unborn at A, active at
B), **dropped** (active at A, deleted at B), **changed** (value differs), unchanged otherwise.

## Open the interactive Temporal Viewer

```bash
flux serve ab.flux [--port 5173] [--no-open]     # boots Vite over the store, opens the browser
```

The viewer (Svelte 5 + DuckDB-WASM, reconstruction held identical to `reconstruct.py` by a parity
test) gives you: a **date scrubber** over a change-density histogram; **daff-style `old → new`**
diff on scrub; hover-peek + click-to-pin **per-cell history** (sparkline incl. `__deleted__` /
resurrection); lifecycle encodings (deleted stripe, resurrected `✦`, heat-tint, Δ gutter); an
**anomaly watcher** (immutable-column change → red ⚠; numeric direction ↑/↓); a simple + SQL `WHERE`
**filter** (meta-fields `changes`/`deleted`/`resurrected`/`flagged`/`id`); and a static/print audit
render.

## Try it fast with a generated store

```bash
uv run flux gen-fixture demo.flux --seed 42          # 1000×20 reproducible demo
uv run python scripts/schema_churn_demo.py churn.flux # column add/drop/rename showcase
uv run flux serve demo.flux
```

## Notes

- **Normalize noise, don't lose it:** FluxState logs format/precision changes faithfully; treat
  `82.00 == 82` as "unchanged" only at read/compare time (the viewer's `≈` toggle does this).
- For a wired A/B compare surface (`Before · Δ · After` stepper, `≈` normalize, clickable
  +added/−dropped/~changed filters), see the Pharos integration in `docs/PHAROS_INTEGRATION.md`.
