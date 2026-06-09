# File: changelog.py
"""Append-only change-log writer for the ``<name>.flux/`` store.

This module is the single *writer* for the changelog-first storage pivot
(feature ``001-changelog-first-pivot``). It owns:

- the dtype codec (``encode_value`` / ``decode_value``)
- UTC normalization (``to_utc``)
- the Change Event Polars schema
- the row-hash prefilter (``hash_rows``) and content-derived ``snapshot_id``
- the ``ChangeLogStore`` substrate: manifest I/O, atomic events writer,
  file listing/pruning, current-state read-back, the keyed diff, and
  idempotent ``capture``.

Reads/reconstruction live in ``reconstruct.py``. See
``specs/001-changelog-first-pivot/contracts/changelog-store.md``.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import polars as pl

SCHEMA_VERSION = 1

# Reserved field name for a deletion-marker row (one per vanished entity).
DELETED_FIELD = "__deleted__"

# Manifest the reader treats as authoritative (only listed files are valid history).
MANIFEST_NAME = "manifest.json"
EVENTS_DIR = "events"


# --------------------------------------------------------------------------- #
# dtype codec (T004)                                                          #
# --------------------------------------------------------------------------- #
# Canonical type tags stored in the `dtype` column. A genuine SQL null is the
# Python value None in the `value` column (NEVER the literal string "NULL") —
# `dtype="null"` marks a deletion-marker row.
NULL_DTYPE = "null"


def dtype_tag(dtype: pl.DataType) -> str:
    """Canonicalize a Polars dtype to the stored ``dtype`` tag.

    Single source of truth shared by the schema (T006), the diff (T014), and
    the codec, so encode/decode always agree with what produced the tag.
    """
    if dtype == pl.Int64:
        return "int64"
    if dtype == pl.Float64:
        return "float64"
    if dtype == pl.Boolean:
        return "bool"
    if dtype == pl.Utf8:  # pl.String alias
        return "utf8"
    if isinstance(dtype, pl.Datetime):
        # e.g. datetime[us, UTC]
        tz = dtype.time_zone or "UTC"
        return f"datetime[{dtype.time_unit}, {tz}]"
    if dtype == pl.Null:
        return NULL_DTYPE
    # Fallback: lower-cased polars repr (keeps the codec total without guessing).
    return str(dtype).lower()


def _norm(dtype: str) -> str:
    return (dtype or "").strip().lower()


def tag_to_dtype(tag: str) -> pl.DataType:
    """Inverse of :func:`dtype_tag`: a stored type tag → the Polars dtype.

    Lets the reader rebuild a typed schema from the manifest (which stores tags),
    so reconstruction restores original types (API-4).
    """
    d = _norm(tag)
    if d.startswith("int"):
        return pl.Int64
    if d.startswith("float"):
        return pl.Float64
    if d in ("bool", "boolean"):
        return pl.Boolean
    if d == NULL_DTYPE:
        return pl.Null
    if d.startswith("datetime"):
        # parse "datetime[<unit>, <tz>]"; default to (us, UTC)
        unit, tz = "us", "UTC"
        inside = tag[tag.find("[") + 1 : tag.rfind("]")] if "[" in tag else ""
        if inside:
            parts = [p.strip() for p in inside.split(",")]
            if parts and parts[0]:
                unit = parts[0]
            if len(parts) > 1 and parts[1]:
                tz = parts[1]
        return pl.Datetime(unit, tz)
    return pl.Utf8


def encode_value(v: Any, dtype: str) -> Optional[str]:
    """Encode a Python value to its string cell representation (T004).

    Returns ``None`` for a genuine null (so the parquet ``value`` column holds a
    real null, not the string ``"NULL"``). ``dtype`` is a canonical type tag.
    """
    if v is None:
        return None
    d = _norm(dtype)
    if d == NULL_DTYPE:
        return None
    if d.startswith("int"):
        return str(int(v))
    if d.startswith("float"):
        return repr(float(v))  # repr round-trips float64 losslessly
    if d in ("bool", "boolean"):
        return "true" if bool(v) else "false"
    if d.startswith("datetime"):
        # tz-aware ISO 8601; preserves microseconds when present
        return v.isoformat()
    # utf8 / str / string / any other tag → string form
    return str(v)


def decode_value(value: Optional[str], dtype: str) -> Any:
    """Decode a stored string cell back to its original typed value (T004)."""
    d = _norm(dtype)
    if value is None or d == NULL_DTYPE:
        return None
    if d.startswith("int"):
        return int(value)
    if d.startswith("float"):
        return float(value)
    if d in ("bool", "boolean"):
        return value.strip().lower() == "true"
    if d.startswith("datetime"):
        dt = datetime.fromisoformat(value)
        return to_utc(dt)
    return value


# --------------------------------------------------------------------------- #
# UTC normalization (T005)                                                    #
# --------------------------------------------------------------------------- #
def to_utc(ts: datetime) -> datetime:
    """Normalize a timestamp to UTC (FR-008).

    tz-aware → convert to UTC; tz-naive → *declare* it UTC (no shift). Every
    event ``timestamp`` flows through here so the store is timezone-uniform.
    """
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


# --------------------------------------------------------------------------- #
# Change Event schema (T006)                                                  #
# --------------------------------------------------------------------------- #
def change_event_schema() -> dict[str, pl.DataType]:
    """Return the ordered Polars schema for a Change Event frame (T006).

    Mirrors ``contracts/changelog-store.md`` → ``events/<ts>.parquet`` columns.
    ``value`` is a nullable Utf8 (a genuine null / deletion marker is a real
    null, decoded back to its type via ``dtype``). ``timestamp`` is UTC.
    """
    return {
        "entity_id": pl.Utf8,
        "timestamp": pl.Datetime("us", "UTC"),
        "field": pl.Utf8,
        "value": pl.Utf8,  # nullable; decoded via `dtype`
        "dtype": pl.Utf8,
        "snapshot_id": pl.Utf8,
    }


# --------------------------------------------------------------------------- #
# Row-hash prefilter (T012) + content-derived snapshot id (T013)             #
# --------------------------------------------------------------------------- #
def _non_key_columns(df: pl.DataFrame, key_column: str) -> list[str]:
    """Non-key columns in a stable (sorted) order — the canonicalization for hashing."""
    return sorted(c for c in df.columns if c != key_column)


def hash_rows(df: pl.DataFrame, key_column: str) -> pl.Series:
    """Per-row content hash over the canonicalized non-key columns (T012 / R1, R2).

    Used as a cheap prefilter: a row whose non-key hash is unchanged between the
    prior and new frame cannot have any changed cell, so it never reaches the
    (expensive) melt+compare. Column order is stabilized (sorted) so the hash is
    canonical regardless of input column order.
    """
    non_key = _non_key_columns(df, key_column)
    if not non_key:
        # Key-only table: no content to hash; all rows hash identically.
        return pl.Series("row_hash", [0] * df.height, dtype=pl.UInt64)
    return df.select(non_key).hash_rows().alias("row_hash")


def snapshot_id(df: pl.DataFrame, key_column: str) -> str:
    """Content-derived id of a snapshot (T013 / R3).

    Deterministic across processes for identical content: rows are canonicalized
    (sorted by key, columns sorted) and folded — via per-row hashes — into a
    sha256 digest. Re-capturing the same snapshot yields the same id, which the
    capture idempotency anti-join uses to recognise an already-recorded batch.
    """
    cols = [key_column] + _non_key_columns(df, key_column)
    canon = df.select(cols).sort(key_column)
    h = hashlib.sha256()
    for v in canon.hash_rows().to_list():
        h.update(int(v).to_bytes(8, "little", signed=False))
    return h.hexdigest()[:16]


class ChangeLogStore:
    """Append-only store backed by a ``<name>.flux/`` folder.

    Glob-readable (``SELECT * FROM '<name>.flux/events/*.parquet'``); the
    ``manifest.json`` is the authoritative commit point. See the store contract.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.events_dir = self.path / EVENTS_DIR
        self.manifest_path = self.path / MANIFEST_NAME

    # --- manifest I/O (T007) ------------------------------------------------ #
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
        """Atomically write the manifest (temp → fsync → rename) — the commit point."""
        self.path.mkdir(parents=True, exist_ok=True)
        tmp = self.manifest_path.with_suffix(".json.tmp")
        data = json.dumps(manifest, indent=2, sort_keys=False).encode("utf-8")
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.manifest_path)

    # --- atomic events writer (T008) --------------------------------------- #
    def _events_filename(self, stamp: datetime) -> str:
        """A UTC, lexically-sortable events filename (so a plain glob sorts by time)."""
        base = to_utc(stamp).strftime("%Y%m%dT%H%M%S%fZ")
        name = f"{base}.parquet"
        # Guarantee uniqueness if two captures land in the same microsecond.
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

        Protocol (crash-safe): write ``events/.<ts>.parquet.tmp`` → fsync → atomic
        rename to ``events/<ts>.parquet`` → recompute manifest in memory → atomic
        manifest rewrite. A crash before the manifest rewrite leaves an orphan
        parquet that readers ignore (STORE-4). Row-group ``min/max(timestamp)``
        stats are written (STORE-5) and the per-file ts range is stamped into the
        manifest entry (R5 file-skip).
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
        # Append-only schema UNION (not overwrite): a column dropped from a later
        # snapshot must still resolve in historical as-of views, since its events
        # remain in history. Latest dtype tag wins if a column is re-seen; the
        # first-seen column order is preserved. (Overwriting here erased dropped
        # columns from every past view — silent time-travel corruption.)
        merged_schema = dict(manifest.get("schema") or {})
        merged_schema.update(schema)
        manifest["schema"] = merged_schema
        manifest["events"].append(entry)
        self.write_manifest(manifest)
        return entry

    # --- file listing / pruning (T009) ------------------------------------- #
    def list_events(self, as_of: Optional[datetime] = None, window=None) -> list[Path]:
        """Return manifest-valid event files (chronological), with file-skip pruning (R5).

        Only files listed in the manifest are considered (orphan parquet ignored,
        STORE-4). A file is pruned when its ``[ts_min, ts_max]`` range cannot
        contain any event in scope:

        - ``as_of`` (a UTC datetime): drop files whose ``ts_min`` is strictly after
          ``as_of`` (they lie entirely in the future relative to the query).
        - ``window`` (``(lo, hi)``, either bound optional): drop files whose range
          lies entirely outside ``[lo, hi]``.
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
                continue  # file entirely after the as-of point
            if lo is not None and ts_max < lo:
                continue  # file entirely before the window
            if hi is not None and ts_min > hi:
                continue  # file entirely after the window
            kept.append(self.path / entry["file"])
        return kept

    # --- store read-back: current state (generalized by reconstruct T020) -- #
    def _read_all_events(self, as_of: Optional[datetime] = None) -> pl.DataFrame:
        """Concatenate all manifest-valid event files (with file-skip pruning)."""
        files = self.list_events(as_of=as_of)
        if not files:
            return pl.DataFrame(schema=change_event_schema())
        return pl.concat([pl.read_parquet(f) for f in files], how="vertical")

    def _materialize_current(
        self, pl_schema: dict, key_column: str, as_of: Optional[datetime] = None
    ) -> Optional[pl.DataFrame]:
        """Reconstruct the live wide table state (latest value per cell, typed).

        Returns ``None`` for an empty store. Entities whose most recent lifecycle
        event is a ``__deleted__`` marker are excluded (US3 refines the resurrection
        chain in T024; with no deletions this is a no-op). Used by ``capture`` to
        obtain the prior frame to diff; ``reconstruct.build_mirror_view`` (T020)
        generalizes this with arbitrary ``as_of``.
        """
        events = self._read_all_events(as_of=as_of)
        if events.is_empty():
            return None
        if as_of is not None:
            events = events.filter(pl.col("timestamp") <= to_utc(as_of))
        if events.is_empty():
            return None

        # Latest event per (entity_id, field) by timestamp.
        latest = (
            events.sort("timestamp")
            .group_by(["entity_id", "field"], maintain_order=True)
            .last()
        )

        # Drop entities currently deleted (latest event of the entity is __deleted__).
        entity_last = (
            events.sort("timestamp").group_by("entity_id", maintain_order=True).last()
        )
        deleted_ids = entity_last.filter(pl.col("field") == DELETED_FIELD)["entity_id"].to_list()
        latest = latest.filter(
            (pl.col("field") != DELETED_FIELD) & (~pl.col("entity_id").is_in(deleted_ids))
        )
        if latest.is_empty():
            return None

        fields = [c for c in pl_schema if c != key_column]
        wide = latest.pivot(
            on="field", index="entity_id", values="value", aggregate_function="first"
        )
        for f in fields:
            if f not in wide.columns:
                wide = wide.with_columns(pl.lit(None, dtype=pl.Utf8).alias(f))

        # Decode entity_id → key dtype and each field → its dtype.
        key_dtype = pl_schema[key_column]
        cols = [
            wide["entity_id"]
            .map_elements(
                lambda v, _t=dtype_tag(key_dtype): decode_value(v, _t), return_dtype=key_dtype
            )
            .alias(key_column)
        ]
        for f in fields:
            pdt = pl_schema[f]
            cols.append(
                wide[f]
                .map_elements(
                    lambda v, _t=dtype_tag(pdt): decode_value(v, _t), return_dtype=pdt
                )
                .alias(f)
            )
        return pl.DataFrame(cols).select([key_column, *fields])

    def _schema_tags(self, df: pl.DataFrame, key_column: str) -> dict:
        """Full column → dtype-tag map (INCLUDING the key) stamped into the manifest,
        so a reader can rebuild the typed schema without the original frame (T020)."""
        return {c: dtype_tag(df.schema[c]) for c in df.columns}

    # --- keyed diff (T014) -------------------------------------------------- #
    def _diff(
        self,
        prev: Optional[pl.DataFrame],
        new: pl.DataFrame,
        key_column: str,
        *,
        timestamp: datetime,
        snap_id: str,
    ) -> pl.DataFrame:
        """Keyed full-outer diff → INSERT/UPDATE Change Events (T014 / R1).

        Row-hash prefilter (T012) gates the expensive melt so only the rows that
        actually changed (or exist on a single side) are unpivoted — keeping the
        work proportional to the *changed* rows, not rows × columns. Per-cell
        comparison is null-safe (Polars ``ne_missing``), so ``None``-vs-``None``
        is not a change and a value→null transition IS recorded.

        Emits INSERT (new key, incl. resurrection), UPDATE (changed cell), and
        DELETE (one ``__deleted__`` marker per vanished entity) events (T014 + T023).
        """
        timestamp = to_utc(timestamp)
        fields = _non_key_columns(new, key_column)
        dtypes = {c: dtype_tag(new.schema[c]) for c in fields}

        new_keys = set(new[key_column].to_list())
        prev_keys = (
            set() if prev is None or prev.is_empty() else set(prev[key_column].to_list())
        )
        insert_keys = new_keys - prev_keys
        common_keys = new_keys & prev_keys
        delete_keys = prev_keys - new_keys  # in prior LIVE state, gone from snapshot

        long_parts: list[pl.DataFrame] = []

        # --- INSERTs (incl. RESURRECTION): non-null cells of a (re)appearing row.
        # `prev` is the live state (deleted entities excluded by _materialize_current),
        # so a previously-deleted id reappearing is a new key here → SET events under
        # the SAME entity_id, giving a continuous trail (U1 / resurrection).
        if insert_keys:
            ins = new.filter(pl.col(key_column).is_in(list(insert_keys)))
            ins_long = self._encode_long(ins, key_column, dtypes).filter(
                pl.col("value").is_not_null()
            )
            long_parts.append(ins_long)

        # --- DELETEs: ONE __deleted__ marker row per vanished entity (U1), not a
        # null row per column. value=None / dtype="null" (set in the select below).
        if delete_keys:
            del_long = pl.DataFrame(
                {"entity_id": [str(k) for k in sorted(delete_keys)]}
            ).with_columns(
                pl.lit(DELETED_FIELD).alias("field"),
                pl.lit(None, dtype=pl.Utf8).alias("value"),
            )
            long_parts.append(del_long.select("entity_id", "field", "value"))

        # --- UPDATEs: prefilter common keys by row-hash, melt only changed ---
        if common_keys:
            common = sorted(common_keys)
            ph = prev.filter(pl.col(key_column).is_in(common)).sort(key_column)
            nh = new.filter(pl.col(key_column).is_in(common)).sort(key_column)
            changed_mask = hash_rows(ph, key_column) != hash_rows(nh, key_column)
            if changed_mask.any():
                new_changed = nh.filter(changed_mask)
                prev_changed = ph.filter(changed_mask)
                new_long = self._encode_long(new_changed, key_column, dtypes)
                prev_long = self._encode_long(prev_changed, key_column, dtypes)
                merged = new_long.join(
                    prev_long, on=["entity_id", "field"], how="left", suffix="_old"
                )
                upd_long = merged.filter(
                    pl.col("value").ne_missing(pl.col("value_old"))  # null-safe inequality
                ).select("entity_id", "field", "value")
                long_parts.append(upd_long)

        if not long_parts:
            return pl.DataFrame(schema=change_event_schema())

        combined = pl.concat(long_parts, how="vertical")
        events = combined.select(
            pl.col("entity_id"),
            pl.lit(timestamp).cast(pl.Datetime("us", "UTC")).alias("timestamp"),
            pl.col("field"),
            pl.col("value").cast(pl.Utf8),
            # __deleted__ marker → dtype "null"; tracked fields → their tag.
            pl.col("field")
            .replace_strict(dtypes, default=NULL_DTYPE, return_dtype=pl.Utf8)
            .alias("dtype"),
            pl.lit(snap_id).alias("snapshot_id"),
        )
        return events.cast(change_event_schema())

    @staticmethod
    def _encode_long(df: pl.DataFrame, key_column: str, dtypes: dict) -> pl.DataFrame:
        """Encode non-key cells to their string form and unpivot to (entity_id, field, value)."""
        encoded = [
            pl.col(c)
            .map_elements(lambda v, _t=dtypes[c]: encode_value(v, _t), return_dtype=pl.Utf8)
            .alias(c)
            for c in dtypes
        ]
        wide = df.select(
            [pl.col(key_column).cast(pl.Utf8).alias("entity_id"), *encoded]
        )
        return wide.unpivot(
            index="entity_id", on=list(dtypes), variable_name="field", value_name="value"
        )

    # --- capture (T015) ----------------------------------------------------- #
    def capture(
        self, df: pl.DataFrame, key_column: str, captured_at: Optional[datetime] = None
    ) -> dict:
        """Diff ``df`` against current state and append one immutable events file (T015).

        Idempotent: re-capturing an already-recorded snapshot (same content-derived
        ``snapshot_id``) is a no-op, as is a capture with no detected changes. On a
        real change, exactly one new ``events/*.parquet`` is appended and the
        manifest committed atomically (FR-002, FR-004).
        """
        if key_column not in df.columns:
            raise ValueError(f"key_column {key_column!r} not in frame columns {df.columns}")

        # A snapshot is a keyed wide table: each entity must appear at most once.
        # Duplicate keys silently emit conflicting events for the same cell at the
        # same timestamp, and the reconstructed value would depend on row order.
        if df.height and df[key_column].is_duplicated().any():
            dupes = df[key_column].filter(df[key_column].is_duplicated()).unique().to_list()
            raise ValueError(
                f"key_column {key_column!r} has duplicate values {sorted(dupes)[:10]} "
                f"({len(dupes)} distinct) — each entity must appear at most once per snapshot"
            )

        captured_at = to_utc(captured_at or datetime.now(timezone.utc))
        snap = snapshot_id(df, key_column)

        manifest = self.read_manifest()
        # Idempotency anti-join at snapshot granularity: this exact snapshot is
        # already committed → nothing to do.
        if any(e["snapshot_id"] == snap for e in manifest["events"]):
            return {"events_added": 0, "snapshot_id": snap, "noop": True}

        prev = self._materialize_current(df.schema, key_column)
        events = self._diff(prev, df, key_column, timestamp=captured_at, snap_id=snap)
        if events.is_empty():
            return {"events_added": 0, "snapshot_id": snap, "noop": True}

        entry = self._append_events(
            events, snap, key_column, self._schema_tags(df, key_column), stamp=captured_at
        )
        return {
            "events_added": events.height,
            "snapshot_id": snap,
            "file": entry["file"],
            "noop": False,
        }
