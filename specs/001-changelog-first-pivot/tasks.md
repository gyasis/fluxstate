# Tasks: Changelog-First Storage Pivot

**Input**: Design documents from `specs/001-changelog-first-pivot/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/

**Tests**: INCLUDED — the spec mandates them (FR-016 + SC-006: "the 7 previously-failing tests
pass on the new API; new change-log tests pass; suite green in the venv"). New tests are written
per story and MUST fail before implementation.

**Organization**: Tasks grouped by user story (US1–US4) for independent implementation/testing.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: US1, US2, US3, US4 (maps to spec.md user stories)
- All paths are repository-root relative (flat Python package, per plan.md Structure Decision)

## Path Conventions

- Modules at repo root: `fluxstate.py` (facade), `changelog.py` (writer/diff), `reconstruct.py` (reads)
- Tests in `TESTS/`
- Store fixtures under pytest `tmp_path`

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Project initialization and skeletons

- [ ] T001 Confirm dev environment in `pyproject.toml`: `uv venv && uv pip install -e .` (polars, pyarrow, orjson, numpy, humanize, tqdm + the now-declared pre-existing `pydantic`); assert NO **new** P1 runtime dependency beyond surfacing pydantic (G1 / spec U3). Verify `import fluxstate` succeeds in a clean venv
- [ ] T002 [P] Verify `pytest` dev-dependency (added to `pyproject.toml` `[tool.poetry.group.dev.dependencies]`) and create `TESTS/conftest.py` with fixtures: `tmp_store` (a `tmp_path/"demo.flux"` path), `df_day1`/`df_day2`/`df_day3` sample `pl.DataFrame`s — NO Snowflake/Snowpark (research R8)
- [ ] T003 [P] Create module skeletons `changelog.py` and `reconstruct.py`, and export `ChangeLogStore` from `__init__.py` alongside `FluxState`

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The `.flux/` store substrate every story reads/writes — schema, codec, manifest, atomic commit

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

- [ ] T004 [P] Implement the dtype codec in `changelog.py`: `encode_value(v, dtype) -> str|None` and `decode_value(value, dtype) -> Any` covering `int64/float64/utf8/bool/datetime[us, UTC]/null`; genuine null is `value=None` (never the string `"NULL"`) — kills the sentinel collision (research R6 / data-model "Change Event")
- [ ] T005 [P] Implement UTC normalization in `changelog.py`: `to_utc(ts)` (tz-aware → convert, tz-naive → declare UTC) used for every event `timestamp` (FR-008 / research R6)
- [ ] T006 Define the Change Event Polars schema in `changelog.py` (`entity_id, timestamp[UTC], field, value, dtype, snapshot_id`) per `contracts/changelog-store.md` (depends on T004, T005)
- [ ] T007 Implement `ChangeLogStore` manifest I/O in `changelog.py`: `read_manifest()` / `write_manifest()` conforming to `contracts/manifest.schema.json`; reader resolves valid event files from the manifest ONLY (orphan parquet ignored) (FR-014 / STORE-4)
- [ ] T008 Implement the atomic events writer in `changelog.py`: write `events/.<ts>.parquet.tmp` → fsync → atomic rename → rewrite manifest via temp+rename; stamp row-group `min/max(timestamp)` stats + per-file ts range into the manifest entry (FR-013 / research R4, R5 / STORE-1, STORE-5)
- [ ] T009 [P] Implement `ChangeLogStore.list_events(as_of=None, window=None)` in `changelog.py`: returns manifest-valid event files, pruning files whose `[ts_min, ts_max]` lies entirely after `as_of`/outside `window` (file-skip, research R5)

**Checkpoint**: Store substrate ready — capture (US1) and reconstruction (US2/US3/US4) can begin

---

## Phase 3: User Story 1 - Append-only, idempotent change capture (Priority: P1) 🎯 MVP

**Goal**: A capture appends exactly one immutable events file of only the changed cells, and
re-capturing the same snapshot is a no-op.

**Independent Test**: Capture two differing snapshots → exactly one new `events/*.parquet` for the
second, prior file unchanged; re-submit the recorded snapshot → zero new events, store unchanged.

### Tests for User Story 1 ⚠️ (write first, must FAIL)

- [ ] T010 [P] [US1] `TESTS/test_changelog.py`: first capture writes one events file + manifest; second differing capture appends exactly one new file; no prior events-file bytes change; `SELECT * FROM 'tmp.flux/events/*.parquet'` via DuckDB returns all events (STORE-1/2/3, SC-001)
- [ ] T011 [P] [US1] `TESTS/test_idempotency.py`: re-capturing an already-recorded snapshot adds zero events and leaves the store byte-identical (SC-002 / API-2)

### Implementation for User Story 1

- [ ] T012 [P] [US1] Implement `row_hash` prefilter in `changelog.py`: `hash_rows` over canonicalized (stable-ordered) non-key columns for both prior and new frames (research R1, R2)
- [ ] T013 [US1] Implement `snapshot_id` computation in `changelog.py` (content-derived hash of the snapshot) (research R3) (depends on T012)
- [ ] T014 [US1] Implement the keyed diff in `changelog.py` `_diff(prev, new)`: full-outer join on `key_column` → keep only row-hash-changed/single-side rows → melt those → filter `old != new` with null-safe `is_not_distinct_from`, emitting INSERT/UPDATE Change Events (research R1 / FR-002, FR-003) (depends on T006, T012)
- [ ] T015 [US1] Implement `ChangeLogStore.capture(df, key_column, captured_at)` in `changelog.py`: diff → idempotency anti-join on `(entity_id, field, snapshot_id)` → atomic append + manifest commit; no-op when nothing changed or snapshot already present (FR-002, FR-004) (depends on T007, T008, T013, T014)
- [ ] T016 [US1] Wire `FluxState.update_mirror_table()` in `fluxstate.py` to delegate to `ChangeLogStore.capture()` (preserve signature; behavior = append-only capture) (FR-011 / API-1) (depends on T015)
- [ ] T017 [P] [US1] `TESTS/test_capture_scale.py`: wide-table guard — ~300 columns × synthetic rows where ~1–5% changed completes via the prefilter without a full melt; assert capture stays O(rows) (SC-008)

**Checkpoint**: US1 fully functional — append-only idempotent capture works and is testable standalone (MVP)

---

## Phase 4: User Story 2 - Accurate historical reconstruction with type fidelity (Priority: P2)

**Goal**: Rebuild table state as-of any past point and per-cell timelines, with values restored to
their original types.

**Independent Test**: Capture a sequence with numeric/datetime fields → reconstruct as-of an
intermediate point and a timeline → both match ground truth and carry original dtypes.

### Tests for User Story 2 ⚠️ (write first, must FAIL)

- [ ] T018 [P] [US2] `TESTS/test_reconstruct.py`: `as_of`/`get_timeline` match ground truth across timestamps; numeric/datetime values come back typed (not string-cast); `travel(T_before_history)` returns empty, not error (SC-004 / API-4)

### Implementation for User Story 2

- [ ] T019 [P] [US2] Implement reconstruction primitives in `reconstruct.py` over a Polars **LazyFrame** (internal): internal series-level `_as_of(history, T)` + public resolver `as_of(entity_id, field, T)` (I2 — keep them distinct), `get_timeline(entity_id, field=None)`, `change_count(entity_id)`; decode each value via the dtype codec; **re-export each as a thin `FluxState` method** so `fs.get_timeline(...)` works (U2 / research R7 / data-model primitives) (depends on T004, T009)
- [ ] T020 [US2] Implement `build_mirror_view(T="now")` in `reconstruct.py`: for each live `entity_id`, resolve each `field` via `as_of(., T)`, cast to dtype → a `pl.DataFrame`; use `list_events` file-skip pruning (FR-009, FR-010, FR-015) (depends on T019)
- [ ] T021 [US2] Wire `FluxState.travel(date)` and `FluxState.query_historical_value(query_date)` in `fluxstate.py` to `reconstruct.py` (UTC-compare; preserve signatures) (FR-011 / API-4, API-6) (depends on T020)

**Checkpoint**: US1 + US2 both work independently — capture and point-in-time/timeline reconstruction

---

## Phase 5: User Story 3 - Delete and resurrection continuity (Priority: P2)

**Goal**: A record present → deleted → present again shows one continuous trail under a single
`entity_id`.

**Independent Test**: Capture a record, capture a snapshot without it, capture one where it returns
→ timeline reads `active → __deleted__ → active` under one `entity_id`.

### Tests for User Story 3 ⚠️ (write first, must FAIL)

- [ ] T022 [P] [US3] `TESTS/test_lifecycle.py`: vanished row logs a `__deleted__` event (value null); reappearance appends SET events to the SAME `entity_id` (resurrection, not new identity); `row_state` during the deleted window reports deleted; full timeline is continuous (SC-003 / API-5)

### Implementation for User Story 3

- [ ] T023 [US3] Extend `changelog.py` `_diff` full-outer branches: "in OLD, not NEW" → emit a **single `__deleted__` marker row** (`field="__deleted__"`, `value=None`, `dtype="null"`, `ts=now`) per `entity_id` — NOT one null row per column (U1); "in NEW, not OLD & previously seen+deleted" → emit SET events to the same `entity_id` (resurrection); never-seen → INSERT (research R4 design-decision §resurrection / FR-005) (depends on T014)
- [ ] T024 [US3] Implement `row_state(entity_id, T)` in `reconstruct.py` → `{state: active|deleted|unborn, resurrected: bool}` by reading the `__deleted__` marker rows (U1) in the lifecycle transition chain; ensure `build_mirror_view` marks/omits deleted rows (FR-005 / data-model state transitions) (depends on T019, T023)

**Checkpoint**: US1 + US2 + US3 independent — capture, reconstruction, and lifecycle continuity

---

## Phase 6: User Story 4 - Multi-format output for downstream consumers (Priority: P3)

**Goal**: Reconstructed view available as dataframe (default), zero-copy columnar table, column file, or flat text.

**Independent Test**: Request each output format → each returns the expected representation with identical contents.

### Tests for User Story 4 ⚠️ (write first, must FAIL)

- [ ] T025 [P] [US4] `TESTS/test_output_formats.py`: `output_format="polars"` → `pl.DataFrame`; `"arrow"` → `pa.Table`; `"parquet"`/`"csv"` write files; contents identical across all four; unknown format → `ValueError` (SC-005 / API-3)

### Implementation for User Story 4

- [ ] T026 [US4] Implement `save_mirror_table(output_path_parquet=None, csv_path=None, *, output_format=None)` in `fluxstate.py` (delegating to `reconstruct.build_mirror_view`) with the **precedence rule** (I1, per contracts/public-api.md): explicit `output_format` wins; else a given `output_path_parquet`→write parquet / `csv_path`→write CSV (legacy behavior preserved); else return `pl.DataFrame`. Support `polars`/`arrow` (zero-copy `pa.Table`)/`parquet`/`csv`; `ValueError` on explicit-unknown format or a file format missing its path (FR-012 / API-3, API-6) (depends on T020)

**Checkpoint**: All four user stories independently functional

---

## Phase 7: Polish & Cross-Cutting Concerns

**Purpose**: Legacy-test rewrite, back-compat wiring, and acceptance validation across stories

- [ ] T027 Re-wire preserved helpers `get_change_statistics()`, `filter(...)`, `_filter(...)`, `filter_for_null_values(...)`, and `load_mirror_table(...)` in `fluxstate.py` to compute over the reconstructed view / change-log (FR-011 / API-6)
- [ ] T028 [P] First run `pytest TESTS/ --co -q` (or run the legacy suite) to **identify which of `Unit_1..8` are the actually-failing set** (U4 — the spec's "7 stale tests" is an assumption; `TESTS/` has 8 `Unit_*` files). Then rewrite that failing set to the new API and decouple from `snowflake.snowpark` (feed `pl.DataFrame` fixtures); audit/retire the remainder and `unit_1_mock.py` (FR-016 / SC-006 / research R8)
- [ ] T029 [P] Reconcile `mirror_validator.py` with the new path WITHOUT adding strict-raise (validator strictness is deferred — research R9); ensure it does not block capture; confirm `pydantic` is declared in `pyproject.toml` so `__init__.py`'s validator import succeeds in a clean venv (spec U3)
- [ ] T030 Run the full hermetic suite `pytest TESTS/ -q` → all rewritten + new tests green in the venv, no Snowflake (SC-006)
- [ ] T031 [P] Execute `quickstart.md` end-to-end incl. the DuckDB `SELECT * FROM '<name>.flux/events/*.parquet'` glob-readability check and assert no Delta/Iceberg directory exists (SC-007 / STORE-3, STORE-6)
- [ ] T032 [P] Update `README.md` to document the change-log API (`update_mirror_table`, `save_mirror_table(output_format=…)`, `travel`, `get_timeline`, `row_state`) and the `.flux/` store layout

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: no dependencies — start immediately
- **Foundational (Phase 2)**: depends on Setup — **BLOCKS all user stories**
- **User Stories (Phase 3–6)**: all depend on Foundational
  - US1 (P1) is the MVP and has no story dependencies
  - US2 (P2) depends only on Foundational; independently testable
  - US3 (P2) reuses US2's reconstruction primitives (T019) and US1's diff (T014) — soft dependency, still independently testable via its own `tmp_path` store
  - US4 (P3) depends on US2's `build_mirror_view` (T020)
- **Polish (Phase 7)**: depends on the stories being delivered (T028/T030 need the full new API)

### Within Each User Story

- Tests written first and FAIL before implementation
- changelog (write) before reconstruct (read) before facade wiring
- Story complete before moving to next priority

### Parallel Opportunities

- Setup: T002, T003 in parallel
- Foundational: T004, T005 parallel; T009 parallel after T007
- US1 tests T010, T011 parallel; T012 parallel; T017 parallel
- Cross-story (after Foundational): US2 and US4 reconstruction can proceed alongside US1 capture if staffed (different files: `reconstruct.py` vs `changelog.py`)
- Polish: T028, T029, T031, T032 parallel

---

## Parallel Example: Foundational Phase

```bash
# After T001–T003, launch the independent substrate pieces together:
Task: "Implement dtype codec in changelog.py"            # T004
Task: "Implement UTC normalization in changelog.py"      # T005  (different functions; coordinate one writer)
```

## Parallel Example: User Story 1

```bash
# Write US1 tests together (must fail first):
Task: "test_changelog.py append-only/no-rewrite/glob-readable"   # T010
Task: "test_idempotency.py re-capture no-op"                      # T011
```

---

## Implementation Strategy

### MVP First (User Story 1 only)

1. Phase 1 Setup → 2. Phase 2 Foundational (CRITICAL — blocks all) → 3. Phase 3 US1
4. **STOP & VALIDATE**: append-only idempotent capture works standalone → demo the `.flux/` store

### Incremental Delivery

1. Setup + Foundational → store substrate ready
2. US1 → append-only capture (MVP) → validate
3. US2 → reconstruction + type fidelity → validate
4. US3 → delete/resurrection continuity → validate
5. US4 → multi-format output → validate
6. Polish → rewrite legacy tests, wire back-compat helpers, run quickstart + suite green

### Suggested MVP scope

**US1 (P1)** alone — append-only, idempotent capture into the `.flux/` store — is the demonstrable
MVP and the core of the storage pivot.

---

## Notes

- [P] = different files, no incomplete-task dependency
- `changelog.py` is the single writer module — tasks touching it within a phase are sequential unless they edit disjoint functions
- Verify each story's tests FAIL before implementing
- Commit after each task or logical group
- Lightweight above all: no new runtime deps; no Delta/Iceberg; store stays glob-readable
- Deferred to fast-follow (NOT in these tasks): checkpoint compaction, Arrow/Lazy streaming reconstruction for the viewer, `pack`/`unpack` zip export, storage adapters, validator strictness decision
