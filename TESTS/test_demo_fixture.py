# File: TESTS/test_demo_fixture.py
"""US7 — seeded 1000×20 demo+stress fixture (FR-017 / FR-018 / SC-004).

The generator's VALUES come from Faker + seeded ``numpy.random`` and the
*passage of time* comes from ``ChangeLogStore.capture()`` over evolving
snapshots. This suite pins the contract:

  * **reproducible** — same seed ⇒ byte-identical store content,
  * **shape** — final live table is 1000 rows × 20 cols spanning the 6 dtypes,
  * **immutable** — immutable columns never change after an entity's birth,
  * **sparse** — each non-initial capture touches a small fraction of cells,
  * **lifecycle** — births, ≥1 ``__deleted__`` marker, ≥1 resurrection present.

NOTE on "byte-identical": the on-disk events filename embeds the wall-clock
time (``capture`` does not thread the deterministic ``captured_at`` into the
filename, and ``changelog.py`` is out of scope here), so we fingerprint the
*store content* — the parquet bytes in manifest order plus the manifest with the
non-deterministic ``file`` name normalized out. Everything that is part of the
recorded history (event data, timestamps, snapshot ids, schema) is deterministic.
"""

import hashlib
import json
import os

import polars as pl

from changelog import ChangeLogStore, DELETED_FIELD
import reconstruct
from scripts import demo_fixture

# 6 conceptual dtypes; categorical is a low-card utf8, so it shares the "utf8"
# tag → 5 distinct stored type tags represent the 6 dtype classes.
EXPECTED_DTYPE_TAGS = {"int64", "float64", "bool", "datetime[us, UTC]", "utf8"}
IMMUTABLE_FIELDS = ["birth_date", "cohort", "mrn", "ssn", "region", "enrolled"]


def _content_fingerprint(store_path) -> str:
    """A content-stable fingerprint of a ``.flux`` store.

    Hashes every event parquet's raw bytes (in manifest/append order) plus the
    manifest JSON with the non-deterministic per-file ``file`` name removed. Two
    stores generated from the same seed into same-named dirs fingerprint equal.
    """
    store = ChangeLogStore(store_path)
    manifest = store.read_manifest()
    h = hashlib.sha256()
    for entry in manifest["events"]:
        with open(os.path.join(store_path, entry["file"]), "rb") as fh:
            h.update(fh.read())
    norm = dict(manifest)
    norm["events"] = [
        {k: v for k, v in e.items() if k != "file"} for e in manifest["events"]
    ]
    h.update(json.dumps(norm, sort_keys=True).encode("utf-8"))
    return h.hexdigest()


def test_same_seed_is_byte_identical(tmp_path):
    """SC-004 — same seed ⇒ identical store content across independent runs."""
    a = tmp_path / "a" / "demo.flux"
    b = tmp_path / "b" / "demo.flux"
    sa = demo_fixture.generate(a, seed=42)
    sb = demo_fixture.generate(b, seed=42)
    assert sa == sb  # summary dict deterministic
    assert _content_fingerprint(a) == _content_fingerprint(b)


def test_final_table_shape_and_dtypes(tmp_path):
    """Final live table is 1000 rows × 20 cols spanning the 6 dtype classes."""
    store_path = tmp_path / "demo.flux"
    summary = demo_fixture.generate(store_path, seed=42)
    assert summary["rows"] == 1000
    assert summary["cols"] == 20

    store = ChangeLogStore(store_path)
    view = reconstruct.build_mirror_view(store, "now")
    assert view.height == 1000
    assert view.width == 20

    # manifest schema spans every expected dtype tag (the 6 dtype classes).
    schema_tags = set(store.read_manifest()["schema"].values())
    assert EXPECTED_DTYPE_TAGS <= schema_tags, schema_tags


def test_immutable_columns_never_change_after_birth(tmp_path):
    """Immutable columns never change once set.

    Their value is identical across every event for a given entity. (A resurrected
    entity re-emits its full row, including the *same* immutable values — that is a
    re-appearance, not a change; the invariant is that the value is never altered.)
    """
    store_path = tmp_path / "demo.flux"
    demo_fixture.generate(store_path, seed=42)
    store = ChangeLogStore(store_path)
    events = store._read_all_events()

    imm = events.filter(pl.col("field").is_in(IMMUTABLE_FIELDS))
    distinct_per_cell = imm.group_by(["entity_id", "field"]).agg(
        pl.col("value").n_unique().alias("n")
    )
    # zero immutable (entity, field) cells ever take more than one distinct value
    assert distinct_per_cell.filter(pl.col("n") > 1).height == 0


def test_change_is_sparse(tmp_path):
    """Each non-initial capture touches well under ~10% of the rows×cols cells."""
    store_path = tmp_path / "demo.flux"
    summary = demo_fixture.generate(store_path, rows=1000, cols=20, steps=40, seed=42)
    store = ChangeLogStore(store_path)
    manifest = store.read_manifest()

    cell_universe = 1000 * 20
    # events[0] is the initial all-rows snapshot; every subsequent file is sparse.
    for entry in manifest["events"][1:]:
        frac = entry["row_count"] / cell_universe
        assert frac < 0.10, (entry["snapshot_id"], frac)

    # and the reported mean change sits in the spec's ~1-5% band.
    assert 1.0 <= summary["mean_change_pct"] <= 5.0


def test_violations_seed_immutable_changes(tmp_path):
    """T035 — ``violations=8`` seeds exactly 8 immutable-column VIOLATIONS.

    Each violated entity has ONE immutable cell that takes >1 distinct value
    across history (detectable directly in the change-log), the summary reports
    the violated (entity_id, field, time), it is byte-identically reproducible,
    and every other fixture invariant (shape, lifecycle) still holds.
    """
    a = tmp_path / "a" / "demo.flux"
    b = tmp_path / "b" / "demo.flux"
    sa = demo_fixture.generate(a, seed=42, violations=8)
    sb = demo_fixture.generate(b, seed=42, violations=8)

    # reproducible byte-for-byte
    assert sa == sb
    assert _content_fingerprint(a) == _content_fingerprint(b)

    # summary reports exactly 8 violations + the verifiable records
    assert sa["violations"] == 8
    assert len(sa["violation_ids"]) == 8
    assert len(sa["violation_records"]) == 8
    for rec in sa["violation_records"]:
        assert rec["field"] in IMMUTABLE_FIELDS
        assert isinstance(rec["entity_id"], int)
        assert rec["timestamp"]  # ISO 8601 string

    # the change-log shows exactly 8 immutable (entity, field) cells that change
    store = ChangeLogStore(a)
    events = store._read_all_events()
    imm = events.filter(pl.col("field").is_in(IMMUTABLE_FIELDS))
    distinct_per_cell = imm.group_by(["entity_id", "field"]).agg(
        pl.col("value").n_unique().alias("n")
    )
    changed = distinct_per_cell.filter(pl.col("n") > 1)
    assert changed.height == 8

    # the changed (entity_id, field) cells match exactly what the summary reported
    changed_set = {
        (int(r["entity_id"]), r["field"]) for r in changed.iter_rows(named=True)
    }
    reported_set = {(rec["entity_id"], rec["field"]) for rec in sa["violation_records"]}
    assert changed_set == reported_set

    # rest of the fixture invariants still hold (shape + lifecycle present).
    view = reconstruct.build_mirror_view(store, "now")
    assert view.height == 1000
    assert view.width == 20
    assert sa["deletes"] >= 1 and sa["resurrections"] >= 1 and sa["births"] >= 1


def test_lifecycle_present(tmp_path):
    """≥1 deletion marker, ≥1 resurrection, ≥1 birth are visibly present."""
    store_path = tmp_path / "demo.flux"
    summary = demo_fixture.generate(store_path, seed=42)
    store = ChangeLogStore(store_path)
    events = store._read_all_events()

    # ≥1 __deleted__ marker.
    deleted_markers = events.filter(pl.col("field") == DELETED_FIELD)
    assert deleted_markers.height >= 1
    assert summary["deletes"] >= 1

    # ≥1 birth (the summary counts births; initial snapshot + later arrivals).
    assert summary["births"] >= 1

    # ≥1 resurrection: an entity that went active → deleted → active, confirmed
    # via reconstruct.row_state at the final time.
    assert summary["resurrections"] >= 1
    deleted_ids = deleted_markers["entity_id"].unique().to_list()
    resurrected = [
        eid for eid in deleted_ids if reconstruct.row_state(store, eid, "now")["resurrected"]
    ]
    assert resurrected, "expected at least one resurrected entity (active→deleted→active)"
