# File: TESTS/test_audit_regressions.py
"""Regression tests for the 2026-06 adversarial audit findings.

Each test fails against the pre-fix code and passes after the fix. IDs map to
the audit verdict table (A1/A2/A4 = general sweep, F3/F4/F5 = targeted hunt).
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import polars as pl
import pytest

import reconstruct
from changelog import ChangeLogStore
from reconstruct import _as_of

REPO = Path(__file__).resolve().parent.parent
T1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
T2 = datetime(2026, 1, 2, tzinfo=timezone.utc)


def _store(tmp_path) -> ChangeLogStore:
    return ChangeLogStore(tmp_path / "s.flux")


# --- A1: historical column must survive a later column drop ----------------- #
def test_a1_dropped_column_survives_in_historical_view(tmp_path):
    s = _store(tmp_path)
    s.capture(pl.DataFrame({"id": [1], "a": [1], "b": [100]}), "id", captured_at=T1)
    s.capture(pl.DataFrame({"id": [1], "a": [2]}), "id", captured_at=T2)  # b dropped

    v1 = reconstruct.build_mirror_view(s, T=T1)
    assert "b" in v1.columns, "dropping b from a later snapshot erased it from the T1 view"
    assert v1.filter(pl.col("id") == 1)["b"][0] == 100


# --- A2: duplicate key values in a snapshot must be rejected ---------------- #
def test_a2_duplicate_key_rejected(tmp_path):
    s = _store(tmp_path)
    with pytest.raises(ValueError, match="duplicate"):
        s.capture(pl.DataFrame({"id": [1, 1, 2], "val": [10, 99, 20]}), "id")


def test_a2_unique_keys_still_ok(tmp_path):
    s = _store(tmp_path)
    res = s.capture(pl.DataFrame({"id": [1, 2, 3], "val": [10, 20, 30]}), "id")
    assert res["events_added"] > 0


# --- A4: _as_of must be order-independent ----------------------------------- #
def test_a4_as_of_handles_unsorted_history():
    dt = lambda y: datetime(y, 1, 1, tzinfo=timezone.utc)  # noqa: E731
    unsorted = [
        {"date": dt(2023), "value": "c"},
        {"date": dt(2020), "value": "a"},
        {"date": dt(2021), "value": "b"},
    ]
    assert _as_of(unsorted, dt(2022))["value"] == "b"
    # sorted input unchanged
    assert _as_of(sorted(unsorted, key=lambda e: e["date"]), dt(2022))["value"] == "b"


# --- F3: small-row fixtures must not crash ---------------------------------- #
@pytest.mark.parametrize("rows", [2, 5, 10, 19, 20, 21])
def test_f3_small_row_fixture_does_not_crash(rows):
    from scripts.demo_fixture import generate

    with tempfile.TemporaryDirectory() as d:
        summary = generate(Path(d) / "x.flux", rows=rows, steps=3, cols=20, seed=42)
    assert summary["births"] >= 1


# --- F4 / F5: CLI error discipline ------------------------------------------ #
def _cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "flux_cli.py", *args],
        cwd=REPO, capture_output=True, text=True,
    )


def test_f4_read_on_missing_store_errors(tmp_path):
    missing = str(tmp_path / "nope.flux")
    r = _cli("travel", missing, "--as-of", "now")
    assert r.returncode != 0, "read on a non-existent store must NOT exit 0"
    assert "not found" in r.stderr.lower()


def test_f5_invalid_as_of_is_clean_error(tmp_path):
    r = _cli("travel", str(tmp_path / "x.flux"), "--as-of", "GARBAGE")
    assert r.returncode != 0
    assert "Traceback" not in r.stderr, "invalid --as-of must not dump a raw traceback"
    assert "invalid" in r.stderr.lower()


def test_f5_invalid_at_is_clean_error(tmp_path):
    snap = tmp_path / "snap.parquet"
    pl.DataFrame({"id": [1], "v": [10]}).write_parquet(snap)
    r = _cli("capture", str(tmp_path / "s.flux"), str(snap), "--key", "id", "--at", "NOT_A_DATE")
    assert r.returncode != 0
    assert "Traceback" not in r.stderr
    assert "invalid" in r.stderr.lower()


# =========================================================================== #
# 2026-06-20 pharos pre-integration audit (schema churn + data-loss findings) #
# =========================================================================== #

# --- F-DROP: a dropped column must read as NULL at/after the drop (no ghost) - #
def test_fdrop_dropped_column_is_null_after_drop(tmp_path):
    s = _store(tmp_path)
    s.capture(pl.DataFrame({"id": [1, 2], "name": ["a", "b"], "score": [99.0, 88.0]}), "id", captured_at=T1)
    s.capture(pl.DataFrame({"id": [1, 2], "name": ["a", "b"]}), "id", captured_at=T2)  # score dropped

    # Before the drop: the value is intact (A1 union schema).
    before = reconstruct.build_mirror_view(s, T=T1)
    assert before.filter(pl.col("id") == 1)["score"][0] == 99.0
    # At/after the drop: the column reads NULL, not the stale "ghost" value.
    after = reconstruct.build_mirror_view(s, T=T2)
    assert after.filter(pl.col("id") == 1)["score"][0] is None
    assert after.filter(pl.col("id") == 2)["score"][0] is None


def test_fdrop_redropping_same_snapshot_is_noop(tmp_path):
    s = _store(tmp_path)
    s.capture(pl.DataFrame({"id": [1], "name": ["a"], "score": [1.0]}), "id", captured_at=T1)
    s.capture(pl.DataFrame({"id": [1], "name": ["a"]}), "id", captured_at=T2)
    # Re-capturing the already-dropped snapshot emits no new tombstones.
    r = s.capture(pl.DataFrame({"id": [1], "name": ["a"]}), "id",
                  captured_at=datetime(2026, 1, 3, tzinfo=timezone.utc))
    assert r["noop"] is True and r["events_added"] == 0


# --- F-RENAME: rename with identical values must NOT be a silent no-op ------- #
def test_frename_same_values_recorded_as_drop_plus_add(tmp_path):
    s = _store(tmp_path)
    s.capture(pl.DataFrame({"id": [1], "price": [100.0]}), "id", captured_at=T1)
    r = s.capture(pl.DataFrame({"id": [1], "new_price": [100.0]}), "id", captured_at=T2)
    assert r["noop"] is False, "a column rename (same values) was silently dropped"

    v = reconstruct.build_mirror_view(s, T=T2)
    assert v.filter(pl.col("id") == 1)["new_price"][0] == 100.0
    assert "price" not in v.columns or v.filter(pl.col("id") == 1)["price"][0] is None


# --- F-NULLKEY: a null key value must be rejected (else silent data loss) ---- #
def test_fnullkey_null_key_rejected(tmp_path):
    s = _store(tmp_path)
    df = pl.DataFrame({"id": [None, 1], "val": ["a", "b"]}, schema={"id": pl.Int64, "val": pl.Utf8})
    with pytest.raises(ValueError, match="null"):
        s.capture(df, "id")


# --- F-CLI: capture must give a clean error (not a traceback) on bad input --- #
def test_fcli_capture_missing_input_is_clean_error(tmp_path):
    r = _cli("capture", str(tmp_path / "s.flux"), str(tmp_path / "nope.parquet"), "--key", "id")
    assert r.returncode != 0
    assert "Traceback" not in r.stderr, "missing input file must not dump a raw traceback"


def test_fcli_capture_bad_key_is_clean_error(tmp_path):
    snap = tmp_path / "snap.parquet"
    pl.DataFrame({"id": [1], "v": [10]}).write_parquet(snap)
    r = _cli("capture", str(tmp_path / "s.flux"), str(snap), "--key", "missing_col")
    assert r.returncode != 0
    assert "Traceback" not in r.stderr, "a bad --key must not dump a raw traceback"
    assert "flux:" in r.stderr.lower()


# --- #4: same-timestamp entities reconstruct in deterministic key order ------ #
def test_issue4_mirror_view_row_order_is_key_sorted(tmp_path):
    s = _store(tmp_path)
    # Capture entities at ONE timestamp in REVERSE id order.
    s.capture(pl.DataFrame({"id": [50, 40, 30], "v": [5, 4, 3]}), "id", captured_at=T1)
    ids = reconstruct.build_mirror_view(s, "now")["id"].to_list()
    assert ids == [30, 40, 50], f"mirror-view rows must be key-sorted, got {ids}"


# --- #3: the STORE is a faithful recorder — full µs survives round-trip ------ #
# (The viewer renders datetime VALUE cells at date/ms resolution by design — a
#  VIEW, not the recorder — but the store itself must never lose precision.)
def test_issue3_store_preserves_microsecond_precision(tmp_path):
    s = _store(tmp_path)
    ts = datetime(2026, 1, 1, 12, 0, 0, 123456, tzinfo=timezone.utc)
    s.capture(
        pl.DataFrame({"id": pl.Series([1], dtype=pl.Int64),
                      "seen": pl.Series([ts], dtype=pl.Datetime("us", "UTC"))}),
        "id", captured_at=T1,
    )
    got = reconstruct.as_of(s, 1, "seen", T2)["value"]
    assert got == ts, "the store must preserve full microsecond precision"
    assert got.microsecond == 123456
