# File: TESTS/test_local_folder.py
"""Wave 4 (US1) — ``LocalFolderStore`` regression + faithful-recorder coverage.

``ChangeLogStore`` now defaults to ``storage.local_folder.LocalFolderStore``
and routes its manifest/events I/O through it (feature 003, T007/T008). These
tests pin that the on-disk ``.flux/`` format and every observable behavior
(capture, reconstruction, idempotency, type fidelity) are unchanged, PLUS
exercise the ``StorageBackend`` protocol methods directly against the backend,
and pin FR-014 (the faithful recorder never semantically normalizes a value —
a change in textual/serialized form alone IS recorded).
"""

from datetime import datetime, timezone

import polars as pl

from changelog import ChangeLogStore
import reconstruct
from storage.local_folder import LocalFolderStore

U = lambda *a: datetime(*a, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Default-backend wiring                                                      #
# --------------------------------------------------------------------------- #
def test_changelog_store_defaults_to_local_folder_store(tmp_store):
    store = ChangeLogStore(tmp_store)
    assert isinstance(store.backend, LocalFolderStore)
    assert store.backend.capabilities.is_table is False
    assert store.backend.capabilities.supports_atomic_meta is True
    assert store.backend.capabilities.supports_time_pushdown is False
    assert store.backend.capabilities.supports_mirror is False


def test_on_disk_layout_is_manifest_json_plus_events_parquet(tmp_store, df_day1):
    store = ChangeLogStore(tmp_store)
    store.capture(df_day1, key_column="id", captured_at=U(2026, 1, 1))

    assert (tmp_store / "manifest.json").exists()
    parquet_files = list((tmp_store / "events").glob("*.parquet"))
    assert len(parquet_files) == 1

    manifest = store.read_manifest()
    assert manifest["events"][0]["file"] == f"events/{parquet_files[0].name}"


# --------------------------------------------------------------------------- #
# Regression: capture -> reconstruct (as-of)                                  #
# --------------------------------------------------------------------------- #
def test_capture_and_reconstruct_as_of(tmp_store, df_day1, df_day2, df_day3):
    store = ChangeLogStore(tmp_store)
    store.capture(df_day1, key_column="id", captured_at=U(2026, 1, 1))
    store.capture(df_day2, key_column="id", captured_at=U(2026, 1, 2))
    store.capture(df_day3, key_column="id", captured_at=U(2026, 1, 3))

    day1 = reconstruct.build_mirror_view(store, U(2026, 1, 1))
    assert sorted(day1["id"].to_list()) == [1, 2, 3]

    day2 = reconstruct.build_mirror_view(store, U(2026, 1, 2))
    assert sorted(day2["id"].to_list()) == [1, 2, 3, 4]
    assert day2.filter(pl.col("id") == 2)["score"][0] == 25.0

    day3 = reconstruct.build_mirror_view(store, U(2026, 1, 3))
    # id 1 vanished in df_day3 (conftest docstring: "id 1 has vanished (delete)")
    assert sorted(day3["id"].to_list()) == [2, 3, 4]
    assert day3.filter(pl.col("id") == 2)["score"][0] == 27.5


# --------------------------------------------------------------------------- #
# Delete -> resurrection continuity                                           #
# --------------------------------------------------------------------------- #
def test_delete_then_resurrection_is_one_continuous_entity(tmp_store):
    store = ChangeLogStore(tmp_store)
    store.capture(pl.DataFrame({"id": [1, 2], "note": ["a", "b"]}),
                  key_column="id", captured_at=U(2026, 1, 1))
    store.capture(pl.DataFrame({"id": [1], "note": ["a"]}),
                  key_column="id", captured_at=U(2026, 1, 2))  # id=2 vanishes
    store.capture(pl.DataFrame({"id": [1, 2], "note": ["a", "b2"]}),
                  key_column="id", captured_at=U(2026, 1, 3))  # id=2 returns

    state = reconstruct.row_state(store, 2, "now")
    assert state == {"state": "active", "resurrected": True}

    timeline = reconstruct.get_timeline(store, 2, field="note")
    # continuous trail under the SAME entity_id: "b" (day1) ... "b2" (day3)
    assert [e["value"] for e in timeline] == ["b", "b2"]


# --------------------------------------------------------------------------- #
# Idempotent re-capture (no new events file)                                  #
# --------------------------------------------------------------------------- #
def test_idempotent_recapture_adds_no_new_events_file(tmp_store, df_day1, df_day2):
    store = ChangeLogStore(tmp_store)
    store.capture(df_day1, key_column="id", captured_at=U(2026, 1, 1))
    store.capture(df_day2, key_column="id", captured_at=U(2026, 1, 2))

    n_files_before = len(list((tmp_store / "events").glob("*.parquet")))
    n_events_before = len(store.read_manifest()["events"])

    result = store.capture(df_day2, key_column="id", captured_at=U(2026, 1, 2))

    assert result["noop"] is True
    assert result["events_added"] == 0
    assert len(list((tmp_store / "events").glob("*.parquet"))) == n_files_before
    assert len(store.read_manifest()["events"]) == n_events_before


# --------------------------------------------------------------------------- #
# Type fidelity                                                               #
# --------------------------------------------------------------------------- #
def test_type_fidelity_across_capture_and_reconstruct(tmp_store):
    store = ChangeLogStore(tmp_store)
    df = pl.DataFrame(
        {
            "id": pl.Series([1, 2], dtype=pl.Int64),
            "count": pl.Series([3, 7], dtype=pl.Int64),
            "score": pl.Series([1.5, -2.25], dtype=pl.Float64),
            "flag": pl.Series([True, False], dtype=pl.Boolean),
            "label": pl.Series(["alpha", "beta"], dtype=pl.Utf8),
            "seen_at": pl.Series(
                [U(2026, 1, 1), U(2026, 1, 2)], dtype=pl.Datetime("us", "UTC")
            ),
        }
    )
    store.capture(df, key_column="id", captured_at=U(2026, 1, 1))

    view = reconstruct.build_mirror_view(store, "now").sort("id")
    assert view.schema["count"] == pl.Int64
    assert view.schema["score"] == pl.Float64
    assert view.schema["flag"] == pl.Boolean
    assert view.schema["label"] == pl.Utf8
    assert view.schema["seen_at"] == pl.Datetime("us", "UTC")

    row = view.row(0, named=True)
    assert row["count"] == 3
    assert row["score"] == 1.5
    assert row["flag"] is True
    assert row["label"] == "alpha"
    assert row["seen_at"] == U(2026, 1, 1)


# --------------------------------------------------------------------------- #
# Faithful recorder (FR-014) — no semantic normalization at capture           #
# --------------------------------------------------------------------------- #
def test_faithful_recorder_records_format_only_changes(tmp_store):
    """A value changed only in SERIALIZED form — "82.00" -> "82", "MARGARET" ->
    "Margaret" — IS captured as a change. FluxState never semantic-equality-
    normalizes text at capture time; it diffs the literal cell value."""
    store = ChangeLogStore(tmp_store)
    df1 = pl.DataFrame({"id": [1, 2], "amount": ["82.00", "10"], "name": ["Margaret", "bob"]})
    store.capture(df1, key_column="id", captured_at=U(2026, 1, 1))

    # Same number/word, different text: "82.00"->"82", "Margaret"->"MARGARET".
    df2 = pl.DataFrame({"id": [1, 2], "amount": ["82", "10"], "name": ["MARGARET", "bob"]})
    result = store.capture(df2, key_column="id", captured_at=U(2026, 1, 2))

    assert result["noop"] is False
    assert result["events_added"] == 2  # only id=1's amount + name changed

    events = store._read_all_events()
    id1_day2 = events.filter(
        (pl.col("entity_id") == "1") & (pl.col("timestamp") == U(2026, 1, 2))
    )
    values = dict(zip(id1_day2["field"].to_list(), id1_day2["value"].to_list()))
    assert values == {"amount": "82", "name": "MARGARET"}

    # id=2 was untouched -> no event recorded for it on day 2.
    id2_day2 = events.filter(
        (pl.col("entity_id") == "2") & (pl.col("timestamp") == U(2026, 1, 2))
    )
    assert id2_day2.height == 0

    # And the timeline shows BOTH textual variants recorded, not collapsed as "unchanged".
    tl = reconstruct.get_timeline(store, 1, field="amount")
    assert [e["value"] for e in tl] == ["82.00", "82"]


# --------------------------------------------------------------------------- #
# StorageBackend protocol methods, exercised directly                        #
# --------------------------------------------------------------------------- #
def test_backend_protocol_methods_directly(tmp_store, df_day1, df_day2):
    store = ChangeLogStore(tmp_store)
    store.capture(df_day1, key_column="id", captured_at=U(2026, 1, 1))
    store.capture(df_day2, key_column="id", captured_at=U(2026, 1, 2))

    meta = store.backend.read_meta()
    assert meta.key_column == "id"
    assert set(meta.schema) == {"id", "name", "score", "active", "seen_at"}
    assert len(meta.event_catalog) == 2

    current = store.backend.read_current_state()
    assert sorted(current["id"].to_list()) == [1, 2, 3, 4]

    all_events = store.backend.read_events()
    assert all_events.height > 0

    # append_events (the generic Protocol entrypoint) is idempotent by snapshot_id:
    # re-appending the events of an already-committed capture is a no-op.
    events_for_day1 = store.backend.read_events(pl.col("timestamp") == U(2026, 1, 1))
    n_files_before = len(list((tmp_store / "events").glob("*.parquet")))
    ref = store.backend.append_events(events_for_day1)
    assert ref.snapshot_id == events_for_day1["snapshot_id"][0]
    assert len(list((tmp_store / "events").glob("*.parquet"))) == n_files_before

    # refresh_mirror is a documented no-op (capabilities.supports_mirror is False).
    assert store.backend.refresh_mirror() is None
