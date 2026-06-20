import { describe, it, expect } from "vitest";
import { decodeValue, changeDirection, _asOf } from "../src/lib/reconstruct";

// F-INF: Python encodes non-finite floats via repr() ('inf'/'-inf'/'nan').
// The viewer must decode them to the same IEEE-754 values Python reconstructs,
// not to NaN (JS Number('inf') === NaN would silently corrupt ±Infinity).
describe("decodeValue float inf/nan (audit F-INF)", () => {
  it("decodes 'inf' to Infinity (not NaN)", () => {
    expect(decodeValue("inf", "float64")).toBe(Infinity);
  });
  it("decodes '-inf' to -Infinity", () => {
    expect(decodeValue("-inf", "float64")).toBe(-Infinity);
  });
  it("decodes 'nan' to NaN", () => {
    expect(Number.isNaN(decodeValue("nan", "float64") as number)).toBe(true);
  });
  it("still decodes ordinary floats", () => {
    expect(decodeValue("1.5", "float64")).toBe(1.5);
  });
});

// F-DIR: large int64 values (|v| > 2^53-1) decode to BigInt; changeDirection
// must still classify up/down/same for them (previously returned 'na').
describe("changeDirection BigInt direction (audit F-DIR)", () => {
  it("returns 'up' for an increasing BigInt pair", () => {
    expect(changeDirection(9007199254740993n, 9007199254740994n)).toEqual({
      dir: "up",
      delta: null,
    });
  });
  it("returns 'down' for a decreasing BigInt pair", () => {
    expect(changeDirection(9007199254740994n, 9007199254740993n)).toEqual({
      dir: "down",
      delta: null,
    });
  });
  it("returns 'same' for equal BigInts", () => {
    expect(changeDirection(9007199254740993n, 9007199254740993n)).toEqual({
      dir: "same",
      delta: null,
    });
  });
});

// F-ASOF: _asOf must be order-independent (match Python `_as_of`, audit A4).
// The JS port previously kept an early break that returned null on unsorted input.
describe("_asOf is order-independent (audit F-ASOF)", () => {
  const D = (y: number) => new Date(Date.UTC(y, 0, 1));
  it("resolves the latest <= T from unsorted history", () => {
    const unsorted = [
      { date: D(2023), value: "c" },
      { date: D(2020), value: "a" },
      { date: D(2021), value: "b" },
    ];
    expect(_asOf(unsorted, D(2022))?.value).toBe("b");
  });
  it("still resolves correctly for sorted history", () => {
    const sorted = [
      { date: D(2020), value: "a" },
      { date: D(2021), value: "b" },
      { date: D(2023), value: "c" },
    ];
    expect(_asOf(sorted, D(2022))?.value).toBe("b");
  });
});
