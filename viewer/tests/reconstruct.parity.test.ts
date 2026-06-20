// File: viewer/tests/reconstruct.parity.test.ts
//
// THE PARITY GATE (T015 / SC-003 / VD-2).
//
// Proves the JS port in `src/lib/reconstruct.ts` reproduces the shipped Python
// `reconstruct.py` EXACTLY. Loads the Python-exported ground truth
// (`tests/fixtures/parity-ground-truth.json`, shaped by
// `contracts/parity.schema.json`) and, for every probe, calls the matching
// reconstruct.ts function over the fixture's raw `events` and asserts the
// result deep-equals the probe's `expected`.
//
// Normalization rule (the only allowed leniency): Python emits datetimes as
// ISO-8601 UTC strings while reconstruct.ts returns JS `Date`s — so a `Date`
// in the actual result is compared *as an instant* against the corresponding
// expected ISO string (`date.getTime() === Date.parse(expected)`). Everything
// else (numbers, booleans, strings, null) is compared strictly, by value.
//
// A failing probe means reconstruct.ts diverges from Python. DO NOT loosen an
// assertion to make it pass — fix the port instead.

import { describe, it, expect } from "vitest";
import groundTruth from "./fixtures/parity-ground-truth.json" with { type: "json" };
import {
  asOf,
  getTimeline,
  rowState,
  changeCount,
  buildMirrorView,
  snapPoints,
  type RawChangeEvent,
} from "../src/lib/reconstruct.ts";

// --- fixture shape ---------------------------------------------------------- //

interface Probe {
  kind: "as_of" | "timeline" | "row_state" | "change_count" | "build_mirror_view";
  entity_id?: string;
  field?: string | null;
  T?: string | null;
  expected: unknown;
}

interface GroundTruth {
  store: string;
  key_column: string;
  schema: Record<string, string>;
  snap_points: string[];
  probes: Probe[];
  events: RawChangeEvent[];
  must_include_cases?: Record<string, boolean>;
}

const gt = groundTruth as unknown as GroundTruth;
const events: RawChangeEvent[] = gt.events;

// T `null` ⇒ "now" (no upper bound). For functions that take a concrete `Date`
// (asOf, rowState) a missing T means "now" → use the max snap point as the
// instant; that is at/after every event so it is equivalent to "now".
const NOW = new Date(
  gt.snap_points.length
    ? Math.max(...gt.snap_points.map((s) => Date.parse(s)))
    : Date.now(),
);

function asOfT(T: string | null | undefined): Date {
  return T == null ? NOW : new Date(T);
}

// --- context-aware deep comparison ----------------------------------------- //
//
// Normalize `actual` (which may contain `Date`s) into a JSON-comparable form,
// using `expected` as the oracle for how a `Date` should render: a `Date` is
// turned into the canonical instant (epoch ms) and the matching expected ISO
// string is turned into the same instant, so "2026-01-02T00:00:00+00:00"
// (Python) and a JS `Date` for the same moment compare equal regardless of
// textual form (`+00:00` vs `.000Z`).

function normalize(actual: unknown, expected: unknown): unknown {
  if (actual instanceof Date) {
    // Compared against the expected ISO string as an instant.
    return { __instant__: actual.getTime() };
  }
  if (Array.isArray(actual)) {
    return actual.map((v, i) =>
      normalize(v, Array.isArray(expected) ? expected[i] : undefined),
    );
  }
  if (actual !== null && typeof actual === "object") {
    const out: Record<string, unknown> = {};
    const exp = (expected ?? {}) as Record<string, unknown>;
    for (const k of Object.keys(actual as Record<string, unknown>)) {
      out[k] = normalize((actual as Record<string, unknown>)[k], exp[k]);
    }
    return out;
  }
  return actual;
}

// Mirror normalization on the expected side: any string sitting opposite a
// `Date` in `actual` is parsed to the same `{__instant__}` shape.
function normalizeExpected(expected: unknown, actual: unknown): unknown {
  if (actual instanceof Date) {
    if (typeof expected !== "string") {
      throw new Error(
        `expected a date string opposite a Date, got ${JSON.stringify(expected)}`,
      );
    }
    return { __instant__: Date.parse(expected) };
  }
  if (Array.isArray(expected)) {
    return expected.map((v, i) =>
      normalizeExpected(v, Array.isArray(actual) ? actual[i] : undefined),
    );
  }
  if (expected !== null && typeof expected === "object") {
    const out: Record<string, unknown> = {};
    const act = (actual ?? {}) as Record<string, unknown>;
    for (const k of Object.keys(expected as Record<string, unknown>)) {
      out[k] = normalizeExpected((expected as Record<string, unknown>)[k], act[k]);
    }
    return out;
  }
  return expected;
}

function expectParity(actual: unknown, expected: unknown, label: string): void {
  expect(normalize(actual, expected), label).toEqual(
    normalizeExpected(expected, actual),
  );
}

// `build_mirror_view` expected rows are POSITIONAL arrays (in `columns` order);
// buildMirrorView returns rows as objects keyed by column. Project to arrays.
function mirrorToArrays(view: ReturnType<typeof buildMirrorView>): {
  columns: string[];
  rows: unknown[][];
} {
  return {
    columns: view.columns,
    rows: view.rows.map((r) => view.columns.map((c) => r[c])),
  };
}

// --- the gate --------------------------------------------------------------- //

describe("reconstruct.ts ⇄ reconstruct.py parity", () => {
  it("has a non-empty probe set covering all five kinds", () => {
    const kinds = new Set(gt.probes.map((p) => p.kind));
    expect(gt.probes.length).toBeGreaterThan(0);
    for (const k of [
      "as_of",
      "timeline",
      "row_state",
      "change_count",
      "build_mirror_view",
    ]) {
      expect(kinds.has(k as Probe["kind"])).toBe(true);
    }
  });

  it("snapPoints(events) === fixture.snap_points (as instants)", () => {
    const actual = snapPoints(events);
    expect(actual.map((d) => d.getTime())).toEqual(
      gt.snap_points.map((s) => Date.parse(s)),
    );
  });

  // One assertion per probe — fail with the exact (kind, entity, field, T).
  gt.probes.forEach((probe, idx) => {
    const label = `probe[${idx}] ${probe.kind} entity=${probe.entity_id ?? "-"} field=${probe.field ?? "-"} T=${probe.T ?? "now"}`;

    it(label, () => {
      switch (probe.kind) {
        case "as_of": {
          const actual = asOf(
            events,
            probe.entity_id!,
            probe.field as string,
            asOfT(probe.T),
          );
          expectParity(actual, probe.expected, label);
          break;
        }
        case "timeline": {
          const actual = getTimeline(
            events,
            probe.entity_id!,
            probe.field ?? undefined,
          );
          expectParity(actual, probe.expected, label);
          break;
        }
        case "row_state": {
          const actual = rowState(events, probe.entity_id!, asOfT(probe.T));
          expectParity(actual, probe.expected, label);
          break;
        }
        case "change_count": {
          const actual = changeCount(events, probe.entity_id!);
          expect(actual, label).toEqual(probe.expected);
          break;
        }
        case "build_mirror_view": {
          const view = buildMirrorView(
            events,
            gt.schema,
            gt.key_column,
            probe.T == null ? "now" : new Date(probe.T),
          );
          expectParity(mirrorToArrays(view), probe.expected, label);
          break;
        }
        default: {
          throw new Error(`unknown probe kind: ${(probe as Probe).kind}`);
        }
      }
    });
  });
});
