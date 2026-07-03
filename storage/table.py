# File: storage/table.py
"""``TableBackend`` — a first-class tabular ``StorageBackend`` (feature 003, Wave 5).

Persists ``flux_events`` (the change-log, always present) as a table, plus an
OPTIONAL materialized ``flux_mirror`` (the reconstructed wide state), per
``specs/003-pluggable-storage-backends/contracts/tables.md`` (FE-1..4, FM-1..2,
M-1..3).

Three physical formats, selected via ``format=``:

- ``"parquet"`` (default, zero new dependency) — ``flux_events`` is a directory
  of immutable Parquet parts (one per capture, never rewritten — FE-1/FE-2),
  directly glob-/table-readable by any engine (FE-3/FE-4). The store
  descriptor (``Meta``) lives in a ``_flux_meta.json`` companion written
  atomically (temp -> fsync -> rename), mirroring
  ``storage.local_folder.LocalFolderStore`` exactly so it is a drop-in
  ``StorageBackend`` for ``changelog.ChangeLogStore`` (including the RAW
  capture-facing helpers ``read_manifest``/``write_manifest``/``list_events``/
  ``_append_events`` that ``ChangeLogStore.capture`` delegates to directly).
- ``"delta"`` / ``"iceberg"`` (opt-in, ``[table]`` extra) — ``flux_events``
  lands in a real Delta/Iceberg table via ``deltalake``/``pyiceberg``,
  lazy-imported so the core install never pulls them in (G1/G8). The store
  descriptor stays in the same ``_flux_meta.json`` JSON sidecar. The clean
  ``StorageBackend`` protocol methods are fully wired for both. Engine capture
  (``ChangeLogStore.capture``/``FluxState.update_mirror_table``): **Parquet and
  Iceberg** are supported directly here (an Iceberg table's data files are plain
  Parquet, so ``list_events``/``_append_events`` diff the table's data-file set
  before/after ``append`` — validated by a parity test vs local). **Delta**
  engine-capture is provided by the Databricks sidecar
  (``sidecars.databricks.DeltaBackend``), which overrides the same helpers; or
  call ``append_events``/``read_events`` directly for Delta.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import polars as pl

from changelog import (
    DELETED_FIELD,
    SCHEMA_VERSION,
    change_event_schema,
    decode_value,
    dtype_tag,
    tag_to_dtype,
    to_utc,
)
from storage.base import Capabilities, EventRef, Meta, MirrorPolicy, Predicate

__all__ = ["TableBackend"]

_META_NAME = "_flux_meta.json"
_MIRROR_FILE = "mirror.parquet"
_SUPPORTED_FORMATS = ("parquet", "delta", "iceberg")


class TableBackend:
    """``flux_events`` (+ optional ``flux_mirror``) as a table (contracts/tables.md).

    Implements the full :class:`storage.base.StorageBackend` protocol PLUS the
    raw manifest-dict helpers (``read_manifest``/``write_manifest``/
    ``list_events``/``_append_events``) that ``changelog.ChangeLogStore``
    delegates to directly — see the module docstring for the format-by-format
    support matrix.
    """

    def __init__(
        self,
        events: str | Path,
        mirror: Optional[str | Path] = None,
        mirror_refresh: str = "on_demand",
        format: str = "parquet",
    ):
        fmt = (format or "parquet").strip().lower()
        if fmt not in _SUPPORTED_FORMATS:
            raise ValueError(
                f"TableBackend: unknown format {format!r}; expected one of {_SUPPORTED_FORMATS}"
            )
        self.format = fmt
        self.events_path = Path(events)
        self.meta_path = self.events_path / _META_NAME
        self.mirror_path = Path(mirror) if mirror else None
        self.mirror_refresh = mirror_refresh or "on_demand"

        # Fail fast on a missing opt-in extra (T014/T017): construction time, not
        # first-capture time, so a misconfigured backend is caught immediately.
        if self.format == "delta":
            self._require_deltalake()
        elif self.format == "iceberg":
            self._require_pyiceberg()

        self.capabilities = Capabilities(
            is_table=True,
            supports_atomic_meta=True,
            # Real predicate pushdown (lazy `pl.scan_parquet` + row-group pruning)
            # only happens on the Parquet path; Delta/Iceberg read-then-filter.
            supports_time_pushdown=(self.format == "parquet"),
            supports_mirror=self.mirror_path is not None,
        )

    # ------------------------------------------------------------------ #
    # Opt-in format guards (T014) — lazy-imported, actionable error       #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _require_deltalake():
        try:
            import deltalake  # noqa: F401
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise ImportError(
                'TableBackend(format="delta") requires the "deltalake" package. '
                'Install it with: pip install "fluxstate[table]"'
            ) from exc
        return deltalake

    @staticmethod
    def _require_pyiceberg():
        try:
            import pyiceberg  # noqa: F401
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise ImportError(
                'TableBackend(format="iceberg") requires the "pyiceberg" package. '
                'Install it with: pip install "fluxstate[table]"'
            ) from exc
        return pyiceberg

    # ------------------------------------------------------------------ #
    # Mirror-policy bookkeeping                                          #
    # ------------------------------------------------------------------ #
    def _refresh_mode(self) -> str:
        if self.mirror_path is None:
            return "off"
        if str(self.mirror_refresh).startswith("cadence"):
            return "cadence"
        return "on_demand"

    def _cadence_n(self) -> Optional[int]:
        s = str(self.mirror_refresh)
        if s.startswith("cadence"):
            _, _, n = s.partition(":")
            try:
                return int(n)
            except ValueError:
                return None
        return None

    def _default_mirror_dict(self) -> dict[str, Any]:
        return MirrorPolicy(
            enabled=self.mirror_path is not None,
            refresh_mode=self._refresh_mode(),
            cadence_n=self._cadence_n(),
        ).to_dict()

    def _maybe_cadence_refresh(self, manifest: dict) -> None:
        """Auto-refresh ``flux_mirror`` every N captures under ``cadence:N`` (T013)."""
        if self.mirror_path is None:
            return
        if self._refresh_mode() != "cadence":
            return
        n = self._cadence_n()
        if not n or n <= 0:
            return
        count = len(manifest.get("events") or [])
        if count % n == 0:
            self.refresh_mirror()

    # ------------------------------------------------------------------ #
    # Meta descriptor <-> raw manifest dict (M-1..3)                     #
    # ------------------------------------------------------------------ #
    def _fresh_manifest(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "store_name": self.events_path.stem,
            "key_column": "",
            "schema": {},
            "events": [],
            "checkpoints": [],
            "mirror": self._default_mirror_dict(),
        }

    def _meta_to_manifest(self, meta: Meta) -> dict:
        return {
            "schema_version": meta.schema_version,
            "store_name": self.events_path.stem,
            "key_column": meta.key_column,
            "schema": dict(meta.schema),
            "events": list(meta.event_catalog),
            "checkpoints": [],
            "mirror": dict(meta.mirror) if meta.mirror else self._default_mirror_dict(),
        }

    def _manifest_to_meta(self, d: dict) -> Meta:
        return Meta(
            key_column=d.get("key_column", ""),
            schema=dict(d.get("schema") or {}),
            event_catalog=list(d.get("events") or []),
            mirror=dict(d.get("mirror") or self._default_mirror_dict()),
            schema_version=d.get("schema_version", SCHEMA_VERSION),
        )

    # ------------------------------------------------------------------ #
    # Raw manifest dict I/O — the ``_flux_meta.json`` companion (M-2)     #
    # Format-agnostic on purpose: a JSON sidecar co-located with the      #
    # events dataset/table works uniformly for parquet/delta/iceberg      #
    # without depending on version-fragile "table properties" APIs.       #
    # ------------------------------------------------------------------ #
    def read_manifest(self) -> dict:
        """Return the descriptor dict (FR-006 / M-1). Fresh empty manifest if uncommitted."""
        if not self.meta_path.exists():
            return self._fresh_manifest()
        with open(self.meta_path, "rb") as fh:
            manifest = json.loads(fh.read())
        manifest.setdefault("checkpoints", [])
        manifest.setdefault("mirror", self._default_mirror_dict())
        return manifest

    def write_manifest(self, manifest: dict) -> None:
        """Atomically write the descriptor (temp -> fsync -> rename) — the commit point (M-3)."""
        self.events_path.mkdir(parents=True, exist_ok=True)
        tmp = self.meta_path.with_suffix(".json.tmp")
        data = json.dumps(manifest, indent=2, sort_keys=False).encode("utf-8")
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.meta_path)

    # ------------------------------------------------------------------ #
    # StorageBackend protocol: read_meta / write_meta                    #
    # ------------------------------------------------------------------ #
    def read_meta(self) -> Meta:
        return self._manifest_to_meta(self.read_manifest())

    def write_meta(self, meta: Meta) -> None:
        self.write_manifest(self._meta_to_manifest(meta))

    # ------------------------------------------------------------------ #
    # Parquet part-file naming (mirrors LocalFolderStore._events_filename)
    # ------------------------------------------------------------------ #
    def _part_filename(self, stamp: datetime) -> str:
        base = to_utc(stamp).strftime("%Y%m%dT%H%M%S%fZ")
        name = f"{base}.parquet"
        n = 1
        while (self.events_path / name).exists():
            name = f"{base}_{n}.parquet"
            n += 1
        return name

    def _append_events_data_parquet(
        self, events: pl.DataFrame, snapshot_id: str, stamp: Optional[datetime] = None
    ) -> dict:
        """Write one immutable Parquet part directly under ``events_path`` (FE-1)."""
        self.events_path.mkdir(parents=True, exist_ok=True)
        stamp = to_utc(stamp) if stamp is not None else to_utc(events["timestamp"].max())
        fname = self._part_filename(stamp)
        final = self.events_path / fname
        tmp = self.events_path / f".{fname}.tmp"
        events.write_parquet(tmp, statistics=True)
        with open(tmp, "rb") as fh:
            os.fsync(fh.fileno())
        os.replace(tmp, final)

        ts_min = events["timestamp"].min()
        ts_max = events["timestamp"].max()
        return {
            "file": fname,
            "snapshot_id": snapshot_id,
            "ts_min": to_utc(ts_min).isoformat(),
            "ts_max": to_utc(ts_max).isoformat(),
            "row_count": events.height,
        }

    # ------------------------------------------------------------------ #
    # list_events (RAW; capture()-facing) — parquet + iceberg (delta via sidecar) #
    # ------------------------------------------------------------------ #
    def list_events(self, as_of: Optional[datetime] = None, window=None) -> list[Path]:
        if self.format == "delta":
            raise NotImplementedError(
                "TableBackend(format='delta') capture is provided by the Databricks "
                "sidecar — use `from sidecars.databricks import DeltaBackend`. Or drive "
                "this backend directly via append_events()/read_events()."
            )
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
            # parquet: one part path in `file`; iceberg: the data-file paths in `files`
            # (absolute) that its `_append_events` recorded per commit.
            for rel in (entry.get("files") or [entry["file"]]):
                p = Path(rel)
                kept.append(p if p.is_absolute() else self.events_path / rel)
        return kept

    # ------------------------------------------------------------------ #
    # _append_events (RAW; capture()-facing) — parquet + iceberg           #
    # ------------------------------------------------------------------ #
    def _append_events(
        self,
        events: pl.DataFrame,
        snapshot_id: str,
        key_column: str,
        schema: dict,
        stamp: Optional[datetime] = None,
    ) -> dict:
        """Atomically append one immutable events part + commit the descriptor (FE-1/M-3).

        Mirrors ``LocalFolderStore._append_events`` exactly (same combined
        schema-union + event-catalog commit that ``ChangeLogStore.capture``
        relies on for a single atomic write per capture), minus the nested
        ``events/`` subdirectory — parts land directly under ``events_path``.
        """
        if self.format == "delta":
            raise NotImplementedError(
                "TableBackend(format='delta') capture is provided by the Databricks "
                "sidecar — use `from sidecars.databricks import DeltaBackend`, or call "
                "append_events()/read_events() directly (StorageBackend protocol)."
            )
        if events.is_empty():
            raise ValueError("_append_events called with no events")

        if self.format == "iceberg":
            entry = self._append_events_capture_iceberg(events, snapshot_id)
        else:
            entry = self._append_events_data_parquet(events, snapshot_id, stamp=stamp)

        manifest = self.read_manifest()
        manifest["key_column"] = key_column
        # Append-only schema UNION (not overwrite) — matches LocalFolderStore.
        merged_schema = dict(manifest.get("schema") or {})
        merged_schema.update(schema)
        manifest["schema"] = merged_schema
        manifest.setdefault("events", []).append(entry)
        self.write_manifest(manifest)

        self._maybe_cadence_refresh(manifest)
        return entry

    # ------------------------------------------------------------------ #
    # StorageBackend protocol: append_events (generic, idempotent, SB-3)  #
    # ------------------------------------------------------------------ #
    def append_events(self, events: pl.DataFrame) -> EventRef:
        if events.is_empty():
            raise ValueError("append_events called with no events")
        snap = events["snapshot_id"][0]

        manifest = self.read_manifest()
        existing = next(
            (e for e in manifest.get("events", []) if e["snapshot_id"] == snap), None
        )
        if existing is not None:
            return EventRef(
                snapshot_id=snap,
                ref=existing.get("file", ""),
                row_count=existing["row_count"],
                ts_min=to_utc(datetime.fromisoformat(existing["ts_min"])),
                ts_max=to_utc(datetime.fromisoformat(existing["ts_max"])),
            )

        if self.format == "parquet":
            entry = self._append_events_data_parquet(events, snap)
        elif self.format == "delta":
            entry = self._append_events_data_delta(events, snap)
        else:
            entry = self._append_events_data_iceberg(events, snap)

        manifest.setdefault("events", []).append(entry)
        self.write_manifest(manifest)
        self._maybe_cadence_refresh(manifest)
        return EventRef(
            snapshot_id=snap,
            ref=entry.get("file", ""),
            row_count=entry["row_count"],
            ts_min=to_utc(datetime.fromisoformat(entry["ts_min"])),
            ts_max=to_utc(datetime.fromisoformat(entry["ts_max"])),
        )

    # ------------------------------------------------------------------ #
    # StorageBackend protocol: read_events (predicate pushdown, SB-5/SB-8)#
    # ------------------------------------------------------------------ #
    def read_events(self, predicate: Optional[Predicate] = None) -> pl.DataFrame:
        if self.format == "parquet":
            return self._read_events_parquet(predicate)
        if self.format == "delta":
            return self._read_events_delta(predicate)
        return self._read_events_iceberg(predicate)

    def _read_events_parquet(self, predicate: Optional[Predicate] = None) -> pl.DataFrame:
        files = self.list_events()
        if not files:
            return pl.DataFrame(schema=change_event_schema())
        # Lazy scan (not eager read+concat) so a timestamp predicate can push down
        # into Parquet row-group pruning — the real `supports_time_pushdown=True`.
        lf = pl.scan_parquet(files)
        if predicate is not None:
            lf = lf.filter(predicate)
        return lf.collect()

    def _read_events_delta(self, predicate: Optional[Predicate] = None) -> pl.DataFrame:
        self._require_deltalake()
        from deltalake import DeltaTable

        if not (self.events_path / "_delta_log").exists():
            return pl.DataFrame(schema=change_event_schema())
        dt = DeltaTable(str(self.events_path))
        df = pl.from_arrow(dt.to_pyarrow_table())
        if predicate is not None:
            df = df.filter(predicate)
        return df

    def _read_events_iceberg(self, predicate: Optional[Predicate] = None) -> pl.DataFrame:
        catalog = self._iceberg_catalog()
        identifier = self._iceberg_identifier()
        if not catalog.table_exists(identifier):
            return pl.DataFrame(schema=change_event_schema())
        table = catalog.load_table(identifier)
        df = pl.from_arrow(table.scan().to_arrow())
        if predicate is not None:
            df = df.filter(predicate)
        return df

    # ------------------------------------------------------------------ #
    # Delta event-data I/O (T014, opt-in, minimal — see module docstring) #
    # ------------------------------------------------------------------ #
    def _append_events_data_delta(self, events: pl.DataFrame, snapshot_id: str) -> dict:
        self._require_deltalake()
        from deltalake import write_deltalake

        self.events_path.mkdir(parents=True, exist_ok=True)
        write_deltalake(str(self.events_path), events.to_arrow(), mode="append")
        ts_min = events["timestamp"].min()
        ts_max = events["timestamp"].max()
        return {
            "file": "<delta-table>",
            "snapshot_id": snapshot_id,
            "ts_min": to_utc(ts_min).isoformat(),
            "ts_max": to_utc(ts_max).isoformat(),
            "row_count": events.height,
        }

    # ------------------------------------------------------------------ #
    # Iceberg event-data I/O (T014, opt-in, minimal — see module docstring)
    # A local, self-contained SQLite catalog rooted next to `events_path`  #
    # so the format works without an external Iceberg catalog service.    #
    # ------------------------------------------------------------------ #
    def _iceberg_catalog(self):
        self._require_pyiceberg()
        from pyiceberg.catalog.sql import SqlCatalog

        self.events_path.mkdir(parents=True, exist_ok=True)
        catalog_db = self.events_path / "catalog.db"
        warehouse = self.events_path / "warehouse"
        return SqlCatalog(
            "fluxstate",
            uri=f"sqlite:///{catalog_db}",
            warehouse=f"file://{warehouse}",
        )

    def _iceberg_identifier(self) -> str:
        return f"fluxstate.{self.events_path.stem or 'flux_events'}"

    def _append_events_data_iceberg(self, events: pl.DataFrame, snapshot_id: str) -> dict:
        catalog = self._iceberg_catalog()
        identifier = self._iceberg_identifier()
        namespace = identifier.split(".", 1)[0]
        try:
            catalog.create_namespace(namespace)
        except Exception:
            pass  # namespace already exists

        arrow_tbl = events.to_arrow()
        if catalog.table_exists(identifier):
            table = catalog.load_table(identifier)
        else:
            table = catalog.create_table(identifier, schema=arrow_tbl.schema)
        table.append(arrow_tbl)

        ts_min = events["timestamp"].min()
        ts_max = events["timestamp"].max()
        return {
            "file": "<iceberg-table>",
            "snapshot_id": snapshot_id,
            "ts_min": to_utc(ts_min).isoformat(),
            "ts_max": to_utc(ts_max).isoformat(),
            "row_count": events.height,
        }

    # ------------------------------------------------------------------ #
    # Iceberg CAPTURE-facing support (engine capture() through the table) #
    # Same pattern as sidecars.databricks.DeltaBackend: an Iceberg table's #
    # committed data files ARE plain Parquet, so we diff the data-file set #
    # before/after `table.append()` and record the new files, which        #
    # `list_events` then hands to the shared `pl.read_parquet` recon path.  #
    # ------------------------------------------------------------------ #
    def _iceberg_current_files(self) -> set[str]:
        catalog = self._iceberg_catalog()
        identifier = self._iceberg_identifier()
        if not catalog.table_exists(identifier):
            return set()
        table = catalog.load_table(identifier)
        files = [r["file_path"] for r in table.inspect.data_files().to_pylist()]
        # `file_path` is an absolute URI; strip a `file://` scheme so it's a plain
        # local path usable by `pl.read_parquet` in `list_events`.
        return {f[len("file://"):] if f.startswith("file://") else f for f in files}

    def _append_events_capture_iceberg(self, events: pl.DataFrame, snapshot_id: str) -> dict:
        before = self._iceberg_current_files()
        self._append_events_data_iceberg(events, snapshot_id)  # appends to the Iceberg table
        new_files = sorted(self._iceberg_current_files() - before)
        ts_min = events["timestamp"].min()
        ts_max = events["timestamp"].max()
        return {
            "file": "<iceberg-table>",
            "files": new_files,
            "snapshot_id": snapshot_id,
            "ts_min": to_utc(ts_min).isoformat(),
            "ts_max": to_utc(ts_max).isoformat(),
            "row_count": events.height,
        }

    # ------------------------------------------------------------------ #
    # StorageBackend protocol: read_current_state                        #
    # ------------------------------------------------------------------ #
    def _current_state_from_events(self, events: pl.DataFrame, meta: Meta) -> pl.DataFrame:
        """Latest-per-cell wide reconstruction (shared by every format).

        Same algorithm as ``LocalFolderStore.read_current_state`` /
        ``changelog.ChangeLogStore._materialize_current``: latest value per
        ``(entity_id, field)``, entities currently ``__deleted__`` excluded,
        values decoded back to their recorded dtype.
        """
        if not meta.key_column or not meta.schema:
            return pl.DataFrame()
        pl_schema = {c: tag_to_dtype(t) for c, t in meta.schema.items()}
        if events.is_empty():
            return pl.DataFrame(schema=pl_schema)

        latest = (
            events.sort("timestamp")
            .group_by(["entity_id", "field"], maintain_order=True)
            .last()
        )
        entity_last = (
            events.sort("timestamp").group_by("entity_id", maintain_order=True).last()
        )
        deleted_ids = entity_last.filter(pl.col("field") == DELETED_FIELD)["entity_id"].to_list()
        latest = latest.filter(
            (pl.col("field") != DELETED_FIELD) & (~pl.col("entity_id").is_in(deleted_ids))
        )
        if latest.is_empty():
            return pl.DataFrame(schema=pl_schema)

        fields = [c for c in pl_schema if c != meta.key_column]
        wide = latest.pivot(
            on="field", index="entity_id", values="value", aggregate_function="first"
        )
        for f in fields:
            if f not in wide.columns:
                wide = wide.with_columns(pl.lit(None, dtype=pl.Utf8).alias(f))

        key_dtype = pl_schema[meta.key_column]
        cols = [
            wide["entity_id"]
            .map_elements(
                lambda v, _t=dtype_tag(key_dtype): decode_value(v, _t), return_dtype=key_dtype
            )
            .alias(meta.key_column)
        ]
        for f in fields:
            pdt = pl_schema[f]
            cols.append(
                wide[f]
                .map_elements(lambda v, _t=dtype_tag(pdt): decode_value(v, _t), return_dtype=pdt)
                .alias(f)
            )
        return pl.DataFrame(cols).select([meta.key_column, *fields])

    def read_current_state(self) -> pl.DataFrame:
        meta = self.read_meta()
        events = self.read_events()
        return self._current_state_from_events(events, meta)

    # ------------------------------------------------------------------ #
    # Optional flux_mirror (T013, FM-1/FM-2)                              #
    # ------------------------------------------------------------------ #
    def _build_mirror(self, at: Optional[datetime] = None) -> pl.DataFrame:
        """As-of wide reconstruction without depending on the parquet-only ``list_events``.

        Used for the Delta/Iceberg fallback; produces identical content to
        ``reconstruct.build_mirror_view`` (same latest-per-cell algorithm via
        ``_current_state_from_events``), satisfying FM-1 without requiring the
        RAW capture-facing helpers.
        """
        meta = self.read_meta()
        events = self.read_events()
        if at is not None:
            events = events.filter(pl.col("timestamp") <= to_utc(at))
        wide = self._current_state_from_events(events, meta)
        if meta.key_column and meta.key_column in wide.columns:
            wide = wide.sort(meta.key_column)
        return wide

    def _write_mirror(self, wide: pl.DataFrame) -> None:
        assert self.mirror_path is not None
        self.mirror_path.mkdir(parents=True, exist_ok=True)
        final = self.mirror_path / _MIRROR_FILE
        tmp = self.mirror_path / f".{_MIRROR_FILE}.tmp"
        wide.write_parquet(tmp, statistics=True)
        with open(tmp, "rb") as fh:
            os.fsync(fh.fileno())
        os.replace(tmp, final)

    def read_mirror(self) -> pl.DataFrame:
        """Read the materialized ``flux_mirror`` dataset (empty frame if never refreshed)."""
        if self.mirror_path is None:
            raise RuntimeError("TableBackend: no `mirror=` location configured")
        final = self.mirror_path / _MIRROR_FILE
        if not final.exists():
            return pl.DataFrame()
        return pl.read_parquet(final)

    def refresh_mirror(self, at: Optional[datetime] = None) -> None:
        """Recompute ``flux_mirror`` from ``flux_events`` (FM-1/FM-2, SB-8).

        No-op when no ``mirror=`` location was configured
        (``capabilities.supports_mirror`` is False) or nothing has been
        captured yet. On the Parquet path, derives the wide view via
        ``reconstruct.build_mirror_view`` (the exact contract wording); on
        Delta/Iceberg falls back to the equivalent local algorithm (see
        ``_build_mirror``) since the RAW ``list_events`` those formats would
        need is parquet-only in this wave. Records the last-materialized
        ``snapshot_id`` as the staleness watermark in ``Meta.mirror`` (FM-2).
        """
        if self.mirror_path is None:
            return None
        meta = self.read_meta()
        if not meta.key_column or not meta.schema:
            return None  # nothing captured yet

        if self.format == "parquet":
            from changelog import ChangeLogStore
            import reconstruct as _reconstruct

            shim = ChangeLogStore(self.events_path, backend=self)
            wide = _reconstruct.build_mirror_view(shim, T=at if at is not None else "now")
        else:
            wide = self._build_mirror(at)

        self._write_mirror(wide)

        watermark = meta.event_catalog[-1]["snapshot_id"] if meta.event_catalog else None
        meta.mirror = MirrorPolicy(
            enabled=True,
            refresh_mode=self._refresh_mode(),
            cadence_n=self._cadence_n(),
            watermark=watermark,
        ).to_dict()
        self.write_meta(meta)
        return None
