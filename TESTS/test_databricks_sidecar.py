# File: TESTS/test_databricks_sidecar.py
"""Wave 7 (US4) — Databricks sidecar (``sidecars.databricks.DeltaBackend``).

Covers T023 (the CORE-is-platform-clean import-graph proof, SC-004/G8 — MUST
pass in the native env, no optional libs required), the missing-``deltalake``
actionable-error guard (mirrors ``test_table_backend.py``'s delta guard test,
also MUST pass natively), and T024 (capture idempotency / change-proportional
growth against a local Delta table standing in for Spark — requires
``deltalake``, ``pytest.importorskip``'d and SKIPPED in this dev env).
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import pytest

U = lambda *a: datetime(*a, tzinfo=timezone.utc)

REPO_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# T023 — the core import graph pulls in NO platform module (SC-004, G8)       #
# --------------------------------------------------------------------------- #
def test_core_import_graph_has_no_platform_modules():
    """Importing every core module MUST NOT pull in deltalake/databricks/pyspark.

    Run in a FRESH subprocess (not this test process's already-populated
    ``sys.modules``) so the assertion reflects a clean interpreter's real
    import graph, not whatever earlier tests in this suite happened to import.
    This is the concrete proof behind "the base install has zero platform
    dependencies" (Constitution G8 / feature 003 SC-004) — platform SDKs are
    confined to `sidecars/*` extras and the core never reaches for them.
    """
    script = textwrap.dedent(
        """
        import sys

        import fluxstate          # noqa: F401 - top-level flat module
        import changelog          # noqa: F401
        import reconstruct        # noqa: F401
        import storage            # noqa: F401
        import storage.base       # noqa: F401
        import storage.table      # noqa: F401
        import storage.local_folder   # noqa: F401
        import storage.object_store   # noqa: F401

        banned = ("deltalake", "databricks", "pyspark")
        leaked = sorted(name for name in sys.modules if name.startswith(banned))
        print(",".join(leaked))
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"core import graph failed to import cleanly:\n{proc.stderr}"
    leaked = proc.stdout.strip()
    assert leaked == "", f"core import pulled in platform module(s): {leaked}"


# --------------------------------------------------------------------------- #
# Missing-extra guard — actionable fluxstate[databricks] error (BLOCKING)      #
# --------------------------------------------------------------------------- #
def test_delta_backend_without_deltalake_raises_actionable_error(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "deltalake", None)
    from sidecars.databricks import DeltaBackend

    with pytest.raises(ImportError, match=r'fluxstate\[databricks\]'):
        DeltaBackend(events=str(tmp_path / "flux_events"))


def test_delta_backend_works_when_deltalake_available(tmp_path):
    pytest.importorskip("deltalake")
    from sidecars.databricks import DeltaBackend

    backend = DeltaBackend(events=str(tmp_path / "flux_events"))
    assert backend.format == "delta"
    assert backend.capabilities.is_table is True
    assert backend.capabilities.supports_mirror is False


# --------------------------------------------------------------------------- #
# T024 — capture idempotency + change-proportional growth (local Delta table) #
# --------------------------------------------------------------------------- #
def test_delta_backend_capture_idempotent_and_change_proportional(tmp_path):
    """A local Delta table stands in for a Databricks-hosted one (no cluster needed —
    ``deltalake`` writes/reads a Delta table directly on local disk; the sidecar
    logic under test — ``list_events``/``_append_events`` over Delta — is
    identical either way).
    """
    pytest.importorskip("deltalake")
    from changelog import ChangeLogStore
    from sidecars.databricks import DeltaBackend

    events_dir = tmp_path / "flux_events"
    backend = DeltaBackend(events=str(events_dir))
    store = ChangeLogStore(events_dir, backend=backend)

    df_day1 = __import__("polars").DataFrame(
        {"id": [1, 2, 3], "risk": [0.1, 0.2, 0.3]}
    )
    r1 = store.capture(df_day1, key_column="id", captured_at=U(2026, 1, 1))
    assert r1["noop"] is False
    assert r1["events_added"] == 3  # 3 entities x 1 non-key column each

    # Re-capturing the IDENTICAL snapshot is a no-op — idempotent by snapshot_id;
    # storage does not grow (SC-005).
    events_before = backend.read_events().height
    r2 = store.capture(df_day1, key_column="id", captured_at=U(2026, 1, 1))
    assert r2["noop"] is True
    assert r2["snapshot_id"] == r1["snapshot_id"]
    assert backend.read_events().height == events_before

    # A capture that changes exactly 3 cells (one per entity's `risk`) appends
    # ~3 event rows — growth is change-proportional, per SC-005, not a full
    # re-melt of the snapshot.
    df_day2 = __import__("polars").DataFrame(
        {"id": [1, 2, 3], "risk": [0.15, 0.25, 0.35]}  # all 3 rows' risk changed
    )
    r3 = store.capture(df_day2, key_column="id", captured_at=U(2026, 1, 2))
    assert r3["noop"] is False
    assert r3["events_added"] == 3

    events_after = backend.read_events().height
    assert events_after == events_before + 3

    # Reconstruction over the Delta-backed events is correct (not just the
    # generic protocol — the RAW capture-facing path list_events/_append_events).
    import reconstruct

    current = reconstruct.build_mirror_view(store, T="now").sort("id")
    assert current["risk"].to_list() == pytest.approx([0.15, 0.25, 0.35])
