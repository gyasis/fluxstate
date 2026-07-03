# File: storage/object_store.py
"""``ObjectStoreBackend`` — a ``StorageBackend`` over any fsspec filesystem (feature 003, T018).

Persists the SAME store layout as :class:`storage.local_folder.LocalFolderStore`
(``manifest.json`` + ``events/*.parquet``) but through **fsspec**, so
``s3://``, ``abfss://``, ``gs://``, a Databricks UC Volume, or a plain
``file://``/local path all work through one implementation. See
``specs/003-pluggable-storage-backends/research.md`` R3 and
``contracts/storage-backend.md`` SB-4.

The one behavioral difference from ``LocalFolderStore`` is *how* the manifest
commit happens: object stores (and the FUSE mounts some platforms expose,
e.g. Databricks UC Volumes) do **not** offer an atomic rename — a rename is
really copy+delete and can tear under a concurrent reader. Object stores DO
guarantee an atomic **single-object PUT**, so ``write_manifest``/``write_meta``
write the whole manifest body in exactly one ``fs.pipe_file`` call — never a
temp-file-then-rename. Event parts are written the same way: one immutable
single-object PUT per capture, never rewritten (SB-1/SB-4).
"""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from pathlib import PurePosixPath
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

__all__ = ["ObjectStoreBackend"]


class ObjectStoreBackend:
    """``StorageBackend`` over an fsspec filesystem — a drop-in for ``LocalFolderStore``.

    Implements the full :class:`storage.base.StorageBackend` protocol
    (``read_meta`` / ``write_meta`` / ``read_current_state`` / ``read_events`` /
    ``append_events`` / ``refresh_mirror``) PLUS the raw-manifest capture-facing
    helpers (``read_manifest`` / ``write_manifest`` / ``list_events`` /
    ``_append_events``) that ``changelog.ChangeLogStore.capture`` delegates to
    directly — same signatures as ``LocalFolderStore``, so
    ``ChangeLogStore(location, backend=ObjectStoreBackend(location))`` (or
    ``FluxState(store=ObjectStoreBackend(location))``) captures exactly like
    the local-folder backend.
    """

    def __init__(self, location: str):
        # fsspec is the `[remote]` extra, not a core dependency (G1/G8) — lazy
        # import here, not at module load time, with an actionable error.
        try:
            import fsspec
        except ImportError as exc:  # pragma: no cover - exercised via monkeypatch in tests
            raise ImportError(
                'ObjectStoreBackend requires the "fsspec" package (plus a '
                "protocol-specific driver such as s3fs/adlfs/gcsfs for remote "
                'URLs). Install it with: pip install "fluxstate[remote]"'
            ) from exc

        self.location = location
        self.fs, base_path = fsspec.core.url_to_fs(location)
        self.base_path = base_path.rstrip("/")
        self.events_dir = f"{self.base_path}/{EVENTS_DIR}"
        self.manifest_path = f"{self.base_path}/{MANIFEST_NAME}"
        self.capabilities = Capabilities(
            is_table=False,
            supports_atomic_meta=True,
            supports_time_pushdown=False,
            supports_mirror=False,
        )

    # --- raw manifest dict I/O (mirrors LocalFolderStore, fsspec-backed) ----- #
    def _fresh_manifest(self) -> dict:
        """A manifest for a store with no commits yet."""
        return {
            "schema_version": SCHEMA_VERSION,
            "store_name": PurePosixPath(self.base_path).name or "flux_events",
            "key_column": "",
            "schema": {},
            "events": [],
            "checkpoints": [],
        }

    def read_manifest(self) -> dict:
        """Return the manifest dict (FR-014 / STORE-4).

        Only files listed under ``events`` are valid history; an orphan
        parquet object present but absent here is ignored. Returns a fresh
        empty manifest when the store does not exist yet.
        """
        if not self.fs.exists(self.manifest_path):
            return self._fresh_manifest()
        data = self.fs.cat_file(self.manifest_path)
        manifest = json.loads(data)
        manifest.setdefault("checkpoints", [])
        return manifest

    def write_manifest(self, manifest: dict) -> None:
        """Atomically write the manifest — the commit point (SB-4, R3).

        A single-object PUT (``fs.pipe_file``, ONE call) of the whole manifest
        body. Deliberately NOT a temp-file-then-rename: rename is a
        copy+delete on object stores / FUSE mounts and can tear; a
        single-object PUT is what S3/ADLS/GCS actually guarantee atomic.
        """
        if self.base_path:
            self.fs.makedirs(self.base_path, exist_ok=True)
        data = json.dumps(manifest, indent=2, sort_keys=False).encode("utf-8")
        self.fs.pipe_file(self.manifest_path, data)

    def _events_filename(self, stamp: datetime) -> str:
        """A UTC, lexically-sortable events filename (so a plain glob sorts by time)."""
        base = to_utc(stamp).strftime("%Y%m%dT%H%M%S%fZ")
        name = f"{base}.parquet"
        n = 1
        while self.fs.exists(f"{self.events_dir}/{name}"):
            name = f"{base}_{n}.parquet"
            n += 1
        return name

    @staticmethod
    def _events_to_bytes(events: pl.DataFrame) -> bytes:
        buf = io.BytesIO()
        events.write_parquet(buf, statistics=True)
        return buf.getvalue()

    def _put_event_object(self, ref: str, data: bytes) -> None:
        """One immutable single-object PUT of an events part (SB-1/SB-4).

        Real filesystems (``LocalFileSystem``, and the FUSE mounts some
        platforms expose) require the parent directory to exist before a
        write; object stores/``MemoryFileSystem`` have no real directories
        and ignore this. ``makedirs`` here is directory bookkeeping only —
        the PUT itself is still the one atomic call.
        """
        self.fs.makedirs(self.events_dir, exist_ok=True)
        self.fs.pipe_file(ref, data)

    def _append_events(
        self,
        events: pl.DataFrame,
        snapshot_id: str,
        key_column: str,
        schema: dict,
        stamp: Optional[datetime] = None,
    ) -> dict:
        """Atomically append one immutable events object and commit the manifest (FR-013).

        This is the combined schema-union + event-catalog commit that
        ``ChangeLogStore.capture`` relies on for a single atomic write per
        capture (SB-1..SB-4). Protocol: write ``events/<ts>.parquet`` as ONE
        single-object PUT (immutable — never rewritten) -> recompute manifest
        in memory -> ONE single-object PUT of the manifest. A crash between
        the two PUTs leaves an orphan parquet object that readers ignore
        (STORE-4), never a torn manifest.
        """
        if events.is_empty():
            raise ValueError("_append_events called with no events")

        stamp = stamp or datetime.now(timezone.utc)
        fname = self._events_filename(stamp)
        ref = f"{self.events_dir}/{fname}"
        self._put_event_object(ref, self._events_to_bytes(events))

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

    def list_events(self, as_of: Optional[datetime] = None, window=None) -> list[io.BytesIO]:
        """Return manifest-valid event contents (chronological), with file-skip pruning (R5).

        Returns pre-fetched ``io.BytesIO`` buffers rather than path strings.
        ``changelog.ChangeLogStore._read_all_events`` and
        ``reconstruct._scan`` open this method's result directly with bare
        ``pl.read_parquet``/``pl.scan_parquet`` — which only understand real
        local filesystem paths or a scheme polars' own IO layer natively
        speaks, not an arbitrary fsspec scheme like ``memory://``. Returning
        buffers (fetched here via ``fs.cat_file``, one call per object) makes
        every fsspec backend transparently readable through that same call
        path, while still sourcing content only from the immutable committed
        objects (SB-6 byte-identical reconstruction across backends).
        """
        manifest = self.read_manifest()
        as_of = to_utc(as_of) if as_of is not None else None
        lo = hi = None
        if window is not None:
            lo, hi = window
            lo = to_utc(lo) if lo is not None else None
            hi = to_utc(hi) if hi is not None else None

        kept: list[io.BytesIO] = []
        for entry in manifest.get("events", []):
            ts_min = to_utc(datetime.fromisoformat(entry["ts_min"]))
            ts_max = to_utc(datetime.fromisoformat(entry["ts_max"]))
            if as_of is not None and ts_min > as_of:
                continue  # file entirely after the as-of point
            if lo is not None and ts_max < lo:
                continue  # file entirely before the window
            if hi is not None and ts_min > hi:
                continue  # file entirely after the window
            ref = f"{self.base_path}/{entry['file']}"
            kept.append(io.BytesIO(self.fs.cat_file(ref)))
        return kept

    # --- StorageBackend protocol (T018) --------------------------------- #
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
        """Persist a :class:`storage.base.Meta` via a single-object atomic PUT (SB-4).

        Preserves the on-disk ``store_name`` / ``checkpoints`` fields already
        in the manifest (they are not part of ``Meta``) so round-tripping
        through this method never drops those keys.
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
        buffers = self.list_events()
        if not buffers:
            df = pl.DataFrame(schema=change_event_schema())
        else:
            df = pl.concat([pl.read_parquet(buf) for buf in buffers], how="vertical")
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

        fname = self._events_filename(events["timestamp"].max())
        ref = f"{self.events_dir}/{fname}"
        self._put_event_object(ref, self._events_to_bytes(events))

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
