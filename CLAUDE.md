<!-- SPECKIT START -->
Active feature: **002-fluxstate-temporal-viewer** ("Temporal Ghost" viewer + `flux` CLI pre-work).

For technologies, project structure, constraints, and design decisions, read the
current plan and its Phase 0/1 artifacts:
- Plan: `specs/002-fluxstate-temporal-viewer/plan.md`
- Spec: `specs/002-fluxstate-temporal-viewer/spec.md`
- Research: `specs/002-fluxstate-temporal-viewer/research.md`
- Data model: `specs/002-fluxstate-temporal-viewer/data-model.md`
- Contracts: `specs/002-fluxstate-temporal-viewer/contracts/` (cli.md, viewer-data.md, parity.schema.json)
- Quickstart: `specs/002-fluxstate-temporal-viewer/quickstart.md`

Builds on the SHIPPED change-log substrate (feature 001): `changelog.py` + `reconstruct.py` +
`<name>.flux/` store. The viewer reproduces the LOCKED prototype in `docs/viewer/` and consumes the real
store via DuckDB-WASM, reconstructing through a JS port of `reconstruct.py` kept honest by a Python-parity
test. Pre-work: a `flux` CLI (stdlib argparse) + a seeded 1000×20 demo/stress fixture (Faker + numpy.random
values, flux `capture()` for the passage of time). Stack: Python ≥3.10 (lib/CLI/fixture) + Svelte 5 + Vite +
DuckDB-WASM (viewer); no heavy grid lib; Faker/numpy are demo/test-only. Keep it lightweight above all.

Prior shipped feature: **001-changelog-first-pivot** — `specs/001-changelog-first-pivot/` (the data substrate).
<!-- SPECKIT END -->
