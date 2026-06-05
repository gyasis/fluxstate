# File: TESTS/test_changelog.py
"""US1 — append-only, immutable, glob-readable change capture (STORE-1/2/3, SC-001).

Written test-first: these assert the intended capture behavior and MUST FAIL
until ``ChangeLogStore.capture`` (T015) is implemented.
"""

import glob

import duckdb
import polars as pl

from changelog import ChangeLogStore


def _events(store_path):
    return sorted(glob.glob(str(store_path / "events" / "*.parquet")))


def test_first_capture_writes_one_file_and_manifest(tmp_store, df_day1):
    store = ChangeLogStore(tmp_store)
    store.capture(df_day1, key_column="id")

    assert len(_events(tmp_store)) == 1, "first capture must write exactly one events file (STORE-1)"
    manifest = store.read_manifest()
    assert len(manifest["events"]) == 1
    assert manifest["key_column"] == "id"


def test_second_differing_capture_appends_one_file_and_leaves_prior_unchanged(
    tmp_store, df_day1, df_day2
):
    store = ChangeLogStore(tmp_store)
    store.capture(df_day1, key_column="id")
    first = _events(tmp_store)[0]
    first_bytes = open(first, "rb").read()

    store.capture(df_day2, key_column="id")
    files = _events(tmp_store)
    assert len(files) == 2, "a second differing capture appends exactly one new file (STORE-1)"
    assert open(first, "rb").read() == first_bytes, "no prior events-file bytes may change (STORE-2)"


def test_store_is_glob_readable_by_duckdb(tmp_store, df_day1, df_day2):
    store = ChangeLogStore(tmp_store)
    store.capture(df_day1, key_column="id")
    store.capture(df_day2, key_column="id")

    glob_path = str(tmp_store / "events" / "*.parquet")
    rows = duckdb.sql(f"SELECT * FROM '{glob_path}'").fetchall()
    assert len(rows) > 0, "DuckDB must read all events via the parquet glob (STORE-3)"

    # The reconstructed change-event columns are present and well-formed.
    cols = [c[0] for c in duckdb.sql(f"DESCRIBE SELECT * FROM '{glob_path}'").fetchall()]
    for expected in ("entity_id", "timestamp", "field", "value", "dtype", "snapshot_id"):
        assert expected in cols
