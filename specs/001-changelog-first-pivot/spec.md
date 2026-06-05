# Feature Specification: Changelog-First Storage Pivot

**Feature Branch**: `001-changelog-first-pivot`  
**Created**: 2026-06-05  
**Status**: Draft  
**Input**: PRD `prd/fluxstate_changelog_pivot_2026-06-05.md` (P1 — changelog-first pivot + CDC hardening)

## User Scenarios & Testing *(mandatory)*

FluxState tracks one table/view's cell-level change history for HIPAA-regulated HCC
risk-coding audit trails. Today it re-materializes a whole "mirror table" (JSON-in-cell
snapshots) on every capture, which rewrites the entire dataset each time, loses type
information by casting everything to strings, and cannot represent a deletion. This feature
makes an **append-only change-log** the source of truth, so each capture writes only what
changed, types are preserved, and deletions are recorded — while the historical-query API
that existing callers depend on keeps working.

### User Story 1 - Append-only, idempotent change capture (Priority: P1)

An audit-pipeline engineer captures successive snapshots of an HCC table. Each capture must
record only the cells that changed since the previous state and must never rewrite the whole
history. Re-running a capture for a snapshot already recorded must be a no-op.

**Why this priority**: This is the storage pivot itself — the foundation every other story
builds on. Without append-only, idempotent capture there is no change-log to reconstruct from,
and the existing scaling/quality problems remain.

**Independent Test**: Capture two differing snapshots of the same table and confirm exactly
one new change-set is appended for the second (no rewrite of the first). Re-submit an
already-captured snapshot and confirm nothing new is written and history is unchanged.

**Acceptance Scenarios**:

1. **Given** an empty store, **When** a first snapshot is captured, **Then** a single
   change-set is recorded containing every populated cell as an initial value, and the store
   manifest references it.
2. **Given** a store with one captured snapshot, **When** a second snapshot changes some cells,
   **Then** only the changed cells are appended as a new change-set and prior change-sets are
   left byte-for-byte unchanged.
3. **Given** a store that already contains a given snapshot, **When** that identical snapshot is
   captured again, **Then** no new change-set is appended and the store is unchanged.
4. **Given** a snapshot with columns in a different order or with renamed-but-equivalent typing,
   **When** it is captured, **Then** only genuinely changed values are recorded (column order
   alone produces no spurious changes).

---

### User Story 2 - Accurate historical reconstruction with type fidelity (Priority: P2)

An auditor needs to answer "what did this record look like as of date X?" and "show me the full
history of this cell." Reconstruction must rebuild the table's state at any past point and
restore each value to its original type (numbers as numbers, dates as dates), not as strings.

**Why this priority**: Reconstruction is the read side that gives the captured change-log its
audit value. It depends on P1 existing but is the primary user-facing payoff.

**Independent Test**: Capture a sequence of snapshots with numeric and datetime fields, then
reconstruct state "as of" an intermediate point and a per-cell timeline; verify both match the
known ground truth and that reconstructed values carry their original types.

**Acceptance Scenarios**:

1. **Given** several captured snapshots, **When** state is reconstructed as of an intermediate
   timestamp, **Then** the result equals the table's actual state at that time.
2. **Given** a numeric or datetime field that changed over time, **When** its history/timeline is
   requested, **Then** each historical value is returned in its original type, not string-cast.
3. **Given** the existing historical-query entry points (`save_mirror_table`, `travel`,
   `query_historical_value`), **When** they are called as before, **Then** they return results
   consistent with the new change-log without callers changing their usage.
4. **Given** a reconstruction request for a timestamp before any data existed, **When** it runs,
   **Then** it returns an empty/appropriate result rather than an error.

---

### User Story 3 - Delete and resurrection continuity (Priority: P2)

A record may disappear from a snapshot (deleted upstream) and later reappear. The audit trail
must show one continuous lineage for that record: present → deleted → present again, all under
the same record identity.

**Why this priority**: Deletions are correctness-critical for a HIPAA audit trail and impossible
to express in the old mirror-table model; capturing them is a core reason for the pivot.

**Independent Test**: Capture a record, capture a snapshot where it is absent, then capture a
snapshot where it returns; reconstruct its timeline and confirm a continuous
active → deleted → active trail under one identity. (Note: this story *extends* US1's
full-outer-join diff and US2's reconstruction primitives rather than standing entirely alone — it
is verifiable in isolation against its own change-log store, but is built after US1/US2.)

**Acceptance Scenarios**:

1. **Given** a record present in snapshot 1 and absent in snapshot 2, **When** snapshot 2 is
   captured, **Then** a single `__deleted__` marker event (value null) is recorded for that
   record's `entity_id`.
2. **Given** a record previously deleted, **When** it reappears in a later snapshot, **Then** a
   reactivation is recorded and its timeline reads active → deleted → active without a new,
   separate identity.
3. **Given** a record's state is reconstructed as of a time when it was deleted, **When**
   reconstruction runs, **Then** the record is absent from (or marked deleted in) that point-in-time
   result.

---

### User Story 4 - Multi-format output for downstream consumers (Priority: P3)

A consumer (the FluxState Viewer, a DuckDB/Polars analysis, or a CSV export) requests the
reconstructed table in the format it needs. The reconstructed view must be available as a
dataframe (default), a zero-copy columnar table, a column-oriented file, or a flat text export.

**Why this priority**: Format flexibility unblocks downstream consumers (notably the viewer) but
is additive on top of correct capture and reconstruction.

**Independent Test**: Request the reconstructed table in each supported output format and confirm
each returns the expected representation with matching contents.

**Acceptance Scenarios**:

1. **Given** a reconstructed table, **When** the default output is requested, **Then** a dataframe
   is returned.
2. **Given** a reconstructed table, **When** the zero-copy columnar format is requested, **Then** a
   columnar table object is returned without a full data copy.
3. **Given** a reconstructed table, **When** a file format (column-oriented or flat text) is
   requested, **Then** a corresponding file/representation is produced with identical contents.

---

### Edge Cases

- **Empty / no-op snapshot**: capturing a snapshot identical to current state appends nothing.
- **Wide HCC tables**: a table with very many columns (~300) and hundreds of thousands of rows is
  captured without melting the full table into an unmanageable intermediate (no per-cell blowup).
- **Column reorder or equivalent retyping** between snapshots produces no spurious change events.
- **Timezone-mixed timestamps**: capture inputs with differing timezones are normalized to a single
  canonical zone so ordering and "as of" comparisons are consistent.
- **Deleted-then-returned record** is one continuous identity, never two.
- **Partial / interrupted capture**: a capture that fails midway must not leave the store in a
  state that is read as a successful partial commit (capture is all-or-nothing).
- **Type that cannot be losslessly tagged**: a value whose type cannot be cleanly captured is
  handled by the chosen validator policy (see Assumptions) rather than silently corrupting history.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST persist change history as an append-only change-log that is the
  single source of truth for all historical queries.
- **FR-002**: Each capture MUST append only the changes since the prior recorded state; it MUST
  NOT rewrite or re-materialize previously recorded history.
- **FR-003**: The system MUST detect changes by comparing the new snapshot to prior state keyed by
  record identity, such that column order and equivalent typing do not produce spurious changes.
- **FR-004**: Capture MUST be idempotent — submitting a snapshot already recorded MUST NOT append
  duplicate events or alter the store.
- **FR-005**: The system MUST record deletions (a previously present record now absent) and
  reactivations (a deleted record reappearing) under a single, continuous record identity.
- **FR-006**: Each recorded change MUST carry, at minimum: record identity, an event timestamp, the
  field changed, the new value, the value's type, and an identifier of the snapshot that produced it.
- **FR-007**: The system MUST preserve and restore each value's original type on reconstruction
  (e.g., numeric and datetime values are not degraded to strings).
- **FR-008**: The system MUST normalize all event timestamps to a single canonical timezone (UTC).
- **FR-009**: The system MUST reconstruct the table's full state as of any past point in time and
  MUST reconstruct the per-cell/per-record timeline of changes.
- **FR-010**: The mirror table MUST be presented as a view derived from the change-log rather than
  as independently stored state.
- **FR-011**: The existing historical-query entry points (`save_mirror_table`, `travel`,
  `query_historical_value`) MUST continue to function for existing callers without requiring them to
  change how they call these operations.
- **FR-012**: The reconstructed-state output MUST be available in multiple formats: a dataframe
  (default), a zero-copy columnar table, a column-oriented file, and a flat text export.
- **FR-013**: Capture MUST be atomic — a failed or interrupted capture MUST NOT leave a partially
  committed change-set that later reads as valid history.
- **FR-014**: The change-log store MUST remain readable by lightweight, ubiquitous analytical tools
  (glob-readable column-oriented files plus a manifest) — no proprietary or opaque container format.
- **FR-015**: The system MUST support time-range scoping of reconstruction so that history outside
  the requested window can be skipped rather than scanned in full.
- **FR-016**: The test suite MUST cover capture, reconstruction, deletion/resurrection, idempotency,
  and type fidelity, and MUST pass against the new API; the 7 previously-failing tests MUST be
  brought to green on the new API.

### Non-Functional / Constraint Requirements

- **NFR-001 (Lightweight constitution)**: The feature MUST NOT add a heavyweight table-format
  dependency (no Delta/Iceberg-style dependency tail) and MUST keep dependencies minimal.
- **NFR-002 (Portability)**: The stored change-log MUST be directly readable by common engines
  (e.g., glob-readable by DuckDB/Polars) without bespoke tooling.
- **NFR-003 (Scale)**: Capture MUST scale to wide tables (~300 columns) over hundreds of thousands
  of rows without per-cell materialization blowup, and the already-shipped O(rows) update behavior
  MUST be preserved.

### Key Entities *(include if feature involves data)*

- **Change-Log Store**: The append-only home of all history — a manifest plus a growing set of
  change-set files (and, later, checkpoints). The authoritative source for every historical query.
- **Change Event**: A single recorded cell change — record identity, event timestamp (canonical
  zone), field, new value, value type, and originating snapshot identity. Deletion and reactivation
  are expressed as events on the record's identity.
- **Snapshot**: A point-in-time input dataset submitted for capture, uniquely identified so repeat
  submissions are idempotent.
- **Reconstructed Mirror View**: The point-in-time or current table state derived on demand from
  the change-log; replaces the previously stored mirror table and feeds the multi-format output.
- **Record Identity**: The stable key that ties all events (including delete/reactivate) for one
  logical record together across snapshots.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Capturing a new differing snapshot appends exactly one new change-set and rewrites
  none of the existing history (0 prior files modified).
- **SC-002**: Re-capturing an already-recorded snapshot results in 0 new events and a byte-identical
  store.
- **SC-003**: A record that is present, then deleted, then present again yields a single continuous
  active → deleted → active timeline under one identity (verified for 100% of such cases in tests).
- **SC-004**: Point-in-time reconstruction matches known ground truth for 100% of tested timestamps,
  with numeric and datetime values restored to their original types (0 string-cast regressions).
- **SC-005**: The default output returns a dataframe and the zero-copy format returns a columnar
  table, with identical contents across all supported output formats.
- **SC-006**: The 7 previously-failing tests plus the new change-log/delete/resurrection/idempotency
  tests all pass; the full suite is green in the project environment.
- **SC-007**: No heavyweight table-format dependency is added (dependency count for that capability
  = 0); the stored change-log is readable directly by a standard analytical engine without custom code.
- **SC-008**: A wide-table capture (~300 columns, hundreds of thousands of rows) completes without
  per-cell materialization blowup and preserves the O(rows) update characteristic from P0.

## Assumptions

- **Validator strictness is deferred** (PRD Open Item). Default assumption for P1: values whose type
  cannot be cleanly captured are coerced/tagged leniently with the discrepancy surfaced (lenient,
  not hard-raise); the strict-raise-vs-lenient decision is finalized in a later cut and does not
  block P1.
- **HCC width** is assumed to be on the order of ~300 columns over 500k rows for sizing the
  change-detection prefilter; exact width is to be confirmed but does not change the design.
- **Store location** for P1 is a local change-log folder; remote/staged storage (e.g., S3 or a
  warehouse stage) is a fast-follow Storage Adapter and out of P1 scope.
- **The FluxState Viewer** is the primary downstream consumer of the change-log but is NOT in this
  feature's code scope; this spec only guarantees the change-log and outputs it consumes.
- **P0 is complete** (O(rows) update via dict-map; stale fork removed) and is the starting point for
  this work; this spec covers P1 only.
- **Compaction/checkpointing and streaming reconstruction** are explicitly fast-follow and out of
  P1 scope; the store layout MUST leave room for them but need not implement them now.
- Existing callers interact through the documented historical-query entry points; no undocumented
  internal access patterns need to be preserved.
