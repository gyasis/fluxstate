# Progress

**Last Updated**: 2026-06-05 18:20:09

## Overall Progress
- Total Tasks: 33
- Completed: 32 ✅
- Pending: 1 ⏳
- Progress: 96%

## Task Breakdown
- [x] T001 Confirm dev environment in `pyproject.toml`: `uv venv && uv pip install -e .` (polars, pyarrow, orjson, numpy, humanize, tqdm + the now-declared pre-existing `pydantic`); assert NO **new** P1 runtime dependency beyond surfacing pydantic (G1 / spec U3). Verify `import fluxstate` succeeds in a clean venv
- [x] T002 [P] Verify `pytest` dev-dependency (added to `pyproject.toml` `[tool.poetry.group.dev.dependencies]`) and create `TESTS/conftest.py` with fixtures: `tmp_store` (a `tmp_path/"demo.flux"` path), `df_day1`/`df_day2`/`df_day3` sample `pl.DataFrame`s — NO Snowflake/Snowpark (research R8)
- [x] T003 [P] Create module skeletons `changelog.py` and `reconstruct.py`, and export `ChangeLogStore` from `__init__.py` alongside `FluxState`
- [x] T004 [P] Implement the dtype codec in `changelog.py`: `encode_value(v, dtype) -> str|None` and `decode_value(value, dtype) -> Any` covering `int64/float64/utf8/bool/datetime[us, UTC]/null`; genuine null is `value=None` (never the string `"NULL"`) — kills the sentinel collision (research R6 / data-model "Change Event")
- [x] T005 [P] Implement UTC normalization in `changelog.py`: `to_utc(ts)` (tz-aware → convert, tz-naive → declare UTC) used for every event `timestamp` (FR-008 / research R6)
- [x] T006 Define the Change Event Polars schema in `changelog.py` (`entity_id, timestamp[UTC], field, value, dtype, snapshot_id`) per `contracts/changelog-store.md` (depends on T004, T005)
- [x] T007 Implement `ChangeLogStore` manifest I/O in `changelog.py`: `read_manifest()` / `write_manifest()` conforming to `contracts/manifest.schema.json`; reader resolves valid event files from the manifest ONLY (orphan parquet ignored) (FR-014 / STORE-4)
- [x] T008 Implement the atomic events writer in `changelog.py`: write `events/.<ts>.parquet.tmp` → fsync → atomic rename → rewrite manifest via temp+rename; stamp row-group `min/max(timestamp)` stats + per-file ts range into the manifest entry (FR-013 / research R4, R5 / STORE-1, STORE-5)
- [x] T009 [P] Implement `ChangeLogStore.list_events(as_of=None, window=None)` in `changelog.py`: returns manifest-valid event files, pruning files whose `[ts_min, ts_max]` lies entirely after `as_of`/outside `window` (file-skip, research R5)
- [x] T010 [P] [US1] `TESTS/test_changelog.py`: first capture writes one events file + manifest; second differing capture appends exactly one new file; no prior events-file bytes change; `SELECT * FROM 'tmp.flux/events/*.parquet'` via DuckDB returns all events (STORE-1/2/3, SC-001)
- [x] T011 [P] [US1] `TESTS/test_idempotency.py`: re-capturing an already-recorded snapshot adds zero events and leaves the store byte-identical (SC-002 / API-2)
- [x] T012 [P] [US1] Implement `row_hash` prefilter in `changelog.py`: `hash_rows` over canonicalized (stable-ordered) non-key columns for both prior and new frames (research R1, R2)
- [x] T013 [US1] Implement `snapshot_id` computation in `changelog.py` (content-derived hash of the snapshot) (research R3) (depends on T012)
- [x] T014 [US1] Implement the keyed diff in `changelog.py` `_diff(prev, new)`: full-outer join on `key_column` → keep only row-hash-changed/single-side rows → melt those → filter `old != new` with null-safe `is_not_distinct_from`, emitting INSERT/UPDATE Change Events (research R1 / FR-002, FR-003) (depends on T006, T012)
- [x] T015 [US1] Implement `ChangeLogStore.capture(df, key_column, captured_at)` in `changelog.py`: diff → idempotency anti-join on `(entity_id, field, snapshot_id)` → atomic append + manifest commit; no-op when nothing changed or snapshot already present (FR-002, FR-004) (depends on T007, T008, T013, T014)
- [x] T016 [US1] Wire `FluxState.update_mirror_table()` in `fluxstate.py` to delegate to `ChangeLogStore.capture()` (preserve signature; behavior = append-only capture) (FR-011 / API-1) (depends on T015)
- [x] T017 [P] [US1] `TESTS/test_capture_scale.py`: wide-table guard — ~300 columns × synthetic rows where ~1–5% changed completes via the prefilter without a full melt; assert capture stays O(rows) (SC-008)
- [x] T018 [P] [US2] `TESTS/test_reconstruct.py`: `as_of`/`get_timeline` match ground truth across timestamps; numeric/datetime values come back typed (not string-cast); `travel(T_before_history)` returns empty, not error (SC-004 / API-4)
- [x] T019 [P] [US2] Implement reconstruction primitives in `reconstruct.py` over a Polars **LazyFrame** (internal): internal series-level `_as_of(history, T)` + public resolver `as_of(entity_id, field, T)` (I2 — keep them distinct), `get_timeline(entity_id, field=None)`, `change_count(entity_id)`; decode each value via the dtype codec; **re-export each as a thin `FluxState` method** so `fs.get_timeline(...)` works (U2 / research R7 / data-model primitives) (depends on T004, T009)
- [x] T020 [US2] Implement `build_mirror_view(T="now")` in `reconstruct.py`: for each live `entity_id`, resolve each `field` via `as_of(., T)`, cast to dtype → a `pl.DataFrame`; use `list_events` file-skip pruning (FR-009, FR-010, FR-015) (depends on T019)
- [x] T021 [US2] Wire `FluxState.travel(date)` and `FluxState.query_historical_value(query_date)` in `fluxstate.py` to `reconstruct.py` (UTC-compare; preserve signatures) (FR-011 / API-4, API-6) (depends on T020)
- [x] T022 [P] [US3] `TESTS/test_lifecycle.py`: vanished row logs a `__deleted__` event (value null); reappearance appends SET events to the SAME `entity_id` (resurrection, not new identity); `row_state` during the deleted window reports deleted; full timeline is continuous (SC-003 / API-5)
- [x] T023 [US3] Extend `changelog.py` `_diff` full-outer branches: "in OLD, not NEW" → emit a **single `__deleted__` marker row** (`field="__deleted__"`, `value=None`, `dtype="null"`, `ts=now`) per `entity_id` — NOT one null row per column (U1); "in NEW, not OLD & previously seen+deleted" → emit SET events to the same `entity_id` (resurrection); never-seen → INSERT (research R4 design-decision §resurrection / FR-005) (depends on T014)
- [x] T024 [US3] Implement `row_state(entity_id, T)` in `reconstruct.py` → `{state: active|deleted|unborn, resurrected: bool}` by reading the `__deleted__` marker rows (U1) in the lifecycle transition chain; ensure `build_mirror_view` marks/omits deleted rows (FR-005 / data-model state transitions) (depends on T019, T023)
- [x] T025 [P] [US4] `TESTS/test_output_formats.py`: `output_format="polars"` → `pl.DataFrame`; `"arrow"` → `pa.Table`; `"parquet"`/`"csv"` write files; contents identical across all four; unknown format → `ValueError` (SC-005 / API-3)
- [x] T026 [US4] Implement `save_mirror_table(output_path_parquet=None, csv_path=None, *, output_format=None)` in `fluxstate.py` (delegating to `reconstruct.build_mirror_view`) with the **precedence rule** (I1, per contracts/public-api.md): explicit `output_format` wins; else a given `output_path_parquet`→write parquet / `csv_path`→write CSV (legacy behavior preserved); else return `pl.DataFrame`. Support `polars`/`arrow` (zero-copy `pa.Table`)/`parquet`/`csv`; `ValueError` on explicit-unknown format or a file format missing its path (FR-012 / API-3, API-6) (depends on T020)
- [x] T027 Re-wire preserved helpers `get_change_statistics()`, `filter(...)`, `_filter(...)`, `filter_for_null_values(...)`, and `load_mirror_table(...)` in `fluxstate.py` to compute over the reconstructed view / change-log (FR-011 / API-6)
- [x] T028 [P] First run `pytest TESTS/ --co -q` (or run the legacy suite) to **identify which of `Unit_1..8` are the actually-failing set** (U4 — the spec's "7 stale tests" is an assumption; `TESTS/` has 8 `Unit_*` files). Then rewrite that failing set to the new API and decouple from `snowflake.snowpark` (feed `pl.DataFrame` fixtures); audit/retire the remainder and `unit_1_mock.py` (FR-016 / SC-006 / research R8)
- [x] T029 [P] Reconcile `mirror_validator.py` with the new path WITHOUT adding strict-raise (validator strictness is deferred — research R9); ensure it does not block capture; confirm `pydantic` is declared in `pyproject.toml` so `__init__.py`'s validator import succeeds in a clean venv (spec U3)
- [x] T030 Run the full hermetic suite `pytest TESTS/ -q` → all rewritten + new tests green in the venv, no Snowflake (SC-006)
- [x] T031 [P] Execute `quickstart.md` end-to-end incl. the DuckDB `SELECT * FROM '<name>.flux/events/*.parquet'` glob-readability check and assert no Delta/Iceberg directory exists (SC-007 / STORE-3, STORE-6)
- [x] T032 [P] Update `README.md` to document the change-log API (`update_mirror_table`, `save_mirror_table(output_format=…)`, `travel`, `get_timeline`, `row_state`) and the `.flux/` store layout
- [P] = different files, no incomplete-task dependency

## Recent Milestones
120b251 [MILESTONE] Dev-kid initialized
