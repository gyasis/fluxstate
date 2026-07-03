"""Databricks sidecar for FluxState — the first platform integration (feature 003).

Provides `DeltaBackend` (a `TableBackend` bound to Delta `flux_events` + optional
`flux_mirror`) and a scheduled-Job/notebook capture template. Requires the
optional extra: `pip install "fluxstate[databricks]"`.

Populated by T021/T022 (Wave 7). Heavy deps (deltalake, databricks-sdk, runtime
pyspark) are confined to this extra — the core never imports them (G1/G8).
"""

__all__: list[str] = []  # "DeltaBackend" added in T021
