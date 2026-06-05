---
tags: [repo:fluxstate]
---

# FluxState — Changelog-First Pivot + CDC Hardening

**Ephemeral PRD** — delete when: P1 merged to fluxstate `main` AND the full test suite passes green on the new API.

- **Status:** DRAFT (design locked; ready for SpecKit → dev-kid)
- **Created:** 2026-06-05
- **Trigger:** Keep-vs-drop review of fluxstate → decision = KEEP + changelog-first pivot. This PRD feeds `/speckit-specify` then dev-kid execution.
- **Related:** repo `gyasis/fluxstate`, branch `fix/cdc-hardening`; design docs in `~/Documents/code/fluxstate/docs/viewer/`
**Branch:** fix/cdc-hardening
**Repo:** fluxstate
**Owner_path:** /home/gyasis/dev
**Branch_at_creation:** fix/cdc-hardening

---

## 1. Context

FluxState is a Python lib that tracks **one** table/view's **cell-level change history** for
Herself Health HCC risk-coding audit trails (HIPAA). A 5-round Claude × Gemini paired debate
(94% confidence) concluded: **KEEP it** (the single-table, engine-portable, cell-level historian
niche is defensible vs dbt-snapshot/Streams) **but execute a "changelog-first storage pivot"** —
the as-is implementation has scaling/quality bombs.

**Already shipped (P0, branch `fix/cdc-hardening`):**
- O(rows²) `update_mirror_table` → **O(rows)** via a `{key → index}` dict-map (smoke-verified, zero regression vs git-stash A/B).
- Removed stale incomplete fork `fluxstate_fixed.py`.

**This PRD = P1**, the changelog-first pivot: make an **append-only change-log** the source of
truth; the old JSON-in-cell "mirror table" becomes a **reconstructed view** so existing API
(`save_mirror_table`, `travel()`, `query_historical_value()`) keeps working. Hard constraint:
**lightweight** (no Delta/Iceberg dep tail; must stay DuckDB/Polars glob-readable).

A separate, already-prototyped deliverable — the **FluxState Viewer** (interactive temporal
table: capsule scrubber, daff-style `old→new` diff-on-scrub, hover/pin history, virtualized 5k
stress test, SQL filter) — has its dev spec + working HTML prototypes in
`~/Documents/code/fluxstate/docs/viewer/`. The viewer consumes this change-log; it is tracked
here as the downstream consumer but is NOT in P1's code scope.

## 2. Current Tasks

**P0 — DONE**
- [x] O(rows²) → O(rows) update via dict-map (smoke-verified)
- [x] Remove `fluxstate_fixed.py`

**P1 — changelog-first pivot (this cut)**
- [ ] `changelog.py` — `.flux/` folder writer: Polars keyed join-diff **+ row-hash prefilter**; append `events_<ts>.parquet`; update `manifest.json`
- [ ] Schema `entity_id, timestamp(UTC), field, value, dtype, snapshot_id`; **full-outer join** for deletes; `snapshot_id` anti-join for idempotency
- [ ] `dtype` type-tag (kills cast-everything-to-string loss); UTC-normalize timestamps
- [ ] Reconstruction: `asOf` / `rowState` / `get_timeline` over the change-log; `mirror_table` becomes a derived view
- [ ] `save_mirror_table(output_format ∈ {polars, arrow, parquet, csv})` — polars(DF) default, arrow zero-copy; LazyFrame internal
- [ ] Parquet row-group `min/max(ts)` metadata for file-skip time-travel
- [ ] Rewrite the 7 stale tests to the new API + add change-log / delete / resurrection / idempotency tests
- [ ] Verify end-to-end in the uv venv; keep deps minimal

**Fast-follow (NOT P1)**
- [ ] checkpoint compaction; Arrow/Lazy streaming reconstruction for the viewer
- [ ] `pack`/`unpack` (`<name>.flux/` folder ↔ `<name>.fluxpack` zip) for share/export
- [ ] Storage Adapter (LocalFS / S3 / Snowflake-stage)
- [ ] decide validator strictness (strict-raise vs lenient)

## 3. Subagent Log

| Spawn | Name | Purpose | Result |
|---|---|---|---|
| 2026-06-05 | Explore ×2 | read real fluxstate repo (mechanism, cost model, bugs, maturity) | grounded the keep/drop verdict + bug list |

## 4. Decisions Log

| Time | Decision | Rationale |
|---|---|---|
| 2026-06-05 | KEEP fluxstate, changelog-first pivot | 5-round Claude×Gemini debate, 94% conf; single-table+portable+cell-timeline niche defensible |
| 2026-06-05 | Diff = Polars keyed join-diff + row-hash prefilter | correctness (col-reorder/type immunity) + avoids 500k×300-col melt blowup on wide HCC tables |
| 2026-06-05 | Store = `<name>.flux/` FOLDER (manifest + events/*.parquet + checkpoints) | append-only, atomic commit, DuckDB-WASM glob-readable; NO Delta/Iceberg (dep tail), NO opaque container |
| 2026-06-05 | `pack`/`unpack` = zip export, fast-follow | live store can't be a zip (no cheap append); zip only for share |
| 2026-06-05 | Output = Polars DF default + Arrow; LazyFrame internal | Arrow = zero-copy to DuckDB-WASM/viewer; satisfies tests 5/7/8 |
| 2026-06-05 | P1 includes dtype + snapshot_id + deletes | cheap schema fields, correctness-critical; compaction/streaming deferred |
| 2026-06-05 | Validator strictness deferred; tests rewritable | strict-raise vs lenient decided later |

## 5. Open Items

- [ ] Validator strictness decision (deferred)
- [ ] Confirm HCC real column count (~300?) to size the row-hash prefilter / column-batched melt
- [ ] Where does the `.flux/` store live in the Herself Health pipeline (local vs Snowflake stage)?

## 6. Acceptance Criteria

- New snapshot → appends a single `events_<ts>.parquet`; **no full-table rewrite**.
- Re-running the same snapshot is idempotent (no double-append; `snapshot_id` anti-join).
- A row deleted then returned shows a continuous `active → __deleted__ → active` trail under one `entity_id`.
- Reconstruction (`asOf`/`get_timeline`) matches ground truth; numeric/datetime types restored via `dtype` (no string-cast loss).
- `save_mirror_table(output_format="polars")` returns a `pl.DataFrame`; `"arrow"` returns a `pa.Table`.
- The 7 previously-failing tests pass on the new API; new change-log tests pass; suite green in the venv.
- No Delta/Iceberg dependency added (lightweight constitution held).

## 6b. Ephemeral Marker

**Delete when:** P1 merged to fluxstate `main` AND full test suite green on the new API.

## 7. Pipeline (next steps for THIS PRD)

1. `/speckit-specify` from this PRD → `spec.md` (in fluxstate repo).
2. `/speckit-plan` + `/speckit-tasks` → dependency-ordered `tasks.md`.
3. `dev-kid` orchestrate/execute the waves (lightweight mode; constitution = "lightweight above all").

## 8. Revision Log

| Version | Date | Change |
|---|---|---|
| 0.1 | 2026-06-05 | Initial draft — P0 done, P1 design locked, ready for SpecKit/dev-kid |
