# Quickstart: Pluggable Storage Backends

How each backend is used once this feature lands. The **local path stays the default** — existing code is
unchanged.

## Install (opt-in extras — base stays lean)

```bash
pip install fluxstate                 # core: Polars + PyArrow only (unchanged)
pip install "fluxstate[remote]"       # + fsspec/s3fs/adlfs/gcsfs  → ObjectStoreBackend
pip install "fluxstate[table]"        # + deltalake/pyiceberg (Parquet needs neither) → TableBackend
pip install "fluxstate[databricks]"   # + Delta/Databricks sidecar
```

## 1. Local folder (default — unchanged, back-compat)

```python
from fluxstate import FluxState
FluxState(df, key_column="id", store_path="patients.flux").update_mirror_table()   # exactly as today
```

## 2. Object store (URI inference — no code change beyond the location)

```python
FluxState(df, key_column="id", store_path="s3://bucket/patients.flux").update_mirror_table()
# abfss://… and gs://… work the same; capture + reconstruct identical to local
```

## 3. First-class table (flux_events + optional flux_mirror)

```python
from fluxstate import FluxState
from fluxstate.storage import TableBackend

fs = FluxState(df, key_column="id", store=TableBackend(
    events="warehouse/patients/flux_events",   # Parquet dataset by default (zero new dep)
    mirror="warehouse/patients/flux_mirror",   # optional; opt-in
    mirror_refresh="on_demand",                # on_demand (default) | cadence:N | off
    format="parquet",                          # parquet (default) | delta | iceberg (opt-in)
))
fs.update_mirror_table()          # appends changed cells to flux_events (idempotent)
fs.refresh_mirror()               # materialize/refresh flux_mirror on demand
# query flux_events directly as a table (any engine):  SELECT * FROM 'warehouse/patients/flux_events/*.parquet'
```

## 4. Databricks sidecar (daily scheduled capture)

```python
# in a scheduled Databricks Job (fluxstate[databricks]); see docs/DATABRICKS.md
from fluxstate import FluxState
from fluxstate.sidecars.databricks import DeltaBackend

snapshot = pl.from_arrow(spark.table("cat.sch.my_view").toArrow())
FluxState(snapshot, key_column="id", store=DeltaBackend(
    events="cat.sch.flux_events", mirror="cat.sch.flux_mirror", mirror_refresh="on_demand",
)).update_mirror_table()          # appends to the Delta flux_events; no-op on unchanged days
```

## 5. Reconstruction is identical everywhere

```python
fs.travel("2026-06-01T00:00:00Z")     # same result regardless of backend (parity)
fs.get_timeline(entity_id=2, field="risk")
```

## Verify

```bash
uv run pytest TESTS/ -q                          # existing suite stays green (back-compat)
uv run pytest TESTS/test_storage_parity.py -q    # reconstruction parity across all backends
```

Success = SC-001 (≥3 targets, same calls) · SC-002 (0 regressions) · SC-003 (parity 100%) ·
SC-004 (base deps unchanged) · SC-005 (no-op unchanged days) · SC-006 (new platform = new sidecar only).
