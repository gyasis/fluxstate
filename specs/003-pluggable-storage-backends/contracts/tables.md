# Contract: flux_events + flux_mirror + Meta

The tabular storage contract for the TableBackend (and the logical contract every backend honors).

## flux_events (ALWAYS present)

The change-log as an append-only table. **Logical schema (invariant across backends):**

| Column | Type (logical) | Notes |
|---|---|---|
| `entity_id` | text | the key value; matches a row across captures |
| `timestamp` | timestamp (UTC) | capture time, UTC-normalized |
| `field` | text | changed column name; `__deleted__` = deletion marker row |
| `value` | text \| null | canonical text encoding; `null` = genuine null OR deletion |
| `dtype` | text | type tag for lossless re-cast; `null` = deletion marker |
| `snapshot_id` | text | content-derived capture id (idempotency key) |

| ID | Rule |
|---|---|
| FE-1 | Append-only; a capture adds rows/a part/a transaction; **no rewrite** of prior data (G4). |
| FE-2 | Re-capturing an existing `snapshot_id` adds **nothing** (idempotent). |
| FE-3 | Physical form: Parquet dataset (default) / Delta / Iceberg — all **open, glob- or table-readable** (G2). |
| FE-4 | Directly queryable as a table (`SELECT * FROM flux_events`) with no FluxState code. |

## flux_mirror (OPTIONAL, materialized)

The reconstructed **wide** state — one row per live entity.

| Property | Contract |
|---|---|
| Presence | opt-in (off by default) |
| Columns | key column first, then tracked fields in schema order; typed per `dtype` |
| Content | equals `reconstruct.build_mirror_view(flux_events @ T)` (current if `at` omitted) |
| Refresh | on-demand (default) OR cadence/staleness-threshold (optional); recorded in Meta |
| Authority | NOT the source of truth — a cache derived from `flux_events`; MAY lag under non-eager refresh |

| ID | Rule |
|---|---|
| FM-1 | `flux_mirror` MUST be reproducible byte-identically by reconstructing `flux_events` (no independent state). |
| FM-2 | With a non-eager refresh policy, staleness MUST be discoverable (watermark in Meta). |

## Meta (store descriptor)

| Field | Contract |
|---|---|
| `schema` | schema union `col→dtype`, carrying 002 add/drop/rename churn |
| `key_column` | the entity key |
| `event_catalog` | valid units + ts ranges + `snapshot_id`s (folder/object backends; supplied by the tx-log for Delta/Iceberg) |
| `mirror` | `{enabled, refresh_mode, cadence_n, watermark}` |
| `schema_version` | format version |

| ID | Rule |
|---|---|
| M-1 | Meta is authoritative; a reader trusts only Meta's catalog (an unlisted stray part is not history). |
| M-2 | Physical form: `manifest.json` (folder/object, atomic PUT) · `_flux_meta.json` companion (Parquet table) · table properties (Delta/Iceberg). |
| M-3 | Meta commit is the atomic **commit point** of a capture (SB-2/SB-4). |
