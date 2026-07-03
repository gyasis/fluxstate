"""Databricks sidecar for FluxState — the first platform integration (feature 003).

Provides `DeltaBackend` (a `TableBackend` bound to Delta `flux_events` + optional
`flux_mirror`) and a scheduled-Job capture template (`job_template.py`). Requires
the optional extra: `pip install "fluxstate[databricks]"`.

Heavy deps (`deltalake`, `databricks-sdk`, runtime `pyspark`) are confined to this
extra — the core (`fluxstate`, `changelog`, `storage`) never imports them (G1/G8,
SB-7). `deltalake` is lazy-imported only when `DeltaBackend` is constructed/used.

``DeltaBackend`` is deliberately unvalidated against a real Databricks/Spark
runtime in this wave — ``deltalake`` is not installed in this dev environment.
It is built by extending ``storage.table.TableBackend`` (already Delta-aware for
the ``StorageBackend`` protocol methods) with the RAW capture-facing helpers
(``list_events`` / ``_append_events``) ``TableBackend`` intentionally leaves
``format="parquet"``-only (see ``storage/table.py`` module docstring), so that
``FluxState(snapshot, key_column=..., store=DeltaBackend(...)).update_mirror_table()``
is a genuine drop-in — the quickstart-documented usage (T021 FR-006/FR-008).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

import polars as pl

from changelog import to_utc
from storage.table import TableBackend

__all__ = ["DeltaBackend"]


class DeltaBackend(TableBackend):
    """``flux_events`` (+ optional ``flux_mirror``) as Delta tables (Databricks sidecar).

    A thin, opinionated subclass of :class:`storage.table.TableBackend` pinned to
    ``format="delta"``. It inherits the full ``StorageBackend`` protocol
    (``read_meta`` / ``write_meta`` / ``read_current_state`` / ``read_events`` /
    ``append_events`` / ``refresh_mirror``) from ``TableBackend`` unchanged, and
    additionally overrides the RAW manifest-dict helpers (``list_events`` /
    ``_append_events``) that ``changelog.ChangeLogStore`` delegates to directly —
    so it is a drop-in for ``FluxState(store=DeltaBackend(...))`` (capture,
    ``.travel()``, ``.get_timeline()``, ``.build_mirror_view()``), not just the
    generic protocol.

    The trick: a Delta table's committed data files ARE plain Parquet on disk
    (only the transaction log, ``_delta_log/``, is Delta-specific) — a Delta
    ``write_deltalake(..., mode="append")`` call adds one or more new Parquet
    files, and those file paths are exactly what ``list_events`` needs to hand
    back for the same file-skip-pruning + ``pl.read_parquet`` reconstruction
    every other backend already uses. This override records the newly-added
    files (via a before/after diff of ``DeltaTable.files()``) in the
    ``_flux_meta.json`` companion (the same descriptor ``TableBackend`` already
    keeps) alongside each event unit's ``ts_min``/``ts_max`` — no reliance on
    "table properties" APIs, which are version-fragile.
    """

    def __init__(
        self,
        events: str | Path,
        mirror: Optional[str | Path] = None,
        mirror_refresh: str = "on_demand",
    ):
        # `format="delta"` is fixed — this sidecar has exactly one physical
        # format. TableBackend.__init__ calls `self._require_deltalake()` for
        # format="delta", which resolves (via MRO) to the override below, so
        # construction fails fast with a `fluxstate[databricks]`-specific
        # message when `deltalake` isn't installed (T021 FR-008).
        super().__init__(events=events, mirror=mirror, mirror_refresh=mirror_refresh, format="delta")

    # ------------------------------------------------------------------ #
    # Opt-in extra guard — databricks-specific message (T021)             #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _require_deltalake():
        try:
            import deltalake  # noqa: F401
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise ImportError(
                'DeltaBackend requires the "deltalake" package. '
                'Install it with: pip install "fluxstate[databricks]"'
            ) from exc
        return deltalake

    # ------------------------------------------------------------------ #
    # Delta-table file listing (best-effort — unverifiable without        #
    # `deltalake` installed; structurally mirrors TableBackend's own      #
    # `_append_events_data_delta` / `_read_events_delta` usage of the     #
    # `deltalake.DeltaTable` API).                                        #
    # ------------------------------------------------------------------ #
    def _delta_table(self):
        self._require_deltalake()
        from deltalake import DeltaTable

        return DeltaTable(str(self.events_path))

    def _current_files(self) -> set[str]:
        """Relative paths (table-root-relative) of every Parquet file backing the table."""
        if not (self.events_path / "_delta_log").exists():
            return set()
        return set(self._delta_table().files())

    # ------------------------------------------------------------------ #
    # list_events (RAW; capture()/reconstruct-facing) — Delta override    #
    # ------------------------------------------------------------------ #
    def list_events(self, as_of: Optional[datetime] = None, window=None) -> list[Path]:
        """Return the Parquet part files (Delta data files) in scope, pruned by ts range.

        Same file-skip-pruning semantics as ``LocalFolderStore``/``TableBackend``
        (parquet format), sourced from the ``files`` list this backend's
        ``_append_events`` records per committed unit in ``_flux_meta.json``.
        """
        manifest = self.read_manifest()
        as_of = to_utc(as_of) if as_of is not None else None
        lo = hi = None
        if window is not None:
            lo, hi = window
            lo = to_utc(lo) if lo is not None else None
            hi = to_utc(hi) if hi is not None else None

        kept: list[Path] = []
        for entry in manifest.get("events", []):
            ts_min = to_utc(datetime.fromisoformat(entry["ts_min"]))
            ts_max = to_utc(datetime.fromisoformat(entry["ts_max"]))
            if as_of is not None and ts_min > as_of:
                continue
            if lo is not None and ts_max < lo:
                continue
            if hi is not None and ts_min > hi:
                continue
            for rel in entry.get("files", []):
                kept.append(self.events_path / rel)
        return kept

    # ------------------------------------------------------------------ #
    # _append_events (RAW; capture()-facing) — Delta override             #
    # ------------------------------------------------------------------ #
    def _append_events(
        self,
        events: pl.DataFrame,
        snapshot_id: str,
        key_column: str,
        schema: dict,
        stamp: Optional[datetime] = None,
    ) -> dict:
        """Append one immutable Delta commit + record its file set in the descriptor.

        Mirrors ``TableBackend._append_events`` (parquet path)'s combined
        schema-union + event-catalog commit that ``ChangeLogStore.capture``
        relies on for a single atomic write per capture — but the data lands
        in the Delta table (``write_deltalake(mode="append")``) instead of a
        bare Parquet part file directly under ``events_path``.
        """
        if events.is_empty():
            raise ValueError("_append_events called with no events")
        self._require_deltalake()
        from deltalake import write_deltalake

        self.events_path.mkdir(parents=True, exist_ok=True)
        before = self._current_files()
        write_deltalake(str(self.events_path), events.to_arrow(), mode="append")
        new_files = sorted(self._current_files() - before)

        ts_min = events["timestamp"].min()
        ts_max = events["timestamp"].max()
        entry = {
            "file": "<delta-table>",
            "files": new_files,
            "snapshot_id": snapshot_id,
            "ts_min": to_utc(ts_min).isoformat(),
            "ts_max": to_utc(ts_max).isoformat(),
            "row_count": events.height,
        }

        manifest = self.read_manifest()
        manifest["key_column"] = key_column
        # Append-only schema UNION (not overwrite) — matches every other backend.
        merged_schema = dict(manifest.get("schema") or {})
        merged_schema.update(schema)
        manifest["schema"] = merged_schema
        manifest.setdefault("events", []).append(entry)
        self.write_manifest(manifest)

        self._maybe_cadence_refresh(manifest)
        return entry
