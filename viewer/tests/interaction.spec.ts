// File: viewer/tests/interaction.spec.ts  (T022 / US4 — SC-002)
//
// Playwright interaction + performance spec for the 100k-row STREAMED path.
// Asserts the windowing contract holds at scale:
//   • the large store loads WITHOUT loading all events into JS (the "streamed"
//     badge + scope note are present, and the page never OOMs/freezes);
//   • rendered table-row count stays ~CONSTANT while scrolling 100k entities
//     (virtualization — constant DOM, FR-011 / SC-002);
//   • scrubbing a step stays responsive — well under ~100ms (≈60fps budget),
//     measured wall-clock from key-press to the table reflecting the new T;
//   • the repaint is DELTA-ONLY — only the cells that actually changed between
//     two adjacent snapshots carry a transition/flash, not the whole window.
//
// REQUIRES a running dev server (default http://localhost:5176 — `flux serve`)
// AND the 100k stress store served at /stress100k.flux. If the store isn't
// present the spec skips with a clear message (generate it with
// `flux gen-fixture stress100k.flux --rows 100000 --steps 18` and symlink it
// into viewer/public/). Run: `npx playwright test` from viewer/.

import { test, expect, type Page } from "@playwright/test";

const LARGE_STORE = "stress100k.flux";
const DEMO_STORE = "demo.flux";

/** Count the table rows currently mounted in the DOM (virtualized window). */
async function rowCount(page: Page): Promise<number> {
  return page.locator(".scroller .spacer .trow").count();
}

/** True if the large (streamed) store is reachable on the running server. */
async function largeStoreAvailable(page: Page): Promise<boolean> {
  const resp = await page.request.get(`/${LARGE_STORE}/manifest.json`).catch(() => null);
  return !!resp && resp.ok();
}

test.describe("US4 scale — 100k streamed windowing (SC-002)", () => {
  test.beforeEach(async ({ page }) => {
    const ok = await largeStoreAvailable(page);
    test.skip(
      !ok,
      `${LARGE_STORE} not served — run \`flux gen-fixture stress100k.flux --rows 100000 --steps 18\` and symlink it into viewer/public/`,
    );
  });

  test("loads 100k via the streamed path without loading all events", async ({ page }) => {
    const errors: string[] = [];
    page.on("console", (m) => {
      if (m.type() === "error") errors.push(m.text());
    });

    await page.goto(`/?store=${LARGE_STORE}`);
    // The header reports the full scale + the STREAMED badge (windowed path).
    await expect(page.locator(".sub .badge")).toHaveText(/streamed/i, { timeout: 30_000 });
    await expect(page.locator(".sub")).toContainText("100,000 entities");
    await expect(page.locator(".scopenote")).toBeVisible();

    // Rows actually render (window loaded), and the count is a small window —
    // NOT 100k. Constant-DOM virtualization caps it well under a few hundred.
    await expect.poll(() => rowCount(page), { timeout: 30_000 }).toBeGreaterThan(10);
    const n = await rowCount(page);
    expect(n).toBeLessThan(120);

    // No console errors (no OOM, no failed window fetch).
    expect(errors, `console errors: ${errors.join("\n")}`).toEqual([]);
  });

  test("rendered-row count stays ~constant while scrolling 100k", async ({ page }) => {
    await page.goto(`/?store=${LARGE_STORE}`);
    await expect(page.locator(".sub .badge")).toHaveText(/streamed/i, { timeout: 30_000 });
    await expect.poll(() => rowCount(page), { timeout: 30_000 }).toBeGreaterThan(10);

    const counts: number[] = [];
    counts.push(await rowCount(page));

    const scroller = page.locator(".scroller");
    // Scroll deep into the 100k list in several jumps; the DOM row count must
    // not grow with scroll position (that would mean we mounted more rows).
    for (const top of [10_000, 100_000, 1_000_000, 3_400_000]) {
      await scroller.evaluate((el, t) => {
        (el as HTMLElement).scrollTop = t as number;
      }, top);
      await page.waitForTimeout(350); // let the window fetch + render settle
      counts.push(await rowCount(page));
    }

    const min = Math.min(...counts);
    const max = Math.max(...counts);
    // Constant DOM: the spread across the whole 0→3.4M-px scroll range is tiny
    // (a couple of rows of overscan jitter) and the absolute count stays small.
    expect(max - min, `row counts: ${counts.join(",")}`).toBeLessThanOrEqual(4);
    expect(max).toBeLessThan(120);
  });

  test("scrubbing a step is responsive (well under ~100ms) at 100k", async ({ page }) => {
    await page.goto(`/?store=${LARGE_STORE}`);
    await expect(page.locator(".sub .badge")).toHaveText(/streamed/i, { timeout: 30_000 });
    await expect.poll(() => rowCount(page), { timeout: 30_000 }).toBeGreaterThan(10);

    // Focus the scrubber slider so ←/→ step events.
    const slider = page.getByRole("slider", { name: "Scrub time" });
    await slider.focus();

    // Warm one step, then measure several step latencies wall-clock. Each step
    // re-reconstructs only the visible window (binary search per cell) → fast.
    await page.keyboard.press("ArrowLeft");
    await page.waitForTimeout(120);

    const latencies: number[] = [];
    for (let i = 0; i < 6; i++) {
      const t0 = await page.evaluate(() => performance.now());
      await page.keyboard.press(i % 2 === 0 ? "ArrowLeft" : "ArrowRight");
      // Wait for the DOM to settle after the step (a microtask + paint).
      await page.evaluate(
        () => new Promise<void>((r) => requestAnimationFrame(() => requestAnimationFrame(() => r()))),
      );
      const t1 = await page.evaluate(() => performance.now());
      latencies.push(t1 - t0);
    }

    const max = Math.max(...latencies);
    const avg = latencies.reduce((a, b) => a + b, 0) / latencies.length;
    // 60fps budget is ~16ms/frame; allow generous headroom for the two-rAF
    // settle + Playwright round-trips. The HARD bar (SC-002) is "responsive" —
    // well under ~100ms per step. Report both for the record.
    console.log(`scrub step latency at 100k — avg ${avg.toFixed(1)}ms, max ${max.toFixed(1)}ms`);
    expect(max).toBeLessThan(100);
  });

  test("repaint is delta-only — a step diffs only the changed cells", async ({ page }) => {
    await page.goto(`/?store=${LARGE_STORE}`);
    await expect(page.locator(".sub .badge")).toHaveText(/streamed/i, { timeout: 30_000 });
    await expect.poll(() => rowCount(page), { timeout: 30_000 }).toBeGreaterThan(10);

    const slider = page.getByRole("slider", { name: "Scrub time" });
    await slider.focus();

    // Step a few events to land on a transition that touches the top window,
    // and capture the MAX number of cells that ever carry an old→new diff
    // (`.d-old`) in any single step — the pinned diff markers persist (no fade
    // in step mode), so this is a stable measure of the per-step change set.
    let maxDiffed = 0;
    let totalCells = 0;
    for (let i = 0; i < 5; i++) {
      await page.keyboard.press("ArrowLeft");
      await page.waitForTimeout(120);
      const diffed = await page.locator(".td.cell .d-old").count();
      totalCells = await page.locator(".scroller .spacer .td.cell").count();
      maxDiffed = Math.max(maxDiffed, diffed);
    }
    console.log(`delta-only repaint — max diffed ${maxDiffed} of ${totalCells} visible cells across 5 steps`);
    expect(totalCells).toBeGreaterThan(0);
    // Delta-only: the per-step diff set is a SMALL fraction of the visible
    // window's cells (the fixture mutates ~3% of cells/step) — never a
    // whole-window repaint. (maxDiffed may legitimately be 0 if the top window's
    // entities didn't change in those steps; the upper bound is the real claim.)
    expect(maxDiffed).toBeLessThan(totalCells * 0.5);
  });
});

test.describe("regression — 1000×20 demo still loads the full path", () => {
  test("demo loads load-all (no streamed badge) with filter + sort", async ({ page }) => {
    const errors: string[] = [];
    page.on("console", (m) => {
      if (m.type() === "error") errors.push(m.text());
    });
    await page.goto(`/?store=${DEMO_STORE}`);
    // Demo is NOT large → no streamed badge; the Filter pane IS present.
    await expect(page.locator(".sub")).toContainText("1,000 entities", { timeout: 30_000 });
    await expect(page.locator(".sub .badge")).toHaveCount(0);
    await expect(page.locator(".scopenote")).toHaveCount(0);
    // Rows render and virtualization caps the DOM.
    await expect.poll(() => rowCount(page), { timeout: 30_000 }).toBeGreaterThan(10);
    expect(await rowCount(page)).toBeLessThan(120);
    expect(errors, `console errors: ${errors.join("\n")}`).toEqual([]);
  });
});
