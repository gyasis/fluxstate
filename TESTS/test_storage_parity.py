# File: TESTS/test_storage_parity.py
"""Wave 8 (US5) — cross-backend reconstruction PARITY, the correctness gate.

T026: drive the SAME seeded multi-snapshot change-log — multiple entities,
several captures at distinct UTC timestamps, a value CHANGE, a row DELETION
followed by a later RESURRECTION, and MIXED dtypes (int/float/str/bool/
datetime) — into every backend via ``FluxState(..., store=backend)
.update_mirror_table()`` (the documented capture path, FR-006/FR-008), then
assert ``travel(T)``/``save_mirror_table``/``get_timeline``/``row_state`` are
IDENTICAL across backends for the SAME logical history (SB-6, FR-010, FR-011,
SC-003) — including explicit type-fidelity (reconstructed dtypes match the
originals), the ``__deleted__`` lifecycle (unborn/deleted/resurrected), and
UTC timestamps.

T027 extends the same parity discipline over a SCHEMA-EVOLUTION sequence
(add / drop / rename — mirrors ``scripts/schema_churn_demo.py`` from feature
002, R9): a dropped column must read null from its drop point onward (no
stale ghost) and a rename must show the old column empty / the new one filled
from the rename point, identically across backends.

Backends compared: ``LocalFolderStore`` (the reference implementation) vs
each of ``TableBackend(format="parquet")`` (runs natively, no optional lib),
``ObjectStoreBackend`` over ``memory://`` (gated on fsspec), and
``sidecars.databricks.DeltaBackend`` (gated on deltalake). The
LocalFolderStore-vs-TableBackend comparison is the one that MUST run
natively in any environment — if it fails, that is a real bug in one of the
two backends, not a fixture problem.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import polars as pl
import pytest

from fluxstate import FluxState
from sidecars.databricks import DeltaBackend
from storage.local_folder import LocalFolderStore
from storage.object_store import ObjectStoreBackend
from storage.table import TableBackend

U = lambda *a: datetime(*a, tzinfo=timezone.utc)

# Backends compared AGAINST the "local_folder" reference in every parity test.
# "table_parquet" needs no optional dependency (runs natively); the other two
# are gated via `pytest.importorskip` inside each test.
OTHER_BACKENDS = ["table_parquet", "object_store_memory", "delta"]


def _skip_unless_available(name: str) -> None:
    if name == "object_store_memory":
        pytest.importorskip("fsspec")
    elif name == "delta":
        pytest.importorskip("deltalake")


def _make_backend(name: str, root: Path):
    """Construct a fresh, empty backend instance of `name` rooted at `root`."""
    if name == "local_folder":
        return LocalFolderStore(root / "local.flux")
    if name == "table_parquet":
        return TableBackend(events=str(root / "table_events"), format="parquet")
    if name == "object_store_memory":
        # fsspec's MemoryFileSystem is process-global; a fresh uuid keeps every
        # backend instance's namespace collision-free across parametrized runs.
        loc = f"memory://parity-{uuid.uuid4().hex}/store"
        return ObjectStoreBackend(loc)
    if name == "delta":
        return DeltaBackend(events=str(root / "delta_events"))
    raise ValueError(f"unknown backend name {name!r}")


def _capture(backend, df: pl.DataFrame, captured_at: datetime) -> dict:
    """Capture one snapshot into `backend` via the documented FluxState path."""
    fs = FluxState(df, key_column="id", store=backend)
    return fs.update_mirror_table(captured_at=captured_at)


def _reader(backend) -> FluxState:
    """A FluxState bound to `backend` for read-side assertions only.

    The frame passed at construction only feeds FluxState's legacy in-memory
    mirror bookkeeping (unrelated to the persisted change-log); it is never
    captured here, so its shape doesn't need to match the seeded history.
    """
    return FluxState(pl.DataFrame({"id": [0]}), key_column="id", store=backend)


# --------------------------------------------------------------------------- #
# T026 — canonical multi-snapshot seed: entities, a value CHANGE, a           #
# DELETION + RESURRECTION, mixed dtypes (int/float/str/bool/datetime).        #
# --------------------------------------------------------------------------- #
def _seed_canonical_history(name: str, root: Path) -> tuple[FluxState, list[datetime]]:
    backend = _make_backend(name, root)
    times = [U(2026, 2, d) for d in range(1, 6)]

    # day1 — birth: ids 1, 2, 3.
    _capture(
        backend,
        pl.DataFrame(
            {
                "id": pl.Series([1, 2, 3], dtype=pl.Int64),
                "name": pl.Series(["alice", "bob", "carol"], dtype=pl.Utf8),
                "score": pl.Series([1.5, 2.5, 3.5], dtype=pl.Float64),
                "active": pl.Series([True, False, True], dtype=pl.Boolean),
                "seen_at": pl.Series([times[0]] * 3, dtype=pl.Datetime("us", "UTC")),
            }
        ),
        times[0],
    )
    # day2 — VALUE CHANGE (id=1 score) + INSERT id=4.
    _capture(
        backend,
        pl.DataFrame(
            {
                "id": pl.Series([1, 2, 3, 4], dtype=pl.Int64),
                "name": pl.Series(["alice", "bob", "carol", "dave"], dtype=pl.Utf8),
                "score": pl.Series([9.9, 2.5, 3.5, 4.5], dtype=pl.Float64),
                "active": pl.Series([True, False, True, True], dtype=pl.Boolean),
                "seen_at": pl.Series(
                    [times[1], times[0], times[0], times[1]], dtype=pl.Datetime("us", "UTC")
                ),
            }
        ),
        times[1],
    )
    # day3 — DELETION: id=2 vanishes.
    _capture(
        backend,
        pl.DataFrame(
            {
                "id": pl.Series([1, 3, 4], dtype=pl.Int64),
                "name": pl.Series(["alice", "carol", "dave"], dtype=pl.Utf8),
                "score": pl.Series([9.9, 3.5, 4.5], dtype=pl.Float64),
                "active": pl.Series([True, True, True], dtype=pl.Boolean),
                "seen_at": pl.Series(
                    [times[1], times[0], times[1]], dtype=pl.Datetime("us", "UTC")
                ),
            }
        ),
        times[2],
    )
    # day4 — RESURRECTION: id=2 returns with a new value (continuous entity_id trail).
    _capture(
        backend,
        pl.DataFrame(
            {
                "id": pl.Series([1, 2, 3, 4], dtype=pl.Int64),
                "name": pl.Series(["alice", "bob2", "carol", "dave"], dtype=pl.Utf8),
                "score": pl.Series([9.9, 20.0, 3.5, 4.5], dtype=pl.Float64),
                "active": pl.Series([True, False, True, True], dtype=pl.Boolean),
                "seen_at": pl.Series(
                    [times[1], times[3], times[0], times[1]], dtype=pl.Datetime("us", "UTC")
                ),
            }
        ),
        times[3],
    )
    # day5 — a further update (bool flips + another score change) so "now"/mid
    # probes have something distinct to prove beyond day4.
    _capture(
        backend,
        pl.DataFrame(
            {
                "id": pl.Series([1, 2, 3, 4], dtype=pl.Int64),
                "name": pl.Series(["alice2", "bob2", "carol", "dave"], dtype=pl.Utf8),
                "score": pl.Series([11.1, 20.0, 3.5, 4.5], dtype=pl.Float64),
                "active": pl.Series([False, False, True, True], dtype=pl.Boolean),
                "seen_at": pl.Series(
                    [times[4], times[3], times[0], times[1]], dtype=pl.Datetime("us", "UTC")
                ),
            }
        ),
        times[4],
    )

    return _reader(backend), times


# --------------------------------------------------------------------------- #
# T027 — schema-evolution seed: add / drop / rename (mirrors 002's            #
# scripts/schema_churn_demo.py, R9).                                          #
# --------------------------------------------------------------------------- #
def _seed_schema_churn(name: str, root: Path) -> tuple[FluxState, list[datetime]]:
    backend = _make_backend(name, root)
    times = [U(2026, 3, d) for d in range(1, 7)]

    # day1 — base columns: id, name, score, region.
    _capture(
        backend,
        pl.DataFrame(
            {
                "id": pl.Series([1, 2, 3, 4], dtype=pl.Int64),
                "name": pl.Series(["alice", "bob", "carol", "dave"], dtype=pl.Utf8),
                "score": pl.Series([10.0, 20.0, 30.0, 40.0], dtype=pl.Float64),
                "region": pl.Series(["NW", "NE", "SW", "SE"], dtype=pl.Utf8),
            }
        ),
        times[0],
    )
    # day2 — ADD `email`; bump two scores.
    _capture(
        backend,
        pl.DataFrame(
            {
                "id": pl.Series([1, 2, 3, 4], dtype=pl.Int64),
                "name": pl.Series(["alice", "bob", "carol", "dave"], dtype=pl.Utf8),
                "score": pl.Series([12.0, 20.0, 35.0, 40.0], dtype=pl.Float64),
                "region": pl.Series(["NW", "NE", "SW", "SE"], dtype=pl.Utf8),
                "email": pl.Series(["a@x", "b@x", "c@x", "d@x"], dtype=pl.Utf8),
            }
        ),
        times[1],
    )
    # day3 — update + entity 5 born.
    _capture(
        backend,
        pl.DataFrame(
            {
                "id": pl.Series([1, 2, 3, 4, 5], dtype=pl.Int64),
                "name": pl.Series(["alice", "bob", "carol", "dave", "erin"], dtype=pl.Utf8),
                "score": pl.Series([12.0, 22.0, 35.0, 40.0, 50.0], dtype=pl.Float64),
                "region": pl.Series(["NW", "NE", "SW", "SE", "NW"], dtype=pl.Utf8),
                "email": pl.Series(["a@x", "b@x", "c@x", "d@x", "e@x"], dtype=pl.Utf8),
            }
        ),
        times[2],
    )
    # day4 — DROP `score`. History before day4 keeps it; from day4 on it reads
    # NULL (a field-level tombstone), never the stale last value.
    _capture(
        backend,
        pl.DataFrame(
            {
                "id": pl.Series([1, 2, 3, 4, 5], dtype=pl.Int64),
                "name": pl.Series(["alice", "bob", "carol", "dave", "erin"], dtype=pl.Utf8),
                "region": pl.Series(["NW", "NE", "SW", "SE", "NW"], dtype=pl.Utf8),
                "email": pl.Series(["a@x", "b@x", "c@x", "d@x", "e@x"], dtype=pl.Utf8),
            }
        ),
        times[3],
    )
    # day5 — RENAME `name` -> `full_name` (recorded as drop(name) + add(full_name)).
    _capture(
        backend,
        pl.DataFrame(
            {
                "id": pl.Series([1, 2, 3, 4, 5], dtype=pl.Int64),
                "full_name": pl.Series(
                    ["Alice A", "Bob B", "Carol C", "Dave D", "Erin E"], dtype=pl.Utf8
                ),
                "region": pl.Series(["NW", "NE", "SW", "SE", "NW"], dtype=pl.Utf8),
                "email": pl.Series(["a@x", "b@x", "c@x", "d@x", "e@x"], dtype=pl.Utf8),
            }
        ),
        times[4],
    )
    # day6 — ADD `status`; DROP `region`.
    _capture(
        backend,
        pl.DataFrame(
            {
                "id": pl.Series([1, 2, 3, 4, 5], dtype=pl.Int64),
                "full_name": pl.Series(
                    ["Alice A", "Bob B", "Carol C", "Dave D", "Erin E"], dtype=pl.Utf8
                ),
                "email": pl.Series(["a@x", "b@x", "c@x", "d@x", "e@x"], dtype=pl.Utf8),
                "status": pl.Series(
                    ["active", "active", "churned", "active", "trial"], dtype=pl.Utf8
                ),
            }
        ),
        times[5],
    )

    return _reader(backend), times


# --------------------------------------------------------------------------- #
# Comparison helper — canonical normalization (sort by key, frame-equal).     #
# --------------------------------------------------------------------------- #
def _assert_view_parity(local_view: pl.DataFrame, other_view: pl.DataFrame, label: str) -> None:
    local_sorted = local_view.sort("id")
    other_sorted = other_view.sort("id")
    assert set(other_sorted.columns) == set(local_sorted.columns), f"{label}: column set mismatch"
    assert other_sorted.schema == local_sorted.schema, f"{label}: dtype mismatch"
    assert other_sorted.equals(local_sorted), f"{label}: row values mismatch"


# --------------------------------------------------------------------------- #
# T026 — the correctness gate.                                                #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("other_name", OTHER_BACKENDS)
def test_cross_backend_reconstruction_parity(other_name, tmp_path):
    _skip_unless_available(other_name)

    local_reader, times = _seed_canonical_history("local_folder", tmp_path / "local")
    other_reader, _ = _seed_canonical_history(other_name, tmp_path / other_name)

    # --- travel(T) at several points: before history, at/between captures, now.
    probe_times: list[Optional[datetime | str]] = [
        times[0] - timedelta(days=1),  # before any history -> empty
        times[0],                      # at the birth capture
        times[1],                      # the value-change + insert capture
        times[2] + timedelta(hours=12),  # between day3 and day4 (id=2 deleted)
        times[3],                      # the resurrection capture
        "now",                          # current
    ]
    for T in probe_times:
        local_view = local_reader.travel(T)
        other_view = other_reader.travel(T)
        _assert_view_parity(local_view, other_view, f"[{other_name}] travel(T={T})")

    # --- current save_mirror_table (polars form).
    local_save = local_reader.save_mirror_table(output_format="polars")
    other_save = other_reader.save_mirror_table(output_format="polars")
    _assert_view_parity(local_save, other_save, f"[{other_name}] save_mirror_table")

    # --- get_timeline(entity, field) parity, incl. field=None (all fields).
    for entity_id in (1, 2, 3, 4):
        for field in (None, "name", "score", "active", "seen_at"):
            local_tl = local_reader.get_timeline(entity_id, field=field)
            other_tl = other_reader.get_timeline(entity_id, field=field)
            assert other_tl == local_tl, (
                f"[{other_name}] timeline mismatch id={entity_id} field={field}\n"
                f"  local={local_tl}\n  other={other_tl}"
            )

    # --- row_state(entity, T) parity across the FULL lifecycle: unborn, active,
    # deleted, resurrected.
    lifecycle_points: list[Optional[datetime | str]] = [
        times[0] - timedelta(days=1),  # unborn (nothing captured yet)
        times[1],                      # active
        times[2],                      # id=2 just deleted
        times[3],                      # id=2 just resurrected
        "now",
    ]
    for entity_id in (1, 2, 3, 4):
        for T in lifecycle_points:
            local_rs = local_reader.row_state(entity_id, T)
            other_rs = other_reader.row_state(entity_id, T)
            assert other_rs == local_rs, (
                f"[{other_name}] row_state mismatch id={entity_id} T={T}: "
                f"local={local_rs} other={other_rs}"
            )

    # --- explicit __deleted__ lifecycle assertions (id=2: unborn -> active ->
    # deleted -> resurrected), identical on both backends.
    for reader, tag in ((local_reader, "local"), (other_reader, other_name)):
        assert reader.row_state(2, times[0] - timedelta(days=1)) == {
            "state": "unborn", "resurrected": False,
        }, tag
        assert reader.row_state(2, times[1]) == {
            "state": "active", "resurrected": False,
        }, tag
        assert reader.row_state(2, times[2]) == {
            "state": "deleted", "resurrected": False,
        }, tag
        assert reader.row_state(2, "now") == {
            "state": "active", "resurrected": True,
        }, tag
        # id=4 is unborn before its day2 birth.
        assert reader.row_state(4, times[0]) == {"state": "unborn", "resurrected": False}, tag

    # --- explicit TYPE FIDELITY: reconstructed dtypes match the originals, on
    # BOTH backends (not just "equal to each other" — equal to the source).
    expected_dtypes = {
        "id": pl.Int64,
        "name": pl.Utf8,
        "score": pl.Float64,
        "active": pl.Boolean,
        "seen_at": pl.Datetime("us", "UTC"),
    }
    for view, tag in ((local_save, "local"), (other_save, other_name)):
        for col, dt in expected_dtypes.items():
            assert view.schema[col] == dt, f"[{tag}] type fidelity broken for {col!r}: got {view.schema[col]}"

    # --- explicit UTC assertions: every reconstructed datetime cell + every
    # get_timeline date is tz-aware UTC (offset zero), on both backends.
    for view, tag in ((local_save, "local"), (other_save, other_name)):
        for dt in view["seen_at"].to_list():
            assert dt.tzinfo is not None and dt.utcoffset() == timedelta(0), (
                f"[{tag}] seen_at not UTC: {dt!r}"
            )
    for reader, tag in ((local_reader, "local"), (other_reader, other_name)):
        tl = reader.get_timeline(1, field="seen_at")
        assert tl, f"[{tag}] expected a non-empty seen_at timeline"
        for entry in tl:
            d = entry["value"]
            assert d.tzinfo is not None and d.utcoffset() == timedelta(0), (
                f"[{tag}] timeline seen_at value not UTC: {d!r}"
            )


# --------------------------------------------------------------------------- #
# T027 — schema-churn parity: add / drop / rename, identical across backends. #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("other_name", OTHER_BACKENDS)
def test_cross_backend_schema_churn_parity(other_name, tmp_path):
    _skip_unless_available(other_name)

    local_reader, times = _seed_schema_churn("local_folder", tmp_path / "local")
    other_reader, _ = _seed_schema_churn(other_name, tmp_path / other_name)

    # --- travel(T) at every capture day + now: full-frame parity throughout
    # the churn sequence (add email, drop score, rename name, add status/drop
    # region).
    for T in [*times, "now"]:
        local_view = local_reader.travel(T)
        other_view = other_reader.travel(T)
        _assert_view_parity(local_view, other_view, f"[{other_name}] schema-churn travel(T={T})")

    # --- DROP `score` (day4): reads NULL from the drop point onward on BOTH
    # backends — never the stale pre-drop value (no ghost).
    for reader, tag in ((local_reader, "local"), (other_reader, other_name)):
        before_drop = reader.travel(times[2])  # day3: score still resolvable
        assert before_drop.filter(pl.col("id") == 1)["score"][0] == 12.0, tag
        after_drop = reader.travel(times[3])  # day4: dropped -> NULL
        assert after_drop.filter(pl.col("id") == 1)["score"][0] is None, tag
        now_view = reader.travel("now")
        assert now_view.filter(pl.col("id") == 1)["score"][0] is None, tag

    # --- RENAME `name` -> `full_name` (day5): old column reads NULL from the
    # rename point, new column is filled — identically on both backends.
    for reader, tag in ((local_reader, "local"), (other_reader, other_name)):
        after_rename = reader.travel(times[4])
        assert after_rename.filter(pl.col("id") == 1)["name"][0] is None, tag
        assert after_rename.filter(pl.col("id") == 1)["full_name"][0] == "Alice A", tag
        before_rename = reader.travel(times[3])
        assert before_rename.filter(pl.col("id") == 1)["name"][0] == "alice", tag

    # --- ADD `status` / DROP `region` (day6): status filled, region NULL from
    # day6 on — identically on both backends.
    for reader, tag in ((local_reader, "local"), (other_reader, other_name)):
        after_day6 = reader.travel(times[5])
        assert after_day6.filter(pl.col("id") == 3)["status"][0] == "churned", tag
        assert after_day6.filter(pl.col("id") == 3)["region"][0] is None, tag
        before_day6 = reader.travel(times[4])
        assert before_day6.filter(pl.col("id") == 3)["region"][0] == "SW", tag

    # --- get_timeline parity for every field that churned.
    for entity_id in (1, 2, 3, 4, 5):
        for field in ("score", "name", "full_name", "region", "email", "status"):
            local_tl = local_reader.get_timeline(entity_id, field=field)
            other_tl = other_reader.get_timeline(entity_id, field=field)
            assert other_tl == local_tl, (
                f"[{other_name}] schema-churn timeline mismatch id={entity_id} field={field}\n"
                f"  local={local_tl}\n  other={other_tl}"
            )

    # --- save_mirror_table (current, post-churn) parity.
    local_save = local_reader.save_mirror_table(output_format="polars")
    other_save = other_reader.save_mirror_table(output_format="polars")
    _assert_view_parity(local_save, other_save, f"[{other_name}] schema-churn save_mirror_table")
