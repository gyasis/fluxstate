# Contract: StorageBackend interface + selection

The seam between `ChangeLogStore` (the engine) and persistence. Any backend/sidecar MUST satisfy this.

## Protocol (`storage/base.py`)

```python
class StorageBackend(Protocol):
    capabilities: Capabilities                      # is_table, supports_atomic_meta,
                                                    # supports_time_pushdown, supports_mirror
    def read_meta(self) -> Meta: ...
    def write_meta(self, meta: Meta) -> None: ...                       # MUST be atomic
    def read_current_state(self) -> pl.DataFrame: ...                   # reconstructed latest mirror
    def read_events(self, predicate: Predicate | None = None) -> pl.DataFrame: ...
    def append_events(self, events: pl.DataFrame) -> EventRef: ...      # atomic + idempotent
    # optional (guarded by capabilities.supports_mirror):
    def refresh_mirror(self, at: datetime | None = None) -> None: ...
```

## Behavioral contract (MUST)

| ID | Rule |
|---|---|
| SB-1 | `append_events` persists changed cells as **one immutable unit**; existing units are **never rewritten** (G4). |
| SB-2 | A capture is **atomic**: interrupted/partial writes MUST NOT be visible on the next `read_*` (FR-013). |
| SB-3 | `append_events` is **idempotent**: an already-present `snapshot_id` (content hash) MUST be a no-op (FR-009). |
| SB-4 | `write_meta` MUST commit atomically — single-object PUT / transaction, **never** a multi-step rename (R3). |
| SB-5 | `read_events` MUST return rows in the invariant Change-Event schema; values encoded as text + `dtype` (type fidelity, FR-010). |
| SB-6 | Reconstruction over `read_events` MUST be **byte-identical across all backends** for the same logical history (FR-011). |
| SB-7 | A backend MUST NOT import platform SDKs into core; platform backends live in `sidecars/` as extras (G8). |
| SB-8 | Unsupported optional ops MUST be advertised via `capabilities` (e.g. `supports_mirror=False`), not raise on probe. |

## Selection contract

`select_backend(location, *, store=None, backend=None) -> StorageBackend`

| Input | Resolves to |
|---|---|
| explicit `store=`/`backend=` object | that backend (highest precedence) |
| plain path / `file://…` | `LocalFolderStore` (**default**) |
| `s3://…` · `abfss://…` · `gs://…` | `ObjectStoreBackend` (`[remote]`) |
| `table://…` or a `TableBackend(...)` object | `TableBackend` (`[table]`) |

- Existing `FluxState(df, key_column, store_path=<local path>)` calls resolve to `LocalFolderStore`
  **unchanged** (G3). `store=`/`backend=` is an **additive** optional parameter.
- Selecting a backend whose extra isn't installed MUST raise a clear, actionable error naming the extra
  (e.g. `pip install "fluxstate[table]"`).
