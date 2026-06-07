# File: TESTS/test_parity_export.py
"""T006 — reconstruction PARITY ground-truth export (SC-003 / VD-2).

This harness exports the *source of truth* for the JS viewer's reconstruction
parity gate. It drives the SHIPPED Python primitives in ``reconstruct.py`` over a
KNOWN ``.flux`` store, captures their exact outputs as probes, and writes a
``contracts/parity.schema.json``-shaped JSON to
``viewer/tests/fixtures/parity-ground-truth.json``.

The viewer test (``viewer/tests/reconstruct.parity.test.ts``) later feeds the same
raw change ``events`` into ``reconstruct.ts`` and asserts it reproduces every
``expected`` value identically. Therefore this file MUST use the real Python
functions — never reimplement reconstruction here.

The KNOWN store deliberately exercises every lifecycle/dtype edge the JS port must
honor (``must_include_cases``):

  id 100 — born(day1) → updated(day2) → DELETED(day3, vanishes) → RESURRECTED(day4)
  id 200 — born(day1) → score becomes a GENUINE NULL(day2, a real null, NOT a delete)
  id 300 — INSERT/birth at day2

Mixed dtypes: int64 (id) / utf8 (name) / float64 (score) / bool (flag) /
datetime[us, UTC] (seen).
"""

import glob
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

from changelog import ChangeLogStore
import reconstruct

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_PATH = REPO_ROOT / "viewer" / "tests" / "fixtures" / "parity-ground-truth.json"


def _utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# JSON serialization — ISO-8601 UTC for datetimes, typed values preserved.    #
# --------------------------------------------------------------------------- #
def _jsonify(v):
    """Make a reconstructed value JSON-safe WITHOUT lying about its type.

    - datetime → ISO-8601 UTC string
    - dict / list → recurse
    - int/float/bool/str/None → passed through (preserved as numbers, bools, …)
    """
    if isinstance(v, datetime):
        return reconstruct.to_utc(v).isoformat()
    if isinstance(v, dict):
        return {k: _jsonify(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonify(x) for x in v]
    return v


def _mirror_to_obj(df: pl.DataFrame) -> dict:
    """build_mirror_view DataFrame → ``{columns:[...], rows:[[...]]}`` typed + JSON-safe."""
    columns = list(df.columns)
    rows = [[_jsonify(c) for c in row] for row in df.iter_rows()]
    return {"columns": columns, "rows": rows}


# --------------------------------------------------------------------------- #
# Build the KNOWN store.                                                       #
# --------------------------------------------------------------------------- #
def _build_known_store(store_path: Path) -> ChangeLogStore:
    store = ChangeLogStore(store_path)

    def frame(ids, names, scores, flags, seens):
        return pl.DataFrame(
            {
                "id": pl.Series(ids, dtype=pl.Int64),
                "name": pl.Series(names, dtype=pl.Utf8),
                "score": pl.Series(scores, dtype=pl.Float64),
                "flag": pl.Series(flags, dtype=pl.Boolean),
                "seen": pl.Series(seens, dtype=pl.Datetime("us", "UTC")),
            }
        )

    # day1: ids 100, 200 born.
    store.capture(
        frame(
            [100, 200], ["alice", "bob"], [1.0, 2.0], [True, False],
            [_utc(2026, 1, 1), _utc(2026, 1, 1)],
        ),
        key_column="id", captured_at=_utc(2026, 1, 1),
    )
    # day2: 100 normal update; 200 score → GENUINE NULL (real null, not delete);
    #       300 INSERT/birth.
    store.capture(
        frame(
            [100, 200, 300], ["alice2", "bob", "carol"], [1.5, None, 3.0],
            [True, False, True],
            [_utc(2026, 1, 2), _utc(2026, 1, 1), _utc(2026, 1, 2)],
        ),
        key_column="id", captured_at=_utc(2026, 1, 2),
    )
    # day3: 100 VANISHES → __deleted__ marker.
    store.capture(
        frame(
            [200, 300], ["bob", "carol"], [None, 3.0], [False, True],
            [_utc(2026, 1, 1), _utc(2026, 1, 2)],
        ),
        key_column="id", captured_at=_utc(2026, 1, 3),
    )
    # day4: 100 RETURNS → resurrection (same entity_id, continuous trail).
    store.capture(
        frame(
            [100, 200, 300], ["alice3", "bob", "carol"], [9.0, None, 3.0],
            [True, False, True],
            [_utc(2026, 1, 4), _utc(2026, 1, 1), _utc(2026, 1, 2)],
        ),
        key_column="id", captured_at=_utc(2026, 1, 4),
    )
    return store


# --------------------------------------------------------------------------- #
# Probe grid + raw-events export → parity ground-truth JSON.                   #
# --------------------------------------------------------------------------- #
def _raw_events(store: ChangeLogStore) -> list[dict]:
    """All manifest-valid ``events/*.parquet`` rows as JSON records (timestamps ISO)."""
    files = [str(p) for p in store.list_events()]
    if not files:
        return []
    ev = pl.concat([pl.read_parquet(f) for f in files], how="vertical")
    ev = ev.sort(["timestamp", "entity_id", "field"])
    out = []
    for r in ev.iter_rows(named=True):
        out.append(
            {
                "entity_id": r["entity_id"],
                "timestamp": reconstruct.to_utc(r["timestamp"]).isoformat(),
                "field": r["field"],
                "value": r["value"],  # raw stored Utf8 string or null
                "dtype": r["dtype"],
                "snapshot_id": r["snapshot_id"],
            }
        )
    return out


def _snap_points(store: ChangeLogStore) -> list[str]:
    ev = pl.concat(
        [pl.read_parquet(str(p)) for p in store.list_events()], how="vertical"
    )
    ts = sorted({reconstruct.to_utc(t).isoformat() for t in ev["timestamp"].to_list()})
    return ts


def _build_ground_truth(store: ChangeLogStore) -> dict:
    key_column, pl_schema = reconstruct._typed_schema(store)
    schema_tags = store.read_manifest()["schema"]

    # Time grid: before history, between captures, and "now".
    T_before = _utc(2025, 12, 31)            # before any event
    T_day2 = _utc(2026, 1, 2)                # at a capture
    T_between = _utc(2026, 1, 2, 12)         # between day2 and day3
    T_day3 = _utc(2026, 1, 3)                # the deletion of id 100
    T_now = None                            # current

    probes: list[dict] = []

    def add(kind, expected, entity_id=None, field=None, T=None):
        p = {"kind": kind, "expected": _jsonify(expected)}
        if entity_id is not None:
            p["entity_id"] = str(entity_id)
        if field is not None:
            p["field"] = field
        # Always carry T for time-scoped probes (null encodes "now").
        if kind in ("as_of", "row_state", "build_mirror_view"):
            p["T"] = T.isoformat() if isinstance(T, datetime) else None
        return probes.append(p)

    # --- as_of probes (incl. genuine null + before-history empty) ---
    add("as_of", reconstruct.as_of(store, 100, "name", T_before),
        entity_id=100, field="name", T=T_before)          # before history → None
    add("as_of", reconstruct.as_of(store, 100, "name", T_day2),
        entity_id=100, field="name", T=T_day2)
    add("as_of", reconstruct.as_of(store, 100, "score", T_between),
        entity_id=100, field="score", T=T_between)        # between captures
    add("as_of", reconstruct.as_of(store, 200, "score", T_day2),
        entity_id=200, field="score", T=T_day2)            # GENUINE NULL cell
    add("as_of", reconstruct.as_of(store, 100, "seen", T_now),
        entity_id=100, field="seen", T=T_now)              # datetime value, "now"
    add("as_of", reconstruct.as_of(store, 300, "score", T_now),
        entity_id=300, field="score", T=T_now)

    # --- timeline probes ---
    add("timeline", reconstruct.get_timeline(store, 100, field="name"),
        entity_id=100, field="name")
    add("timeline", reconstruct.get_timeline(store, 200, field="score"),
        entity_id=200, field="score")                      # includes a null entry
    add("timeline", reconstruct.get_timeline(store, 100, None),
        entity_id=100, field=None)                         # all fields, with `field` key

    # --- row_state probes (deleted + resurrected) ---
    add("row_state", reconstruct.row_state(store, 100, T_day3),
        entity_id=100, T=T_day3)                           # DELETED at day3
    add("row_state", reconstruct.row_state(store, 100, "now"),
        entity_id=100, T=T_now)                            # RESURRECTED (active)
    add("row_state", reconstruct.row_state(store, 100, T_before),
        entity_id=100, T=T_before)                         # unborn
    add("row_state", reconstruct.row_state(store, 200, "now"),
        entity_id=200, T=T_now)                            # active

    # --- change_count probes ---
    add("change_count", reconstruct.change_count(store, 100), entity_id=100)
    add("change_count", reconstruct.change_count(store, 200), entity_id=200)
    add("change_count", reconstruct.change_count(store, 300), entity_id=300)

    # --- build_mirror_view at several T (incl. before-history empty) ---
    add("build_mirror_view",
        _mirror_to_obj(reconstruct.build_mirror_view(store, T_before)), T=T_before)
    add("build_mirror_view",
        _mirror_to_obj(reconstruct.build_mirror_view(store, T_day2)), T=T_day2)
    add("build_mirror_view",
        _mirror_to_obj(reconstruct.build_mirror_view(store, T_day3)), T=T_day3)  # 100 deleted
    add("build_mirror_view",
        _mirror_to_obj(reconstruct.build_mirror_view(store, "now")), T=T_now)    # 100 back

    # --- coverage assertions (the export MUST exercise these) ---
    deleted = reconstruct.row_state(store, 100, T_day3)
    resurrected = reconstruct.row_state(store, 100, "now")
    genuine_null = reconstruct.as_of(store, 200, "score", T_day2)
    before_view = reconstruct.build_mirror_view(store, T_before)
    must_include = {
        "deleted_entity": deleted["state"] == "deleted",
        "resurrected_entity": resurrected["resurrected"] is True,
        # genuine null: a real None value that is NOT a deletion (entity stays active).
        "genuine_null_cell": (
            genuine_null is not None
            and genuine_null["value"] is None
            and reconstruct.row_state(store, 200, T_day2)["state"] == "active"
        ),
        "as_of_before_history_empty": (
            reconstruct.as_of(store, 100, "name", T_before) is None
            and before_view.height == 0
        ),
    }

    return {
        "store": store.path.name,
        "key_column": key_column,
        "schema": schema_tags,
        "snap_points": _snap_points(store),
        "probes": probes,
        "must_include_cases": must_include,
        "events": _raw_events(store),
    }


# --------------------------------------------------------------------------- #
# The pytest.                                                                   #
# --------------------------------------------------------------------------- #
def test_parity_export_writes_ground_truth(tmp_path):
    store_path = tmp_path / "parity.flux"
    store = _build_known_store(store_path)

    ground_truth = _build_ground_truth(store)

    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE_PATH.write_text(json.dumps(ground_truth, indent=2), encoding="utf-8")

    # File written.
    assert FIXTURE_PATH.exists(), f"parity ground truth not written to {FIXTURE_PATH}"

    # Schema-required keys present (per contracts/parity.schema.json).
    on_disk = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    for key in ("store", "key_column", "schema", "snap_points", "probes"):
        assert key in on_disk, f"required key {key!r} missing from export"
    # T006 additionally requires the raw events + coverage cases.
    assert "events" in on_disk and on_disk["events"], "raw events missing/empty"
    assert "must_include_cases" in on_disk

    # The store really contains delete + resurrection + genuine null + empty-before-history.
    cases = on_disk["must_include_cases"]
    assert cases["deleted_entity"] is True, "no deletion case present"
    assert cases["resurrected_entity"] is True, "no resurrection case present"
    assert cases["genuine_null_cell"] is True, "no genuine-null-cell case present"
    assert cases["as_of_before_history_empty"] is True, "no empty-before-history case present"

    # Probe grid is non-trivial and covers every primitive kind.
    kinds = {p["kind"] for p in on_disk["probes"]}
    assert kinds == {"as_of", "timeline", "row_state", "change_count", "build_mirror_view"}, kinds
    assert len(on_disk["probes"]) >= 15

    # A deletion marker really exists in the raw events (dtype="null").
    assert any(
        e["field"] == "__deleted__" and e["dtype"] == "null" for e in on_disk["events"]
    ), "no __deleted__ marker in exported events"
