# File: flux_cli.py
"""``flux`` — a thin, stdlib-``argparse`` command surface over the FluxState
change-log library.

Every subcommand is a wrapper over the shipped library (``changelog.py`` /
``reconstruct.py`` / ``fluxstate.py``) — output MUST equal the library
function (FR-015). Text by default; ``--json`` for machine output.

Registered as a console entry point: ``flux = flux_cli:main``.
Full arg/output contract: ``specs/002-fluxstate-temporal-viewer/contracts/cli.md``.

**Wave 3 (T008): READ subcommands implemented** — ``travel`` / ``timeline`` /
``row-state`` / ``view`` / ``info`` are thin wrappers over ``reconstruct.*`` and
``FluxState.save_mirror_table`` whose output equals the library (FR-015). Text by
default; ``--json`` for machine output (datetimes → ISO-8601 UTC). ``capture`` /
``gen-fixture`` / ``serve`` remain stubs (Wave 4 / US7).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from typing import Any, Optional, Sequence

import polars as pl

from changelog import ChangeLogStore, to_utc
import reconstruct
from fluxstate import FluxState


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _jsonify(v: Any) -> Any:
    """JSON-safe a reconstructed value without lying about its type.

    Datetimes → ISO-8601 UTC strings; dict/list recurse; scalars pass through.
    Mirrors ``TESTS/test_parity_export.py:_jsonify`` so CLI ``--json`` output
    matches the library's typed values.
    """
    if isinstance(v, datetime):
        return to_utc(v).isoformat()
    if isinstance(v, dict):
        return {k: _jsonify(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonify(x) for x in v]
    return v


def _emit_json(obj: Any) -> None:
    """Write a JSON document to stdout (ISO-8601 datetimes, typed scalars)."""
    print(json.dumps(_jsonify(obj), indent=2))


def _records(df: pl.DataFrame) -> list[dict]:
    """A DataFrame as a list of JSON-safe ``{column: value}`` records."""
    return [_jsonify(r) for r in df.iter_rows(named=True)]


# --------------------------------------------------------------------------- #
# Subcommand handlers
# --------------------------------------------------------------------------- #


def _load_frame(path: str) -> pl.DataFrame:
    """Load a capture input into a Polars DataFrame (``.parquet`` or ``.csv``)."""
    lower = path.lower()
    if lower.endswith(".parquet"):
        return pl.read_parquet(path)
    if lower.endswith(".csv"):
        return pl.read_csv(path)
    raise ValueError(f"unsupported input {path!r}: expected a .parquet or .csv file")


def cmd_capture(args: argparse.Namespace) -> int:
    """``flux capture <store> <input> --key <col> [--at <ISO>]`` → ``ChangeLogStore.capture``.

    Thin wrapper: load the snapshot, hand it to the shipped library (which owns the
    append-only + idempotent guarantees), and print the result dict
    (``events_added/snapshot_id/file/noop``). ``--json`` for machine output (CLI-1).
    """
    df = _load_frame(args.input)
    captured_at = datetime.fromisoformat(args.at) if args.at else None
    store = ChangeLogStore(args.store)
    result = store.capture(df, key_column=args.key, captured_at=captured_at)
    if args.json:
        _emit_json(result)
    else:
        for k in ("noop", "events_added", "snapshot_id", "file"):
            if k in result:
                print(f"{k}={result[k]}")
    return 0


def cmd_travel(args: argparse.Namespace) -> int:
    """``flux travel <store> --as-of <ISO|now>`` → ``build_mirror_view(store, T)``.

    Prints a text table by default; ``--json`` → typed records. A T before any
    history yields an empty result (NOT an error) and exit 0 (CLI-2).
    """
    store = ChangeLogStore(args.store)
    view = reconstruct.build_mirror_view(store, T=args.as_of)
    if args.json:
        _emit_json(_records(view))
    else:
        print(view)
    return 0


def cmd_timeline(args: argparse.Namespace) -> int:
    """``flux timeline <store> <entity_id> [--field <col>]`` → ``reconstruct.get_timeline``.

    Output equals the library return ``[{date, value[, field]}]`` (CLI-3); values
    typed. ``--json`` emits ISO-8601 dates.
    """
    store = ChangeLogStore(args.store)
    timeline = reconstruct.get_timeline(store, args.entity_id, field=args.field)
    if args.json:
        _emit_json(timeline)
    else:
        for entry in timeline:
            row = {k: _jsonify(v) for k, v in entry.items()}
            print("  ".join(f"{k}={row[k]}" for k in row))
    return 0


def cmd_row_state(args: argparse.Namespace) -> int:
    """``flux row-state <store> <entity_id> [--as-of <ISO|now>]`` → ``reconstruct.row_state``.

    Output equals the library return ``{state, resurrected}`` (CLI-3).
    """
    store = ChangeLogStore(args.store)
    state = reconstruct.row_state(store, args.entity_id, T=args.as_of)
    if args.json:
        _emit_json(state)
    else:
        print(f"state={state['state']}  resurrected={state['resurrected']}")
    return 0


def _is_now(as_of: str) -> bool:
    return as_of is None or as_of.strip().lower() == "now"


def cmd_view(args: argparse.Namespace) -> int:
    """``flux view <store> [--as-of] [--format] [--out]`` → ``build_mirror_view`` (+ save).

    For ``polars``/``arrow`` (no ``--out``): print/emit the materialized view. For
    ``parquet``/``csv`` (require ``--out``): when ``--as-of`` is "now" the write is
    delegated to ``FluxState.save_mirror_table`` (the library writer); for a
    specific as-of the same ``build_mirror_view`` frame is written directly so file
    output equals the library at that point in time.
    """
    store = ChangeLogStore(args.store)
    view = reconstruct.build_mirror_view(store, T=args.as_of)

    if args.format in ("parquet", "csv"):
        if not args.out:
            print(f"--format {args.format} requires --out <path>", file=sys.stderr)
            return 2
        if _is_now(args.as_of):
            fs = FluxState.__new__(FluxState)  # thin: reuse the library writer only
            fs.store = store
            kwargs = (
                {"output_path_parquet": args.out}
                if args.format == "parquet"
                else {"csv_path": args.out}
            )
            out_path = fs.save_mirror_table(output_format=args.format, **kwargs)
        else:
            if args.format == "parquet":
                view.write_parquet(args.out)
            else:
                view.write_csv(args.out)
            out_path = args.out
        if args.json:
            _emit_json({"format": args.format, "out": str(out_path)})
        else:
            print(out_path)
        return 0

    # polars / arrow → materialize to stdout
    if args.json:
        _emit_json(_records(view))
    else:
        print(view)
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    """``flux info <store>`` → manifest summary (schema, key, #events, ts range, snaps)."""
    store = ChangeLogStore(args.store)
    manifest = store.read_manifest()
    events = manifest.get("events", [])

    ts_min = min((e["ts_min"] for e in events), default=None)
    ts_max = max((e["ts_max"] for e in events), default=None)
    snap_points = sorted({e["snapshot_id"] for e in events})

    summary = {
        "store": manifest.get("store_name") or store.path.stem,
        "key_column": manifest.get("key_column") or None,
        "schema": manifest.get("schema", {}),
        "events_files": len(events),
        "ts_min": ts_min,
        "ts_max": ts_max,
        "snapshot_count": len(snap_points),
    }

    if args.json:
        _emit_json(summary)
    else:
        print(f"store:          {summary['store']}")
        print(f"key_column:     {summary['key_column']}")
        print(f"schema:         {summary['schema']}")
        print(f"events files:   {summary['events_files']}")
        print(f"ts range:       {ts_min} .. {ts_max}")
        print(f"snapshot count: {summary['snapshot_count']}")
    return 0


def cmd_gen_fixture(args: argparse.Namespace) -> int:
    """``flux gen-fixture <store> [--rows --cols --steps --seed]`` → ``demo_fixture.generate``.

    Thin wrapper that WIRES the call to the seeded generator and prints its summary.
    The generator itself lands in US7 (T012-T014); until then it raises
    ``NotImplementedError`` — which is expected here.
    """
    from scripts import demo_fixture

    summary = demo_fixture.generate(
        args.store,
        rows=args.rows,
        cols=args.cols,
        steps=args.steps,
        seed=args.seed,
        violations=args.violations,
    )
    if args.json:
        _emit_json(summary)
    else:
        print(summary)
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """``flux serve <store> [--port --no-open]`` → launch the viewer (Vite) over the store.

    Starts ``npm run dev`` in ``viewer/`` bound to the given store. The store
    location is passed to the app via the ``VITE_FLUX_STORE`` env var and as a Vite
    ``--`` passthrough arg; the dev URL is printed. NOTE: the viewer actually
    *consumes* the store in US2 (T018) — for now ``serve`` only starts Vite bound to
    the store path and prints the URL. Imported lazily + guarded so that importing
    this module (or ``--help``) never starts a server (does not block the test suite).
    """
    import os
    import subprocess
    from pathlib import Path

    store_path = str(Path(args.store).resolve())
    viewer_dir = Path(__file__).resolve().parent / "viewer"
    url = f"http://localhost:{args.port}/"

    env = {**os.environ, "VITE_FLUX_STORE": store_path}
    cmd = [
        "npm", "run", "dev", "--",
        "--port", str(args.port),
        "--strictPort",
    ]
    if not args.no_open:
        cmd.append("--open")

    print(f"flux serve: launching viewer over {store_path}")
    print(f"flux serve: {url}")
    # NOTE: viewer consumes the store in US2 (T018); today this only boots Vite.
    proc = subprocess.Popen(cmd, cwd=str(viewer_dir), env=env)
    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
    return proc.returncode or 0


# --------------------------------------------------------------------------- #
# Parser construction
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flux",
        description="FluxState change-log CLI — capture, time-travel, and serve a .flux store.",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    # capture
    p = sub.add_parser("capture", help="capture a snapshot into a .flux store")
    p.add_argument("store", metavar="<store.flux>", help="path to the .flux store")
    p.add_argument(
        "input", metavar="<input.parquet|csv>", help="snapshot to capture"
    )
    p.add_argument("--key", required=True, metavar="<col>", help="key column")
    p.add_argument("--at", metavar="<ISO>", help="captured-at timestamp (ISO 8601)")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.set_defaults(func=cmd_capture)

    # travel
    p = sub.add_parser("travel", help="reconstruct the table as-of a point in time")
    p.add_argument("store", metavar="<store.flux>", help="path to the .flux store")
    p.add_argument(
        "--as-of", required=True, metavar="<ISO|now>", help="point in time"
    )
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.set_defaults(func=cmd_travel)

    # timeline
    p = sub.add_parser("timeline", help="per-cell history of an entity")
    p.add_argument("store", metavar="<store.flux>", help="path to the .flux store")
    p.add_argument("entity_id", metavar="<entity_id>", help="entity id")
    p.add_argument("--field", metavar="<col>", help="restrict to one field")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.set_defaults(func=cmd_timeline)

    # row-state
    p = sub.add_parser("row-state", help="lifecycle state of an entity at a time")
    p.add_argument("store", metavar="<store.flux>", help="path to the .flux store")
    p.add_argument("entity_id", metavar="<entity_id>", help="entity id")
    p.add_argument("--as-of", metavar="<ISO|now>", default="now", help="point in time")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.set_defaults(func=cmd_row_state)

    # view
    p = sub.add_parser("view", help="materialize the as-of mirror view")
    p.add_argument("store", metavar="<store.flux>", help="path to the .flux store")
    p.add_argument("--as-of", metavar="<ISO|now>", default="now", help="point in time")
    p.add_argument(
        "--format",
        choices=["polars", "arrow", "parquet", "csv"],
        default="polars",
        help="output format",
    )
    p.add_argument("--out", metavar="<path>", help="write to path instead of stdout")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.set_defaults(func=cmd_view)

    # info
    p = sub.add_parser("info", help="store summary (schema, key, events, ts range)")
    p.add_argument("store", metavar="<store.flux>", help="path to the .flux store")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.set_defaults(func=cmd_info)

    # gen-fixture
    p = sub.add_parser("gen-fixture", help="generate the seeded demo+stress fixture store")
    p.add_argument("store", metavar="<store.flux>", help="path to the .flux store")
    p.add_argument("--rows", type=int, default=1000, help="number of rows")
    p.add_argument("--cols", type=int, default=20, help="number of columns")
    p.add_argument("--steps", type=int, default=40, help="number of capture steps")
    p.add_argument("--seed", type=int, default=42, help="random seed")
    p.add_argument(
        "--violations",
        type=int,
        default=0,
        help="seed N deliberate immutable-column violations (default 0)",
    )
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.set_defaults(func=cmd_gen_fixture)

    # serve
    p = sub.add_parser("serve", help="launch the viewer over the store")
    p.add_argument("store", metavar="<store.flux>", help="path to the .flux store")
    p.add_argument("--port", type=int, default=5173, help="port to serve on")
    p.add_argument("--no-open", action="store_true", help="do not open a browser")
    p.set_defaults(func=cmd_serve)

    return parser


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
