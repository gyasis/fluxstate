# File: TESTS/test_table_backend.py
"""Wave 5 (US2) — ``TableBackend`` (``flux_events`` + optional ``flux_mirror``).

Covers T016 (append/idempotency/no-rewrite, on-demand + cadence mirror refresh,
parity vs ``LocalFolderStore``) and T017 (missing-extra actionable error for
``format="delta"``/``"iceberg"`` without the ``[table]`` extra).
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

import polars as pl
import pytest

from changelog import ChangeLogStore
import reconstruct
from storage.local_folder import LocalFolderStore
from storage.table import TableBackend

U = lambda *a: datetime(*a, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# capabilities                                                                #
# --------------------------------------------------------------------------- #
def test_capabilities_flags(tmp_path):
    no_mirror = TableBackend(events=str(tmp_path / "flux_events"))
    assert no_mirror.capabilities.is_table is True
    assert no_mirror.capabilities.supports_atomic_meta is True
    assert no_mirror.capabilities.supports_time_pushdown is True
    assert no_mirror.capabilities.supports_mirror is False

    with_mirror = TableBackend(
        events=str(tmp_path / "flux_events2"), mirror=str(tmp_path / "flux_mirror2")
    )
    assert with_mirror.capabilities.supports_mirror is True


# --------------------------------------------------------------------------- #
# flux_events: append-only, one part per capture, glob-readable               #
# --------------------------------------------------------------------------- #
def test_flux_events_is_a_directory_of_immutable_parquet_parts(
    tmp_path, df_day1, df_day2, df_day3
):
    events_dir = tmp_path / "flux_events"
    backend = TableBackend(events=str(events_dir))
    store = ChangeLogStore(events_dir, backend=backend)

    store.capture(df_day1, key_column="id", captured_at=U(2026, 1, 1))
    store.capture(df_day2, key_column="id", captured_at=U(2026, 1, 2))
    store.capture(df_day3, key_column="id", captured_at=U(2026, 1, 3))

    parts = sorted(events_dir.glob("*.parquet"))
    assert len(parts) == 3
    assert (events_dir / "_flux_meta.json").exists()

    # Directly queryable as a table with no FluxState code (FE-4).
    direct = pl.concat([pl.read_parquet(p) for p in parts], how="vertical")
    assert direct.height == store.backend.read_events().height


def test_idempotent_recapture_adds_no_new_part(tmp_path, df_day1):
    events_dir = tmp_path / "flux_events"
    backend = TableBackend(events=str(events_dir))
    store = ChangeLogStore(events_dir, backend=backend)

    r1 = store.capture(df_day1, key_column="id", captured_at=U(2026, 1, 1))
    assert r1["noop"] is False
    parts_after_first = sorted(events_dir.glob("*.parquet"))
    assert len(parts_after_first) == 1

    r2 = store.capture(df_day1, key_column="id", captured_at=U(2026, 1, 1))
    assert r2["noop"] is True
    assert r2["snapshot_id"] == r1["snapshot_id"]
    parts_after_second = sorted(events_dir.glob("*.parquet"))
    assert parts_after_second == parts_after_first


def test_no_rewrite_of_existing_parts_on_later_capture(tmp_path, df_day1, df_day2):
    events_dir = tmp_path / "flux_events"
    backend = TableBackend(events=str(events_dir))
    store = ChangeLogStore(events_dir, backend=backend)

    store.capture(df_day1, key_column="id", captured_at=U(2026, 1, 1))
    first_part = next(iter(events_dir.glob("*.parquet")))
    original_bytes = first_part.read_bytes()
    original_mtime_ns = first_part.stat().st_mtime_ns

    store.capture(df_day2, key_column="id", captured_at=U(2026, 1, 2))

    # The original part is untouched — a NEW part was added, not a rewrite (FE-1).
    assert first_part.read_bytes() == original_bytes
    assert first_part.stat().st_mtime_ns == original_mtime_ns
    parts = sorted(events_dir.glob("*.parquet"))
    assert len(parts) == 2


def test_append_events_protocol_entrypoint_is_idempotent(tmp_path, df_day1):
    events_dir = tmp_path / "flux_events"
    backend = TableBackend(events=str(events_dir))
    store = ChangeLogStore(events_dir, backend=backend)
    store.capture(df_day1, key_column="id", captured_at=U(2026, 1, 1))

    all_events = backend.read_events()
    ref1 = backend.append_events(all_events)
    ref2 = backend.append_events(all_events)  # same snapshot_id -> no-op
    assert ref1.snapshot_id == ref2.snapshot_id
    assert len(list(events_dir.glob("*.parquet"))) == 1


# --------------------------------------------------------------------------- #
# flux_mirror: on-demand + cadence refresh                                    #
# --------------------------------------------------------------------------- #
def test_flux_mirror_on_demand_matches_build_mirror_view(tmp_path, df_day1, df_day2):
    events_dir = tmp_path / "flux_events"
    mirror_dir = tmp_path / "flux_mirror"
    backend = TableBackend(events=str(events_dir), mirror=str(mirror_dir))
    store = ChangeLogStore(events_dir, backend=backend)

    store.capture(df_day1, key_column="id", captured_at=U(2026, 1, 1))
    store.capture(df_day2, key_column="id", captured_at=U(2026, 1, 2))

    # Not refreshed yet -> empty (on_demand is opt-in-to-refresh, not eager).
    assert backend.read_mirror().is_empty()

    backend.refresh_mirror()
    expected = reconstruct.build_mirror_view(store, T="now").sort("id")
    got = backend.read_mirror().sort("id")
    assert got.equals(expected)

    # Watermark recorded (FM-2 staleness discoverability).
    meta = backend.read_meta()
    assert meta.mirror.get("watermark") is not None
    assert meta.mirror.get("enabled") is True


def test_flux_mirror_cadence_refresh_every_n_captures(tmp_path, df_day1, df_day2, df_day3):
    events_dir = tmp_path / "flux_events"
    mirror_dir = tmp_path / "flux_mirror"
    backend = TableBackend(
        events=str(events_dir), mirror=str(mirror_dir), mirror_refresh="cadence:2"
    )
    store = ChangeLogStore(events_dir, backend=backend)

    store.capture(df_day1, key_column="id", captured_at=U(2026, 1, 1))
    assert backend.read_mirror().is_empty()  # 1st capture: cadence not yet hit

    store.capture(df_day2, key_column="id", captured_at=U(2026, 1, 2))
    after_2 = backend.read_mirror()
    assert not after_2.is_empty()  # 2nd capture: cadence hit -> auto-refreshed
    expected_after_2 = reconstruct.build_mirror_view(store, T="now").sort("id")
    assert after_2.sort("id").equals(expected_after_2)

    store.capture(df_day3, key_column="id", captured_at=U(2026, 1, 3))
    # 3rd capture: cadence not hit again -> mirror stays at its (now stale) value.
    assert backend.read_mirror().sort("id").equals(expected_after_2)


# --------------------------------------------------------------------------- #
# Parity vs LocalFolderStore (SB-6 / FR-011)                                  #
# --------------------------------------------------------------------------- #
def test_parity_vs_local_folder_store(tmp_path, df_day1, df_day2, df_day3):
    local_path = tmp_path / "local.flux"
    local_store = ChangeLogStore(local_path)

    table_events_dir = tmp_path / "table_events"
    table_backend = TableBackend(events=str(table_events_dir))
    table_store = ChangeLogStore(table_events_dir, backend=table_backend)

    for store in (local_store, table_store):
        store.capture(df_day1, key_column="id", captured_at=U(2026, 1, 1))
        store.capture(df_day2, key_column="id", captured_at=U(2026, 1, 2))
        store.capture(df_day3, key_column="id", captured_at=U(2026, 1, 3))

    for T in (U(2026, 1, 1), U(2026, 1, 2), U(2026, 1, 3), "now"):
        local_view = reconstruct.build_mirror_view(local_store, T=T).sort("id")
        table_view = reconstruct.build_mirror_view(table_store, T=T).sort("id")
        assert table_view.equals(local_view), f"mismatch at T={T}"

    # row_state / timeline parity for a resurrection-free, plain-lifecycle entity.
    for entity_id in (1, 2, 3, 4):
        assert reconstruct.row_state(table_store, entity_id) == reconstruct.row_state(
            local_store, entity_id
        )
        assert reconstruct.get_timeline(table_store, entity_id) == reconstruct.get_timeline(
            local_store, entity_id
        )


def test_parity_delete_then_resurrection(tmp_path):
    local_store = ChangeLogStore(tmp_path / "local2.flux")
    table_backend = TableBackend(events=str(tmp_path / "table_events2"))
    table_store = ChangeLogStore(tmp_path / "table_events2", backend=table_backend)

    snaps = [
        (pl.DataFrame({"id": [1, 2], "note": ["a", "b"]}), U(2026, 1, 1)),
        (pl.DataFrame({"id": [1], "note": ["a"]}), U(2026, 1, 2)),  # id=2 vanishes
        (pl.DataFrame({"id": [1, 2], "note": ["a", "b2"]}), U(2026, 1, 3)),  # id=2 returns
    ]
    for store in (local_store, table_store):
        for df, ts in snaps:
            store.capture(df, key_column="id", captured_at=ts)

    for T in (U(2026, 1, 1), U(2026, 1, 2), U(2026, 1, 3), "now"):
        local_view = reconstruct.build_mirror_view(local_store, T=T).sort("id")
        table_view = reconstruct.build_mirror_view(table_store, T=T).sort("id")
        assert table_view.equals(local_view), f"mismatch at T={T}"

    assert reconstruct.row_state(table_store, 2, T=U(2026, 1, 2)) == reconstruct.row_state(
        local_store, 2, T=U(2026, 1, 2)
    )
    assert reconstruct.row_state(table_store, 2, T="now") == reconstruct.row_state(
        local_store, 2, T="now"
    )


# --------------------------------------------------------------------------- #
# FluxState(store=TableBackend(...)) wiring (T015)                            #
# --------------------------------------------------------------------------- #
def test_fluxstate_store_kwarg_captures_via_table_backend(tmp_path):
    from fluxstate import FluxState

    events_dir = tmp_path / "flux_events"
    mirror_dir = tmp_path / "flux_mirror"
    backend = TableBackend(events=str(events_dir), mirror=str(mirror_dir))

    df = pl.DataFrame({"id": [1, 2], "risk": [0.4, 0.7]})
    fs = FluxState(df, key_column="id", store=backend)
    assert fs.store.backend is backend

    result = fs.update_mirror_table(captured_at=U(2026, 1, 1))
    assert result["noop"] is False
    assert len(list(events_dir.glob("*.parquet"))) == 1

    fs.refresh_mirror()
    mirror = backend.read_mirror().sort("id")
    assert sorted(mirror["id"].to_list()) == [1, 2]


def test_fluxstate_backend_kwarg_still_works_as_alias(tmp_path):
    from fluxstate import FluxState

    events_dir = tmp_path / "flux_events"
    backend = TableBackend(events=str(events_dir))
    df = pl.DataFrame({"id": [1], "risk": [0.4]})
    fs = FluxState(df, key_column="id", backend=backend)
    assert fs.store.backend is backend
    fs.update_mirror_table(captured_at=U(2026, 1, 1))
    assert len(list(events_dir.glob("*.parquet"))) == 1


def test_fluxstate_rejects_conflicting_backend_and_store(tmp_path):
    from fluxstate import FluxState

    b1 = TableBackend(events=str(tmp_path / "e1"))
    b2 = TableBackend(events=str(tmp_path / "e2"))
    df = pl.DataFrame({"id": [1], "risk": [0.4]})
    with pytest.raises(ValueError):
        FluxState(df, key_column="id", backend=b1, store=b2)


# --------------------------------------------------------------------------- #
# T017 — missing [table] extra -> actionable error                            #
# --------------------------------------------------------------------------- #
def test_delta_format_without_deltalake_raises_actionable_error(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "deltalake", None)
    with pytest.raises(ImportError, match=r'fluxstate\[table\]'):
        TableBackend(events=str(tmp_path / "flux_events"), format="delta")


def test_iceberg_format_without_pyiceberg_raises_actionable_error(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "pyiceberg", None)
    with pytest.raises(ImportError, match=r'fluxstate\[table\]'):
        TableBackend(events=str(tmp_path / "flux_events"), format="iceberg")


def test_delta_format_works_when_deltalake_available():
    pytest.importorskip("deltalake")
    # If deltalake IS installed, construction must succeed (no false-positive guard).
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        TableBackend(events=f"{d}/flux_events", format="delta")


def test_iceberg_format_works_when_pyiceberg_available():
    pytest.importorskip("pyiceberg")
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        TableBackend(events=f"{d}/flux_events", format="iceberg")


def test_unknown_format_raises_value_error(tmp_path):
    with pytest.raises(ValueError):
        TableBackend(events=str(tmp_path / "flux_events"), format="csv")
