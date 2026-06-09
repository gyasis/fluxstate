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
