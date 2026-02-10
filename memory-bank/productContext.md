# Product Context

## Why FluxState Exists

### Business Context: Herself Health
Herself Health is a healthcare organization managing patient data in Snowflake. They face specific challenges that FluxState addresses:

1. **Regulatory Compliance**
   - Healthcare data is subject to HIPAA and other regulations
   - Complete audit trails are mandatory for patient records
   - Need to prove when data changed and by whom

2. **Cost Management**
   - Snowflake charges by compute time (warehouse usage)
   - Different departments and projects need isolated cost tracking
   - FluxState enables monitoring which operations consume resources
   - Supports the warehouse allocation strategy (see Patient/Proposal.md)

3. **Data Quality Assurance**
   - ETL pipelines process sensitive medical data
   - Errors in data transformation can impact patient care
   - Historical versioning enables debugging and validation

4. **HCC Risk Coding Workflow**
   - Medical risk adjustment coding for Medicare Advantage
   - ICD-10 codes tied to HCC codes affect reimbursement
   - Need to track when risk codes are created, billed, resolved, or disputed
   - Reconciliation between problem lists, visit notes, and billing

## Problems Solved

### Problem 1: No Audit Trail
**Before FluxState**: Database updates overwrite previous values with no history.
**After FluxState**: Every change is preserved with timestamp and historical value.

### Problem 2: Pipeline Debugging
**Before**: When data quality issues arise, no way to trace the transformation history.
**After**: Time-travel queries show exactly what the data looked like at any point.

### Problem 3: Environment Validation
**Before**: Comparing dev/staging/prod data requires manual snapshots.
**After**: Load mirror tables from different environments and directly compare histories.

### Problem 4: Cost Attribution
**Before**: Snowflake warehouse costs are aggregated, unclear which projects drive expenses.
**After**: Track data operations per warehouse, enabling project-level cost analysis.

### Problem 5: Compliance Reporting
**Before**: Cannot prove data lineage or change history for auditors.
**After**: Complete, validated, timestamped audit trail exportable for compliance.

## Target Users

1. **Data Engineers**: Building and maintaining ETL pipelines
2. **Healthcare Analysts**: Reconciling medical coding and billing data
3. **Compliance Officers**: Auditing data changes for regulatory requirements
4. **Finance/Operations**: Tracking Snowflake compute costs by department
5. **Data Scientists**: Analyzing temporal patterns in healthcare data

## Strategic Fit

FluxState aligns with Herself Health's broader data strategy:
- **Warehouse Segmentation**: Supports dedicated warehouses for dev, production, Tuva, Athena project
- **Pipeline Observability**: Enables monitoring of Snowpark-based data transformations
- **Cost Optimization**: Provides data to optimize warehouse sizing and auto-suspend policies
- **Data Governance**: Centralizes change tracking for compliance and audit purposes

## Success Metrics

- **Compliance**: 100% audit trail coverage for regulated data
- **Cost Visibility**: Per-warehouse cost attribution accuracy
- **Pipeline Reliability**: Mean time to detect/resolve data quality issues
- **Developer Productivity**: Time saved debugging data transformations
- **Storage Efficiency**: Compression ratio vs naive full-table snapshots
