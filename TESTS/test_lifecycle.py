# File: TESTS/test_lifecycle.py
"""US3 — delete & resurrection continuity (SC-003 / API-5).

A row present → deleted → present again is one continuous trail under a single
``entity_id``: a vanished row logs a single ``__deleted__`` marker (value null),
reappearance appends SET events to the SAME entity_id (resurrection, not a new
identity), and ``row_state`` reports the deleted window correctly.
"""

from datetime import datetime, timezone

import polars as pl

from changelog import ChangeLogStore, DELETED_FIELD
import reconstruct

U = lambda *a: datetime(*a, tzinfo=timezone.utc)


def _lifecycle_store(tmp_store):
    store = ChangeLogStore(tmp_store)
    # Day 1: id 3 present
    store.capture(pl.DataFrame({"id": [1, 2, 3], "risk": [0.4, 0.7, 0.2]}),
                  key_column="id", captured_at=U(2026, 1, 1))
    # Day 2: id 3 vanishes
    store.capture(pl.DataFrame({"id": [1, 2], "risk": [0.4, 0.9]}),
                  key_column="id", captured_at=U(2026, 1, 2))
    # Day 3: id 3 returns
    store.capture(pl.DataFrame({"id": [1, 2, 3], "risk": [0.4, 0.9, 0.5]}),
                  key_column="id", captured_at=U(2026, 1, 3))
    return store


def test_vanished_row_logs_single_deleted_marker(tmp_store):
    store = ChangeLogStore(tmp_store)
    store.capture(pl.DataFrame({"id": [1, 2, 3], "risk": [0.4, 0.7, 0.2], "status": ["A", "A", "B"]}),
                  key_column="id", captured_at=U(2026, 1, 1))
    store.capture(pl.DataFrame({"id": [1, 2], "risk": [0.4, 0.9], "status": ["A", "A"]}),
                  key_column="id", captured_at=U(2026, 1, 2))

    events = store._read_all_events()
    del_rows = events.filter((pl.col("entity_id") == "3") & (pl.col("field") == DELETED_FIELD))
    # exactly ONE __deleted__ marker for id 3 (not one null row per column)
    assert del_rows.height == 1, f"expected 1 __deleted__ marker, got {del_rows.height}"
    assert del_rows["value"][0] is None
    assert del_rows["dtype"][0] == "null"


def test_resurrection_is_same_entity_continuous_timeline(tmp_store):
    store = _lifecycle_store(tmp_store)

    # The full risk timeline for id 3 lives under ONE entity_id: 0.2 (day1) → 0.5 (day3)
    tl = reconstruct.get_timeline(store, 3, field="risk")
    assert [e["value"] for e in tl] == [0.2, 0.5], tl

    # Full lifecycle timeline (all fields) shows the __deleted__ marker between them.
    full = reconstruct.get_timeline(store, 3)
    fields_in_order = [e["field"] for e in full]
    assert DELETED_FIELD in fields_in_order, fields_in_order


def test_row_state_across_lifecycle(tmp_store):
    store = _lifecycle_store(tmp_store)

    assert reconstruct.row_state(store, 3, U(2026, 1, 1)) == {"state": "active", "resurrected": False}
    assert reconstruct.row_state(store, 3, U(2026, 1, 2)) == {"state": "deleted", "resurrected": False}
    assert reconstruct.row_state(store, 3, U(2026, 1, 3)) == {"state": "active", "resurrected": True}
    # before its first appearance → unborn
    assert reconstruct.row_state(store, 3, U(2025, 12, 31))["state"] == "unborn"


def test_deleted_row_omitted_from_mirror_view_during_deleted_window(tmp_store):
    store = _lifecycle_store(tmp_store)
    day2 = reconstruct.build_mirror_view(store, U(2026, 1, 2))
    assert "3" not in [str(x) for x in day2["id"].to_list()], "deleted row must be omitted at day2"
    # after resurrection it is back
    now = reconstruct.build_mirror_view(store, "now")
    assert 3 in now["id"].to_list()
