# CLAUDE.md - FluxState Project Intelligence

This file contains project-specific insights, patterns, and preferences discovered during FluxState development. It helps future Claude sessions work more effectively on this codebase.

---

## Project-Specific Patterns

### Mirror Table Initialization Modes
When working with FluxState, always clarify which mode is needed:

**Init Mode** (`mode="init"`):
```python
flux = FluxState(table=df, key_column='id', mode='init')
```
- Use for: First-time setup, creating new mirror tables
- Creates fresh historical entries for all cells
- All values get current timestamp

**Compare Mode** (`mode="compare"`):
```python
flux = FluxState(table=df, key_column='id', mode='compare', expect_serialized=True)
```
- Use for: Ongoing change detection, loading existing mirrors
- Requires `expect_serialized=True` when loading from Parquet
- Compares current data against historical values

**Common Mistake**: Forgetting `expect_serialized=True` when loading from Parquet causes JSON parsing errors.

---

### String Normalization is Critical
All non-key values are normalized to strings before comparison. This prevents false positives from type differences:

```python
# These are considered identical:
123 (int) → "123" (string)
123.0 (float) → "123.0" (string)
"123" (string) → "123" (string)
```

**Implication**: Cannot perform numeric operations on historical values without explicit casting.

**When Modifying**: If you need to preserve numeric types, you'll need to refactor the comparison logic and add a type metadata field.

---

### Date Format Standards
FluxState uses strict date format: `YYYY-MM-DD HH:MM:SS` (24-hour, zero-padded)

**Parser Supports**:
- Standard format: `2024-01-15 14:30:00`
- ISO format (auto-converts): `2024-01-15T14:30:00`
- Date-only (fuzzy matching): `2024-01-15` → defaults to end-of-day

**Common Pitfall**: Sub-second precision is lost (stored as strings, truncated to seconds)

---

### The Flattening Bug Guard
The `flatten_if_needed()` function prevents double-nesting: `[[{date, value}]]` vs `[{date, value}]`

**Why It Exists**: JSON deserialization sometimes wraps single-item lists in extra brackets.

**When to Care**: If you're modifying serialization logic, always test for this edge case.

**Test Command**:
```python
assert not any(isinstance(x, list) and len(x) == 1 and isinstance(x[0], list) for x in mirror_table.values())
```

---

### Deletion Tracking Semantics
Deleted rows are tracked by appending `null` values, NOT by removing entries:

```python
# Row 123 deleted at 2024-01-20
mirror_table['name'][index_of_123] = [
    {'date': '2024-01-15 10:00:00', 'value': 'John'},
    {'date': '2024-01-20 15:00:00', 'value': None}  # Deletion marker
]
```

**Why**: Preserves complete audit trail (when row was deleted, what value it had before)

**Restoration**: If row reappears later, new entry is appended (tracks restore event)

---

## User Preferences (Herself Health Team)

### Snowflake Warehouse Strategy
The team uses **dedicated warehouses per department/project** (see Patient/Proposal.md):
- `INHOUSE_DEV_WH`: Internal development and testing
- `TUVA_DEV_WH`: Tuva's development tasks
- `TUVA_PROD_WH`: Tuva production operations
- `ATHENA_PROJECT_WH`: Athena project tracking
- `PRODUCTION_PIPELINES_WH`: Snowpark, Lambda, pipelines
- `SPECIAL_PROJECTS_WH`: Experiments, one-time jobs

**Implication**: When deploying FluxState, ask which warehouse to use. Cost tracking depends on correct warehouse assignment.

---

### HCC Risk Coding Workflow
Primary use case is tracking ICD-10 codes → HCC codes for Medicare Advantage risk adjustment:

**Key Concepts**:
- **ICD-10 Code**: Diagnosis code (e.g., `E11.9` = Type 2 diabetes)
- **HCC Code**: Hierarchical Condition Category (e.g., `19` = Diabetes without complications)
- **Billing Codes**: ICD-10 codes billed for a visit
- **Problem List**: ICD-10 codes on patient's problem list

**Workflow States**:
1. **Pending**: Risk code created but not yet resolved
2. **Billed**: Code appears on bill (success)
3. **Resolved**: Code marked as resolved (closed)
4. **Disagree**: Code deleted (provider disagreed)
5. **Ignored**: Code not addressed within timeframe

**FluxState Use**: Track when risk codes change state, who changed them, and why.

---

### Performance Expectations
Healthcare data is often large (millions of rows):

**Acceptable Performance** (based on Snowpark context):
- 1M rows initialization: ~2-5 minutes
- 1M rows update detection: ~1-3 minutes
- Time-travel query: <30 seconds

**Unacceptable Performance**:
- >10 minutes for 1M rows
- Out-of-memory errors on standard Snowflake warehouse (MEDIUM)

**Optimization Priority**: Storage efficiency > query speed (Snowflake storage is expensive)

---

## Critical Implementation Paths

### Path 1: Loading Existing Mirror Table
```python
# CORRECT
flux = FluxState.load_mirror_table('path/to/mirror.parquet', key_column='id')
flux.table = current_df  # Update with new data
flux.update_mirror_table()
flux.save_mirror_table('path/to/mirror.parquet')

# INCORRECT (missing expect_serialized flag)
flux = FluxState(table=pl.read_parquet('mirror.parquet'), mode='compare')
# ^ Will fail: JSON parsing error on serialized strings
```

---

### Path 2: Time-Travel Query
```python
# CORRECT (supports fuzzy date matching)
snapshot = flux.travel('2024-01-15')  # Defaults to end-of-day
snapshot = flux.travel('2024-01-15T14:30:00')  # Exact time

# INCORRECT (will raise ValueError)
snapshot = flux.travel('Jan 15, 2024')  # Not ISO format
```

---

### Path 3: Filtering for Changes in Date Range
```python
# CORRECT
filtered = flux.filter(
    column_filters={'status': 'Pending'},
    date_range=('2024-01-01 00:00:00', '2024-01-31 23:59:59')
)

# INCORRECT (wrong date format)
filtered = flux.filter(date_range=('2024-01-01', '2024-01-31'))
# ^ Missing time, will fail comparison
```

---

## Testing Philosophy

### What Good Tests Look Like
```python
# Good: Tests actual behavior, not implementation
def test_change_detection_tracks_updates(self):
    flux = FluxState(initial_df, key_column='id')
    modified_df = initial_df.with_columns(pl.col('age') + 1)
    flux.table = modified_df
    flux.update_mirror_table()

    # Verify change was recorded
    history = flux.mirror_table['age'][0]
    assert len(history) == 2  # Original + updated value
    assert history[1]['value'] == '26'
```

```python
# Bad: Tests internal implementation details
def test_convert_to_string_exists(self):
    assert hasattr(flux, 'convert_to_string')  # Too coupled to implementation
```

### Edge Cases to Always Test
1. **Empty Tables**: Zero rows
2. **Single Row**: Boundary case for loops
3. **All Nulls**: Column with only NULL values
4. **No Changes**: Update with identical data
5. **Schema Mismatch**: Different columns in update

---

## Common Error Messages & Solutions

### Error: "mirror_table is not set in FluxState"
**Cause**: Initialization failed (mode not recognized)
**Solution**: Check `mode` parameter is either `"init"` or `"compare"`

### Error: "JSONDecodeError: Expecting value"
**Cause**: Loading Parquet file without `expect_serialized=True`
**Solution**: Add `expect_serialized=True` to constructor

### Error: "Validation failed for column X"
**Cause**: History array structure is corrupted
**Solution**: Check for double-nesting, ensure each entry has `date` and `value` keys

### Error: "KeyError: 'id'"
**Cause**: Key column name doesn't match actual column
**Solution**: Verify `key_column` parameter matches DataFrame column name exactly

---

## Performance Optimization Strategies

### When FluxState is Slow
1. **Check History Depth**: Cells with 1000+ changes → slow serialization
   - Solution: Prune old history beyond retention policy
2. **Large Tables**: 10M+ rows → memory issues
   - Solution: Partition by date or entity ID, process in batches
3. **Validation Overhead**: Pydantic adds ~15% runtime
   - Solution: Make validation optional for trusted sources

### Memory Optimization Checklist
- [ ] Use `pl.scan_parquet()` instead of `pl.read_parquet()` for lazy loading
- [ ] Process in chunks (e.g., 1M rows at a time)
- [ ] Clear intermediate DataFrames with `del df; gc.collect()`
- [ ] Use Snowflake's `RESULT_SCAN()` to avoid re-loading data

---

## Snowpark-Specific Gotchas

### Gotcha 1: UDF Size Limits
Snowflake UDFs have 10 MB compressed size limit. FluxState + dependencies ~8 MB.

**Solution**: Use `snowflake-python-udf-packager` to bundle efficiently.

### Gotcha 2: Pandas vs Polars
Snowpark natively returns Pandas DataFrames. FluxState uses Polars.

**Solution**: Convert at boundary: `pl.from_pandas(snowpark_df)`

### Gotcha 3: Serialization Overhead
Snowflake serializes data to/from UDFs. Large tables = slow.

**Solution**: Use stored procedures for batch processing, UDFs only for queries.

---

## Deployment Checklist

### Before Deploying to Snowflake
- [ ] Test with production-scale data (1M+ rows)
- [ ] Profile memory usage (`tracemalloc` or `memory_profiler`)
- [ ] Confirm key column is indexed in source table
- [ ] Validate date formats match Snowflake's `TIMESTAMP_NTZ`
- [ ] Test with Snowflake stage storage (not just local Parquet)
- [ ] Verify warehouse has sufficient memory (MEDIUM or larger)
- [ ] Document expected runtime and cost per execution

---

## Project Evolution Notes

### Why Polars Instead of Pandas?
- **Decision Date**: Early development
- **Rationale**: 5-10x faster, lower memory, better datetime handling
- **Trade-off**: Less mature ecosystem, Snowpark uses pandas natively
- **Revert Threshold**: If Polars proves incompatible with Snowpark, consider pandas

### Why Pydantic Validation?
- **Decision Date**: Mid-development
- **Rationale**: Prevent data corruption from double-nesting bug
- **Trade-off**: ~15% performance overhead
- **Revert Threshold**: If validation overhead becomes critical, make it optional

### Why String Normalization?
- **Decision Date**: Early development (after initial type mismatch bugs)
- **Rationale**: Prevent false positives from type coercion
- **Trade-off**: Loses numeric type information
- **Revert Threshold**: If numeric operations become critical, add type metadata field

---

## Future Considerations

### Potential Refactors
1. **Type Preservation**: Add `type` field to historical records
2. **Diff-Based Storage**: Store only deltas for large text fields
3. **Schema Versioning**: Track column add/remove/rename events
4. **Metadata Layer**: Add `user`, `source_system`, `transaction_id` to each change

### Integration Opportunities
1. **dbt**: Materialize mirror tables as incremental models
2. **Airflow**: Custom operators for scheduled change detection
3. **Great Expectations**: Validation rules for mirror table integrity
4. **Tableau**: Custom connector for querying historical data

---

## Contact & Ownership

**Primary User**: Herself Health data team
**Domain**: Healthcare data (HIPAA-regulated)
**Primary Use Case**: HCC risk coding reconciliation
**Deployment Target**: Snowflake with Snowpark

**For Questions**:
1. Check memory-bank documentation (projectbrief.md, systemPatterns.md)
2. Review example.py for real-world usage pattern
3. Consult Patient/Proposal.md for warehouse strategy context

---

## Quick Reference Commands

### Initialize New Mirror Table
```python
from fluxstate import FluxState
import polars as pl

df = pl.read_csv('data.csv')
flux = FluxState(df, key_column='id', mode='init')
flux.save_mirror_table('mirror.parquet')
```

### Update Existing Mirror Table
```python
flux = FluxState.load_mirror_table('mirror.parquet', key_column='id')
flux.table = pl.read_csv('updated_data.csv')
flux.update_mirror_table()
flux.save_mirror_table('mirror.parquet')
```

### Time-Travel Query
```python
snapshot = flux.travel('2024-01-15')
print(snapshot)
```

### Filter by Date Range
```python
filtered = flux.filter(date_range=('2024-01-01 00:00:00', '2024-01-31 23:59:59'))
```

### Find Null Values
```python
nulls = flux.filter_for_null_values(
    column_name='status',
    date_range=('2024-01-01', '2024-01-31')
)
```

---

**Last Updated**: 2026-02-09 (Memory-Bank Initialization Session)
