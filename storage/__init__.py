"""Pluggable storage backends for the FluxState change-log (feature 003).

The change-log engine (`changelog.ChangeLogStore`) persists through the
`StorageBackend` protocol defined in `storage.base`, so *where/how* bytes are
stored is swappable while the change-event model and reconstruction stay
platform-agnostic (Constitution Principle I / G8).

Public surface (filled in by feature 003):
  - `StorageBackend`, `Capabilities`, `Meta`, `EventRef`  (from `.base`)
  - `select_backend(location, *, store=None, backend=None)`
  - `LocalFolderStore`, `ObjectStoreBackend`, `TableBackend`

Platform integrations (Databricks, …) live in the sibling `sidecars/` package
as optional extras and are never imported here.
"""

from __future__ import annotations

from typing import Optional

from .base import Capabilities, EventRef, Meta, MirrorPolicy, StorageBackend
from .local_folder import LocalFolderStore
from .object_store import ObjectStoreBackend
from .table import TableBackend

__all__ = [
    "StorageBackend",
    "Capabilities",
    "Meta",
    "MirrorPolicy",
    "EventRef",
    "select_backend",
    "LocalFolderStore",
    "ObjectStoreBackend",
    "TableBackend",
]


def select_backend(
    location: str,
    *,
    store: Optional[StorageBackend] = None,
    backend: Optional[StorageBackend] = None,
) -> StorageBackend:
    """Resolve a `location` (or an explicit object) to a `StorageBackend` (contract §Selection).

    Precedence:
      1. an explicit `store=`/`backend=` object — returned as-is (highest precedence).
      2. `location` scheme dispatch:
         - `table://…` -> `TableBackend` (`storage.table`, extra `[table]`)
         - `s3://…` / `abfss://…` / `gs://…` -> `ObjectStoreBackend` (`storage.object_store`, extra `[remote]`)
         - a plain path / `file://…` -> `LocalFolderStore` (`storage.local_folder`, the **default**; G3)

    Backend classes are imported lazily (inside this function) so that
    importing `storage` never requires an extra's dependencies, and so this
    function works today even though `LocalFolderStore` / `ObjectStoreBackend`
    / `TableBackend` haven't landed yet (they arrive in T008/T012/T018).
    Selecting a scheme whose backend/extra isn't available yet raises a clear,
    actionable error naming the extra to install.
    """
    if store is not None and backend is not None and store is not backend:
        raise ValueError("select_backend: pass only one of `store=`/`backend=`, not both")
    explicit = store if store is not None else backend
    if explicit is not None:
        return explicit

    scheme = location.split("://", 1)[0].lower() if "://" in location else ""

    if scheme == "table":
        try:
            from storage.table import TableBackend
        except ImportError as exc:  # pragma: no cover - exercised once T018 lands
            raise ImportError(
                "table:// locations require the TableBackend, which is not yet "
                "available. Install it with: pip install \"fluxstate[table]\""
            ) from exc
        return TableBackend(location)

    if scheme in ("s3", "abfss", "gs"):
        try:
            from storage.object_store import ObjectStoreBackend
        except ImportError as exc:  # pragma: no cover - exercised once T012 lands
            raise ImportError(
                f"{scheme}:// locations require the ObjectStoreBackend, which is "
                'not yet available. Install it with: pip install "fluxstate[remote]"'
            ) from exc
        return ObjectStoreBackend(location)

    # Default: plain path or `file://` -> LocalFolderStore (G3).
    try:
        from storage.local_folder import LocalFolderStore
    except ImportError as exc:  # pragma: no cover - exercised once T008 lands
        raise NotImplementedError(
            "LocalFolderStore is not yet available in this build of fluxstate "
            "(it lands in a later wave of feature 003)."
        ) from exc
    path = location[len("file://") :] if scheme == "file" else location
    return LocalFolderStore(path)
