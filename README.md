# FluxState

> **FluxState is a faithful recorder of what the source emits — not a semantic-equality engine.**

> Cell-level Change Data Capture (CDC) system with temporal versioning for healthcare data workflows

### Guiding principle

**FluxState is a faithful recorder of what the source emits, not a semantic-equality engine.**
It records what changed in the source's *serialized output*, verbatim. If a source emits
`1.1` one run and `1.10` the next, or shifts a timestamp's timezone representation, that **is**
a change and FluxState surfaces it — it does not canonicalize values to decide they're "really
equal." Every value is normalized to a canonical **text** form for storage/comparison, with a
per-column `dtype` tag carried alongside so a consumer can re-cast when it wants typed
sort/compare. This is by design: faithfulness over cleverness.

FluxState is a Python library that tracks every cell-level change in database tables with full historical versioning. Designed specifically for healthcare data pipelines at Herself Health, it maintains temporal "mirror tables" where each cell contains a complete audit trail.

## Core Concept

FluxState stores history as an **append-only change-log** in a lightweight
`<name>.flux/` folder. Each `update_mirror_table()` captures one snapshot by
diffing it against the prior state and appending **one immutable Parquet file of
only the changed cells** — no full-table rewrite, no per-cell JSON blobs. Any
historical state is *reconstructed* on read, with values restored to their
original types.

```text
<name>.flux/
├── manifest.json          # authoritative: schema + valid event files + per-file ts range
└── events/
    ├── 20260605T000000Z.parquet   # one capture's change events (immutable)
    └── 20260606T000000Z.parquet   # append = a NEW file; existing files never rewritten
```

Each change event is a row `(entity_id, timestamp, field, value, dtype, snapshot_id)`.
A deletion is a single `__deleted__` marker row (not one null per column), so a
record that is deleted and later re-inserted keeps **one continuous timeline**.

The store is plain Parquet — **glob-readable by any engine** with no FluxState code:

```sql
SELECT * FROM '<name>.flux/events/*.parquet';   -- DuckDB / DuckDB-WASM / Polars
```

## Why FluxState?

- **Healthcare Compliance**: HIPAA-compliant audit trails for patient data
- **Cost Tracking**: Monitor Snowflake compute costs by tracking data changes
- **Pipeline Debugging**: See exactly what changed, when, and why
- **Data Validation**: Verify transformations between dev/staging/prod environments
- **Time-Travel Queries**: Query data state at any point in history

## Features

- ✅ **Append-only change-log** — each capture appends one immutable Parquet file of only the changed cells
- ✅ **Idempotent capture** — re-capturing the same snapshot is a no-op
- ✅ **Time-travel reconstruction** with **type fidelity** — values restored to their original dtypes (not string-cast)
- ✅ **Delete + resurrection continuity** — one continuous timeline per `entity_id`
- ✅ **Glob-readable store** — plain Parquet; queryable by DuckDB / Polars with no FluxState code (no Delta/Iceberg)
- ✅ **Wide-table safe** — row-hash-prefiltered keyed diff stays O(changed rows)
- ✅ **Multi-format output** — Polars / Arrow (zero-copy) / Parquet / CSV
- ✅ **Lightweight** — Polars + PyArrow only; no heavy runtime dependency

## Quick Start

```python
import polars as pl
from fluxstate import FluxState

# Day 1: first snapshot → writes <name>.flux/events/<ts>.parquet + manifest.json
t1 = pl.DataFrame({"id": [1, 2, 3], "risk": [0.4, 0.7, 0.2], "status": ["A", "A", "B"]})
fs = FluxState(t1, key_column="id", store_path="patients.flux")
fs.update_mirror_table()

# Day 2: one cell changes; row 3 disappears (logged as a __deleted__ marker)
t2 = pl.DataFrame({"id": [1, 2], "risk": [0.4, 0.9], "status": ["A", "A"]})
fs = FluxState(t2, key_column="id", store_path="patients.flux")
fs.update_mirror_table()      # appends ONE new events file
fs.update_mirror_table()      # idempotent re-run → adds nothing

# Reconstruct, time-travel, inspect history (values come back typed)
current   = fs.save_mirror_table(output_format="polars")   # pl.DataFrame (or "arrow"/"parquet"/"csv")
as_of_day1 = fs.travel("2026-06-05T00:00:00Z")             # state as of a past point
timeline   = fs.get_timeline(entity_id=2, field="risk")    # [{date, value}, …]
state      = fs.row_state(entity_id=3, T="now")            # {"state": ..., "resurrected": ...}
```

## Change-Log API

| Method | Behavior |
|---|---|
| `update_mirror_table(captured_at=None)` | Capture the current snapshot: keyed join-diff vs prior state → append one immutable events file (idempotent). |
| `save_mirror_table(output_path_parquet=None, csv_path=None, *, output_format=None)` | Reconstructed current view. `output_format` ∈ `polars` / `arrow` / `parquet` / `csv`; legacy positional paths still write files. |
| `travel(date)` | Reconstructed table state **as of** `date` (UTC); before any history → empty (not an error). |
| `query_historical_value(query_date)` | `{entity_id: {column: value}}` as of `query_date`, values restored to original dtype. |
| `get_timeline(entity_id, field=None)` | Per-cell timeline `[{date, value}]`, typed. |
| `row_state(entity_id, T="now")` | `{state: active\|deleted\|unborn, resurrected: bool}` for the lifecycle chain. |
| `change_count(entity_id)` | Number of change events recorded for an entity. |

The reconstruction primitives also exist as module functions in `reconstruct.py`
(`as_of`, `get_timeline`, `row_state`, `build_mirror_view`, …) taking a
`ChangeLogStore` explicitly.

## `flux` CLI

Drive the whole change-log from the terminal (no code needed):

```bash
flux capture <store.flux> <input.parquet|csv> --key id [--at <ISO>]  # append-only capture
flux travel <store.flux> --as-of 2026-03-01T00:00:00Z               # reconstruct table as-of T (typed)
flux timeline <store.flux> <id> [--field risk]                       # per-cell history [{date,value}]
flux row-state <store.flux> <id> [--as-of now]                       # {state, resurrected}
flux view <store.flux> [--as-of T] [--format polars|arrow|parquet|csv] [--out …]
flux info <store.flux>                                               # schema, key, #events, ts range
flux gen-fixture <store.flux> --seed 42 [--violations 8]             # seeded demo/stress fixture
flux serve <store.flux>                                              # launch the Temporal Viewer over the store
```

Every subcommand is a thin wrapper over the library — output equals the function it calls. `--json` everywhere.

## Temporal Viewer ("Temporal Ghost")

An interactive, in-browser viewer over one tracked table (`flux serve <store.flux>` → `http://localhost:5173`).
Built in Svelte 5 + Vite + DuckDB-WASM (reads the `.flux/` parquet store directly in the browser); the JS
reconstruction is held identical to `reconstruct.py` by a parity test.

- **Time-travel** — scrub a date slider over the change-density histogram (event-snapped); every cell snaps to
  its as-of value; changed cells show a daff-style `old → new` (pinned on step, fade on play).
- **Inspect** — hover a cell for a peek; click to pin its full history (sparkline + every event incl.
  `__deleted__`/resurrection) with a "now" marker that tracks the slider.
- **Lifecycle** — deleted rows stripe out, resurrected show `✦`, per-cell heat-tint + per-row Δ gutter.
- **Anomaly watcher** — a change to an *immutable* column (`id`/`birth_date`/`cohort`/`mrn`) is flagged **red ⚠**;
  numeric changes show **direction** (↑/↓ + signed delta). Collect flagged rows via the **filter** (simple + SQL
  `WHERE`, with meta-fields `changes`/`deleted`/`resurrected`/`flagged`/`id`; "showing X of N").
- **Scale** — row virtualization (constant DOM) keeps scrub ~60fps on the 1000×20 fixture.
- **Static / print** — a non-interactive audit render (latest + ghost value, lifecycle spark-path, auto-callouts,
  `@media print`).

Spec + design: `specs/002-fluxstate-temporal-viewer/` and the locked dev-spec `docs/viewer/fluxstate-viewer-dev-spec.md`.

## Tech Stack

- **Python 3.10+**
- **Polars** - Lightning-fast dataframes (the change-detection + reconstruction engine)
- **PyArrow/Parquet** - Columnar, glob-readable storage
- **orjson / numpy / humanize / tqdm** - supporting utilities
- **Pydantic** - Runtime validation (mirror validator)
- **DuckDB** - test-only, proves the `.flux/` store is glob-readable

> No heavy runtime dependency (no Delta/Iceberg). Snowpark is **not** required —
> the test suite runs hermetically on in-memory `pl.DataFrame`s.

## Installation

```bash
# Clone the repository
git clone https://github.com/gyasis/fluxstate.git
cd fluxstate

# Install with uv (recommended) — creates .venv and installs the project
# plus its dev/test group from the committed uv.lock (reproducible).
uv sync

# Run the CLI and tests through the synced env:
uv run flux --help
uv run pytest TESTS/ -q

# Or a plain editable install with pip:
pip install -e .
```

## Use Cases

### HCC Risk Coding Workflow
Track patient risk assessment changes for CMS-HCC submissions:
```python
fs = FluxState(hcc_df, key_column="PATIENT_ID", mode="init")
# ... later when HCC codes are updated
changes = fs.update_mirror_table()  # Audit trail for compliance
```

### Snowflake Pipeline Validation
Verify data transformations in production pipelines:
```python
# Stage 1: Raw data
fs_raw = FluxState(raw_df, key_column="RECORD_ID", mode="init")

# Stage 2: After transformation
fs_transformed = FluxState(transformed_df, key_column="RECORD_ID", mode="compare")
validation_report = fs_transformed.update_mirror_table()
```

### Cost Monitoring
Track which departments/projects generate the most data changes:
```python
# Separate warehouses per team (see Patient/Proposal.md)
changes = fs.update_mirror_table()
cost_metrics = analyze_compute_usage(changes)  # Custom analysis
```

## Project Structure

```
fluxstate/
├── fluxstate.py              # FluxState facade (capture + reconstruction wiring)
├── changelog.py              # ChangeLogStore: dtype codec, keyed diff, atomic append, manifest
├── reconstruct.py            # reads: as_of / get_timeline / row_state / build_mirror_view
├── mirror_validator.py       # Pydantic validation schemas
├── example.py                # Snowpark/Elation integration example (legacy)
├── test_fluxstate_validator.py
├── Patient/                  # Production implementations
│   ├── Proposal.md          # Snowflake warehouse strategy
│   ├── patient_capture.py
│   └── mirror_table_log.json
├── scripts/
│   ├── Hcc_capture.py
│   └── PATIENT.py
├── TESTS/                   # Unit tests
└── memory-bank/             # Project documentation
```

## Documentation

- **[`docs/API.md`](docs/API.md)** — full API reference + usage guide for the change-log
  system (`FluxState`, `ChangeLogStore`, `reconstruct`), the `.flux/` store layout, the
  manifest, dtype tags, migration notes, and guarantees. Every example is runnable.
- **[`docs/VISUALIZATIONS.md`](docs/VISUALIZATIONS.md)** — catalogue of every visual surface
  in the Temporal Ghost viewer (scrubber, as-of diff table, windowed table, inspector, filter,
  static audit view, anomaly signals): what each shows, the data it needs, and how it behaves.
- **[`docs/PHAROS_INTEGRATION.md`](docs/PHAROS_INTEGRATION.md)** — how to embed FluxState into
  Pharos: the library-audit mode (`harbor` pattern), the temporal-view mode (Table Gen),
  schema-evolution behavior for churning dbt sources, and the integration contract/gotchas.

See `memory-bank/` for comprehensive project documentation:
- **projectbrief.md** - Core concepts and architecture
- **systemPatterns.md** - Design patterns and data flows
- **techContext.md** - Technical stack and constraints
- **CLAUDE.md** - Development patterns and gotchas

## Development

```bash
# Run tests
pytest test_fluxstate_validator.py

# Run specific test suite
pytest TESTS/

# Check validation
python mirror_validator.py
```

## Healthcare Context

FluxState was developed at **Herself Health** to support:
- **Tuva Health** integration (dev/prod environments)
- **Athena Project** data pipelines
- **HCC Risk Adjustment** compliance tracking
- **Multi-warehouse cost optimization** (see Patient/Proposal.md)

## License

[Specify license]

## Contributing

[Contributing guidelines]

## Contact

[Contact information]

---

**Status**: Late development stage (85% complete) - Core functionality complete, production hardening in progress.
