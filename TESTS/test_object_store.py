# File: TESTS/test_object_store.py
"""Wave 6 (US3) — ``ObjectStoreBackend`` (fsspec — s3/abfss/gs/UC-Volume/local, T020).

Covers: capture -> reconstruct (as_of/current) over a ``memory://`` store;
atomic-meta (SB-4/R3 — a single-object PUT, never a temp-file-then-rename);
no-rewrite of existing immutable event objects (SB-1); cross-process read (a
FRESH ``ObjectStoreBackend`` instance re-reading from a REAL on-disk location
proves nothing is held only in the writer's Python-object memory); and parity
vs ``LocalFolderStore`` (SB-6 — reconstruction is byte-identical for the same
logical history, regardless of backend).

``fsspec`` provides a built-in in-process ``memory://`` filesystem, so no
``s3fs``/cloud credentials are needed to exercise the backend's logic. Each
test that needs a REAL fsspec install calls ``pytest.importorskip("fsspec")``
individually (mirrors ``test_table_backend.py``'s per-test
``pytest.importorskip("deltalake"/"pyiceberg")``) so the missing-extra guard
test below (which must pass with NO fsspec installed at all) is never itself
skipped by a module-level import guard.
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone

import polars as pl
import pytest

from changelog import ChangeLogStore
import reconstruct
from storage.object_store import ObjectStoreBackend

U = lambda *a: datetime(*a, tzinfo=timezone.utc)


def _memory_location(name: str) -> str:
    """A fresh, collision-free ``memory://`` location (fsspec's MemoryFileSystem
    is a process-global store, so every test gets its own unique root)."""
    return f"memory://{name}-{uuid.uuid4().hex}/store"


# --------------------------------------------------------------------------- #
# capabilities                                                                #
# --------------------------------------------------------------------------- #
def test_capabilities_flags():
    pytest.importorskip("fsspec")
    backend = ObjectStoreBackend(_memory_location("caps"))
    assert backend.capabilities.is_table is False
    assert backend.capabilities.supports_atomic_meta is True
    assert backend.capabilities.supports_time_pushdown is False
    assert backend.capabilities.supports_mirror is False


# --------------------------------------------------------------------------- #
# capture -> reconstruct (as_of / current) over memory://                     #
# --------------------------------------------------------------------------- #
def test_capture_and_reconstruct_as_of_over_memory_store(df_day1, df_day2, df_day3):
    pytest.importorskip("fsspec")
    loc = _memory_location("captureasof")
    backend = ObjectStoreBackend(loc)
    store = ChangeLogStore(loc, backend=backend)

    store.capture(df_day1, key_column="id", captured_at=U(2026, 1, 1))
    store.capture(df_day2, key_column="id", captured_at=U(2026, 1, 2))
    store.capture(df_day3, key_column="id", captured_at=U(2026, 1, 3))

    day1 = reconstruct.build_mirror_view(store, U(2026, 1, 1))
    assert sorted(day1["id"].to_list()) == [1, 2, 3]

    day2 = reconstruct.build_mirror_view(store, U(2026, 1, 2))
    assert sorted(day2["id"].to_list()) == [1, 2, 3, 4]
    assert day2.filter(pl.col("id") == 2)["score"][0] == 25.0

    day3 = reconstruct.build_mirror_view(store, U(2026, 1, 3))
    assert sorted(day3["id"].to_list()) == [2, 3, 4]  # id 1 vanished
    assert day3.filter(pl.col("id") == 2)["score"][0] == 27.5

    current = reconstruct.build_mirror_view(store, "now")
    assert current.equals(day3)


def test_idempotent_recapture_over_memory_store(df_day1, df_day2):
    pytest.importorskip("fsspec")
    loc = _memory_location("idempotent")
    backend = ObjectStoreBackend(loc)
    store = ChangeLogStore(loc, backend=backend)

    store.capture(df_day1, key_column="id", captured_at=U(2026, 1, 1))
    store.capture(df_day2, key_column="id", captured_at=U(2026, 1, 2))
    n_events_before = len(store.read_manifest()["events"])

    result = store.capture(df_day2, key_column="id", captured_at=U(2026, 1, 2))

    assert result["noop"] is True
    assert result["events_added"] == 0
    assert len(store.read_manifest()["events"]) == n_events_before


# --------------------------------------------------------------------------- #
# Atomic meta commit (SB-4 / R3) — single-object PUT, never temp+rename       #
# --------------------------------------------------------------------------- #
def test_manifest_commit_is_a_single_object_put_no_tmp_artifacts(df_day1, df_day2):
    pytest.importorskip("fsspec")
    loc = _memory_location("atomicmeta")
    backend = ObjectStoreBackend(loc)
    store = ChangeLogStore(loc, backend=backend)

    store.capture(df_day1, key_column="id", captured_at=U(2026, 1, 1))
    store.capture(df_day2, key_column="id", captured_at=U(2026, 1, 2))

    all_objects = backend.fs.find(backend.base_path)
    # No temp-file-then-rename artifacts of any kind (".tmp" suffix or a
    # leading "." staging name) — write_manifest/_append_events only ever
    # issue a single fs.pipe_file PUT to the FINAL object path.
    assert all(".tmp" not in obj for obj in all_objects)
    assert all(not obj.rsplit("/", 1)[-1].startswith(".") for obj in all_objects)

    # Exactly one manifest object, at the expected final path.
    manifest_hits = [o for o in all_objects if o.endswith("manifest.json")]
    assert manifest_hits == [backend.manifest_path]


def test_no_rewrite_of_existing_event_objects(df_day1, df_day2):
    pytest.importorskip("fsspec")
    loc = _memory_location("norewrite")
    backend = ObjectStoreBackend(loc)
    store = ChangeLogStore(loc, backend=backend)

    store.capture(df_day1, key_column="id", captured_at=U(2026, 1, 1))
    manifest_after_1 = store.read_manifest()
    first_ref = f"{backend.base_path}/{manifest_after_1['events'][0]['file']}"
    original_bytes = backend.fs.cat_file(first_ref)

    store.capture(df_day2, key_column="id", captured_at=U(2026, 1, 2))

    # The first event object is byte-identical after a LATER capture — a NEW
    # object was added, the existing one was never rewritten (SB-1).
    assert backend.fs.cat_file(first_ref) == original_bytes
    manifest_after_2 = store.read_manifest()
    assert len(manifest_after_2["events"]) == 2
    assert manifest_after_2["events"][0]["file"] == manifest_after_1["events"][0]["file"]


# --------------------------------------------------------------------------- #
# Cross-process read — a FRESH backend instance re-reading from real disk     #
# (nothing is held only in the writer's Python-object memory)                 #
# --------------------------------------------------------------------------- #
def test_cross_process_read_fresh_instance_same_location(tmp_path, df_day1, df_day2, df_day3):
    pytest.importorskip("fsspec")
    loc = str(tmp_path / "objstore")  # a real on-disk location (no file:// needed)

    writer_backend = ObjectStoreBackend(loc)
    writer_store = ChangeLogStore(loc, backend=writer_backend)
    writer_store.capture(df_day1, key_column="id", captured_at=U(2026, 1, 1))
    writer_store.capture(df_day2, key_column="id", captured_at=U(2026, 1, 2))
    writer_store.capture(df_day3, key_column="id", captured_at=U(2026, 1, 3))
    expected = reconstruct.build_mirror_view(writer_store, "now").sort("id")

    # A brand-new ObjectStoreBackend/ChangeLogStore pointed at the SAME
    # location, with no reference to `writer_backend`/`writer_store` at all —
    # proves the reconstruction comes from what was persisted to the
    # filesystem, not from state cached on the writer's Python object.
    del writer_backend, writer_store
    reader_backend = ObjectStoreBackend(loc)
    reader_store = ChangeLogStore(loc, backend=reader_backend)
    got = reconstruct.build_mirror_view(reader_store, "now").sort("id")

    assert got.equals(expected)
    assert sorted(got["id"].to_list()) == [2, 3, 4]


# --------------------------------------------------------------------------- #
# Parity vs LocalFolderStore (SB-6 / FR-011)                                  #
# --------------------------------------------------------------------------- #
def test_parity_vs_local_folder_store(tmp_path, df_day1, df_day2, df_day3):
    pytest.importorskip("fsspec")
    local_store = ChangeLogStore(tmp_path / "local.flux")

    loc = _memory_location("parity")
    object_backend = ObjectStoreBackend(loc)
    object_store = ChangeLogStore(loc, backend=object_backend)

    for store in (local_store, object_store):
        store.capture(df_day1, key_column="id", captured_at=U(2026, 1, 1))
        store.capture(df_day2, key_column="id", captured_at=U(2026, 1, 2))
        store.capture(df_day3, key_column="id", captured_at=U(2026, 1, 3))

    for T in (U(2026, 1, 1), U(2026, 1, 2), U(2026, 1, 3), "now"):
        local_view = reconstruct.build_mirror_view(local_store, T=T).sort("id")
        object_view = reconstruct.build_mirror_view(object_store, T=T).sort("id")
        assert object_view.equals(local_view), f"mismatch at T={T}"

    for entity_id in (1, 2, 3, 4):
        assert reconstruct.row_state(object_store, entity_id) == reconstruct.row_state(
            local_store, entity_id
        )
        assert reconstruct.get_timeline(object_store, entity_id) == reconstruct.get_timeline(
            local_store, entity_id
        )


def test_parity_delete_then_resurrection(tmp_path):
    pytest.importorskip("fsspec")
    local_store = ChangeLogStore(tmp_path / "local2.flux")
    loc = _memory_location("parityresurrect")
    object_backend = ObjectStoreBackend(loc)
    object_store = ChangeLogStore(loc, backend=object_backend)

    snaps = [
        (pl.DataFrame({"id": [1, 2], "note": ["a", "b"]}), U(2026, 1, 1)),
        (pl.DataFrame({"id": [1], "note": ["a"]}), U(2026, 1, 2)),  # id=2 vanishes
        (pl.DataFrame({"id": [1, 2], "note": ["a", "b2"]}), U(2026, 1, 3)),  # id=2 returns
    ]
    for store in (local_store, object_store):
        for df, ts in snaps:
            store.capture(df, key_column="id", captured_at=ts)

    for T in (U(2026, 1, 1), U(2026, 1, 2), U(2026, 1, 3), "now"):
        local_view = reconstruct.build_mirror_view(local_store, T=T).sort("id")
        object_view = reconstruct.build_mirror_view(object_store, T=T).sort("id")
        assert object_view.equals(local_view), f"mismatch at T={T}"

    assert reconstruct.row_state(object_store, 2, T=U(2026, 1, 2)) == reconstruct.row_state(
        local_store, 2, T=U(2026, 1, 2)
    )
    assert reconstruct.row_state(object_store, 2, T="now") == reconstruct.row_state(
        local_store, 2, T="now"
    )


# --------------------------------------------------------------------------- #
# FluxState(store=ObjectStoreBackend(...)) wiring                            #
# --------------------------------------------------------------------------- #
def test_fluxstate_store_kwarg_captures_via_object_store():
    pytest.importorskip("fsspec")
    from fluxstate import FluxState

    loc = _memory_location("fluxstate")
    backend = ObjectStoreBackend(loc)

    df = pl.DataFrame({"id": [1, 2], "risk": [0.4, 0.7]})
    fs = FluxState(df, key_column="id", store=backend)
    assert fs.store.backend is backend

    result = fs.update_mirror_table(captured_at=U(2026, 1, 1))
    assert result["noop"] is False

    view = reconstruct.build_mirror_view(fs.store, "now").sort("id")
    assert sorted(view["id"].to_list()) == [1, 2]


# --------------------------------------------------------------------------- #
# select_backend dispatch (T019)                                              #
# --------------------------------------------------------------------------- #
def test_select_backend_resolves_object_store_schemes(monkeypatch):
    """``s3://``/``abfss://``/``gs://`` dispatch to ``ObjectStoreBackend`` (contract §Selection).

    Stubs ``fsspec.core.url_to_fs`` to a ``memory://`` filesystem so the
    dispatch/construction path is exercised without requiring the real
    ``s3fs``/``adlfs``/``gcsfs`` drivers to be installed — this test is about
    *routing*, not live cloud connectivity.
    """
    fsspec = pytest.importorskip("fsspec")
    from storage import select_backend

    mem_fs, _ = fsspec.core.url_to_fs(_memory_location("dispatch"))
    monkeypatch.setattr(fsspec.core, "url_to_fs", lambda loc, **kw: (mem_fs, "/dispatch-test"))

    for scheme in ("s3", "abfss", "gs"):
        backend = select_backend(f"{scheme}://bucket/prefix")
        assert isinstance(backend, ObjectStoreBackend)


# --------------------------------------------------------------------------- #
# Missing-extra actionable error (fsspec import guard) — runs with NO fsspec  #
# install required; must not be skipped by any module-level import guard.    #
# --------------------------------------------------------------------------- #
def test_missing_fsspec_raises_actionable_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "fsspec", None)
    with pytest.raises(ImportError, match=r'fluxstate\[remote\]'):
        ObjectStoreBackend(_memory_location("missingfsspec"))
