#!/usr/bin/env python3
"""Generate a SCHEMA-CHURN demo .flux store — a monitored source whose COLUMNS
change between captures (add / drop / rename), to showcase FluxState's
schema-evolution behavior (audit 2026-06: F-DROP, F-RENAME) live in the viewer.

This is the dbt/dev-source case behind GitHub issue #6: a Pharos "Table Gen"
source re-introspects on refresh, so columns churn between runs. A normal
fixed-schema fixture (scripts/demo_fixture.py) never exercises this; this one does.

The store is deterministic (no RNG) and tiny, so the timeline is easy to read by
scrubbing. Each capture is one "day"; the narrative:

  day1  base columns        : id, name, score, region
  day2  ADD    email         (absent/NULL before day2, values from day2 on)
  day3  update + new entity 5
  day4  DROP   score         (kept in history < day4; reads NULL from day4 on —
                              a field-level tombstone, NOT a stale "ghost" value)
  day5  RENAME name->full_name (recorded as drop name + add full_name)
  day6  ADD status; DROP region

Usage:
    uv run python scripts/schema_churn_demo.py [out_path]
    # default out_path: ./schema_churn.flux
    # then view it (see docs): copy into viewer/public/ and open ?store=schema_churn.flux
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone

import polars as pl

from changelog import ChangeLogStore


def _utc(day: int) -> datetime:
    return datetime(2026, 1, day, tzinfo=timezone.utc)


def generate(out_path: str = "schema_churn.flux") -> dict:
    store = ChangeLogStore(out_path)

    # day1 — base schema: id, name, score, region
    store.capture(
        pl.DataFrame(
            {
                "id": pl.Series([1, 2, 3, 4], dtype=pl.Int64),
                "name": pl.Series(["alice", "bob", "carol", "dave"], dtype=pl.Utf8),
                "score": pl.Series([10.0, 20.0, 30.0, 40.0], dtype=pl.Float64),
                "region": pl.Series(["NW", "NE", "SW", "SE"], dtype=pl.Utf8),
            }
        ),
        "id", captured_at=_utc(1),
    )

    # day2 — ADD `email`; bump two scores.
    store.capture(
        pl.DataFrame(
            {
                "id": pl.Series([1, 2, 3, 4], dtype=pl.Int64),
                "name": pl.Series(["alice", "bob", "carol", "dave"], dtype=pl.Utf8),
                "score": pl.Series([12.0, 20.0, 35.0, 40.0], dtype=pl.Float64),
                "region": pl.Series(["NW", "NE", "SW", "SE"], dtype=pl.Utf8),
                "email": pl.Series(["a@x", "b@x", "c@x", "d@x"], dtype=pl.Utf8),
            }
        ),
        "id", captured_at=_utc(2),
    )

    # day3 — update + entity 5 born.
    store.capture(
        pl.DataFrame(
            {
                "id": pl.Series([1, 2, 3, 4, 5], dtype=pl.Int64),
                "name": pl.Series(["alice", "bob", "carol", "dave", "erin"], dtype=pl.Utf8),
                "score": pl.Series([12.0, 22.0, 35.0, 40.0, 50.0], dtype=pl.Float64),
                "region": pl.Series(["NW", "NE", "SW", "SE", "NW"], dtype=pl.Utf8),
                "email": pl.Series(["a@x", "b@x", "c@x", "d@x", "e@x"], dtype=pl.Utf8),
            }
        ),
        "id", captured_at=_utc(3),
    )

    # day4 — DROP `score` (gone from the source schema). History before day4 keeps
    # it; from day4 on it reconstructs as NULL (tombstone), not the last value.
    store.capture(
        pl.DataFrame(
            {
                "id": pl.Series([1, 2, 3, 4, 5], dtype=pl.Int64),
                "name": pl.Series(["alice", "bob", "carol", "dave", "erin"], dtype=pl.Utf8),
                "region": pl.Series(["NW", "NE", "SW", "SE", "NW"], dtype=pl.Utf8),
                "email": pl.Series(["a@x", "b@x", "c@x", "d@x", "e@x"], dtype=pl.Utf8),
            }
        ),
        "id", captured_at=_utc(4),
    )

    # day5 — RENAME `name` -> `full_name` (values carried over). Recorded as
    # drop(name) + add(full_name); `name` reads NULL from day5, `full_name` set.
    store.capture(
        pl.DataFrame(
            {
                "id": pl.Series([1, 2, 3, 4, 5], dtype=pl.Int64),
                "full_name": pl.Series(
                    ["Alice A", "Bob B", "Carol C", "Dave D", "Erin E"], dtype=pl.Utf8
                ),
                "region": pl.Series(["NW", "NE", "SW", "SE", "NW"], dtype=pl.Utf8),
                "email": pl.Series(["a@x", "b@x", "c@x", "d@x", "e@x"], dtype=pl.Utf8),
            }
        ),
        "id", captured_at=_utc(5),
    )

    # day6 — ADD `status`; DROP `region`.
    store.capture(
        pl.DataFrame(
            {
                "id": pl.Series([1, 2, 3, 4, 5], dtype=pl.Int64),
                "full_name": pl.Series(
                    ["Alice A", "Bob B", "Carol C", "Dave D", "Erin E"], dtype=pl.Utf8
                ),
                "email": pl.Series(["a@x", "b@x", "c@x", "d@x", "e@x"], dtype=pl.Utf8),
                "status": pl.Series(
                    ["active", "active", "churned", "active", "trial"], dtype=pl.Utf8
                ),
            }
        ),
        "id", captured_at=_utc(6),
    )

    manifest = store.read_manifest()
    summary = {
        "store": out_path,
        "captures": len(manifest["events"]),
        "union_columns": list(manifest["schema"].keys()),
    }
    return summary


def main(argv: list[str]) -> int:
    out = argv[1] if len(argv) > 1 else "schema_churn.flux"
    s = generate(out)
    print(f"schema-churn demo store written: {s['store']}")
    print(f"  captures (days): {s['captures']}")
    print(f"  union schema   : {s['union_columns']}")
    print("  timeline       : +email(d2)  -score(d4)  name->full_name(d5)  +status -region(d6)")
    print(f"\nView it:  cp -r {out} viewer/public/  &&  (cd viewer && npm run dev)")
    print(f"          then open  http://localhost:5173/?store={out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
