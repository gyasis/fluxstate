# FluxState — Design Decision Record

**Date:** 2026-06-05
**Method:** 5-round Claude × Gemini paired debate + 2 adversarial Explore passes over the real repo (`gyasis/fluxstate`, ~6.3K LOC)
**Joint confidence:** 94%
**Companion:** `fluxstate-keep-or-drop-onepager.html` (same folder)

---

## Verdict

**KEEP FluxState — but execute a "changelog-first storage pivot."**
Do **not** drop it for dbt-snapshot / Snowflake Streams. Do **not** keep it as-is.

The defensible niche: a **single-table/view**, **engine-portable** (Polars-on-anything),
**cell-level** change historian that emits a ready-to-render per-cell timeline and runs
with **zero DDL rights, off Snowflake entirely**. No existing tool owns that exact sliver.

Scored matrix (weighted for a solo eng — HCC audit today, reuse tomorrow):
KEEP-storage-pivot **95** · KEEP-as-is 92 · DROP→dbt-snapshot 85 · DROP→Streams 77.

---

## When to revisit (DROP triggers)

Flip to dbt-snapshot / Streams if **any**:
- dataset > 10M rows or high-velocity
- primary query is population-wide compliance ("which 5,000 changed X in Q3")
- **weekly** whole-table point-in-time reconstruction ("state of all 50k patients on date T") — *the single biggest flip-risk*
- you have AccountAdmin **and** the source is permanently Snowflake-only

KEEP overrides even at scale if: no CREATE-STREAM rights (shadow-IT analyst),
schema drifts weekly (ragged source), or the source has no clean primary keys.

---

## The build (on the existing code)

| Step | What | Effort | Blocker |
|---|---|---|---|
| **P0** | Kill `fluxstate_fixed.py`; replace O(R²) `.index()`-in-loop update with a **Polars keyed join-diff** (join on PK → unpivot → filter old≠new) | ~1d | YES |
| **P1** | Redirect `save_mirror_table` from full-parquet-rewrite to **append-only change-log**; write one `events_<ts>.parquet` per run into a directory (parquet is immutable — never re-write one file) | ~1d | YES |
| **P2** | **Tagged-value type fix**: store `value` + `dtype` tag, cast back on read. Ends string-cast type loss + `"NULL"`-vs-`None` collision (Polars `is_null`/`fill_null`). Force all timestamps to **UTC** | ~2d | YES |
| **P3** | `get_timeline(id)` reconstruction (lazy scan→filter→sort→struct) rebuilds the ergonomic `[{date,value}]` for the auditor UI — without storing JSON-in-cell | ~1d | no |
| **P4** | Idempotency anti-join on `(entity_id, field, snapshot_id)`; **FULL** join so vanished rows log a `__deleted__` event | ~0.5d | no |

### Change-log schema
`change_log/events_<ts>.parquet` columns:
`entity_id`, `timestamp` (UTC), `field`, `value` (string), `dtype` (type tag), `snapshot_id` (row hash for idempotency)

### Disappearing / reappearing rows (the resurrection trail)
Per run, **FULL outer join** old ⟕ new on the primary key:
- in OLD, not NEW → log `__deleted__` (value=None, ts=now)
- in NEW, not OLD → already in changelog? never-seen = INSERT; seen+deleted = **RESURRECTION** (append SET events to the **same** `entity_id`)
- in both, cell differs → normal change event

Because a resurrected row carries the same `entity_id`, its timeline is continuous:
`active → (deleted) → active again`. This cross-time trail is the one thing
dbt-snapshot / Oxen / daff do **not** give for free — it's FluxState's strongest reason to exist.

---

## Diff & render — current scope

The whole job right now is the **command-line core**: the keyed diff + change-log + the
reconstruction/timeline commands. The diff engine is **~20 lines of Polars** (keyed join-diff) —
no external diff dependency needed for detection.

**daff — IN THE SPEC (now).** The one external tool we adopt, and only as the **renderer**:
`daff.DiffRender().html()` turns a two-snapshot diff into a color-coded, column-reorder-aware
before/after HTML table for a compliance officer. It is *not* the engine (Polars detects
changes) — it's the human-facing audit render behind `flux audit-bundle <id>`.
Optional import; pure library; no service.
- Visual reference: `fluxstate-daff-audit-render.html` (same folder) — working mock of the
  render plus the cross-time timeline + resurrection trail daff alone can't produce.

**Both daff and the Polars diff are two-table diffs** — no memory across snapshots, so they
can't track resurrection over time. That's FluxState's job (the keyed, time-ordered changelog).

---

### 🛰️ Oxen — FAR FUTURE only (out of current scope)
Not now. Revisit **only** when FluxState grows from "one table's historian" into versioning
**bigger views / a corpus of tables** — git-style branch/commit/diff across many files, team
collaboration, reproducible dataset snapshots. Until that day the Polars keyed diff is enough.
Bookmark, not a dependency: https://docs.oxen.ai/concepts/diffs

---

## Adversarial audit — real bugs (fix order = P0→P2)

- **HIGH** — O(rows²) update: linear `.index()` inside per-row loop (L226/253)
- **HIGH** — full-parquet rewrite every run: O(R×C×H) scaling bomb (L289/294)
- **HIGH** — two divergent files (`fluxstate.py` vs `fluxstate_fixed.py`)
- **MED** — all non-key cols cast to `Utf8` → type/precision loss (L61-65)
- **MED** — sentinel `"NULL"` collides with real `None` (L49/L218)
- **MED** — JSON-in-cell anti-pattern kills columnar compression (L289)
- **MED** — Pydantic validator runs on init only, never on update

Maturity: prototype/late-dev (~85% claimed, inflated); smoke tests on 5–10 rows; **zero** scale tests.
Real use: Herself Health HCC risk-coding audit trail.

---

## Improvements worth adding (beyond P0–P4)

- **Storage Adapter** interface (LocalFS / S3 / Snowflake-stage) so "engine-agnostic" is actually true
- **Checkpoint compaction** every N runs → keeps whole-table time-travel from going O(N)
- **Row-hash skip**: a tiny `(id, row_hash)` summary skips unchanged rows instantly at scale
- **`generate_audit_bundle(id)`** → self-contained offline HTML (daff render) for compliance

---

## P1 architecture — LOCKED decisions (2026-06-05, Claude × Gemini debate)

**P0 already shipped on branch `fix/cdc-hardening`:** O(rows²) update → O(rows) via a
`{key → index}` dict-map (smoke-verified, zero regression); stale `fluxstate_fixed.py` removed.

The "changelog-first pivot": **source of truth = an append-only change-log**; the old
JSON-in-cell mirror becomes a **reconstructed view** over it (so `save_mirror_table`,
`travel()`, `query_historical_value()` keep working).

| # | Decision | Detail |
|---|---|---|
| A | **Diff = Polars keyed join-diff + row-hash prefilter** | full-outer join prev ⟗ new on key → keep only rows whose row-hash changed (~1–5%) → melt *those* → filter `old != new` (null-safe `is_not_distinct_from`). The prefilter avoids the 500k×300-col melt blowup — wide-table safe. Replaces the Python per-cell loop (correctness: column-reorder + type-change immunity). |
| B | **Store = a `<name>.flux/` FOLDER** | `manifest.json` (source of truth: schema + valid file list) + `events/*.parquet` (immutable; append = new file) + `checkpoints/`. Atomic commit = write parquet → update manifest. Stamp `min/max(ts)` into **parquet row-group metadata** so Polars/DuckDB skip whole files on time-travel (Delta-class perf, zero Delta dep). **No** Delta/Iceberg (heavy dep tail), **no** opaque container — must stay `SELECT * FROM '<name>.flux/events/*.parquet'`-readable (DuckDB-WASM). |
| B2 | **`pack` / `unpack` = zip export** | `<name>.flux/` (folder) ↔ `<name>.fluxpack` (zip) for sharing/archive only — **fast-follow, not P1**. You cannot append into a zip cheaply, so the live store is never a zip. |
| C | **Output = Polars DataFrame (default) + Arrow Table** | `output_format ∈ {polars (default), arrow, parquet, csv}`. Arrow = zero-copy to DuckDB-WASM / the viewer. **LazyFrame stays internal** (powers windowed streaming reconstruction). Satisfies tests 5/7/8 (`output_format="polars"` returns a DF). |
| D | **P1 includes: `dtype` tag + `snapshot_id` + full-outer-join deletes** | cheap schema fields, correctness-critical (kills cast-to-string loss; idempotency/which-batch audit; prevents "ghost patient" accumulation). **Deferred to fast-follow:** checkpoint compaction, Arrow/Lazy streaming-reconstruction for the viewer, `pack`/`unpack`. |
| 5 | **Validator strictness — deferred** | tests are rewritable; strict-raise vs lenient-coerce decided later. |

**Change-log schema:** `entity_id, timestamp(UTC), field, value, dtype, snapshot_id`
**The 7 failing tests** will be rewritten to the new API as part of P1 (they were V1-Pydantic /
pre-`output_format` drift — see git history).
