"""Storage-layer types + the `StorageBackend` protocol (feature 003).

This module is the seam between `changelog.ChangeLogStore` (the engine) and
persistence. It defines the protocol + the small value types every backend
speaks; the concrete change-event schema, idempotency, and reconstruction stay
invariant (feature 001). Contract: `specs/003-pluggable-storage-backends/
contracts/storage-backend.md` + `.../tables.md`.

Wave-2 (T004): full `StorageBackend` Protocol per the contract's behavioral
rules (SB-1..SB-8). Meta/EventRef fields are left as Wave-1 scaffolded them
(Wave 3 / T005 refines them further); they already satisfy this Protocol.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional, Protocol, runtime_checkable

import polars as pl

__all__ = [
    "StorageBackend",
    "Capabilities",
    "Meta",
    "MirrorPolicy",
    "EventRef",
    "Predicate",
    "CHANGE_EVENT_COLUMNS",
    "DELETED_FIELD",
    "NULL_DTYPE",
]

# A predicate `read_events` MAY push down (e.g. a `pl.col("timestamp") <= T`
# expression enables ts/row-group pruning, SB-8 `supports_time_pushdown`). A
# backend without pushdown support MAY simply read-then-filter with it.
Predicate = pl.Expr

# --------------------------------------------------------------------------- #
# Change-Event schema — INVARIANT (feature 001). Every backend reads/writes    #
# rows in exactly this shape; only the physical container varies (files vs     #
# table). These mirror `changelog.py` (DELETED_FIELD / NULL_DTYPE) and are      #
# restated here as the storage-layer's canonical view so backends don't import #
# the engine (avoids a base<-changelog cycle). data-model.md §"Change Event".   #
# --------------------------------------------------------------------------- #
CHANGE_EVENT_COLUMNS: tuple[str, ...] = (
    "entity_id",
    "timestamp",
    "field",
    "value",
    "dtype",
    "snapshot_id",
)
# `field == DELETED_FIELD` marks a deletion row; `dtype == NULL_DTYPE` + value None
# distinguishes a deletion/null marker from a genuine typed null.
DELETED_FIELD = "__deleted__"
NULL_DTYPE = "null"


@dataclass(frozen=True)
class Capabilities:
    """Feature flags a backend advertises so the engine adapts without isinstance."""

    is_table: bool = False
    supports_atomic_meta: bool = True
    supports_time_pushdown: bool = False
    supports_mirror: bool = False


@dataclass
class MirrorPolicy:
    """Policy for the optional materialized `flux_mirror` (data-model.md §flux_mirror).

    Physical form varies by backend; this is the canonical logical shape a
    backend serializes into `Meta.mirror`.
    """

    enabled: bool = False
    # "on_demand" (default) | "cadence" | "off". Clarify Q2 (2026-07-01): opt-in;
    # on-demand default plus an optional cadence/staleness-threshold mode.
    refresh_mode: str = "on_demand"
    cadence_n: Optional[int] = None      # refresh every N captures (cadence mode)
    watermark: Optional[str] = None      # last-materialized snapshot_id / capture marker (staleness)

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "refresh_mode": self.refresh_mode,
            "cadence_n": self.cadence_n,
            "watermark": self.watermark,
        }

    @classmethod
    def from_dict(cls, d: Optional[dict[str, Any]]) -> "MirrorPolicy":
        d = d or {}
        return cls(
            enabled=bool(d.get("enabled", False)),
            refresh_mode=str(d.get("refresh_mode", "on_demand")),
            cadence_n=d.get("cadence_n"),
            watermark=d.get("watermark"),
        )


@dataclass
class Meta:
    """The authoritative store descriptor a reader trusts (FR-006, data-model.md §Meta).

    Supersedes/replaces `manifest.json` where a real table exists (M-1/M-2). Its
    physical form varies by backend: `manifest.json` (local/object, atomic PUT),
    a `_flux_meta.json` companion (Parquet TableBackend), or table properties
    (Delta/Iceberg) — but the logical fields are the same everywhere.
    """

    key_column: str = ""
    # schema union, column -> dtype tag (carries the 002 add/drop/rename churn).
    schema: dict[str, str] = field(default_factory=dict)
    # valid event units + ts ranges + snapshot_ids (folder/object backends; a
    # Delta/Iceberg transaction log supplies this instead).
    event_catalog: list[dict[str, Any]] = field(default_factory=list)
    # `flux_mirror` policy (see MirrorPolicy). Stored as a plain dict for backend
    # serialization; use MirrorPolicy.from_dict(meta.mirror) for typed access.
    mirror: dict[str, Any] = field(default_factory=dict)
    schema_version: int = 1


@dataclass(frozen=True)
class EventRef:
    """Handle for one committed capture unit (for logging/idempotency, not reconstruction)."""

    snapshot_id: str
    ref: str = ""
    row_count: int = 0
    ts_min: Optional[datetime] = None
    ts_max: Optional[datetime] = None


@runtime_checkable
class StorageBackend(Protocol):
    """Persistence seam between `changelog.ChangeLogStore` (the engine) and storage.

    Any backend/sidecar implementing this Protocol MUST honor the behavioral
    contract in `specs/003-pluggable-storage-backends/contracts/storage-backend.md`
    (SB-1..SB-8), summarized per-method below. `capabilities` lets the engine
    adapt to what a backend supports without `isinstance` checks (SB-8).

    SB-7: a `StorageBackend` implementation MUST NOT import platform SDKs —
    platform-specific backends live in `sidecars/` as opt-in extras (G8).
    """

    capabilities: Capabilities

    def read_meta(self) -> Meta:
        """Load the store descriptor (schema union, key column, event catalog, mirror policy)."""
        ...

    def write_meta(self, meta: Meta) -> None:
        """Persist the descriptor.

        MUST commit atomically — a single-object PUT / transaction, never a
        multi-step rename (SB-4). This is the commit point of a capture (M-3).
        """
        ...

    def read_current_state(self) -> pl.DataFrame:
        """Return the reconstructed latest mirror (input to the keyed diff on capture)."""
        ...

    def read_events(self, predicate: Optional[Predicate] = None) -> pl.DataFrame:
        """Return Change-Event rows for reconstruction.

        Rows MUST be in the invariant Change-Event schema — `entity_id`,
        `timestamp`, `field`, `value`, `dtype`, `snapshot_id` — with values
        encoded as text + a `dtype` tag for lossless re-cast (SB-5).
        Reconstruction over these rows MUST be byte-identical across all
        backends for the same logical history (SB-6). `predicate` is an
        optional pushdown filter (e.g. a timestamp bound); a backend without
        `capabilities.supports_time_pushdown` MAY read-then-filter with it.
        """
        ...

    def append_events(self, events: pl.DataFrame) -> EventRef:
        """Persist one capture's Change Events and return a handle to the committed unit.

        MUST be atomic: an interrupted/partial write MUST NOT be visible on
        the next `read_*` call (SB-2). MUST be idempotent by `snapshot_id`: if
        the events' `snapshot_id` is already committed, this is a no-op
        (SB-3). Committed units are immutable and are never rewritten by a
        later call (SB-1, G4).
        """
        ...

    def refresh_mirror(self, at: Optional[datetime] = None) -> None:
        """Recompute the optional materialized `flux_mirror` (FM-1/FM-2).

        Only meaningful when `capabilities.supports_mirror` is True; callers
        MUST check the flag before invoking this (SB-8) rather than relying on
        it to raise. A backend that doesn't support a mirror simply advertises
        `supports_mirror=False` and never needs this method called.
        """
        ...
