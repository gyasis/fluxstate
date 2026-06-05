# Legacy tests (retired)

These files were retired during the **changelog-first storage pivot**
(`001-changelog-first-pivot`). They are kept here for reference only and are
**not part of the active test suite**.

## Why retired

- They import `snowflake.snowpark` and require a live Snowflake session, so they
  cannot run in the hermetic venv (SC-006: the suite must be green with no
  Snowflake).
- They exercise the **removed in-memory mirror-table API** (JSON-in-cell
  dict-of-lists, `mode="compare"` serialized loads) that the change-log pivot
  replaced.
- They are not even collected by pytest — their names (`Unit_*.py`,
  `*Test.py`, `unit_1_mock.py`) don't match pytest's `test_*.py` pattern.

## What replaced them

The new per-user-story suite in `TESTS/` is the rewrite to the change-log API:

| Concern | New test |
|---|---|
| Append-only / no-rewrite / glob-readable | `test_changelog.py` |
| Idempotent re-capture | `test_idempotency.py` |
| Wide-table O(rows) capture | `test_capture_scale.py` |
| Reconstruction + type fidelity | `test_reconstruct.py` |
| Delete / resurrection continuity | `test_lifecycle.py` |
| Multi-format output | `test_output_formats.py` |

## Files

`Unit_1.py` … `Unit_8.py`, `unit_1_mock.py`, `FluxStateValidatorTest.py`.

Safe to delete entirely once you're confident nothing references them.
