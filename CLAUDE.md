<!-- SPECKIT START -->
Active feature: **001-changelog-first-pivot** (changelog-first storage pivot).

For technologies, project structure, constraints, and design decisions, read the
current plan and its Phase 0/1 artifacts:
- Plan: `specs/001-changelog-first-pivot/plan.md`
- Spec: `specs/001-changelog-first-pivot/spec.md`
- Research: `specs/001-changelog-first-pivot/research.md`
- Data model: `specs/001-changelog-first-pivot/data-model.md`
- Contracts: `specs/001-changelog-first-pivot/contracts/` (public-api.md, changelog-store.md, manifest.schema.json)
- Quickstart: `specs/001-changelog-first-pivot/quickstart.md`

Stack: Python ≥3.10, Polars + PyArrow (no new runtime deps in P1; no Delta/Iceberg).
Store: append-only `<name>.flux/` folder (manifest.json + immutable events/*.parquet),
glob-readable by DuckDB. Keep it lightweight above all.
<!-- SPECKIT END -->
