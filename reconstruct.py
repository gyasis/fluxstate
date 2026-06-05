# File: reconstruct.py
"""Reader/reconstruction primitives over the ``<name>.flux/`` change log.

This module is the *reader* counterpart to ``changelog.py``. It rebuilds
table state as-of any point in time, per-cell timelines, row lifecycle
state, and the materialized mirror view — all over a Polars LazyFrame on
the manifest-valid events files (streaming-ready, internal).

Each primitive is re-exported as a thin ``FluxState`` method (U2) so both
``fs.get_timeline(...)`` and ``reconstruct.get_timeline(store, ...)`` work.
See ``specs/001-changelog-first-pivot/contracts/public-api.md``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

import polars as pl

from changelog import DELETED_FIELD, ChangeLogStore, decode_value, tag_to_dtype, to_utc


def _resolve_T(T: str | datetime | None) -> Optional[datetime]:
    """Normalize a time argument to a UTC datetime, or ``None`` for 'now'/current."""
    if T is None or (isinstance(T, str) and T.strip().lower() == "now"):
        return None
    if isinstance(T, str):
        T = datetime.fromisoformat(T)
    return to_utc(T)


def _typed_schema(store: ChangeLogStore) -> tuple[Optional[str], dict]:
    """(key_column, {column: pl.DataType}) reconstructed from the manifest tags."""
    manifest = store.read_manifest()
    key_column = manifest.get("key_column") or None
    schema = {col: tag_to_dtype(tag) for col, tag in manifest.get("schema", {}).items()}
    return key_column, schema


def _scan(store: ChangeLogStore, as_of: Optional[datetime] = None) -> Optional[pl.LazyFrame]:
    """LazyFrame over the manifest-valid event files (with file-skip pruning), or None."""
    files = store.list_events(as_of=as_of)
    if not files:
        return None
    lf = pl.scan_parquet(files)
    if as_of is not None:
        lf = lf.filter(pl.col("timestamp") <= to_utc(as_of))
    return lf


def _entity_series(
    store: ChangeLogStore,
    entity_id: Any,
    field: Optional[str] = None,
    as_of: Optional[datetime] = None,
) -> pl.DataFrame:
    """Collected, time-ordered change events for one entity (optionally one field)."""
    lf = _scan(store, as_of=as_of)
    if lf is None:
        return pl.DataFrame(schema={"timestamp": pl.Datetime("us", "UTC"),
                                    "field": pl.Utf8, "value": pl.Utf8, "dtype": pl.Utf8})
    lf = lf.filter(pl.col("entity_id") == str(entity_id))
    if field is not None:
        lf = lf.filter(pl.col("field") == field)
    return lf.select("timestamp", "field", "value", "dtype").sort("timestamp").collect()


# --------------------------------------------------------------------------- #
# Series-level + public resolvers (T019)                                      #
# --------------------------------------------------------------------------- #
def _as_of(history: list[dict], T: datetime) -> Optional[dict]:
    """Series-level resolver (I2): latest ``{date, value}`` at or before ``T``.

    ``history`` is an ascending-by-date list for ONE cell. Distinct from the
    public :func:`as_of`, which first looks up the series then applies this.
    """
    T = to_utc(T)
    result = None
    for entry in history:
        if to_utc(entry["date"]) <= T:
            result = entry
        else:
            break
    return result


def as_of(store: ChangeLogStore, entity_id: Any, field: str, T: datetime) -> Optional[dict]:
    """Public resolver: the value of ``(entity_id, field)`` as of ``T`` (decoded), or None."""
    df = _entity_series(store, entity_id, field, as_of=T)
    if df.is_empty():
        return None
    last = df.row(df.height - 1, named=True)
    return {"date": last["timestamp"], "value": decode_value(last["value"], last["dtype"])}


def get_timeline(
    store: ChangeLogStore, entity_id: Any, field: Optional[str] = None
) -> list[dict]:
    """Per-cell timeline rebuilt lazily from change events (values decoded to dtype).

    With ``field`` set → ``[{date, value}]`` for that cell. With ``field=None`` →
    every change for the entity as ``[{date, field, value}]`` (field key added).
    """
    df = _entity_series(store, entity_id, field)
    out: list[dict] = []
    for r in df.iter_rows(named=True):
        item = {"date": r["timestamp"], "value": decode_value(r["value"], r["dtype"])}
        if field is None:
            item["field"] = r["field"]
        out.append(item)
    return out


def change_count(store: ChangeLogStore, entity_id: Any) -> int:
    """Total number of change events recorded for an entity."""
    return _entity_series(store, entity_id).height


# --------------------------------------------------------------------------- #
# Materialized mirror view (T020)                                             #
# --------------------------------------------------------------------------- #
def build_mirror_view(store: ChangeLogStore, T: str | datetime = "now") -> pl.DataFrame:
    """Reconstruct the full table state as-of ``T`` into a typed ``pl.DataFrame``.

    For each live ``entity_id`` the latest value of each ``field`` at/before ``T``
    is resolved and cast back to its recorded dtype (API-4). Deleted rows are
    omitted; ``T`` before any history yields an empty (not erroring) frame with
    the table's columns. ``list_events`` file-skip pruning bounds the scan (R5).
    """
    key_column, pl_schema = _typed_schema(store)
    as_of = _resolve_T(T)

    if key_column is None or not pl_schema:
        return pl.DataFrame()  # store has no committed schema yet

    view = store._materialize_current(pl_schema, key_column, as_of=as_of)
    if view is None:
        # No state at/before T → empty frame, but keep the table's columns/types.
        return pl.DataFrame(schema=pl_schema)
    return view


# --------------------------------------------------------------------------- #
# Lifecycle state (T024)                                                       #
# --------------------------------------------------------------------------- #
def row_state(store: ChangeLogStore, entity_id: Any, T: str | datetime = "now") -> dict:
    """Return ``{state: active|deleted|unborn, resurrected: bool}`` at ``T`` (FR-005).

    Reads the lifecycle chain (``__deleted__`` markers interleaved with SET events):

    - **unborn** — no events for the entity at/before ``T``.
    - **deleted** — the most recent event at/before ``T`` is a ``__deleted__`` marker.
    - **active** — otherwise.

    ``resurrected`` is True when a deletion was followed by a later SET in the
    chain (a continuous ``active → deleted → active`` trail under one entity_id).
    """
    df = _entity_series(store, entity_id, as_of=_resolve_T(T))
    if df.is_empty():
        return {"state": "unborn", "resurrected": False}

    resurrected = False
    deleted_pending = False
    last_field = None
    for r in df.iter_rows(named=True):
        last_field = r["field"]
        if r["field"] == DELETED_FIELD:
            deleted_pending = True
        else:
            if deleted_pending:
                resurrected = True
            deleted_pending = False

    state = "deleted" if last_field == DELETED_FIELD else "active"
    return {"state": state, "resurrected": resurrected}
