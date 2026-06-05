# File: TESTS/test_output_formats.py
"""US4 — multi-format output for downstream consumers (SC-005 / API-3).

``save_mirror_table`` returns/writes the reconstructed mirror view in the
requested representation; contents are identical across all four; an unknown
explicit format raises ``ValueError``; legacy positional calls still write
parquet (API-6).
"""

from datetime import datetime, timezone

import polars as pl
import pyarrow as pa
import pytest

from fluxstate import FluxState

U = lambda *a: datetime(*a, tzinfo=timezone.utc)


def _fs(tmp_store):
    fs = FluxState(pl.DataFrame({"id": [1, 2], "risk": [0.4, 0.7]}),
                   key_column="id", store_path=str(tmp_store))
    fs.update_mirror_table(captured_at=U(2026, 1, 1))
    fs2 = FluxState(pl.DataFrame({"id": [1, 2], "risk": [0.4, 0.9]}),
                    key_column="id", store_path=str(tmp_store))
    fs2.update_mirror_table(captured_at=U(2026, 1, 2))
    return fs2


def _norm(df: pl.DataFrame):
    return df.sort("id").to_dicts()


def test_polars_format_returns_dataframe(tmp_store):
    fs = _fs(tmp_store)
    out = fs.save_mirror_table(output_format="polars")
    assert isinstance(out, pl.DataFrame)
    assert _norm(out) == [{"id": 1, "risk": 0.4}, {"id": 2, "risk": 0.9}]


def test_arrow_format_returns_pa_table(tmp_store):
    fs = _fs(tmp_store)
    out = fs.save_mirror_table(output_format="arrow")
    assert isinstance(out, pa.Table)
    assert _norm(pl.from_arrow(out)) == _norm(fs.save_mirror_table(output_format="polars"))


def test_parquet_and_csv_write_files_with_identical_contents(tmp_store, tmp_path):
    fs = _fs(tmp_store)
    base = fs.save_mirror_table(output_format="polars")

    pq_path = tmp_path / "out.parquet"
    csv_path = tmp_path / "out.csv"
    rp = fs.save_mirror_table(output_path_parquet=str(pq_path), output_format="parquet")
    rc = fs.save_mirror_table(csv_path=str(csv_path), output_format="csv")
    assert pq_path.exists() and csv_path.exists()
    assert str(rp) == str(pq_path) and str(rc) == str(csv_path)

    assert _norm(pl.read_parquet(pq_path)) == _norm(base)
    assert _norm(pl.read_csv(csv_path)) == _norm(base)


def test_unknown_format_raises(tmp_store):
    fs = _fs(tmp_store)
    with pytest.raises(ValueError):
        fs.save_mirror_table(output_format="yaml")


def test_format_missing_its_path_raises(tmp_store):
    fs = _fs(tmp_store)
    with pytest.raises(ValueError):
        fs.save_mirror_table(output_format="parquet")  # no output_path_parquet


def test_legacy_positional_parquet_still_writes_file(tmp_store, tmp_path):
    """API-6: save_mirror_table('out.parquet') writes parquet (does NOT return a DataFrame)."""
    fs = _fs(tmp_store)
    pq_path = tmp_path / "legacy.parquet"
    result = fs.save_mirror_table(str(pq_path))
    assert pq_path.exists()
    assert not isinstance(result, pl.DataFrame)
