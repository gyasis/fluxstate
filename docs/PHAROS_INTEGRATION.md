# Integrating FluxState into Pharos

> **FluxState is a faithful recorder of what the source emits, not a semantic-equality engine.**
> Keep this in mind throughout: FluxState records the source's *serialized output*, verbatim. If a
> value's representation changes (`1.1` → `1.10`, a timezone shift), that **is** a change and it will
> appear — FluxState never canonicalizes values to decide they're "really equal."

This guide is for the **pharos side** — how Pharos consumes FluxState. There are two integration
modes, and Pharos already uses (or plans) both:

| Mode | Pharos surface | What FluxState provides | Needs the viewer? |
|---|---|---|---|
| **A. Library audit** | `harbor` (PHI de-id audit, PRD 017) | tamper-evident before→after snapshots via `ChangeLogStore` + signed `snapshot_id`s | No (library only) |
| **B. Temporal view** | **Table Gen** quick-view (schema-agnostic source) | a `.flux/` store of a monitored dbt/dev source + the embeddable visualizations | Yes (see [VISUALIZATIONS.md](./VISUALIZATIONS.md)) |

---

## 0. Packaging / dependency

FluxState is a uv/PEP-621 project (`hatchling` backend, committed `uv.lock`). Pull it in editable
for development, or add it as a path/VCS dependency:

```bash
# in the consuming Pharos service venv
uv add --editable ~/Documents/code/fluxstate         # or a path/git dependency
uv run python -c "import changelog, reconstruct; print('ok')"
```

Runtime deps are `polars, pyarrow, orjson, numpy, humanize, tqdm, pydantic` (lower-bound pins, no
upper caps — so they won't fight Pharos's resolver). Keep FluxState an **optional** import where it's
an audit layer (harbor already does this — if `polars`/FluxState isn't importable, harbor still signs
over content hashes and notes the snapshot layer was unavailable, rather than failing the run).

---

## Mode A — Library audit (the `harbor` pattern)

Harbor captures a **before** and **after** snapshot of a transformed dataset; the two content-derived
`snapshot_id`s are embedded in the audit pack and covered by the composite Ed25519+ML-DSA signature.
The pattern (see `harbor/audit/fluxsnap.py`) is the recommended shape for any audit use:

```python
from changelog import ChangeLogStore
import polars as pl

# 1. Positional key: a synthetic row index. This is the right choice for audit —
#    it's ALWAYS present and unique, so it sidesteps null/duplicate-key rejection
#    (see "Gotchas" below) and survives a schema/key change between before/after.
ROW_KEY = "__row__"
def framed(rows: list[dict]) -> pl.DataFrame:
    return pl.DataFrame([{ROW_KEY: i, **{k: ("" if v is None else str(v)) for k, v in r.items()}}
                         for i, r in enumerate(rows)])

store = ChangeLogStore(store_dir)                      # a .flux/ folder
store.capture(framed(before_rows), ROW_KEY)           # snapshot 1 (before)
r = store.capture(framed(after_rows), ROW_KEY)        # snapshot 2 (after)
before_id, after_id = ...                              # from snapshot_id of each capture
```

Why stringifying values is *correct* here, not lossy: FluxState stores every value as canonical text
with a `dtype` tag anyway. Pre-stringifying just makes the audit's intent explicit. The `snapshot_id`
is a SHA-256 over canonicalized row values — deterministic across processes, so the same content
always yields the same id (the basis of idempotency **and** tamper-evidence).

**What you get back:** `capture()` returns `{events_added, snapshot_id, file, noop}`. `noop=True`
means the snapshot's content was already recorded.

---

## Mode B — Temporal view of a monitored source (Table Gen)

This is the integration that surfaced issue #6: a Pharos **Table Gen** quick-view is schema-agnostic
and re-introspects on refresh, so a FluxState-monitored dbt/dev source is exactly the case where
**columns churn between runs**. The flow:

1. **Each refresh, capture the current snapshot** into the source's `.flux/` store:
   ```python
   store.capture(current_df, key_column=<the source's natural key>, captured_at=<run time>)
   ```
   Use the source's real primary key as `key_column` (not a positional index) so an entity's history
   is continuous across refreshes even as rows reorder.
2. **Serve the `.flux/` store to the viewer.** The viewer reads `manifest.json` + `events/*.parquet`
   directly via DuckDB-WASM and reconstructs any point in time. Embed the components you need — the
   scrubber, the as-of table, the inspector, the static audit view — per
   [VISUALIZATIONS.md](./VISUALIZATIONS.md) §1–§8 (each lists its props/data contract).

### Schema evolution is handled (issue #6 — the key thing for dbt sources)

As of the 2026-06 audit, capture/reconstruct tolerate a monitored table **gaining, dropping, or
renaming** columns between captures:

| Source change between refreshes | FluxState behavior |
|---|---|
| **Column added** | tracked from first appearance; as-of views **before** its introduction show it as `NULL`, **after** show its values. |
| **Column dropped** | historical (pre-drop) views keep the column + values; at/after the drop the column reads **`NULL`** (a field-level tombstone is emitted — no stale "ghost" value). |
| **Column renamed** (same values) | recorded as **drop-of-old + add-of-new** (not a silent no-op): old column tombstoned, new column set. |

The `manifest.json` schema is a **union with valid-time semantics** — a column is resolvable in any
view whose time falls in its lifetime. No schema migration or store rebuild is needed; it's all
append-only.

### What the viewer guarantees to a consumer

- **Deterministic row order** — mirror-view rows are key-sorted on both the Python and JS sides, so a
  given T always renders rows in the same order (issue #4).
- **Type-faithful values** — `int64` beyond 2⁵³ stays exact (BigInt); `±Infinity`/`NaN` render
  faithfully; numeric/datetime aren't string-cast in the engine.
- **Datetime display is ms-resolution** — the viewer is a `Date`-based *view*; the **store keeps full
  µs**. (Recorder vs. view — VD-3.) Datetime value cells render as `YYYY-MM-DD` in the table.
- **Parity** — the JS reconstruction is byte-parity with Python, enforced by a test on every probe
  (deleted/resurrected/genuine-null/big-int/same-timestamp-order).

---

## Gotchas / contract (learned from the 2026-06 pre-integration audit)

Feed these into the Pharos→FluxState boundary:

1. **Keys must be non-null and unique per snapshot.** `capture()` now **rejects** a null key value
   (it was silently committed-but-unreadable before) and rejects duplicate keys. For Table Gen, use a
   real PK; for audit, use a positional `__row__` (harbor pattern) so this is automatic.
2. **A representation change IS a change.** `1.1`→`1.10`, a Decimal scale change, a tz-format change
   will appear as changes. This is by design (faithful recorder). If Pharos wants semantic-equality,
   normalize *before* `capture()` — FluxState will not do it for you.
3. **`dtype` tag is the column metadata.** Every value is stored as text + a `dtype` tag. To get
   typed sort/compare on the Pharos side, re-cast from the tag — don't assume reconstruction hands
   back a native `Date`/`Decimal` (it returns the canonical text for those; numerics and datetimes in
   the viewer engine are cast, but exotic types stay text).
4. **`.flux/` store contract.** A folder with `manifest.json` (authoritative: schema union + valid
   event files + per-file ts range) + `events/*.parquet` (immutable, glob-readable:
   `SELECT * FROM '<name>.flux/events/*.parquet'`). Crash-safe: an orphan parquet not in the manifest
   is ignored. Idempotent: re-capturing identical content is a no-op.
5. **CLI is automation-safe.** `flux capture|travel|timeline|row-state|view|info|gen-fixture|serve`
   give clean stderr messages + non-zero exit codes on bad input (missing store/file, bad key, bad
   date) — safe to script from Pharos pipelines.

---

## Quick reference

- **Library API:** `changelog.ChangeLogStore.capture(df, key_column, captured_at)` ·
  `reconstruct.build_mirror_view(store, T)` / `as_of` / `get_timeline` / `row_state` / `change_count`.
- **CLI:** `uv run flux <cmd>` (see `specs/002-fluxstate-temporal-viewer/contracts/cli.md`).
- **Viewer + its data contracts:** [VISUALIZATIONS.md](./VISUALIZATIONS.md) and
  `specs/002-fluxstate-temporal-viewer/contracts/viewer-data.md` (VD-1…VD-6).
- **Store contract:** `specs/001-changelog-first-pivot/contracts/changelog-store.md` +
  `manifest.schema.json`.
- **Pharos side already in flight:** `harbor` audit (`services/harbor/harbor/audit/fluxsnap.py`,
  PRD 017) — Mode A reference.
