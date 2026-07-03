"""Optional platform sidecars for FluxState (feature 003).

Each subpackage wires the `storage.StorageBackend` protocol to one platform's
native store (Delta table, Snowflake stage, Postgres/Supabase table, …) and
ships as an OPTIONAL extra (e.g. `pip install "fluxstate[databricks]"`).

Constitution Principle I / G8: platform code lives ONLY here — the core never
imports a sidecar, and a sidecar never re-implements the change-log or
reconstruction. Databricks is the first sidecar, not the design center.
"""
