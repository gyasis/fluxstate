# FluxState Constitution

The non-negotiable principles that govern every FluxState change. Specs run a **Constitution
Check** against the gates below (G1–G8); the `001`/`002` specs already cite G1–G6. A change that
violates a gate is rejected or must justify the deviation in its plan's Complexity Tracking.

## Core Principles

### I. Platform-Agnostic — Available on ALL Data Platforms (NON-NEGOTIABLE, FOREMOST) · G8
FluxState works with **any** data platform and must never be coded for a specific one. Databricks,
Snowflake, PostgreSQL, a generic Lakehouse, LakeBase, Supabase, object stores (S3/ADLS/GCS), or a
plain local filesystem — all are reached through the **pluggable storage interface**, and every
platform-specific integration ships as an **optional sidecar / extra** (e.g. `[databricks]`,
`[snowflake]`, `[postgres]`, `[supabase]`), **never** inside the core. The core change-log model and
reconstruction are platform-free; adding a new platform means adding a sidecar, not touching the
engine. No platform is privileged — Databricks is simply the first sidecar, not the design center.

### II. Faithful Recorder — not a semantic-equality engine (NON-NEGOTIABLE) · G7
FluxState records what the source emits in its *serialized output*, verbatim. `1.1` → `1.10`, a
timezone-representation shift, a precision change — each **is** a change and is surfaced; FluxState
never canonicalizes values to decide they are "really equal." Every value is normalized to a
canonical **text** form for storage/comparison, carrying a per-column `dtype` tag so a consumer can
re-cast for typed sort/compare. Faithfulness over cleverness. (Semantic "normalize" behavior, e.g.
the viewer's `≈` toggle, is a *read-time consumer choice*, never baked into what is recorded.)

### III. Lightweight Above All · G1
The core stays minimal — **Polars + PyArrow only**; no heavyweight table-format or platform SDK
dependency (no Delta/Iceberg/Snowpark/`databricks-sdk`) in the core dependency tree. Any capability
needing heavy or platform-specific libraries ships as an **optional extra** (Principle I), never as
a core dep. `pip install fluxstate` must remain lean.

### IV. Portable & Glob-Readable · G2
A store is readable by ubiquitous, lightweight tools with **no FluxState code** — plain columnar
Parquet (glob-readable by DuckDB / DuckDB-WASM / Polars) plus a plain manifest, or a first-class
open table (Delta/Iceberg/partitioned-Parquet dataset). **No proprietary or opaque container
format.** Engine-portability is the product; backends may vary but must never lock data in.

### V. Append-Only, Immutable & Idempotent · G4
Each capture appends **one immutable** unit (a new Parquet event file / a table transaction) — an
existing events file is **never rewritten**. Capture is **atomic** (the manifest/transaction is the
commit point). Capture is **idempotent**: a content-derived `snapshot_id` anti-join makes
re-submitting an already-recorded snapshot a no-op. Deletions are a single `__deleted__` marker
(one continuous timeline per `entity_id`, delete→resurrect preserved). Timestamps are UTC-normalized.
Values round-trip at full fidelity via the `dtype` tag (no cast-everything-to-string loss).

### VI. API Back-Compat & Reconstruction Parity · G3
Existing public API signatures (`update_mirror_table`, `save_mirror_table`, `travel`,
`query_historical_value`, `get_timeline`, `row_state`, …) are preserved; changes are additive.
Reconstruction (`as_of` / `timeline` / `row_state` / mirror) MUST return **identical results across
every implementation** — Python, the JS viewer port, and any storage backend / platform sidecar —
enforced by a parity test against ground truth.

### VII. Test Coverage as a Gate · G5
Every change keeps green a suite covering **capture, reconstruction, deletion/resurrection,
idempotency, and type fidelity**, plus the reconstruction-parity test. New backends/sidecars add
their own contract + parity tests. Tests run hermetically (in-memory `pl.DataFrame`s; no live
warehouse required).

### VIII. Simplicity / YAGNI · G6
Build for a real, present use case; **defer** speculative machinery (compaction, streaming
reconstruction, pack/unpack, extra sidecars) until a concrete need exists. Prefer the lighter option
on any tie. Optional extras over core bloat; **one engine-agnostic algorithm over per-platform
forks**.

## Additional Constraints

- **Platform integrations are sidecars, always.** A platform sidecar wires the core's storage
  interface to that platform's native store (Delta table, Snowflake stage/table, Postgres/Supabase
  table, object-store path). It MUST NOT re-implement the change-log or reconstruction, and MUST NOT
  leak platform assumptions back into core.
- **Economy / no wasted work.** Idempotent ingestion is mandatory — never re-write or re-process
  unchanged content (content-hash `snapshot_id` before any append). Storage grows with how much data
  *changes*, not table-size × captures.
- **Storage is pluggable, the model is fixed.** The change-event schema
  `(entity_id, timestamp, field, value, dtype, snapshot_id)`, the `__deleted__` marker, idempotency,
  and reconstruction are invariant; only *where/how bytes persist* varies by backend/sidecar.
- **Stack.** Python ≥ 3.10; Polars (change-detection + reconstruction), PyArrow/Parquet (columnar
  storage). Viewer: Svelte 5 + Vite + DuckDB-WASM. Faker/numpy/DuckDB are demo/test-only.

## Development Workflow & Quality Gates

- **Spec-driven.** Non-trivial features go through SpecKit (`/speckit-specify` → `-plan` → `-tasks`),
  seeded by an in-repo PRD in `prd/`. dev-kid executes tasks; sentinel/verification per `dev-kid.yml`.
- **Constitution Check (blocking).** Every plan includes a Constitution Check table scoring the change
  against G1–G8. A violation is rejected unless justified in Complexity Tracking with a lighter
  alternative considered. **G8 (platform-agnostic) is the first thing checked** for any storage/
  integration work: is a platform assumption leaking into core? If yes, it must move to a sidecar.
- **Parity is the merge gate** for any change touching reconstruction or adding a backend/sidecar.

## Governance

This constitution supersedes ad-hoc practice. Amendments require: a documented rationale, a version
bump (semver below), and — if a principle changes — a migration/compat note. Every PR/spec is expected
to verify compliance; unavoidable complexity must be justified, not hidden. Runtime development guidance
lives in `AGENTS.md`, `CLAUDE.md`, and `docs/API.md`.

**Version**: 1.0.0 | **Ratified**: 2026-07-01 | **Last Amended**: 2026-07-01
