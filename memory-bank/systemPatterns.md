# System Patterns & Architecture

## Core Architecture

### Mirror Table Concept
FluxState creates a parallel "mirror" structure that shadows the source table:

**Source Table**:
```
id | name  | age
1  | John  | 25
2  | Jane  | 30
```

**Mirror Table** (internal representation):
```python
{
    'id': [1, 2],  # Key column (unchanged)
    'name': [
        [{'date': '2024-01-01 10:00:00', 'value': 'John'}],
        [{'date': '2024-01-01 10:00:00', 'value': 'Jane'}]
    ],
    'age': [
        [
            {'date': '2024-01-01 10:00:00', 'value': '25'},
            {'date': '2024-01-15 14:30:00', 'value': '26'}  # Age changed
        ],
        [{'date': '2024-01-01 10:00:00', 'value': '30'}]
    ]
}
```

## Technology Stack

### Data Processing: Polars
- **Why Polars**: 5-10x faster than pandas, lower memory footprint
- **Usage**: All DataFrame operations, schema casting, filtering
- **Pattern**: Keep operations lazy where possible, materialize only when needed

### JSON Serialization: orjson
- **Why orjson**: 2-3x faster than stdlib json, handles datetimes natively
- **Usage**: Encoding/decoding cell history arrays
- **Pattern**: Use `.decode('utf-8')` after orjson.dumps() for string output

### Storage: Parquet via PyArrow
- **Why Parquet**: Columnar format, excellent compression for repeated JSON structures
- **Usage**: Persistent storage of mirror tables
- **Pattern**: Store JSON arrays as strings in Parquet files

### Validation: Pydantic
- **Why Pydantic**: Runtime type checking, automatic coercion, clear error messages
- **Usage**: Validate mirror table structure before serialization
- **Models**:
  - `HistoricalRecord`: Single {date, value} entry
  - `MirrorTableColumn`: List of HistoricalRecord objects
  - `MirrorTableValidator`: Orchestrates validation logic

## Key Design Patterns

### Pattern 1: Initialization Modes
FluxState supports two modes via the `mode` parameter:

**Init Mode** (`mode="init"`):
- Creates fresh mirror table from source data
- Every cell gets first historical entry with current timestamp
- Use case: First-time setup of change tracking

**Compare Mode** (`mode="compare"`):
- Loads existing mirror table from Parquet file
- Compares with current source data to detect changes
- Use case: Ongoing change detection and history updates

**Serialization Flag** (`expect_serialized`):
- `True`: Assumes columns contain JSON strings (from Parquet)
- `False`: Assumes columns contain raw values (from database)

### Pattern 2: String Normalization
All non-key values are normalized to strings for consistent comparison:

```python
def convert_to_string(value):
    if value is None:
        return "NULL"
    elif isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    else:
        return str(value).strip()
```

**Rationale**: Prevents false positives from type coercion (e.g., 123 vs "123")

### Pattern 3: Nested List Flattening
Prevents double-nesting bug `[[{date, value}]]` vs `[{date, value}]`:

```python
def flatten_if_needed(obj):
    if (isinstance(obj, list) and len(obj) == 1
        and isinstance(obj[0], list) and is_list_of_dicts(obj[0])):
        return obj[0]  # Unwrap single-item list
    return obj
```

### Pattern 4: Change Detection
Only append new history entry if value actually changed:

```python
last_value_str = str(last_entry["value"]).strip()
new_value_str = convert_to_string(row[col])

if new_value_str != last_value_str:
    mirror_table[col][index].append({
        "date": current_date,
        "value": new_value_str
    })
```

### Pattern 5: Deletion Tracking
Deleted rows are tracked by appending `null` values:

```python
# Row exists in mirror but not in current table
if id_ in mirror_ids - main_ids:
    if last_value != "NULL":
        mirror_table[col][index].append({
            "date": current_date,
            "value": None  # Marks deletion
        })
```

## Data Flow

### Initialization Flow
1. Load source table into Polars DataFrame
2. Cast all non-key columns to string type
3. Create mirror dictionary with same schema
4. For each cell: wrap value in `[{date, value}]` structure
5. Validate structure with Pydantic
6. Return FluxState instance

### Update Flow
1. Load existing mirror table from Parquet
2. Deserialize JSON strings back to list[dict] objects
3. Load current source table
4. Identify added, deleted, and modified rows
5. For modified rows: compare string-normalized values
6. Append new history entries only for actual changes
7. Validate updated structure
8. Serialize and save to Parquet

### Query Flow
1. Load mirror table
2. Apply filters (date range, column values, nulls)
3. Deserialize relevant columns
4. Filter historical records based on criteria
5. Reconstruct DataFrame with matching records
6. Return filtered result

## Critical Implementation Details

### Date Handling
- **Storage Format**: `YYYY-MM-DD HH:MM:SS` (24-hour, zero-padded)
- **Parsing**: Supports both strict format and ISO format (auto-converts)
- **Comparison**: Always use datetime objects for range queries
- **Time Travel**: Fuzzy matching allows date-only queries (defaults to end-of-day)

### NULL Semantics
- `None` (Python) → `"NULL"` (string) in mirror table
- Distinguishes between "never set" vs "explicitly nulled"
- Deletion tracking uses `None` value to mark row removal

### Performance Considerations
- **Lazy Evaluation**: Polars queries are lazy by default, call `.collect()` explicitly
- **Batch Processing**: Use `iter_rows(named=True)` for memory-efficient iteration
- **Index Lookups**: Use dictionary lookups for key column, not repeated scans
- **JSON Parsing**: orjson significantly faster than stdlib for large histories

## Extensibility Points

1. **Custom Serialization**: Override `save_mirror_table()` for different storage backends
2. **Metadata Enrichment**: Extend history records with user, source system, or transaction IDs
3. **Compression Strategies**: Implement value deduplication for repeated values
4. **Schema Evolution**: Add column-level metadata tracking for schema changes
5. **Distributed Processing**: Partition mirror tables for parallel change detection
