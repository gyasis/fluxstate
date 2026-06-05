# File: TESTS/test_reconstruct.py
"""US2 — historical reconstruction with type fidelity (SC-004 / API-4).

``as_of`` / ``get_timeline`` must match ground truth across timestamps and return
values in their ORIGINAL types (not string-cast); ``travel`` before any history
returns empty (not an error).
"""

from datetime import datetime, timezone

import polars as pl

from changelog import ChangeLogStore
import reconstruct

U = lambda *a: datetime(*a, tzinfo=timezone.utc)


def _seed(store):
    """Three dated captures of a small typed table (id, risk:float, status:str)."""
    store.capture(
        pl.DataFrame({"id": [1, 2], "risk": [0.4, 0.7], "status": ["A", "A"]}),
        key_column="id", captured_at=U(2026, 1, 1),
    )
    store.capture(
        pl.DataFrame({"id": [1, 2], "risk": [0.4, 0.9], "status": ["A", "B"]}),
        key_column="id", captured_at=U(2026, 1, 2),
    )
    store.capture(
        pl.DataFrame({"id": [1, 2], "risk": [0.5, 0.9], "status": ["A", "B"]}),
        key_column="id", captured_at=U(2026, 1, 3),
    )


def test_as_of_matches_ground_truth_across_time(tmp_store):
    store = ChangeLogStore(tmp_store)
    _seed(store)

    # id=2 risk: 0.7 (Jan1) → 0.9 (Jan2), unchanged Jan3
    assert reconstruct.as_of(store, 2, "risk", U(2026, 1, 1))["value"] == 0.7
    assert reconstruct.as_of(store, 2, "risk", U(2026, 1, 2))["value"] == 0.9
    # a point between captures resolves to the latest prior value
    assert reconstruct.as_of(store, 2, "risk", U(2026, 1, 2, 12))["value"] == 0.9
    # before any history → None
    assert reconstruct.as_of(store, 2, "risk", U(2025, 12, 31)) is None


def test_values_come_back_typed_not_string(tmp_store):
    store = ChangeLogStore(tmp_store)
    _seed(store)
    v = reconstruct.as_of(store, 1, "risk", U(2026, 1, 3))["value"]
    assert isinstance(v, float) and v == 0.5, f"risk must be float, got {type(v)}"


def test_get_timeline_is_typed_and_ordered(tmp_store):
    store = ChangeLogStore(tmp_store)
    _seed(store)
    tl = reconstruct.get_timeline(store, 2, field="risk")
    assert [e["value"] for e in tl] == [0.7, 0.9], tl
    assert all(isinstance(e["value"], float) for e in tl)
    # dates ascending
    assert [e["date"] for e in tl] == sorted(e["date"] for e in tl)


def test_series_level_as_of_distinct_from_public(tmp_store):
    # I2: the internal series-level resolver works on a raw history list.
    history = [{"date": U(2026, 1, 1), "value": 0.7}, {"date": U(2026, 1, 2), "value": 0.9}]
    assert reconstruct._as_of(history, U(2026, 1, 1, 12))["value"] == 0.7
    assert reconstruct._as_of(history, U(2025, 1, 1)) is None


def test_travel_before_history_is_empty_not_error(tmp_store):
    """travel() before any captured history returns an empty frame (T021/API-4)."""
    from fluxstate import FluxState

    store = ChangeLogStore(tmp_store)
    _seed(store)
    fs = FluxState(
        pl.DataFrame({"id": [1, 2], "risk": [0.5, 0.9], "status": ["A", "B"]}),
        key_column="id", store_path=str(tmp_store),
    )
    result = fs.travel("2025-12-31T00:00:00Z")
    assert isinstance(result, pl.DataFrame) and result.height == 0
