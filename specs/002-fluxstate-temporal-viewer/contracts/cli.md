# Contract: `flux` CLI (pre-work launcher)

**Feature**: `002-fluxstate-temporal-viewer` · **Date**: 2026-06-06

A thin, stdlib-`argparse` command surface that makes FluxState fully driveable from the terminal. Every
subcommand is a wrapper over the shipped library — **output MUST equal the library function** (FR-015). Text
by default; `--json` for machine output (errors → stderr, non-zero exit on failure).

Registered as a console entry point: `flux = flux_cli:main` in `pyproject.toml`.

```text
flux <command> [args] [--json]
```

| Command | Args | Library call | Output |
|---|---|---|---|
| `capture` | `<store.flux> <input.parquet\|csv> --key <col> [--at <ISO>]` | `ChangeLogStore.capture(df, key, captured_at)` | capture result `{events_added, snapshot_id, file, noop}` |
| `travel` | `<store.flux> --as-of <ISO\|now>` | `reconstruct.build_mirror_view(store, T)` | reconstructed table (text table / json records); empty (not error) if before history |
| `timeline` | `<store.flux> <entity_id> [--field <col>]` | `reconstruct.get_timeline(store, id, field)` | `[{date, value}]` typed; with `--field` omitted → all fields incl. `field` |
| `row-state` | `<store.flux> <entity_id> [--as-of <ISO\|now>]` | `reconstruct.row_state(store, id, T)` | `{state: active\|deleted\|unborn, resurrected: bool}` |
| `view` | `<store.flux> [--as-of <ISO\|now>] [--format polars\|arrow\|parquet\|csv] [--out <path>]` | `build_mirror_view` + `FluxState.save_mirror_table` | the materialized view (printed or written) |
| `gen-fixture` | `<store.flux> [--rows 1000] [--cols 20] [--steps 40] [--seed 42]` | `scripts/demo_fixture.py` | writes the seeded demo store; prints summary (rows, steps, births/deletes/resurrections, mean change %) |
| `row-count` / `info` | `<store.flux>` | `read_manifest` + counts | store summary (schema, key, #events files, ts range, snap count) |
| `serve` | `<store.flux> [--port 5173] [--no-open]` | launch `viewer/` (Vite) over the store | prints the URL; serves the viewer reading this store |

## Behavioral guarantees (testable — `TESTS/test_cli.py`)

- **CLI-1**: `flux capture` twice on differing snapshots → exactly one new events file on the 2nd; a third
  identical capture is a no-op (`noop:true`, store byte-identical). (== `ChangeLogStore.capture` guarantees.)
- **CLI-2**: `flux travel --as-of <T>` equals `build_mirror_view(store, T)` exactly, types preserved;
  `--as-of` before history → empty result, exit 0 (NOT an error).
- **CLI-3**: `flux timeline` / `row-state` outputs equal the corresponding `reconstruct.*` return values
  (incl. deleted + resurrected entities), values typed.
- **CLI-4**: `flux gen-fixture --seed N` is reproducible — same seed → byte-identical store.
- **CLI-5**: `flux serve` starts the viewer bound to the given store; `--json`/errors go to stdout/stderr per
  convention; unknown args → usage + non-zero exit.

## Non-goals

No new runtime dependency (stdlib argparse only). No write paths beyond `capture`/`gen-fixture`/`view --out`.
No multi-store/multi-table operations.
