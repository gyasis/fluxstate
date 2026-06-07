# File: scripts/demo_fixture.py
"""Generate the seeded 1000x20 demo+stress fixture ``<name>.flux/`` store.

VALUES come from **Faker** + a seeded ``numpy.random`` generator — the flux
library never invents values. The **passage of time** comes from feeding
~``steps`` evolving snapshots through ``ChangeLogStore.capture()``; flux records
the change-log, it does not synthesize data (research R5).

The 20 columns span 6 dtypes (int64, float64, utf8, bool, datetime[us,UTC], and
a low-cardinality categorical utf8) partitioned into deliberate behavior classes:

  - **Immutable** (set at birth, NEVER change): ``id`` (int64 key), ``birth_date``
    (datetime), ``cohort`` (categorical), ``mrn`` (utf8 Faker), plus ``ssn``,
    ``region``, ``enrolled`` — pinned at the entity's first appearance.
  - **Mutable** (re-rolled over time): ``risk`` (float64), ``score`` (int64),
    ``last_seen`` (datetime), ``active`` (bool), ``weight`` (float64),
    ``visits`` (int64), ``flagged`` (bool), ``note`` (utf8 Faker).
  - **Categorical / incremental** (low-card pools, some monotone): ``status``
    (A→B→C forward transition), ``tier``, ``stage`` (forward), ``plan``.

Per step only **~1-5% of cells** mutate (a small random subset of rows × the
mutable/categorical columns); immutable columns are never touched after birth.
The history is deterministically scripted to contain record **births** (unborn→
active partway through), **deletions** (a single ``__deleted__`` marker), and
**resurrections** (a deleted id returns under the SAME id), plus a handful of
**volatile hotspot** rows that change far more often than the rest.

Everything is seeded — ``Faker.seed_instance(seed)`` + ``numpy.random.default_rng
(seed)`` + a deterministic ``captured_at`` date sequence — so the same ``seed``
yields a **byte-identical** store every run.

See ``specs/002-fluxstate-temporal-viewer/data-model.md`` (Demo Fixture),
``research.md`` R5, ``spec.md`` US7 / FR-017 / FR-018 / SC-004.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import polars as pl
from faker import Faker

# Make the flat top-level modules importable when run as a script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from changelog import ChangeLogStore  # noqa: E402

KEY_COLUMN = "id"

# Low-cardinality categorical pools.
_COHORTS = ["alpha", "beta", "gamma", "delta"]
_REGIONS = ["north", "south", "east", "west"]
_STATUS_CHAIN = ["A", "B", "C"]  # forward-only transition
_STAGE_CHAIN = ["intake", "review", "active", "closed"]  # forward-only
_TIERS = ["bronze", "silver", "gold", "platinum"]
_PLANS = ["basic", "standard", "premium"]

# Column classification (drives which cells may mutate).
_IMMUTABLE = ["birth_date", "cohort", "mrn", "ssn", "region", "enrolled"]
_MUTABLE = ["risk", "score", "last_seen", "active", "weight", "visits", "flagged", "note", "balance"]
_CATEGORICAL = ["status", "tier", "stage", "plan"]
# id + 6 immutable + 8 mutable + 4 categorical/incremental + birth_date counted = 20
_ALL_COLS = [KEY_COLUMN] + _IMMUTABLE + _MUTABLE + _CATEGORICAL
assert len(_ALL_COLS) == 20, f"expected 20 columns, got {len(_ALL_COLS)}: {_ALL_COLS}"

_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _us(days: int) -> datetime:
    """A deterministic UTC datetime ``days`` after the fixed epoch (microsecond-clean)."""
    return _EPOCH + timedelta(days=days)


# --------------------------------------------------------------------------- #
# Per-entity value synthesis (Faker + seeded numpy.random)                     #
# --------------------------------------------------------------------------- #
def _birth_immutables(eid: int, fake: Faker, rng) -> dict:
    """Immutable cell values pinned at an entity's birth (never re-rolled)."""
    return {
        "birth_date": _us(int(rng.integers(0, 365))),
        "cohort": _COHORTS[int(rng.integers(0, len(_COHORTS)))],
        "mrn": fake.bothify(text="MRN-#######"),
        "ssn": fake.bothify(text="###-##-####"),
        "region": _REGIONS[int(rng.integers(0, len(_REGIONS)))],
        "enrolled": bool(rng.integers(0, 2)),
    }


def _initial_mutables(rng) -> dict:
    """Starting mutable + categorical values for a freshly-born entity."""
    return {
        "risk": round(float(rng.random()), 4),
        "score": int(rng.integers(0, 1000)),
        "last_seen": _us(int(rng.integers(0, 30))),
        "active": True,
        "weight": round(float(rng.normal(80.0, 15.0)), 2),
        "visits": int(rng.integers(0, 5)),
        "flagged": bool(rng.integers(0, 2)),
        "balance": round(float(rng.normal(500.0, 200.0)), 2),
        "note": "",  # filled lazily on mutation so births stay legible
        "status": "A",
        "tier": _TIERS[int(rng.integers(0, 2))],  # start low; may climb
        "stage": "intake",
        "plan": _PLANS[int(rng.integers(0, len(_PLANS)))],
    }


def _mutate_cell(row: dict, col: str, fake: Faker, rng, day: int) -> None:
    """Re-roll one mutable / categorical cell in-place (immutable cols never reach here)."""
    if col == "risk":
        row[col] = round(float(rng.random()), 4)
    elif col == "score":
        row[col] = int(rng.integers(0, 1000))
    elif col == "last_seen":
        row[col] = _us(day)
    elif col == "active":
        row[col] = bool(rng.integers(0, 2))
    elif col == "weight":
        row[col] = round(row[col] + float(rng.normal(0.0, 2.0)), 2)
    elif col == "visits":
        row[col] = int(row[col]) + 1  # monotone counter
    elif col == "flagged":
        row[col] = bool(rng.integers(0, 2))
    elif col == "balance":
        row[col] = round(row[col] + float(rng.normal(0.0, 50.0)), 2)
    elif col == "note":
        row[col] = fake.sentence(nb_words=4)
    elif col == "status":
        i = _STATUS_CHAIN.index(row[col])
        if i < len(_STATUS_CHAIN) - 1:  # forward-only A→B→C
            row[col] = _STATUS_CHAIN[i + 1]
    elif col == "stage":
        i = _STAGE_CHAIN.index(row[col])
        if i < len(_STAGE_CHAIN) - 1:  # forward-only
            row[col] = _STAGE_CHAIN[i + 1]
    elif col == "tier":
        i = _TIERS.index(row[col])
        row[col] = _TIERS[min(i + 1, len(_TIERS) - 1)]  # climbs
    elif col == "plan":
        row[col] = _PLANS[int(rng.integers(0, len(_PLANS)))]


# --------------------------------------------------------------------------- #
# Frame assembly                                                               #
# --------------------------------------------------------------------------- #
def _frame(live_rows: dict[int, dict]) -> pl.DataFrame:
    """Build a typed snapshot frame from the live entity rows (stable id order)."""
    ids = sorted(live_rows)
    data = {KEY_COLUMN: pl.Series(KEY_COLUMN, ids, dtype=pl.Int64)}
    cols = {
        "birth_date": pl.Datetime("us", "UTC"),
        "cohort": pl.Utf8,
        "mrn": pl.Utf8,
        "ssn": pl.Utf8,
        "region": pl.Utf8,
        "enrolled": pl.Boolean,
        "risk": pl.Float64,
        "score": pl.Int64,
        "last_seen": pl.Datetime("us", "UTC"),
        "active": pl.Boolean,
        "weight": pl.Float64,
        "visits": pl.Int64,
        "flagged": pl.Boolean,
        "balance": pl.Float64,
        "note": pl.Utf8,
        "status": pl.Utf8,
        "tier": pl.Utf8,
        "stage": pl.Utf8,
        "plan": pl.Utf8,
    }
    for col, dt in cols.items():
        data[col] = pl.Series(col, [live_rows[i][col] for i in ids], dtype=dt)
    return pl.DataFrame(data).select(_ALL_COLS)


# --------------------------------------------------------------------------- #
# Generator                                                                    #
# --------------------------------------------------------------------------- #
# Immutable columns eligible for a seeded VIOLATION flip (one dtype each:
# categorical utf8, plain utf8, datetime) — a deliberate data-integrity breach
# the anomaly-watcher (a later task) is meant to catch.
_VIOLATION_FIELDS = ["cohort", "mrn", "birth_date"]


def _violate_cell(row: dict, field: str, rng) -> None:
    """Flip ONE immutable cell to a new, different value in-place (a VIOLATION).

    Immutable columns must never change; this deliberately changes one so the
    anomaly-watcher has something to catch. The new value is drawn from the
    seeded ``rng`` and guaranteed different from the current value.
    """
    if field == "cohort":
        cur = row["cohort"]
        alt = [c for c in _COHORTS if c != cur]
        row["cohort"] = alt[int(rng.integers(0, len(alt)))]
    elif field == "mrn":
        # New MRN that differs from the current one (deterministic via rng).
        new = f"MRN-{int(rng.integers(0, 10_000_000)):07d}"
        while new == row["mrn"]:
            new = f"MRN-{int(rng.integers(0, 10_000_000)):07d}"
        row["mrn"] = new
    elif field == "birth_date":
        cur = row["birth_date"]
        new = _us(int(rng.integers(0, 365)))
        while new == cur:
            new = _us(int(rng.integers(0, 365)))
        row["birth_date"] = new


def generate(store_path, rows=1000, cols=20, steps=40, seed=42, violations=0) -> dict:
    """Generate the seeded demo fixture store at ``store_path``.

    Synthesizes ``rows`` entities (Faker + seeded numpy) and evolves them across
    ``steps`` dated ``ChangeLogStore.capture()`` calls with sparse per-step change
    plus scripted births / deletions / resurrections. ``cols`` is fixed at 20 (the
    fixture's deliberate dtype mix); a different value raises. Returns a summary
    dict. Same ``seed`` ⇒ byte-identical store.

    ``violations`` (default 0): seed N deliberate IMMUTABLE-COLUMN VIOLATIONS for
    the anomaly-watcher to catch. With ``violations > 0``, N deterministically
    chosen entities each have ONE immutable cell (``cohort`` / ``mrn`` /
    ``birth_date``) flipped to a new value ONCE at a seeded mid-history step.
    Immutable columns should never change, so this is a data-integrity breach.
    The violation RNG is independent of the main value/lifecycle RNG, so
    ``violations=0`` is byte-identical to the original behavior, and a given
    ``(seed, violations)`` pair is reproducible byte-for-byte.
    """
    if cols != 20:
        raise ValueError(f"demo fixture is fixed at 20 columns (got cols={cols})")
    if violations < 0:
        raise ValueError(f"violations must be >= 0 (got {violations})")

    fake = Faker()
    fake.seed_instance(seed)  # seed THIS instance's RNG for reproducible Faker values
    rng = np.random.default_rng(seed)
    # Independent RNG for the violation schedule + new values, so the presence of
    # violations never perturbs the main value/lifecycle stream (violations=0 ⇒
    # byte-identical to the original store).
    vrng = np.random.default_rng((seed, 0x7E5701))  # 0x7E5701 ~ "TEST01" salt

    store = ChangeLogStore(store_path)

    # Reserve a chunk of ids to be BORN later so unborn→active happens partway.
    n_born_later = max(20, rows // 20)  # ~5% appear after the initial snapshot
    initial_count = rows - n_born_later

    # Build every entity's value record up front (Faker/numpy do all value work).
    entities: dict[int, dict] = {}
    for eid in range(1, rows + 1):
        row = {KEY_COLUMN: eid}
        row.update(_birth_immutables(eid, fake, rng))
        row.update(_initial_mutables(rng))
        entities[eid] = row

    initial_ids = list(range(1, initial_count + 1))
    born_later_ids = list(range(initial_count + 1, rows + 1))

    # Volatile hotspots: a handful of initial rows that mutate far more often.
    hotspot_ids = sorted(
        int(x) for x in rng.choice(initial_ids, size=min(8, len(initial_ids)), replace=False)
    )

    # Scripted lifecycle ids (deterministic, drawn from the seeded RNG). Every
    # deleted id is later resurrected under the SAME id so the FINAL live table
    # is the full ``rows`` (the ``__deleted__`` markers stay in history regardless,
    # satisfying "≥1 deletion marker"). ``resurrect_ids`` come back mid-history;
    # ``late_return_ids`` are deleted early and only return near the very end (a
    # longer deleted window) — both are resurrections in the change-log.
    pool = [i for i in initial_ids if i not in hotspot_ids]
    rng.shuffle(pool)
    resurrect_ids = sorted(pool[:3])
    late_return_ids = sorted(pool[3:6])
    deleted_ids = resurrect_ids + late_return_ids

    live: dict[int, dict] = {i: dict(entities[i]) for i in initial_ids}

    # --- VIOLATIONS: seeded immutable-column flips (T035) ------------------- #
    # Pick `violations` initial entities that stay live the whole history (not in
    # the lifecycle/hotspot pools), assign each ONE immutable field + a seeded
    # mid-history step, and flip that cell ONCE at that step. Recorded so the
    # anomaly-watcher target is verifiable. Drawn from the independent `vrng`.
    violation_plan: dict[int, tuple[str, int]] = {}  # entity_id -> (field, step)
    violation_ids: list[int] = []
    if violations > 0:
        stable_pool = sorted(
            i for i in initial_ids if i not in hotspot_ids and i not in deleted_ids
        )
        if violations > len(stable_pool):
            raise ValueError(
                f"violations={violations} exceeds available stable entities ({len(stable_pool)})"
            )
        chosen = sorted(
            int(x) for x in vrng.choice(stable_pool, size=violations, replace=False)
        )
        # Mid-history window so the flip is clearly inside [1, steps-1] history.
        lo_step = max(1, steps // 4)
        hi_step = max(lo_step + 1, (3 * steps) // 4)
        for eid in chosen:
            field = _VIOLATION_FIELDS[int(vrng.integers(0, len(_VIOLATION_FIELDS)))]
            vstep = int(vrng.integers(lo_step, hi_step))
            violation_plan[eid] = (field, vstep)
            violation_ids.append(eid)

    summary = {"births": 0, "deletes": 0, "resurrections": 0}
    per_step_change_pct: list[float] = []

    # --- step 0: the initial snapshot (all initial ids born) -------------- #
    res = store.capture(_frame(live), key_column=KEY_COLUMN, captured_at=_us(0))
    summary["births"] += len(initial_ids)

    # Schedule lifecycle events across the remaining steps (deterministic). All
    # births land in the first half; all deletions/resurrections complete before
    # the final step so the as-of-now table is full (1000) yet the history shows
    # births, __deleted__ markers, and resurrections.
    half = max(2, steps // 2)
    births_at = {}
    for k, eid in enumerate(born_later_ids):
        births_at.setdefault(1 + (k % (half - 1)), []).append(eid)
    delete_at = {}
    for k, eid in enumerate(resurrect_ids):
        delete_at[eid] = 1 + (k % (half - 1))  # deleted early
    for k, eid in enumerate(late_return_ids):
        delete_at[eid] = 1 + (k % (half - 1))  # also deleted early...
    # ...but resurrected at different times: mid-history vs near-the-end.
    resurrect_at = {eid: delete_at[eid] + 2 for eid in resurrect_ids}
    for k, eid in enumerate(late_return_ids):
        resurrect_at[eid] = steps - 2 - k  # return in the last few steps

    # --- steps 1..steps-1: sparse evolution + scripted lifecycle ---------- #
    for step in range(1, steps):
        day = step  # one day per step (deterministic captured_at)
        changed_cells = 0

        # Births scheduled for this step (unborn → active).
        for eid in births_at.get(step, []):
            live[eid] = dict(entities[eid])
            summary["births"] += 1

        # Deletions scheduled for this step (active → deleted = vanish from frame).
        for eid, dstep in delete_at.items():
            if dstep == step and eid in live:
                del live[eid]
                summary["deletes"] += 1

        # Resurrections scheduled for this step (deleted → active, SAME id).
        for eid, rstep in resurrect_at.items():
            if rstep == step and eid not in live:
                live[eid] = dict(entities[eid])
                # bump a couple of mutable cells so the return is visible
                _mutate_cell(live[eid], "status", fake, rng, day)
                _mutate_cell(live[eid], "last_seen", fake, rng, day)
                summary["resurrections"] += 1

        # VIOLATIONS scheduled for this step: flip ONE immutable cell once. Uses
        # the independent `vrng`, so it never perturbs the main value stream.
        for eid, (field, vstep) in violation_plan.items():
            if vstep == step and eid in live:
                _violate_cell(live[eid], field, vrng)

        live_ids = list(live)
        n_live = len(live_ids)

        # Sparse mutation: target ~1-5% of ALL cells per step. We pick a row
        # subset and change 1-2 mutable cells each; rows_to_touch is derived from
        # the cell-fraction target (cells = rows_to_touch * ~1.5 changed cells).
        frac = 0.015 + float(rng.random()) * 0.03  # 1.5-4.5% of cells targeted
        avg_cells_per_row = 1.5
        rows_to_touch = max(1, int(round(frac * n_live * 20 / avg_cells_per_row)))
        rows_to_touch = min(rows_to_touch, n_live)
        touched = set(int(x) for x in rng.choice(live_ids, size=rows_to_touch, replace=False))

        # Hotspots: always mutate (if live) with several cells changed.
        for eid in hotspot_ids:
            if eid in live:
                touched.add(eid)

        mutable_pool = _MUTABLE + _CATEGORICAL
        for eid in touched:
            row = live[eid]
            if eid in hotspot_ids:
                n_cells = int(rng.integers(3, 6))  # volatile: 3-5 cells
            else:
                n_cells = int(rng.integers(1, 3))  # 1-2 cells
            picks = rng.choice(len(mutable_pool), size=min(n_cells, len(mutable_pool)), replace=False)
            for ci in picks:
                col = mutable_pool[int(ci)]
                _mutate_cell(row, col, fake, rng, day)
            changed_cells += min(n_cells, len(mutable_pool))

        if n_live:
            per_step_change_pct.append(100.0 * changed_cells / (n_live * 20))
        store.capture(_frame(live), key_column=KEY_COLUMN, captured_at=_us(day))

    manifest = store.read_manifest()
    events = manifest.get("events", [])
    # Verifiable violation list: (entity_id, field, step, timestamp) in id order.
    violation_records = [
        {
            "entity_id": eid,
            "field": violation_plan[eid][0],
            "step": violation_plan[eid][1],
            "timestamp": _us(violation_plan[eid][1]).isoformat(),
        }
        for eid in sorted(violation_ids)
    ]
    summary_out = {
        "rows": rows,
        "cols": 20,
        "steps": steps,
        "seed": seed,
        "events_files": len(events),
        "total_events": sum(e["row_count"] for e in events),
        "births": summary["births"],
        "deletes": summary["deletes"],
        "resurrections": summary["resurrections"],
        "mean_change_pct": round(
            sum(per_step_change_pct) / len(per_step_change_pct), 3
        ) if per_step_change_pct else 0.0,
        "violations": len(violation_ids),
        "violation_ids": sorted(violation_ids),
        "violation_records": violation_records,
    }
    return summary_out


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="demo_fixture",
        description="Generate the seeded FluxState demo+stress fixture store.",
    )
    parser.add_argument("store", metavar="<store.flux>", help="path to the .flux store")
    parser.add_argument("--rows", type=int, default=1000, help="number of rows")
    parser.add_argument("--cols", type=int, default=20, help="number of columns")
    parser.add_argument("--steps", type=int, default=40, help="number of capture steps")
    parser.add_argument("--seed", type=int, default=42, help="random seed")
    parser.add_argument(
        "--violations",
        type=int,
        default=0,
        help="seed N deliberate immutable-column violations (default 0)",
    )
    args = parser.parse_args(argv)
    summary = generate(
        args.store,
        rows=args.rows,
        cols=args.cols,
        steps=args.steps,
        seed=args.seed,
        violations=args.violations,
    )
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
