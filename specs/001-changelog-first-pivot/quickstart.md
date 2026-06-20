# Quickstart: Changelog-First Storage Pivot

**Feature**: `001-changelog-first-pivot` · **Date**: 2026-06-05

How the pivoted FluxState is used and validated. Mirrors the spec's acceptance scenarios so it
doubles as a manual smoke checklist.

---

## Environment

```bash
cd ~/Documents/code/fluxstate
uv sync                                   # deps from uv.lock: polars, pyarrow, orjson, numpy, humanize, tqdm (+ dev group)
```
No new runtime dependency is added in P1. Tests run **without** Snowflake (in-memory `pl.DataFrame`).

---

## Capture → append-only change-log

```python
import polars as pl
from fluxstate import FluxState

# Day 1: first snapshot
t1 = pl.DataFrame({"id": [1, 2, 3], "risk": [0.4, 0.7, 0.2], "status": ["A", "A", "B"]})
fs = FluxState(t1, key_column="id")
fs.update_mirror_table()          # writes <name>.flux/events/<ts1>.parquet + manifest.json

# Day 2: one cell changes; row 3 disappears
t2 = pl.DataFrame({"id": [1, 2], "risk": [0.4, 0.9], "status": ["A", "A"]})
fs = FluxState(t2, key_column="id")
fs.update_mirror_table()          # appends ONE new events file; row 3 logged __deleted__

# Re-run day 2 (idempotent): no new file, store unchanged
fs.update_mirror_table()
```

**Expected**: `events/` holds exactly two parquet files after day 2; the third call adds none.
(SC-001, SC-002 / STORE-1, STORE-2)

---

## Time-travel & reconstruction with type fidelity

```python
mirror_now = fs.save_mirror_table(output_format="polars")   # pl.DataFrame, current state
arrow_now  = fs.save_mirror_table(output_format="arrow")    # pa.Table (zero-copy)

state_day1 = fs.travel("2026-06-05T00:00:00Z")              # state as of day 1
v = fs.query_historical_value("2026-06-05T00:00:00Z")       # risk is float, not "0.4" string
timeline = fs.get_timeline(entity_id=2, field="risk")        # [{date, value}, …], values typed
```

**Expected**: `state_day1` shows row 3 present; numeric/datetime values come back with their
original dtype (SC-004 / API-4).

---

## Delete → resurrection continuity

```python
# Day 3: row 3 returns
t3 = pl.DataFrame({"id": [1, 2, 3], "risk": [0.4, 0.9, 0.5], "status": ["A", "A", "B"]})
fs = FluxState(t3, key_column="id")
fs.update_mirror_table()

fs.row_state(entity_id=3, T="now")          # {"state": "active", "resurrected": True}
fs.get_timeline(entity_id=3, field="risk")  # active → __deleted__ → active, ONE entity_id
```

**Expected**: a single continuous trail under `id=3`, not a new identity (SC-003 / API-5).

---

## Glob-readability (portability check)

```sql
-- DuckDB (or DuckDB-WASM in the browser) reads the store with no FluxState code:
SELECT * FROM '<name>.flux/events/*.parquet';
```
**Expected**: all change events returned; no Delta/Iceberg directory present (STORE-3, STORE-6 / G1, G2).

---

## Test suite (hermetic)

```bash
pytest TESTS/ -q
```
**Expected (SC-006)**: the rewritten `Unit_1..7` pass on the new API, the new
`test_changelog / test_idempotency / test_lifecycle / test_reconstruct / test_output_formats`
pass, and the full suite is green in the venv — no Snowflake connection required.

---

## Acceptance ↔ artifact map

| Spec criterion | Verified by |
|---|---|
| SC-001 append-only, no rewrite | capture section / STORE-1, STORE-2 |
| SC-002 idempotent re-capture | third `update_mirror_table` call / API-2 |
| SC-003 delete+resurrection continuity | resurrection section / API-5 |
| SC-004 reconstruction + type fidelity | time-travel section / API-4 |
| SC-005 multi-format output parity | `output_format` calls / API-3 |
| SC-006 suite green in venv | `pytest TESTS/` |
| SC-007 no heavy dep / glob-readable | DuckDB query / STORE-3, STORE-6 |
| SC-008 wide-table O(rows) capture | row-hash prefilter (perf test, /speckit.tasks) |
