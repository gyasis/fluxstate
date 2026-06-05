# Active Context

**Last Updated**: 2026-06-05 18:15:44

## Current Focus
test: retire legacy Snowflake-coupled unit tests (T028)

Move the pre-pivot unit tests into TESTS/legacy/ — they import
snowflake.snowpark (can't run hermetically), exercise the removed in-memory
mirror-table API, and aren't pytest-collected by naming. The new per-story
suite (test_changelog/idempotency/capture_scale/reconstruct/lifecycle/
output_formats) is the rewrite to the change-log API. Kept (not deleted) for
reference; recoverable from history. Active suite stays green (21 passed).

Retired: Unit_1..8.py, unit_1_mock.py, FluxStateValidatorTest.py.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>

## Recent Changes
```
 .claude/activity_stream.md                  |  9 +++++++++
 memory-bank/private/gyasis/activeContext.md | 25 ++++++++++++------------
 memory-bank/private/gyasis/progress.md      |  2 +-
 3 files changed, 23 insertions(+), 13 deletions(-)
```

## Modified Files
.claude/activity_stream.md
memory-bank/private/gyasis/activeContext.md
memory-bank/private/gyasis/progress.md

## Next Actions
- Continue implementation
- Run tests
- Create checkpoint
