# File: TESTS/conftest.py
"""Shared pytest fixtures for the change-log-first test suite.

No Snowflake / Snowpark (research R8): every fixture feeds plain Polars
DataFrames. The sample frames model three daily snapshots of the same
keyed table so the capture / reconstruction / lifecycle stories can be
exercised against a known ground truth:

  day1 → id 1,2,3 present
  day2 → id 2 updated (score), id 4 inserted
  day3 → id 1 deleted (vanished), id 2 updated again

Types are deliberately mixed (int64 / utf8 / float64 / bool / datetime[us, UTC])
so type-fidelity reconstruction (US2) has something to prove.
"""

import os
import sys
from datetime import datetime, timezone

import polars as pl
import pytest

# Make the flat top-level modules (changelog, reconstruct, fluxstate) importable
# when pytest is invoked from the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _utc(y, m, d):
    return datetime(y, m, d, tzinfo=timezone.utc)


@pytest.fixture
def tmp_store(tmp_path):
    """Path to a fresh ``demo.flux`` store under pytest's tmp_path."""
    return tmp_path / "demo.flux"


@pytest.fixture
def df_day1():
    """Initial snapshot: ids 1, 2, 3."""
    return pl.DataFrame(
        {
            "id": pl.Series([1, 2, 3], dtype=pl.Int64),
            "name": pl.Series(["alice", "bob", "carol"], dtype=pl.Utf8),
            "score": pl.Series([10.0, 20.0, 30.0], dtype=pl.Float64),
            "active": pl.Series([True, True, False], dtype=pl.Boolean),
            "seen_at": pl.Series(
                [_utc(2026, 1, 1), _utc(2026, 1, 1), _utc(2026, 1, 1)],
                dtype=pl.Datetime("us", "UTC"),
            ),
        }
    )


@pytest.fixture
def df_day2():
    """Second snapshot: id 2's score changes; id 4 inserted; id 3 unchanged; id 1 unchanged."""
    return pl.DataFrame(
        {
            "id": pl.Series([1, 2, 3, 4], dtype=pl.Int64),
            "name": pl.Series(["alice", "bob", "carol", "dave"], dtype=pl.Utf8),
            "score": pl.Series([10.0, 25.0, 30.0, 40.0], dtype=pl.Float64),
            "active": pl.Series([True, True, False, True], dtype=pl.Boolean),
            "seen_at": pl.Series(
                [_utc(2026, 1, 1), _utc(2026, 1, 2), _utc(2026, 1, 1), _utc(2026, 1, 2)],
                dtype=pl.Datetime("us", "UTC"),
            ),
        }
    )


@pytest.fixture
def df_day3():
    """Third snapshot: id 1 has vanished (delete); id 2 updated again; ids 3, 4 unchanged."""
    return pl.DataFrame(
        {
            "id": pl.Series([2, 3, 4], dtype=pl.Int64),
            "name": pl.Series(["bob", "carol", "dave"], dtype=pl.Utf8),
            "score": pl.Series([27.5, 30.0, 40.0], dtype=pl.Float64),
            "active": pl.Series([False, False, True], dtype=pl.Boolean),
            "seen_at": pl.Series(
                [_utc(2026, 1, 3), _utc(2026, 1, 1), _utc(2026, 1, 2)],
                dtype=pl.Datetime("us", "UTC"),
            ),
        }
    )
