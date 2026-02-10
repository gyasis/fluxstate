# Technical Context

## Technology Stack

### Core Dependencies

**Python 3.11+**
- Minimum version required for type hints and performance improvements
- Tested on Python 3.10, but 3.11+ recommended for production

**Polars 1.5.0+**
- High-performance DataFrame library (Rust-based)
- Lazy evaluation for query optimization
- Native support for complex types (lists, structs)
- Memory-efficient compared to pandas

**orjson 3.10.7+**
- Fast JSON serialization/deserialization
- 2-3x faster than standard library json
- Native datetime handling without custom encoders

**PyArrow 17.0.0+**
- Parquet file format support
- Columnar data representation
- Efficient compression and encoding

**Pydantic (implicit dependency via mirror_validator.py)**
- Runtime data validation
- Automatic type coercion
- Clear error messages for schema violations

**Supporting Libraries**:
- `humanize 4.10.0+`: Human-readable file sizes
- `tqdm 4.66.5+`: Progress bars for large operations
- `numpy 2.1.0+`: Numerical operations (indirect dependency)

### Snowflake Integration

**Snowpark SDK**
- Python library for Snowflake data processing
- UDF/stored procedure support
- Example usage in `example.py`: Healthcare HCC coding workflow
- Enables server-side execution of FluxState logic

**Snowflake Warehouse Strategy**
- Dedicated warehouses per department/project (see Patient/Proposal.md)
- FluxState can track which warehouse processes which data
- Cost attribution by monitoring warehouse usage patterns

## Development Environment

### Installation
```bash
# Using Poetry (recommended)
poetry install

# Using pip
pip install polars orjson pyarrow pydantic humanize tqdm numpy
```

### Project Structure
```
fluxstate/
├── __init__.py              # Package exports
├── fluxstate.py             # Main FluxState class (675 lines)
├── mirror_validator.py      # Pydantic validation schemas (190 lines)
├── example.py               # Snowpark integration example (384 lines)
├── pyproject.toml           # Poetry dependency management
├── test_fluxstate_validator.py  # Unit tests
├── scripts/                 # Utility scripts
├── TESTS/                   # Test data and outputs
└── Patient/
    └── Proposal.md          # Warehouse allocation strategy doc
```

### Configuration Files

**pyproject.toml** (Poetry configuration):
- Dependencies: polars, orjson, pyarrow, pydantic, humanize, tqdm, numpy
- Build system: poetry-core
- Version: 0.1.0 (early development)
- No CLI entry points defined yet

**__init__.py** (Package exports):
```python
from .fluxstate import FluxState
from .mirror_validator import MirrorTableValidator, HistoricalRecord, MirrorTableColumn
```

## Testing Strategy

### Unit Tests (test_fluxstate_validator.py)
Covers:
1. Initialization and validation
2. Data type validation
3. Historical record structure
4. Error handling
5. Serialization/deserialization
6. Change tracking

**Test Data Pattern**:
```python
test_data = {
    'id': [1, 2, 3],
    'name': ['John', 'Jane', 'Bob'],
    'age': ['25', '30', '35']  # Pre-normalized to strings
}
```

### Integration Testing
- `example.py` serves as integration test for Snowpark
- Processes HCC risk coding workflow from Herself Health
- Demonstrates real-world usage pattern

### Coverage Gaps
- No tests for time-travel queries
- No tests for large-scale performance
- No tests for concurrent updates
- No Snowpark unit tests (requires Snowflake credentials)

## Deployment Considerations

### Snowflake Deployment
1. **UDF Deployment**: Package FluxState as Snowpark UDF
2. **Stored Procedure**: Wrap change detection in scheduled procedure
3. **Stage Storage**: Use Snowflake internal stages for Parquet files
4. **Warehouse Assignment**: Deploy to appropriate warehouse per project

### Constraints & Limitations

**Memory Constraints**:
- Large tables with many changes → large history arrays
- Polars memory-efficient but still limited by RAM
- Consider partitioning by date or entity ID for massive tables

**Snowflake Limits**:
- VARCHAR column max size: 16 MB (JSON serialization must fit)
- UDF timeout: 60 seconds default (adjust for large tables)
- Stored procedure memory: 2 GB per execution

**Concurrency**:
- No built-in locking mechanism
- Not safe for concurrent updates to same mirror table
- Use Snowflake transactions or external coordination

**Schema Evolution**:
- Adding columns: Works (new columns get fresh histories)
- Removing columns: Manual cleanup required
- Renaming columns: Treated as remove + add (history lost)

### Performance Characteristics

**Time Complexity**:
- Initialization: O(n*m) for n rows, m columns
- Update detection: O(n*m) comparison, O(k) appends for k changes
- Time travel query: O(n*h) for n rows, h history depth
- Filtering: O(n*h*f) for f filter predicates

**Space Complexity**:
- Storage grows with: (rows × columns × change_frequency × time)
- Compression ratio: ~60-80% with Parquet for typical healthcare data
- JSON overhead: ~40 bytes per history entry (date + value + formatting)

**Optimization Strategies**:
1. Store only changed columns (don't serialize unchanged histories)
2. Prune old history beyond retention policy
3. Use dictionary encoding for repeated values
4. Partition mirror tables by date ranges

## Security & Compliance

### Data Handling
- All PHI (Protected Health Information) stored in Snowflake
- FluxState processes data in-memory (no local persistence in UDF mode)
- Audit trail timestamps are UTC (Snowflake default)

### Access Control
- Relies on Snowflake role-based access control (RBAC)
- Mirror tables inherit permissions from source tables
- No additional authentication layer in FluxState itself

### Encryption
- At rest: Snowflake's native encryption
- In transit: Snowflake HTTPS/TLS
- In memory: No encryption (relies on secure compute environment)

## Known Issues & Caveats

1. **Double-Nesting Bug** (FIXED): `flatten_if_needed()` prevents `[[{date, value}]]`
2. **Type Coercion**: All values normalized to strings (loses numeric type info)
3. **Datetime Precision**: Stored as strings, loses sub-second precision
4. **No Diff Compression**: Each history entry stores full value (no deltas)
5. **Validation Overhead**: Pydantic validation adds ~10-15% runtime cost

## Future Enhancements

### Planned Features
- **Schema Versioning**: Track column additions/removals/renames
- **Diff-Based Storage**: Store only diffs for large text fields
- **Incremental Updates**: Process only new/changed rows
- **Batch Validation**: Validate in chunks for massive tables
- **Metadata Layer**: Track who/what/why for each change

### Performance Optimizations
- Lazy deserialization: Parse JSON only when accessed
- Columnar filtering: Skip columns not in query
- Parallel processing: Multi-threaded change detection
- Caching layer: Keep frequently-accessed histories in memory

### Integration Targets
- **dbt**: Materialize mirror tables as dbt incremental models
- **Airflow**: DAG operators for scheduled change detection
- **Dagster**: Assets for mirror table management
- **Great Expectations**: Validation rules for mirror table integrity
