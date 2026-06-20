# File: TESTS/test_cli.py
"""T007 — ``flux`` CLI ↔ library parity (US1 / contracts/cli.md).

Every read subcommand is a thin wrapper whose output MUST equal the corresponding
library function (FR-015). These tests drive ``flux_cli.main([...])`` in-process and
assert the parsed CLI output (captured stdout, ``--json``) equals the direct
``reconstruct.*`` / ``build_mirror_view`` return values over the SAME store.

Guarantees exercised (contracts/cli.md → "Behavioral guarantees"):

- **CLI-2** — ``travel --as-of T`` records equal ``build_mirror_view(store, T)``;
  ``--as-of`` before any history → empty result + exit 0 (NOT an error).
- **CLI-3** — ``timeline`` / ``row-state`` outputs equal the ``reconstruct.*``
  returns, incl. a deleted-then-resurrected entity, values typed.
- **CLI-1** (capture) and **CLI-4** (gen-fixture reproducibility) are WRITTEN here
  but skipped — their impls land later (capture=T009/Wave4; gen-fixture=US7/
  T012-T014). T010 un-skips CLI-1; gen-fixture stays covered by ``test_demo_fixture``.

Stores are built with ``ChangeLogStore.capture(df, key, captured_at=...)`` under
``tmp_path`` (deterministic timestamps, no Snowflake).
"""

import json
from datetime import datetime, timezone

import polars as pl
import pytest

import flux_cli
import reconstruct
from changelog import ChangeLogStore


def _utc(y, m, d):
    return datetime(y, m, d, tzinfo=timezone.utc)


def _run(capsys, argv):
    """Run ``flux_cli.main(argv)`` in-process; return ``(exit_code, stdout)``."""
    code = flux_cli.main(argv)
    out = capsys.readouterr().out
    return code, out


def _frame(ids, names, scores):
    return pl.DataFrame(
        {
            "id": pl.Series(ids, dtype=pl.Int64),
            "name": pl.Series(names, dtype=pl.Utf8),
            "score": pl.Series(scores, dtype=pl.Float64),
        }
    )


@pytest.fixture
def lifecycle_store(tmp_path):
    """A store exercising insert / update / delete / resurrection with typed values.

      day1 → ids 1, 2, 3 born
      day2 → id 2 score updated; id 4 inserted; id 1 vanishes (DELETE)
      day3 → id 1 RETURNS (resurrection); id 2 updated again
    """
    store = ChangeLogStore(tmp_path / "demo.flux")
    store.capture(_frame([1, 2, 3], ["a", "b", "c"], [10.0, 20.0, 30.0]),
                  key_column="id", captured_at=_utc(2026, 1, 1))
    store.capture(_frame([2, 3, 4], ["b", "c", "d"], [25.0, 30.0, 40.0]),
                  key_column="id", captured_at=_utc(2026, 1, 2))
    store.capture(_frame([1, 2, 3, 4], ["a", "b2", "c", "d"], [10.0, 27.5, 30.0, 40.0]),
                  key_column="id", captured_at=_utc(2026, 1, 3))
    return store


# --------------------------------------------------------------------------- #
# CLI-2 — travel                                                              #
# --------------------------------------------------------------------------- #
def test_cli_travel_json_equals_build_mirror_view(capsys, lifecycle_store):
    """`flux travel --as-of T --json` records == build_mirror_view(store, T)."""
    store = lifecycle_store
    T = _utc(2026, 1, 2)

    code, out = _run(capsys, ["travel", str(store.path), "--as-of", T.isoformat(), "--json"])
    assert code == 0
    cli_records = json.loads(out)

    view = reconstruct.build_mirror_view(store, T=T)
    lib_records = [_jsonify(r) for r in view.iter_rows(named=True)]
    assert cli_records == lib_records
    # typed value sanity: id 2's score was 25.0 at day2 (a float, not a string).
    rec2 = next(r for r in cli_records if r["id"] == 2)
    assert rec2["score"] == 25.0 and isinstance(rec2["score"], float)


def test_cli_travel_now_equals_library(capsys, lifecycle_store):
    store = lifecycle_store
    code, out = _run(capsys, ["travel", str(store.path), "--as-of", "now", "--json"])
    assert code == 0
    cli_records = json.loads(out)
    view = reconstruct.build_mirror_view(store, T="now")
    assert cli_records == [_jsonify(r) for r in view.iter_rows(named=True)]


def test_cli_travel_before_history_empty_exit_zero(capsys, lifecycle_store):
    """CLI-2: --as-of before any event → empty result, exit 0 (NOT an error)."""
    store = lifecycle_store
    code, out = _run(capsys, ["travel", str(store.path), "--as-of", "2025-12-31", "--json"])
    assert code == 0
    assert json.loads(out) == []
    # matches the library: build_mirror_view before history is empty.
    assert reconstruct.build_mirror_view(store, T=_utc(2025, 12, 31)).height == 0


# --------------------------------------------------------------------------- #
# CLI-3 — timeline / row-state                                                #
# --------------------------------------------------------------------------- #
def test_cli_timeline_field_equals_library(capsys, lifecycle_store):
    """`flux timeline <id> --field score --json` == reconstruct.get_timeline(...)."""
    store = lifecycle_store
    code, out = _run(capsys, ["timeline", str(store.path), "2", "--field", "score", "--json"])
    assert code == 0
    cli = json.loads(out)
    lib = reconstruct.get_timeline(store, "2", field="score")
    assert cli == [_jsonify(e) for e in lib]
    # id 2's score: 20.0 (day1) → 25.0 (day2) → 27.5 (day3), all typed floats.
    assert [e["value"] for e in cli] == [20.0, 25.0, 27.5]


def test_cli_timeline_all_fields_equals_library(capsys, lifecycle_store):
    store = lifecycle_store
    code, out = _run(capsys, ["timeline", str(store.path), "2", "--json"])
    assert code == 0
    cli = json.loads(out)
    lib = reconstruct.get_timeline(store, "2", field=None)
    assert cli == [_jsonify(e) for e in lib]
    # field key present when --field omitted.
    assert all("field" in e for e in cli)


def test_cli_row_state_deleted_equals_library(capsys, lifecycle_store):
    """CLI-3: a deleted entity at the delete time == reconstruct.row_state."""
    store = lifecycle_store
    T = _utc(2026, 1, 2)  # id 1 was deleted at day2
    code, out = _run(capsys, ["row-state", str(store.path), "1", "--as-of", T.isoformat(), "--json"])
    assert code == 0
    cli = json.loads(out)
    lib = reconstruct.row_state(store, "1", T=T)
    assert cli == lib
    assert cli["state"] == "deleted"


def test_cli_row_state_resurrected_equals_library(capsys, lifecycle_store):
    """CLI-3: id 1 returns at day3 → active + resurrected, equal to the library."""
    store = lifecycle_store
    code, out = _run(capsys, ["row-state", str(store.path), "1", "--as-of", "now", "--json"])
    assert code == 0
    cli = json.loads(out)
    lib = reconstruct.row_state(store, "1", T="now")
    assert cli == lib
    assert cli["state"] == "active" and cli["resurrected"] is True


def test_cli_row_state_unborn_equals_library(capsys, lifecycle_store):
    store = lifecycle_store
    code, out = _run(capsys, ["row-state", str(store.path), "1", "--as-of", "2025-12-31", "--json"])
    assert code == 0
    cli = json.loads(out)
    assert cli == reconstruct.row_state(store, "1", T=_utc(2025, 12, 31))
    assert cli["state"] == "unborn"


# --------------------------------------------------------------------------- #
# view / info — thin-wrapper sanity                                           #
# --------------------------------------------------------------------------- #
def test_cli_view_json_equals_travel(capsys, lifecycle_store):
    store = lifecycle_store
    code, out = _run(capsys, ["view", str(store.path), "--as-of", "now", "--json"])
    assert code == 0
    view = reconstruct.build_mirror_view(store, T="now")
    assert json.loads(out) == [_jsonify(r) for r in view.iter_rows(named=True)]


def test_cli_view_csv_now_writes_library_output(capsys, lifecycle_store, tmp_path):
    store = lifecycle_store
    out_path = tmp_path / "view.csv"
    code, _ = _run(capsys, ["view", str(store.path), "--format", "csv", "--out", str(out_path)])
    assert code == 0 and out_path.exists()
    written = pl.read_csv(out_path)
    expected = reconstruct.build_mirror_view(store, T="now")
    assert written.sort("id").to_dicts() == expected.sort("id").to_dicts()


def test_cli_view_file_format_requires_out(capsys, lifecycle_store):
    store = lifecycle_store
    code, _ = _run(capsys, ["view", str(store.path), "--format", "parquet"])
    assert code != 0  # missing --out → non-zero exit


def test_cli_info_json_reports_manifest(capsys, lifecycle_store):
    store = lifecycle_store
    code, out = _run(capsys, ["info", str(store.path), "--json"])
    assert code == 0
    info = json.loads(out)
    manifest = store.read_manifest()
    assert info["key_column"] == manifest["key_column"]
    assert info["schema"] == manifest["schema"]
    assert info["events_files"] == len(manifest["events"])
    assert info["snapshot_count"] == len({e["snapshot_id"] for e in manifest["events"]})


# --------------------------------------------------------------------------- #
# CLI-1 (capture) + CLI-4 (gen-fixture) — written now, impls land later.      #
# --------------------------------------------------------------------------- #
def test_cli_capture_matches_changelog(capsys, tmp_path):
    """CLI-1: `flux capture` twice on differing snapshots → one new events file on the
    2nd; a third identical capture is a no-op (noop:true). Equals ChangeLogStore.capture.
    """
    store_path = tmp_path / "cap.flux"
    snap1 = tmp_path / "snap1.parquet"
    snap2 = tmp_path / "snap2.parquet"
    _frame([1, 2], ["a", "b"], [1.0, 2.0]).write_parquet(snap1)
    _frame([1, 2], ["a", "B"], [1.0, 9.0]).write_parquet(snap2)

    code, out = _run(capsys, ["capture", str(store_path), str(snap1), "--key", "id",
                              "--at", "2026-01-01T00:00:00+00:00", "--json"])
    assert code == 0
    r1 = json.loads(out)
    assert r1["noop"] is False

    code, out = _run(capsys, ["capture", str(store_path), str(snap2), "--key", "id",
                              "--at", "2026-01-02T00:00:00+00:00", "--json"])
    r2 = json.loads(out)
    assert r2["noop"] is False and r2["events_added"] > 0

    # Third identical capture of snap2 → no-op.
    code, out = _run(capsys, ["capture", str(store_path), str(snap2), "--key", "id",
                              "--at", "2026-01-03T00:00:00+00:00", "--json"])
    assert json.loads(out)["noop"] is True

    store = ChangeLogStore(store_path)
    assert len(store.read_manifest()["events"]) == 2


def test_cli_gen_fixture_reproducible(capsys, tmp_path):
    """CLI-4: `flux gen-fixture --seed N` is reproducible — same seed → same store."""
    a = tmp_path / "a.flux"
    b = tmp_path / "b.flux"
    _run(capsys, ["gen-fixture", str(a), "--seed", "42", "--rows", "50", "--steps", "5"])
    _run(capsys, ["gen-fixture", str(b), "--seed", "42", "--rows", "50", "--steps", "5"])
    sa = ChangeLogStore(a)
    sb = ChangeLogStore(b)
    ev_a = pl.concat([pl.read_parquet(f) for f in sa.list_events()])
    ev_b = pl.concat([pl.read_parquet(f) for f in sb.list_events()])
    assert ev_a.sort(["timestamp", "entity_id", "field"]).equals(
        ev_b.sort(["timestamp", "entity_id", "field"])
    )


# Re-export the CLI's JSON normalizer so library returns are compared on equal footing.
_jsonify = flux_cli._jsonify
