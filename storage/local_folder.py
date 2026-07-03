# File: storage/local_folder.py
"""``LocalFolderStore`` — the default ``StorageBackend``: a plain ``<name>.flux/`` folder.

Behavior-identical extraction of the local-filesystem persistence that used to
live inline in ``changelog.ChangeLogStore`` (feature 001). ``ChangeLogStore``
now defaults to this backend (feature 003, T007/T008) so existing ``.flux/``
stores keep their EXACT on-disk shape (``manifest.json`` + ``events/*.parquet``,
atomic temp -> fsync -> rename writes) — only *where the bytes are written*
moved behind the seam, not the format itself. See ``specs/
003-pluggable-storage-backends/contracts/storage-backend.md`` (SB-1..SB-8).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import polars as pl

from changelog import (
    DELETED_FIELD,
    EVENTS_DIR,
    MANIFEST_NAME,
    SCHEMA_VERSION,
    change_event_schema,
    decode_value,
    dtype_tag,
    tag_to_dtype,
    to_utc,
)
from storage.base import Capabilities, EventRef, Meta, Predicate

__all__ = ["LocalFolderStore"]


class LocalFolderStore:
    """Default backend: a local folder of ``manifest.json`` + ``events/*.parquet``.

    Implements the full :class:`storage.base.StorageBackend` protocol
    (``read_meta`` / ``write_meta`` / ``read_current_state`` / ``read_events`` /
    ``append_events`` / ``refresh_mirror``) PLUS the raw-manifest helpers
    (``read_manifest`` / ``write_manifest`` / ``list_events`` / ``_append_events``)
    that ``changelog.ChangeLogStore`` delegates to directly. Those helpers are
    moved verbatim from the original inline implementation so the physical
    format and the single-atomic-write-per-capture semantics are unchanged.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.events_dir = self.path / EVENTS_DIR
        self.manifest_path = self.path / MANIFEST_NAME
        self.capabilities = Capabilities(
            is_table=False,
            supports_atomic_meta=True,
            supports_time_pushdown=False,
            supports_mirror=False,
        )

    # --- raw manifest dict I/O (moved verbatim from changelog.ChangeLogStore) - #
    def _fresh_manifest(self) -> dict:
        """A manifest for a store with no commits yet."""
        return {
            "schema_version": SCHEMA_VERSION,
            "store_name": self.path.stem,
            "key_column": "",
            "schema": {},
            "events": [],
            "checkpoints": [],
        }

    def read_manifest(self) -> dict:
        """Return the manifest dict (FR-014 / STORE-4).

        Only files listed under ``events`` are valid history; an orphan parquet
        on disk but absent here is ignored. Returns a fresh empty manifest when
        the store does not exist yet.
        """
        if not self.manifest_path.exists():
            return self._fresh_manifest()
        with open(self.manifest_path, "rb") as fh:
            manifest = json.loads(fh.read())
        manifest.setdefault("checkpoints", [])
        return manifest

    def write_manifest(self, manifest: dict) -> None:
        """Atomically write the manifest (temp -> fsync -> rename) — the commit point."""
        self.path.mkdir(parents=True, exist_ok=True)
        tmp = self.manifest_path.with_suffix(".json.tmp")
        data = json.dumps(manifest, indent=2, sort_keys=False).encode("utf-8")
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.manifest_path)

    def _events_filename(self, stamp: datetime) -> str:
        """A UTC, lexically-sortable events filename (so a plain glob sorts by time)."""
        base = to_utc(stamp).strftime("%Y%m%dT%H%M%S%fZ")
        name = f"{base}.parquet"
        n = 1
        while (self.events_dir / name).exists():
            name = f"{base}_{n}.parquet"
            n += 1
        return name

    def _append_events(
        self,
        events: pl.DataFrame,
        snapshot_id: str,
        key_column: str,
        schema: dict,
        stamp: Optional[datetime] = None,
    ) -> dict:
        """Atomically append one immutable events file and commit the manifest (FR-013).

        This is the combined schema-union + event-catalog commit that
        ``ChangeLogStore.capture`` relies on for a single atomic write per
        capture (SB-1..SB-4). Protocol (crash-safe): write
        ``events/.<ts>.parquet.tmp`` -> fsync -> atomic rename to
        ``events/<ts>.parquet`` -> recompute manifest in memory -> atomic
        manifest rewrite. A crash before the manifest rewrite leaves an orphan
        parquet that readers ignore (STORE-4).
        """
        if events.is_empty():
            raise ValueError("_append_events called with no events")
        self.events_dir.mkdir(parents=True, exist_ok=True)

        stamp = stamp or datetime.now(timezone.utc)
        fname = self._events_filename(stamp)
        final = self.events_dir / fname
        tmp = self.events_dir / f".{fname}.tmp"

        # Write parquet with statistics (row-group min/max for the timestamp column).
        events.write_parquet(tmp, statistics=True)
        with open(tmp, "rb") as fh:
            os.fsync(fh.fileno())
        os.replace(tmp, final)

        ts_min = events["timestamp"].min()
        ts_max = events["timestamp"].max()
        entry = {
            "file": f"{EVENTS_DIR}/{fname}",
            "snapshot_id": snapshot_id,
            "ts_min": to_utc(ts_min).isoformat(),
            "ts_max": to_utc(ts_max).isoformat(),
            "row_count": events.height,
        }

        manifest = self.read_manifest()
        manifest["key_column"] = key_column
        # Append-only schema UNION (not overwrite) — see changelog.py for rationale.
        merged_schema = dict(manifest.get("schema") or {})
        merged_schema.update(schema)
        manifest["schema"] = merged_schema
        manifest["events"].append(entry)
        self.write_manifest(manifest)
        return entry

    def list_events(self, as_of: Optional[datetime] = None, window=None) -> list[Path]:
        """Return manifest-valid event files (chronological), with file-skip pruning (R5)."""
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
                continue  # file entirely after the as-of point
            if lo is not None and ts_max < lo:
                continue  # file entirely before the window
            if hi is not None and ts_min > hi:
                continue  # file entirely after the window
            kept.append(self.path / entry["file"])
        return kept

    # --- StorageBackend protocol (T008) -------------------------------------- #
    def read_meta(self) -> Meta:
        """Load the store descriptor as a :class:`storage.base.Meta` (SB contract)."""
        d = self.read_manifest()
        return Meta(
            key_column=d.get("key_column", ""),
            schema=dict(d.get("schema") or {}),
            event_catalog=list(d.get("events") or []),
            mirror=dict(d.get("mirror") or {}),
            schema_version=d.get("schema_version", SCHEMA_VERSION),
        )

    def write_meta(self, meta: Meta) -> None:
        """Persist a :class:`storage.base.Meta` atomically (SB-4).

        Preserves the on-disk ``store_name`` / ``checkpoints`` fields already in
        the manifest (they are not part of ``Meta``) so round-tripping through
        this method never drops those keys.
        """
        d = self.read_manifest()
        d["key_column"] = meta.key_column
        d["schema"] = dict(meta.schema)
        d["events"] = list(meta.event_catalog)
        d["mirror"] = dict(meta.mirror)
        d["schema_version"] = meta.schema_version
        self.write_manifest(d)

    def read_current_state(self) -> pl.DataFrame:
        """The reconstructed latest mirror, typed per the store's own committed schema.

        Entities whose most recent lifecycle event is a ``__deleted__`` marker
        are excluded. Returns an empty frame for a store with no committed
        schema yet.
        """
        meta = self.read_meta()
        if not meta.key_column or not meta.schema:
            return pl.DataFrame()
        pl_schema = {c: tag_to_dtype(t) for c, t in meta.schema.items()}
        events = self.read_events()
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

    def read_events(self, predicate: Optional[Predicate] = None) -> pl.DataFrame:
        """All manifest-valid Change Events, optionally filtered by ``predicate`` (SB-5/SB-8)."""
        files = self.list_events()
        if not files:
            df = pl.DataFrame(schema=change_event_schema())
        else:
            df = pl.concat([pl.read_parquet(f) for f in files], how="vertical")
        if predicate is not None:
            df = df.filter(predicate)
        return df

    def append_events(self, events: pl.DataFrame) -> EventRef:
        """Persist one capture's Change Events; atomic + idempotent by ``snapshot_id`` (SB-1..3).

        Generic Protocol entrypoint: unlike ``_append_events`` (used internally
        by ``ChangeLogStore.capture`` to also commit the schema union), this
        derives everything it needs from ``events`` itself and leaves the
        manifest's ``key_column`` / ``schema`` untouched.
        """
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
                ref=existing["file"],
                row_count=existing["row_count"],
                ts_min=to_utc(datetime.fromisoformat(existing["ts_min"])),
                ts_max=to_utc(datetime.fromisoformat(existing["ts_max"])),
            )

        self.events_dir.mkdir(parents=True, exist_ok=True)
        fname = self._events_filename(events["timestamp"].max())
        final = self.events_dir / fname
        tmp = self.events_dir / f".{fname}.tmp"
        events.write_parquet(tmp, statistics=True)
        with open(tmp, "rb") as fh:
            os.fsync(fh.fileno())
        os.replace(tmp, final)

        ts_min = events["timestamp"].min()
        ts_max = events["timestamp"].max()
        entry = {
            "file": f"{EVENTS_DIR}/{fname}",
            "snapshot_id": snap,
            "ts_min": to_utc(ts_min).isoformat(),
            "ts_max": to_utc(ts_max).isoformat(),
            "row_count": events.height,
        }
        manifest.setdefault("events", []).append(entry)
        self.write_manifest(manifest)
        return EventRef(
            snapshot_id=snap,
            ref=entry["file"],
            row_count=entry["row_count"],
            ts_min=to_utc(ts_min),
            ts_max=to_utc(ts_max),
        )

    def refresh_mirror(self, at: Optional[datetime] = None) -> None:
        """No-op: ``capabilities.supports_mirror`` is False (SB-8) — no ``flux_mirror`` here."""
        return None
