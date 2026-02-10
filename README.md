# FluxState

> Cell-level Change Data Capture (CDC) system with temporal versioning for healthcare data workflows

FluxState is a Python library that tracks every cell-level change in database tables with full historical versioning. Designed specifically for healthcare data pipelines at Herself Health, it maintains temporal "mirror tables" where each cell contains a complete audit trail.

## Core Concept

Unlike traditional CDC that just captures changes, FluxState maintains a temporal mirror where every cell contains its entire history:

```python
# Standard table
PATIENT_ID | STATUS
123        | "active"

# FluxState mirror table
PATIENT_ID | STATUS
123        | [
              {"date": "2024-01-01 10:00:00", "value": "pending"},
              {"date": "2024-02-15 14:30:00", "value": "active"}
            ]
```

## Why FluxState?

- **Healthcare Compliance**: HIPAA-compliant audit trails for patient data
- **Cost Tracking**: Monitor Snowflake compute costs by tracking data changes
- **Pipeline Debugging**: See exactly what changed, when, and why
- **Data Validation**: Verify transformations between dev/staging/prod environments
- **Time-Travel Queries**: Query data state at any point in history

## Features

- ✅ **Cell-level change tracking** with microsecond precision
- ✅ **Polars-based** for 10-100x performance vs Pandas
- ✅ **Pydantic validation** for runtime data integrity
- ✅ **Parquet serialization** for efficient storage
- ✅ **Snowpark integration** for production Snowflake pipelines
- ✅ **Time-travel queries** to reconstruct historical states
- ✅ **Three initialization modes**: init, compare, load

## Quick Start

```python
from fluxstate import FluxState

# Initialize new mirror from table
fs = FluxState(
    table=patient_df,
    key_column="PATIENT_ID",
    mode="init"
)

# Serialize to Parquet
mirror_df = fs.serialize_mirror_table()
mirror_df.write_parquet("patient_mirror.parquet")

# Later: Load and detect changes
fs_updated = FluxState(
    table=updated_patient_df,
    key_column="PATIENT_ID",
    mode="compare",
    expect_serialized=True,
    mirror_path="patient_mirror.parquet"
)

changes = fs_updated.update_mirror_table()
print(f"Detected {len(changes)} cell changes")
```

## Tech Stack

- **Python 3.11+**
- **Polars** - Lightning-fast dataframes
- **orjson** - High-performance JSON serialization
- **PyArrow/Parquet** - Columnar storage
- **Pydantic** - Runtime validation
- **Snowpark** - Snowflake integration (optional)

## Installation

```bash
# Clone the repository
git clone https://github.com/gyasis/fluxstate.git
cd fluxstate

# Install with uv (recommended)
uv pip install -e .

# Or with pip
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
├── fluxstate.py              # Main FluxState class
├── mirror_validator.py       # Pydantic validation schemas
├── example.py                # Snowpark/Elation integration example
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
