// File: viewer/tests/static-view.test.ts  (T028 — dev-spec §2.5)
//
// PURE unit test for the static / print audit view's DATA SHAPING. No browser:
// we build an in-memory event set, build the ReconIndex, and assert
// buildStaticView produces the right shapes:
//   • each cell's `value` = latest, `ghost` = the PRIOR value, `dir` matches
//     changeDirection(ghost, value) (↑/↓ for numerics);
//   • a deleted row is marked state="deleted";
//   • a resurrected row carries resurrected=true with a resurrected spark phase;
//   • an immutable-violation (flagged) row is called out;
//   • the most-volatile row is called out.
//
// The reconstruction primitives themselves are covered by the parity gate; here
// we only assert the static view's own derivation on top of them.

import { describe, it, expect } from "vitest";
import {
  buildIndex,
  immutableViolations,
  changeDirection,
  type RawChangeEvent,
} from "../src/lib/reconstruct.ts";
import { buildStaticView, buildCallouts } from "../src/lib/static-view.ts";

const DELETED = "__deleted__";

// A tiny store: key column `id` (immutable), tracked `score` (int) + `status`
// (utf8). Three entities exercising change / deletion / resurrection / violation.
const schema: Record<string, string> = {
  id: "utf8",
  score: "int64",
  status: "utf8",
};
const keyColumn = "id";

function ev(
  entity_id: string,
  ts: string,
  field: string,
  value: string | null,
  dtype: string,
): RawChangeEvent {
  return { entity_id, timestamp: ts, field, value, dtype, snapshot_id: ts };
}

// A: score 10 → 25 (up), status active → active (no change). Most volatile.
// B: created, then deleted (state=deleted at now).
// C: created, deleted, then resurrected (resurrected=true).
// D: immutable `id` column changed → violation (flagged).
const events: RawChangeEvent[] = [
  // A
  ev("A", "2026-01-01T00:00:00+00:00", "score", "10", "int64"),
  ev("A", "2026-01-01T00:00:00+00:00", "status", "active", "utf8"),
  ev("A", "2026-02-01T00:00:00+00:00", "score", "18", "int64"),
  ev("A", "2026-03-01T00:00:00+00:00", "score", "25", "int64"),
  // B — deleted and stays deleted
  ev("B", "2026-01-05T00:00:00+00:00", "score", "5", "int64"),
  ev("B", "2026-04-01T00:00:00+00:00", DELETED, null, "null"),
  // C — deleted then resurrected
  ev("C", "2026-01-10T00:00:00+00:00", "score", "7", "int64"),
  ev("C", "2026-02-10T00:00:00+00:00", DELETED, null, "null"),
  ev("C", "2026-05-10T00:00:00+00:00", "score", "9", "int64"),
  // D — immutable id violation: the `id` cell takes two distinct values
  ev("D", "2026-01-15T00:00:00+00:00", "id", "D", "utf8"),
  ev("D", "2026-01-15T00:00:00+00:00", "score", "3", "int64"),
  ev("D", "2026-06-15T00:00:00+00:00", "id", "D-X", "utf8"),
];

const IMMUTABLE = ["id", "birth_date", "cohort", "mrn"];

function build() {
  const index = buildIndex(events);
  const violations = immutableViolations(index, IMMUTABLE);
  const sv = buildStaticView(index, schema, keyColumn, violations, "now");
  const byId = new Map(sv.rows.map((r) => [r.id, r]));
  return { sv, byId, violations };
}

describe("buildStaticView — data shaping", () => {
  it("columns are [key, ...tracked fields] in schema order", () => {
    const { sv } = build();
    expect(sv.columns).toEqual(["id", "score", "status"]);
  });

  it("cell ghost subtext = the PRIOR value, and dir matches changeDirection (numeric ↑)", () => {
    const { byId } = build();
    const a = byId.get("A")!;
    const scoreCell = a.cells.find((c) => c.field === "score")!;
    expect(scoreCell.value).toBe(25); // latest
    expect(scoreCell.ghost).toBe(18); // prior (the 2026-02-01 value, not the first)
    expect(scoreCell.dir).toEqual(changeDirection(18, 25));
    expect(scoreCell.dir?.dir).toBe("up");
    expect(scoreCell.dir?.delta).toBe(7);
  });

  it("a single-write cell has no ghost and no direction", () => {
    const { byId } = build();
    const a = byId.get("A")!;
    const statusCell = a.cells.find((c) => c.field === "status")!;
    expect(statusCell.value).toBe("active");
    expect(statusCell.ghost).toBeNull();
    expect(statusCell.dir).toBeNull();
  });

  it("a deleted row is marked state=deleted", () => {
    const { byId } = build();
    const b = byId.get("B")!;
    expect(b.state).toBe("deleted");
    expect(b.resurrected).toBe(false);
  });

  it("a resurrected row carries resurrected=true and a resurrected spark phase", () => {
    const { byId } = build();
    const c = byId.get("C")!;
    expect(c.state).toBe("active");
    expect(c.resurrected).toBe(true);
    expect(c.spark.some((s) => s.kind === "resurrected")).toBe(true);
    expect(c.spark.some((s) => s.kind === "deleted")).toBe(true);
  });

  it("an immutable-violation row is flagged", () => {
    const { byId } = build();
    const d = byId.get("D")!;
    expect(d.flagged).toBe(true);
  });

  it("change density (changeCount) reflects tracked-field events (deletion markers excluded from cell ghosts)", () => {
    const { byId } = build();
    // A: score has 3 events, status 1 → 4 tracked changes.
    expect(byId.get("A")!.changeCount).toBe(4);
  });
});

describe("buildCallouts — auto-callouts", () => {
  it("calls out every flagged violation row and the most volatile row", () => {
    const { sv } = build();
    const violationCallouts = sv.callouts.filter((c) => c.kind === "violation");
    const volatileCallouts = sv.callouts.filter((c) => c.kind === "volatile");

    // D is flagged → exactly one violation callout for it.
    expect(violationCallouts.map((c) => c.entityId)).toContain("D");

    // A is the most volatile non-flagged row → a volatile callout.
    expect(volatileCallouts.map((c) => c.entityId)).toContain("A");

    // A flagged row is NOT double-counted as volatile.
    const ids = volatileCallouts.map((c) => c.entityId);
    expect(ids).not.toContain("D");
  });

  it("ranks volatile callouts by descending change count", () => {
    const { sv } = build();
    const vols = sv.callouts.filter((c) => c.kind === "volatile");
    // The top volatile row should be A (4 changes), ahead of C/B (fewer).
    expect(vols[0].entityId).toBe("A");
  });
});
