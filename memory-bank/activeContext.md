# Active Context

## Current State (2026-02-09)

### Immediate Focus
The FluxState project is in **late development** stage with core functionality complete. Current focus is on **productionization and deployment readiness**.

### Recent Changes
- Code has been moved to `/home/gyasis/Documents/code/fluxstate/` (root code directory)
- Memory-bank documentation system being initialized (this session)
- No git repository currently set up (not initialized yet)

### Active Work Items

:hourglass_flowing_sand: **Memory-Bank Initialization** (IN PROGRESS)
- Creating comprehensive project documentation
- Establishing memory bank structure for future sessions

:white_large_square: **Git Repository Setup** (PENDING)
- Initialize git repository
- Create `.gitignore` for Python/Snowflake projects
- Initial commit with current codebase
- Consider: branch strategy (main, develop, feature branches?)

:white_large_square: **Project Organization** (PENDING)
- Add README.md with quickstart guide
- Add LICENSE file (confirm with stakeholders)
- Add CONTRIBUTING.md if open-sourcing
- Create examples/ directory with documented use cases
- Move test_fluxstate_validator.py to tests/ directory

:white_large_square: **Documentation** (PENDING)
- API documentation (docstrings → Sphinx or MkDocs)
- Architecture diagrams (mirror table concept, data flow)
- Snowpark deployment guide
- Performance tuning guide
- Troubleshooting guide

:white_large_square: **Production Hardening** (PENDING)
- Add logging configuration (currently ad-hoc)
- Error handling improvements (more specific exceptions)
- Input validation (schema compatibility checks)
- Performance profiling (identify bottlenecks)
- Memory optimization for large tables

:white_large_square: **Testing Gaps** (PENDING)
- Integration tests for Snowpark deployment
- Performance tests with large datasets
- Edge case tests (empty tables, schema changes, nulls)
- Concurrent update scenarios

:white_large_square: **Packaging & Distribution** (PENDING)
- Publish to PyPI or internal artifact repository
- Version management strategy (semantic versioning)
- Release notes automation
- CI/CD pipeline (GitHub Actions or Jenkins)

## Critical Decisions Needed

### 1. Deployment Strategy
**Question**: How will FluxState be deployed in production?

**Options**:
- A) Snowpark UDF (stateless, called per-query)
- B) Snowpark Stored Procedure (scheduled, batch processing)
- C) External Python service (e.g., Airflow DAG)
- D) Hybrid: UDF for queries, SP for updates

**Implications**:
- UDF: Requires packaging as single-file or with dependencies
- SP: Needs scheduling strategy (cron-like)
- External: More flexible but adds infrastructure complexity

### 2. Storage Location
**Question**: Where should mirror tables be stored?

**Options**:
- A) Snowflake tables (native, queryable, expensive storage)
- B) Snowflake stages (cheaper, Parquet files, less queryable)
- C) External blob storage (S3/Azure/GCS, cheapest, most complex)

**Implications**:
- Tables: Easiest to query, highest cost
- Stages: Good balance, requires UDF to read
- External: Lowest cost, most engineering overhead

### 3. Change Detection Trigger
**Question**: What triggers the change detection process?

**Options**:
- A) Manual execution (user calls function)
- B) Time-based schedule (every hour/day)
- C) Event-driven (Snowflake stream/task)
- D) On-demand (before each query)

**Implications**:
- Manual: Full control, no automation
- Scheduled: Predictable, may miss real-time changes
- Event-driven: Most responsive, most complex setup
- On-demand: Always fresh, performance impact

### 4. Retention Policy
**Question**: How long should change history be retained?

**Options**:
- A) Forever (complete audit trail)
- B) Rolling window (e.g., 7 years for compliance)
- C) Configurable per table/column
- D) Prune old changes based on storage limits

**Implications**:
- Forever: Unbounded storage growth
- Rolling: Automated cleanup, loses old data
- Configurable: Maximum flexibility, most complexity
- Storage-based: Unpredictable retention period

## Blockers & Dependencies

### No Blockers Currently
- Core code is functional and tested
- No external API dependencies
- No infrastructure provisioning required yet

### Soft Dependencies (for production)
- Snowflake account access (for Snowpark deployment)
- Decision on deployment strategy (see above)
- Approval for warehouse usage/costs

## Next Steps (Prioritized)

1. **Complete Memory-Bank Initialization** (THIS SESSION)
   - Finish documentation files
   - Update CLAUDE.md with project-specific insights

2. **Initialize Git Repository** (IMMEDIATE NEXT)
   - Set up .gitignore
   - Initial commit
   - Push to remote (GitHub/GitLab/Bitbucket?)

3. **Create README.md** (IMMEDIATE NEXT)
   - Installation instructions
   - Quickstart example
   - Link to full documentation

4. **Address Critical Decisions** (BEFORE DEPLOYMENT)
   - Schedule meeting with stakeholders
   - Document decisions in project brief
   - Update architecture accordingly

5. **Production Hardening** (BEFORE DEPLOYMENT)
   - Logging improvements
   - Error handling
   - Performance testing

## Environment Notes

### Development Machine
- Location: `/home/gyasis/Documents/code/fluxstate/`
- OS: Linux 5.15.0-164-generic
- Python: 3.11+ (via Poetry environment)

### Snowflake Environment (Target)
- Organization: Herself Health
- Warehouses: Multiple (see Patient/Proposal.md for allocation strategy)
- Primary Use Case: HCC risk coding reconciliation

## Session Continuity Notes

### For Next Session
When resuming work on FluxState:

1. **Check Task Status**: Review memory-bank/progress.md for what's complete
2. **Review Decisions**: Check if critical decisions have been made
3. **Check Git Status**: Confirm repository setup and branch state
4. **Review Recent Commits**: Understand what changed since last session
5. **Check Open Issues**: Any bugs or feature requests logged?

### Key Files to Reference
- **fluxstate.py**: Core logic, 675 lines, main FluxState class
- **mirror_validator.py**: Validation schemas, 190 lines, Pydantic models
- **example.py**: Real-world Snowpark integration, HCC coding workflow
- **test_fluxstate_validator.py**: Unit tests, coverage baseline
- **Patient/Proposal.md**: Business context, warehouse strategy

### Important Patterns to Remember
- Always use `mode="compare"` with `expect_serialized=True` when loading from Parquet
- All non-key columns are cast to strings before comparison
- `flatten_if_needed()` prevents double-nesting bug
- Time-travel queries support fuzzy date matching
