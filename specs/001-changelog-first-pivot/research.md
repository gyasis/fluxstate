# Phase 0 Research: Changelog-First Storage Pivot

**Feature**: `001-changelog-first-pivot` · **Date**: 2026-06-05

The architecture is **locked** by the PRD and `docs/viewer/fluxstate-design-decision.md`
(P1 decisions A–D, 94% joint confidence). This file resolves the remaining open mechanics —
the items that would otherwise be `NEEDS CLARIFICATION` in Technical Context — so Phase 1 can
proceed with no ambiguity. Each item: **Decision / Rationale / Alternatives considered**.

---

## R1 — Change-detection algorithm (wide-table safe)

**Decision**: Polars **keyed full-outer join** of prior-state vs new snapshot on the key column,
gated by a **row-hash prefilter**. Compute a stable per-row hash over the non-key columns of both
sides; keep only rows whose hash differs (or that exist on only one side); **melt only those rows**
to long form; then filter `old != new` using null-safe `is_not_distinct_from`.

**Rationale**: The prefilter means the expensive melt touches only the ~1–5% of rows that actually
changed, avoiding a 500k×300-column unpivot blowup on wide HCC tables. A keyed join is immune to
column reordering and equivalent retyping (eliminates spurious diffs), unlike the old positional
per-row Python loop. Null-safe comparison prevents `None`-vs-`None` registering as a change.

**Alternatives considered**:
- *Per-cell Python loop (status quo)*: O(rows²) before P0, still positional/fragile — rejected.
- *Melt-then-join everything*: correct but materializes rows×cols long frame — blows up at HCC width.
- *External diff lib (daff) as the engine*: daff is a two-table renderer with no cross-snapshot
  memory; adopted later only as the audit **renderer**, never the detection engine.

---

## R2 — Row-hash: which hash, over what

**Decision**: Hash the **canonicalized non-key cell values per row** (stable column order, values
rendered with their dtype tag) into a single value via Polars' `hash` over the selected columns
(`pl.DataFrame.hash_rows` / `hash` expression). The same hash doubles as the basis for
`snapshot_id` granularity (see R3).

**Rationale**: A row-level hash is the cheapest correct prefilter — equal hash ⇒ row unchanged ⇒
skip. Canonical column ordering before hashing preserves column-reorder immunity. Reusing Polars'
native hashing avoids pulling in a crypto dependency (lightweight).

**Alternatives considered**:
- *Per-cell hashing*: finer but defeats the point — we want to skip whole unchanged rows fast.
- *hashlib/sha over stringified rows*: slower, extra serialization, no benefit over Polars hashing.
- *No prefilter*: correctness-equivalent but the melt cost is the documented blow-up risk.

---

## R3 — Idempotency: the `snapshot_id` anti-join

**Decision**: Each capture run carries a `snapshot_id` (a hash identifying the input snapshot /
batch). Before appending, **anti-join** candidate events against already-recorded events on
`(entity_id, field, snapshot_id)`; if the snapshot's events already exist, append nothing.

**Rationale**: Re-running the same snapshot must be a no-op (FR-004 / SC-002). Keying idempotency
on `snapshot_id` (rather than wall-clock time) makes "same input ⇒ same identity" deterministic and
lets the manifest record which batch produced which file for audit.

**Alternatives considered**:
- *Dedup on `(entity_id, field, value, timestamp)`*: timestamp drift between runs breaks it.
- *Caller-supplied run id only*: works but we still want content-derived identity for true idempotency.

---

## R4 — Store layout & atomic commit

**Decision**: `<name>.flux/` folder = `manifest.json` (authoritative: schema + ordered list of
valid event files + checkpoints) + `events/<ts>.parquet` (immutable) + `checkpoints/` (reserved).
Commit protocol: write the new parquet to a temp name → fsync → atomically rename into `events/` →
rewrite `manifest.json` via temp-file + atomic rename. A reader trusts **only files listed in the
manifest**; an orphan parquet without a manifest entry is ignored (failed/partial capture).

**Rationale**: Gives Delta-class atomicity with zero Delta dependency (FR-013 atomic, FR-014
glob-readable). The manifest-as-commit-point means a crash mid-write never yields a half-valid
history. Plain JSON + parquet stays DuckDB-WASM `SELECT *`-readable.

**Alternatives considered**:
- *Delta Lake / Iceberg*: exactly the heavy dep tail the constitution forbids.
- *Single growing parquet*: parquet is immutable; appending means rewrite (the P1 scaling bomb).
- *Directory listing as source of truth (no manifest)*: can't distinguish committed vs partial files.

---

## R5 — File-skip time-travel (parquet row-group metadata)

**Decision**: When writing `events/<ts>.parquet`, stamp `min(timestamp)`/`max(timestamp)` into the
parquet **row-group statistics** (native to pyarrow/polars parquet writes; optionally mirror the
range in the manifest entry). Reconstruction for `asOf(T)` / a time window reads the manifest (and
row-group stats) to **skip whole files** whose range is entirely after `T` (or outside the window).

**Rationale**: Turns time-travel into a file-pruned scan instead of reading all history — the
"Delta-class perf, zero Delta dep" decision (B). Mirroring the range in the manifest lets a reader
prune without even opening the parquet footers.

**Alternatives considered**:
- *Scan all events then filter*: O(history) per query — degrades as the log deepens.
- *External index/db of ranges*: extra moving part; the manifest already serves this.
- *Checkpoint compaction*: complementary and deferred to fast-follow (keeps whole-table travel sub-O(N)).

---

## R6 — Type fidelity (`dtype` tag) & UTC

**Decision**: Store `value` as a string alongside a `dtype` type tag (e.g. `int64`, `float64`,
`utf8`, `datetime[us, UTC]`, `bool`, `null`). On reconstruction, cast each value back to `dtype`.
Normalize all timestamps to **UTC** at capture (tz-aware → convert; tz-naive → assume/declare UTC).
A genuine `None`/null is recorded as `dtype=null` (or value=null), distinct from a literal `"NULL"`
string — killing the legacy sentinel collision.

**Rationale**: Ends cast-everything-to-string precision loss (FR-007, SC-004) and the `"NULL"`-vs-
`None` collision (design-decision MED bugs). UTC normalization makes ordering and `asOf` comparisons
correct across mixed-tz inputs (FR-008).

**Alternatives considered**:
- *Store native parquet-typed value columns per dtype*: schema explosion / sparse columns — heavier.
- *Keep all-string (status quo)*: the exact quality bomb this pivot exists to fix.

---

## R7 — Output formats & LazyFrame internals

**Decision**: `save_mirror_table(output_format ∈ {polars, arrow, parquet, csv})` — `polars`
(default) returns a `pl.DataFrame`, `arrow` returns a `pa.Table` (zero-copy to DuckDB-WASM/the
viewer), `parquet`/`csv` write files. Reconstruction is built on a **Polars LazyFrame** internally
(kept private) so windowed/streaming reconstruction can be added in fast-follow without an API change.

**Rationale**: Satisfies FR-012 / SC-005 and the viewer's zero-copy need (decision C). LazyFrame
internal-only keeps the public surface small while leaving the streaming door open (YAGNI-respecting).

**Alternatives considered**:
- *Eager-only*: forecloses the streaming reconstruction the viewer will want.
- *Expose LazyFrame publicly now*: premature surface area before the streaming use case lands.

---

## R8 — Test decoupling from Snowflake (the rewrite path)

**Decision**: Rewrite the legacy `TESTS/Unit_1..7` (currently `unittest` + live
`snowflake.snowpark` sessions) to **pytest** fixtures that feed **Polars `DataFrame`s directly** to
`FluxState` — no Snowflake connection. New change-log/lifecycle/idempotency/reconstruct/output tests
follow the same in-memory pattern, writing the `.flux/` store to a `tmp_path`.

**Rationale**: The 7 tests "previously failing" are V1-Pydantic / pre-`output_format` drift **and**
bound to an external warehouse, making them non-hermetic. FluxState's contract is "Polars on
anything" — the table source is a `pl.DataFrame`, so tests need no Snowflake. Hermetic tests are a
precondition for the "suite green in the venv" success criterion (SC-006).

**Alternatives considered**:
- *Keep Snowpark in tests*: non-hermetic, needs creds, can't run in CI/venv — rejected.
- *Mock Snowpark*: more mock scaffolding than just handing in a DataFrame; no added coverage.

---

## R9 — Validator strictness (explicitly deferred)

**Decision**: Leave `mirror_validator.py` as-is for P1; do **not** wire strict-raise behavior into
the capture path. Capture coerces/tags via the `dtype` mechanism and surfaces discrepancies
leniently. The strict-raise-vs-lenient decision is a separate, later cut.

**Rationale**: PRD decision 5 / Open Item — tests are rewritable and the strictness choice doesn't
block the storage pivot. Deferring keeps P1 scoped (G6 simplicity).

**Alternatives considered**:
- *Decide strictness now*: out of scope, expands P1, no design consensus yet.

---

## Resolved unknowns summary

| Technical Context item | Resolution |
|---|---|
| Change-detection at HCC width | R1 keyed join-diff + R2 row-hash prefilter |
| Idempotency mechanism | R3 `snapshot_id` anti-join on `(entity_id, field, snapshot_id)` |
| Atomic, glob-readable store | R4 `.flux/` folder + manifest-as-commit |
| Time-travel performance | R5 parquet row-group `min/max(ts)` file-skip |
| Type loss / tz | R6 `dtype` tag + UTC normalization |
| Output formats / streaming door | R7 `{polars,arrow,parquet,csv}`, LazyFrame internal |
| Hermetic tests | R8 pytest + in-memory `pl.DataFrame`, `tmp_path` store |
| Validator strictness | R9 deferred (no P1 change) |

**No `NEEDS CLARIFICATION` markers remain.** Proceed to Phase 1.
