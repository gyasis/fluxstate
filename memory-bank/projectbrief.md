# FluxState Project Brief

## Overview
FluxState is a **Change Data Capture (CDC) system** that tracks cell-level changes in database tables with full historical versioning. It's designed specifically for healthcare data workflows at Herself Health, with native Snowflake/Snowpark integration.

## Problem Statement
Healthcare organizations need to:
- Track every change to patient data for compliance and audit trails
- Maintain complete historical versions of database records
- Debug data pipelines and validate transformations
- Monitor Snowflake warehouse costs by tracking data operations
- Compare data states across development and production environments

## Core Concept
FluxState creates "mirror tables" that store the entire change history of each cell as JSON arrays of `{date, value}` objects. Every time a cell value changes, a new timestamped entry is appended to its history array.

## Key Features
1. **Cell-Level Change Tracking**: Every individual cell maintains its complete change history
2. **Time Travel**: Query data as it existed at any point in time
3. **Efficient Storage**: Uses Parquet serialization with orjson for performance
4. **Schema Validation**: Pydantic-based validation ensures data integrity
5. **Snowpark Integration**: Native support for Snowflake data processing pipelines
6. **Flexible Filtering**: Query historical data by date ranges, column values, or null detection

## Use Cases at Herself Health
- **Compliance**: Healthcare regulations (HIPAA) require complete audit trails
- **Cost Tracking**: Monitor Snowflake compute costs by tracking data transformations
- **Pipeline Debugging**: Trace when and how data values changed during ETL processes
- **Data Validation**: Compare production vs development environments
- **HCC Risk Coding**: Track medical risk code changes and billing reconciliation

## Technical Foundation
- **Language**: Python 3.11+
- **Core Library**: Polars for high-performance DataFrame operations
- **Serialization**: orjson for fast JSON encoding/decoding
- **Storage**: Parquet format via PyArrow
- **Validation**: Pydantic models for schema enforcement
- **Target Platform**: Snowflake data warehouse with Snowpark

## Status
Core functionality is complete and tested. Project needs production hardening: git repository setup, documentation, packaging, and deployment strategy.
