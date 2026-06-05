# File: TESTS/test_idempotency.py
"""US1 — idempotent capture (SC-002 / API-2).

Re-capturing an already-recorded snapshot must add zero events and leave the
store byte-identical. Written test-first: MUST FAIL until ``capture`` (T015)
implements the idempotency anti-join.
"""

import glob
import hashlib

import polars as pl

from changelog import ChangeLogStore


def _fingerprint(store_path):
    """Stable fingerprint of every file in the store (path + bytes)."""
    h = hashlib.sha256()
    for p in sorted(glob.glob(str(store_path / "**" / "*"), recursive=True)):
        h.update(p.encode())
        try:
            with open(p, "rb") as fh:
                h.update(fh.read())
        except (IsADirectoryError, PermissionError):
            pass
    return h.hexdigest()


def test_recapture_same_snapshot_is_a_noop(tmp_store, df_day1, df_day2):
    store = ChangeLogStore(tmp_store)
    store.capture(df_day1, key_column="id")
    store.capture(df_day2, key_column="id")

    before = _fingerprint(tmp_store)
    n_events_before = len(store.read_manifest()["events"])

    # Re-submit the exact same latest snapshot — nothing changed.
    store.capture(df_day2, key_column="id")

    assert len(store.read_manifest()["events"]) == n_events_before, "re-capture must add zero events"
    assert _fingerprint(tmp_store) == before, "store must be byte-identical after a re-capture"


def test_capture_with_no_changes_is_a_noop(tmp_store, df_day1):
    store = ChangeLogStore(tmp_store)
    store.capture(df_day1, key_column="id")
    before = _fingerprint(tmp_store)

    store.capture(df_day1, key_column="id")  # identical frame
    assert _fingerprint(tmp_store) == before
