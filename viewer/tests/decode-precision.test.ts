import { describe, it, expect } from "vitest";
import { decodeValue } from "../src/lib/reconstruct";

// Audit F1: int64 values beyond float64's exact-integer range must not round.
describe("decodeValue int64 precision (audit F1)", () => {
  it("keeps small ints as number", () => {
    expect(decodeValue("100", "int64")).toBe(100);
    expect(typeof decodeValue("100", "int64")).toBe("number");
  });
  it("preserves int64 > 2^53 exactly via BigInt", () => {
    // 2^53 + 1 — the canonical float64-lossy integer.
    expect(decodeValue("9007199254740993", "int64")).toBe(9007199254740993n);
    // a plain Number would have collapsed to 9007199254740992
    expect(decodeValue("9007199254740993", "int64")).not.toBe(9007199254740992);
  });
  it("preserves large negative int64 exactly", () => {
    expect(decodeValue("-9223372036854775808", "int64")).toBe(-9223372036854775808n);
  });
});
