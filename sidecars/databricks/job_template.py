"""Databricks scheduled-Job capture TEMPLATE (feature 003, US4 / T022).

A runnable PATTERN for a daily Databricks Job task (Python notebook / wheel
task) that captures a view's cell-level deltas into a Delta ``flux_events``
table (+ optional materialized ``flux_mirror``) via :class:`DeltaBackend`,
entirely on Databricks — no Volume/FUSE sync dance (see ``docs/DATABRICKS.md``
Pattern A vs this sidecar).

This module is import-safe without ``pyspark``/``deltalake`` installed (both
are lazy — ``pyspark`` is only referenced inside ``capture_view``'s type hints
as a string and ``deltalake`` only inside :class:`DeltaBackend`). The
``if __name__ == "__main__"`` block is a documentation-only example; it is
guarded so simply importing this module (e.g. from a test) never touches Spark
or a real cluster.
"""

from __future__ import annotations

from typing import Any, Optional

import polars as pl

from fluxstate import FluxState
from sidecars.databricks import DeltaBackend

__all__ = ["read_view_as_polars", "capture_view"]


def read_view_as_polars(spark: Any, view: str) -> pl.DataFrame:
    """Read a Spark view/table's CURRENT snapshot into a Polars DataFrame.

    Driver-side by default (``toArrow()`` is the fast path — zero-copy Arrow
    handoff; falls back to ``toPandas()`` for older Spark/connector versions
    that don't expose ``toArrow()``). This is the "read today's snapshot" step
    of the daily capture — the diff itself happens inside ``FluxState.capture``
    (a table-level keyed join, not a per-row UDF; docs/DATABRICKS.md Gotchas).
    """
    table = spark.table(view)
    try:
        return pl.from_arrow(table.toArrow())
    except Exception:
        return pl.from_pandas(table.toPandas())


def read_view_as_polars_applyinpandas(spark: Any, view: str, num_partitions: int = 200) -> pl.DataFrame:
    """Distributed variant of :func:`read_view_as_polars` for VERY large views.

    Optional path (R8): when a view is too large to comfortably collect
    driver-side via ``toArrow()``/``toPandas()``, repartition it and pull each
    partition's Arrow batch through ``mapInArrow`` (a thin, allocation-light
    cousin of ``applyInPandas``), then concatenate driver-side. Still a single
    Polars snapshot at the end — the diff/capture algorithm is unchanged and
    stays driver-side (it's a keyed join over the WHOLE snapshot, not
    partitionable without re-deriving cross-partition state).

    Prefer :func:`read_view_as_polars` unless the view genuinely does not fit
    in driver memory as Arrow — this path trades simplicity for scale.
    """
    import pyarrow as pa

    def _to_arrow_batches(iterator):
        for batch in iterator:
            yield batch

    df = spark.table(view).repartition(num_partitions)
    batches: list[pa.RecordBatch] = list(
        df.mapInArrow(_to_arrow_batches, df.schema).toLocalIterator()
    )
    if not batches:
        return pl.DataFrame()
    return pl.from_arrow(pa.Table.from_batches(batches))


def capture_view(
    spark: Any,
    view: str,
    key_column: str,
    events_table: str,
    mirror_table: Optional[str] = None,
    mirror_refresh: str = "on_demand",
    distributed: bool = False,
) -> dict:
    """Capture one day's snapshot of ``view`` into Delta ``flux_events`` (idempotent).

    The daily Job task body: read the view -> ``FluxState(..., store=DeltaBackend(...))``
    -> ``update_mirror_table()`` (appends changed cells; a no-op on an unchanged
    day, per SC-005) -> optionally ``refresh_mirror()`` when ``mirror_table`` is
    set and the policy is on-demand (``"each_capture"``/``"cadence:N"`` refresh
    automatically inside ``update_mirror_table()``; ``"on_demand"`` requires this
    explicit call).

    Parameters mirror the quickstart (``specs/003-pluggable-storage-backends/
    quickstart.md`` §4): ``events_table``/``mirror_table`` are fully-qualified
    Unity Catalog names, e.g. ``"catalog.schema.flux_events"``.
    """
    snapshot = (
        read_view_as_polars_applyinpandas(spark, view)
        if distributed
        else read_view_as_polars(spark, view)
    )

    fs = FluxState(
        snapshot,
        key_column=key_column,
        store=DeltaBackend(
            events=events_table,
            mirror=mirror_table,
            mirror_refresh=mirror_refresh,
        ),
    )
    result = fs.update_mirror_table()

    if mirror_table is not None and mirror_refresh == "on_demand":
        fs.refresh_mirror()

    return result


if __name__ == "__main__":  # pragma: no cover - documentation example only
    # Runs ONLY inside an actual Databricks notebook/Job (needs a live `spark`
    # session + a real view). Never executed by import or by the test suite.
    capture_view(
        spark,  # noqa: F821 - injected by the Databricks notebook runtime
        view="catalog.schema.my_view",
        key_column="id",
        events_table="catalog.schema.flux_events",
        mirror_table="catalog.schema.flux_mirror",
        mirror_refresh="on_demand",
    )
