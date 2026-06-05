# File: TESTS/test_capture_scale.py
"""US1 — wide-table scale guard (SC-008).

A ~300-column table where only ~1–5% of rows change must capture via the
row-hash prefilter WITHOUT melting every cell — capture work stays
proportional to the number of *changed* rows, not rows × columns.

Written test-first: MUST FAIL until ``capture`` (T015) + the prefilter path
(T012/T014) are wired.
"""

import polars as pl

from changelog import ChangeLogStore

N_ROWS = 2000
N_COLS = 300


def _wide_frame(seed_offset=0, changed_ids=()):
    cols = {"id": pl.Series(list(range(N_ROWS)), dtype=pl.Int64)}
    changed = set(changed_ids)
    for c in range(N_COLS):
        base = [float(r * 1000 + c) for r in range(N_ROWS)]
        if seed_offset and changed:
            for r in changed:
                base[r] += seed_offset  # perturb only the changed rows
        cols[f"f{c}"] = pl.Series(base, dtype=pl.Float64)
    return pl.DataFrame(cols)


def test_wide_table_capture_is_row_proportional(tmp_store):
    store = ChangeLogStore(tmp_store)

    base = _wide_frame()
    store.capture(base, key_column="id")

    # ~2% of rows change (40 of 2000); each across all 300 columns.
    changed_ids = list(range(0, N_ROWS, 50))  # 40 rows
    nxt = _wide_frame(seed_offset=7.0, changed_ids=changed_ids)
    store.capture(nxt, key_column="id")

    manifest = store.read_manifest()
    assert len(manifest["events"]) == 2, "second capture appends exactly one events file"

    # Only changed cells are recorded: 40 rows × 300 cols = 12_000 events — NOT
    # the full 2000 × 300 = 600_000. This is the prefilter doing its job.
    latest = manifest["events"][-1]
    assert latest["row_count"] == len(changed_ids) * N_COLS, (
        f"expected {len(changed_ids) * N_COLS} change events, got {latest['row_count']}"
    )
    assert latest["row_count"] < N_ROWS * N_COLS // 10, "must not melt the whole frame"
