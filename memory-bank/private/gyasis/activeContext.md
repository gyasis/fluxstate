# Active Context

**Last Updated**: 2026-06-05 18:20:09

## Current Focus
feat: changelog-first storage pivot (001)

Replace the in-memory JSON-in-cell mirror table with an append-only,
glob-readable change-log store. History is captured as immutable Parquet
event files under <name>.flux/ and reconstructed on read with full type
fidelity. Runtime stays Polars + PyArrow only — no Delta/Iceberg, no new
runtime dependency.

New modules
- changelog.py  — ChangeLogStore: dtype codec, UTC normalization, Change
  Event schema, row-hash-prefiltered keyed diff, content-derived snapshot_id,
  atomic events writer (temp→fsync→rename), manifest commit, current-state
  read-back. INSERT/UPDATE/DELETE(+resurrection) via __deleted__ markers.
- reconstruct.py — as_of / get_timeline / change_count / build_mirror_view /
  row_state over a Polars LazyFrame, with file-skip pruning.

Facade (fluxstate.py), signatures preserved (FR-011)
- update_mirror_table() now performs an idempotent capture.
- save_mirror_table(..., output_format=) with the I1 precedence rule
  (polars/arrow/parquet/csv).
- travel / query_historical_value reconstruct from the change-log (typed).
- get_change_statistics / filter / filter_for_null_values recomputed over
  the reconstructed view. as_of/get_timeline/change_count/row_state exposed.
- Removed a dead pandas import (kept Polars-first; no new dep).

Tests (hermetic, no Snowflake) — 21 passing across US1–US4:
append-only, idempotency, wide-table O(rows), reconstruction+type fidelity,
delete/resurrection continuity, multi-format parity. Legacy Snowflake-coupled
unit tests retired to TESTS/legacy/. duckdb+pytz added as test-only deps.

Docs: docs/API.md (full reference + runnable usage guide); README updated.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>

## Recent Changes
```
 .claude/activity_stream.md                  | 12 ++++++
 memory-bank/private/gyasis/activeContext.md | 50 ++++++++++++++++--------
 memory-bank/private/gyasis/progress.md      |  2 +-
 3 files changed, 47 insertions(+), 17 deletions(-)
```

## Modified Files
.claude/activity_stream.md
memory-bank/private/gyasis/activeContext.md
memory-bank/private/gyasis/progress.md

## Next Actions
- Continue implementation
- Run tests
- Create checkpoint
