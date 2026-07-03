"""Regression tests for the 2026-07-03 adversarial audit of feature 003.

Each test pins a CONFIRMED bug the audit found in code paths the main suite
didn't exercise. See the audit verdict table (F1/F2/H3/H7).
"""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from fluxstate import FluxState
from storage import TableBackend, select_backend


def test_f1_table_uri_scheme_is_stripped(tmp_path):
    """F1: `select_backend('table://<path>')` must yield an ABSOLUTE, usable path,
    not `Path('table:/<path>')` (relative, silently wrong dir)."""
    abs_dir = tmp_path / "flux_events"
    b = select_backend(f"table://{abs_dir}")  # e.g. table:///tmp/.../flux_events
    assert isinstance(b, TableBackend)
    assert b.events_path.is_absolute()
    assert b.events_path == abs_dir


def test_f2_repeated_empty_capture_still_deletes(tmp_path):
    """F2: an empty snapshot always hashes to the same snapshot_id; the 2nd
    'delete everything' must NOT be dropped as a false idempotent duplicate."""
    store = str(tmp_path / "t.flux")
    empty = pl.DataFrame({"id": [], "x": []}, schema={"id": pl.Utf8, "x": pl.Int64})

    FluxState(pl.DataFrame({"id": ["A", "B"], "x": [1, 2]}), key_column="id", store_path=store).update_mirror_table()
    FluxState(empty, key_column="id", store_path=store).update_mirror_table()          # delete A,B
    FluxState(pl.DataFrame({"id": ["C", "D"], "x": [3, 4]}), key_column="id", store_path=store).update_mirror_table()
    r2 = FluxState(empty, key_column="id", store_path=store).update_mirror_table()      # delete C,D

    assert r2["noop"] is False                       # the 2nd empty capture must NOT be a no-op
    final = FluxState(pl.DataFrame(), key_column="id", store_path=store).save_mirror_table(output_format="polars")
    assert final.height == 0                          # everything is deleted


def test_h3_reconstruct_survives_delta_optimize_vacuum(tmp_path):
    """H3: table-format reconstruction must read the LIVE table, so Delta
    OPTIMIZE/VACUUM (which removes the capture-time data files a static ledger
    referenced) does not break `.travel()`/`save_mirror_table()`."""
    pytest.importorskip("deltalake")
    from deltalake import DeltaTable
    from sidecars.databricks import DeltaBackend

    events = str(tmp_path / "flux_events")
    be = DeltaBackend(events=events)
    t1 = dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)
    t2 = dt.datetime(2026, 6, 2, tzinfo=dt.timezone.utc)
    FluxState(pl.DataFrame({"id": [1, 2, 3], "v": [1, 2, 3]}), key_column="id", store=be).update_mirror_table(captured_at=t1)
    FluxState(pl.DataFrame({"id": [1, 2], "v": [1, 9]}), key_column="id", store=be).update_mirror_table(captured_at=t2)

    # Real table maintenance: compact then vacuum away the now-unreferenced old files.
    dt_tbl = DeltaTable(events)
    dt_tbl.optimize.compact()
    dt_tbl.vacuum(retention_hours=0, dry_run=False, enforce_retention_duration=False)

    # Reconstruction must still work (live scan), not FileNotFoundError on stale ledger files.
    current = FluxState(pl.DataFrame(), key_column="id", store=be).save_mirror_table(output_format="polars").sort("id")
    assert current.height == 2  # id 3 deleted; ids 1,2 remain (v=1,9)


def test_h7_iceberg_catalog_is_cached_per_instance(tmp_path):
    """H7: the SqlCatalog (a SQLAlchemy engine) must be built once per instance,
    not on every call."""
    pytest.importorskip("pyiceberg")
    pytest.importorskip("sqlalchemy")
    be = TableBackend(events=str(tmp_path / "ice"), format="iceberg")
    assert be._iceberg_catalog() is be._iceberg_catalog()
