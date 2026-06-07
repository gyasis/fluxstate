# Quickstart: FluxState Temporal Viewer

**Feature**: `002-fluxstate-temporal-viewer` · **Date**: 2026-06-06

How the Viewer is run and validated. Doubles as a manual smoke checklist; mirrors the spec's acceptance.

---

## 0. Environment

```bash
cd ~/Documents/code/fluxstate
uv pip install -e .                 # runtime unchanged (Polars + PyArrow)
uv pip install faker numpy duckdb pytz pytest   # demo/test-only deps (NOT runtime)
# viewer:
cd viewer && npm install            # Svelte 5 + Vite + DuckDB-WASM
```

## 1. CLI pre-work (US1) — drive the store from the terminal

```bash
# generate the seeded 1000x20 demo/stress store (Faker+numpy values, flux capture for time)
flux gen-fixture demo.flux --rows 1000 --cols 20 --steps 40 --seed 42
flux info demo.flux                       # schema, key, #events files, ts range, snap count

flux travel demo.flux --as-of 2026-03-01T00:00:00Z   # reconstructed table as-of a past point (typed)
flux timeline demo.flux 42 --field risk              # [{date,value}] typed
flux row-state demo.flux 7 --as-of now               # {state, resurrected}
flux view demo.flux --format parquet --out /tmp/now.parquet
```

**Expected**: every command's output equals the library function; `gen-fixture` with the same `--seed` is
byte-identical on re-run; `travel` before history → empty (not an error). (CLI-1..5)

## 2. Launch the viewer (US2–US6)

```bash
flux serve demo.flux                # → http://localhost:5173 over demo.flux
# or:  cd viewer && npm run dev
```

Walk the locked interaction model:
- **Scrub** the capsule slider → cells snap to as-of values; the track shows the change-density histogram;
  Play / ‹ › / arrows **snap to events**; `as of <date>` updates. (US2)
- A changed cell shows `old → new`, **pinned** on step, **fades** on play/drag; `⇄` toggles it off. (US2)
- **Hover** a cell → last-3 peek; **click** → pinned full-history inspector with a "now" marker that tracks
  the slider; deleted rows stripe, resurrected show `✦`, unborn fade `·`, Δ gutter + heat-tint visible. (US3)
- Scroll the table → only ~25–40 rows render (constant DOM); large stores stay smooth. (US4)
- **Filter**: set simple controls or type a `WHERE`; both narrow to the same rows; "showing X of N"; invalid
  SQL → inline error. (US5)
- Open the **static/print** view → ghosted latest values + lifecycle spark-path + callouts; prints clean. (US6)

## 3. Reconstruction parity (the correctness gate)

```bash
# Python exports ground truth for a known store (incl. delete + resurrection + genuine null)
pytest TESTS/test_parity_export.py -q          # writes parity ground-truth JSON
# viewer asserts its JS reconstruction matches Python exactly
cd viewer && npm run test                       # reconstruct.parity.test.ts → 100% match (SC-003 / VD-2)
```

**Expected**: JS `as_of` / `timeline` / `row_state` / `change_count` / `build_mirror_view` equal Python for
every probe, including deleted/resurrected/null. Any mismatch fails.

## 4. Hermetic test suite

```bash
pytest TESTS/ -q                                # CLI + fixture + parity export (no Snowflake)
cd viewer && npm run test && npm run test:e2e    # JS parity + Playwright interaction/perf smoke
```

**Expected (acceptance):**
- `demo.flux` (1000×20) scrubs/filters/inspects at ~60fps; every lifecycle/diff/heat feature is visibly
  exercised by the seeded story. (SC-001/005)
- A 100k-row synthetic store scrolls + scrubs ~60fps with constant rendered-row count. (SC-002)
- Fixture is reproducible; sparse (~1–5%/step) change; ≥1 birth, ≥1 delete, ≥1 resurrection present. (SC-004)
- Every history op is doable from the CLI without code. (SC-006)

## Acceptance ↔ artifact map

| Spec criterion | Verified by |
|---|---|
| SC-001 / SC-005 viewer fps + all interactions | `flux serve demo.flux` + Playwright smoke |
| SC-002 100k rows ~60fps constant DOM | virtualization + DuckDB-WASM windowing (US4) |
| SC-003 JS == Python reconstruction | parity export + `reconstruct.parity.test.ts` |
| SC-004 reproducible sparse fixture w/ lifecycle | `flux gen-fixture --seed` + `test_demo_fixture.py` |
| SC-006 full CLI capability | `TESTS/test_cli.py` |
| SC-007 simple==SQL filter | filter component test |
