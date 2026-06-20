<script lang="ts">
  // File: viewer/src/lib/scrubber.svelte  (T016 / US2 — dev-spec §2.1)
  //
  // The "Temporal Ghost" capsule scrubber. The track IS the change-density
  // histogram (inline-SVG segments shaded by `densityBuckets`); an elapsed-time
  // fill sits left of the round knob (the playhead). The control is EVENT-SNAPPED:
  // ←/→, ‹/›, and Play jump to actual change events (`snapPoints`), never raw days.
  // Controls: ▶ Play / ⏸, ‹ › step, ⟳ loop, ⇄ diff toggle, and an `as of <date>`
  // readout. Play advances over snapPoints via requestAnimationFrame.
  //
  // PURE PRESENTATION: receives snapPoints + density as PROPS (the parent shell
  // derives them from the store in T018). Owns no data access. Emits the current
  // playhead `value` (bindable Date), `playing`, and `diffOn` (bindable) back up,
  // plus a `step` callback so the table can pin/fade per discrete vs play moves.

  import type { DensityBucket } from "./reconstruct";

  // ── Props ────────────────────────────────────────────────────────────────
  interface Props {
    /** Sorted unique event timestamps — the only legal playhead positions. */
    snapPoints: Date[];
    /** Change-density histogram buckets shading the capsule track. */
    density: DensityBucket[];
    /** Current playhead instant (bindable). Snapped to a snapPoint on any move. */
    value?: Date;
    /** Whether Play is running (bindable). */
    playing?: boolean;
    /** ⇄ diff-on-scrub enabled (bindable) — forwarded to the table. */
    diffOn?: boolean;
    /** ms between Play steps (event-to-event). */
    playIntervalMs?: number;
    /**
     * Notified on every playhead move with how the move was driven:
     *   "step" → discrete (←/→, ‹/›) — table PINS the diff
     *   "play" → Play/loop or drag — table lingers-then-fades
     * Lets the table choose pinned-vs-fade without reading our internals.
     */
    onmove?: (t: Date, mode: "step" | "play") => void;
  }

  let {
    snapPoints,
    density,
    value = $bindable(),
    playing = $bindable(false),
    diffOn = $bindable(true),
    playIntervalMs = 900,
    onmove,
  }: Props = $props();

  // ── Time domain (derived from snapPoints) ─────────────────────────────────
  // The capsule maps the [firstEvent, lastEvent] span to 0..100%. With <2 snap
  // points everything collapses to a single position (span guarded to ≥1ms).
  const minMs = $derived(
    snapPoints.length ? snapPoints[0].getTime() : 0,
  );
  const maxMs = $derived(
    snapPoints.length ? snapPoints[snapPoints.length - 1].getTime() : 1,
  );
  const spanMs = $derived(Math.max(1, maxMs - minMs));

  // Default the playhead to the latest event once we know the domain.
  $effect(() => {
    if (value === undefined && snapPoints.length) {
      value = snapPoints[snapPoints.length - 1];
    }
  });

  const valueMs = $derived(value ? value.getTime() : maxMs);
  /** Playhead position as a 0..100 percentage along the capsule. */
  const pct = $derived(
    Math.max(0, Math.min(100, ((valueMs - minMs) / spanMs) * 100)),
  );

  // Density shading: map each bucket to an opacity (faint=quiet, solid=busy).
  const maxBucket = $derived(
    density.reduce((m, b) => Math.max(m, b.changeCount), 0),
  );
  function bucketOpacity(count: number): number {
    if (!maxBucket || count <= 0) return 0;
    return 0.16 + 0.72 * (count / maxBucket);
  }
  /** Left-offset % of a snap tick along the capsule. */
  function snapLeft(d: Date): number {
    return Math.max(0, Math.min(100, ((d.getTime() - minMs) / spanMs) * 100));
  }

  // ── Snapping ──────────────────────────────────────────────────────────────
  /** Nearest snap point to an arbitrary instant (for drag → snap). */
  function snapTo(ms: number): Date | undefined {
    if (!snapPoints.length) return undefined;
    let best = snapPoints[0];
    let bestD = Math.abs(best.getTime() - ms);
    for (const s of snapPoints) {
      const d = Math.abs(s.getTime() - ms);
      if (d < bestD) {
        bestD = d;
        best = s;
      }
    }
    return best;
  }
  function nextSnap(ms: number): Date | undefined {
    return snapPoints.find((s) => s.getTime() > ms);
  }
  function prevSnap(ms: number): Date | undefined {
    for (let i = snapPoints.length - 1; i >= 0; i--) {
      if (snapPoints[i].getTime() < ms) return snapPoints[i];
    }
    return undefined;
  }

  /** Commit a new playhead instant + tell the parent how it moved. */
  function commit(d: Date | undefined, mode: "step" | "play"): void {
    if (!d) return;
    value = d;
    onmove?.(d, mode);
  }

  // ── Discrete step controls (‹ ›, ←/→) — PIN the diff ─────────────────────
  function stepNext(): void {
    commit(nextSnap(valueMs) ?? snapPoints[snapPoints.length - 1], "step");
  }
  function stepPrev(): void {
    commit(prevSnap(valueMs) ?? snapPoints[0], "step");
  }

  // ── Range-input drag → snap (PLAY-style: linger then fade) ────────────────
  let dragRaf = 0;
  function onScrubInput(e: Event): void {
    const frac = Number((e.target as HTMLInputElement).value) / 1000;
    const targetMs = minMs + frac * spanMs;
    if (dragRaf) cancelAnimationFrame(dragRaf);
    dragRaf = requestAnimationFrame(() => commit(snapTo(targetMs), "play"));
  }

  // ── Play / loop via requestAnimationFrame ─────────────────────────────────
  let loop = $state(false);
  let playFrame = 0;
  let lastStepAt = 0;

  function stopPlay(): void {
    playing = false;
    if (playFrame) cancelAnimationFrame(playFrame);
    playFrame = 0;
  }
  function startPlay(): void {
    if (!snapPoints.length) return;
    // Restart from the beginning if parked at the end.
    if (valueMs >= maxMs) commit(snapPoints[0], "play");
    playing = true;
    lastStepAt = performance.now();
    playFrame = requestAnimationFrame(tick);
  }
  function tick(now: number): void {
    if (!playing) return;
    if (now - lastStepAt >= playIntervalMs) {
      lastStepAt = now;
      const nx = nextSnap(valueMs);
      if (nx) {
        commit(nx, "play");
      } else if (loop) {
        commit(snapPoints[0], "play");
      } else {
        stopPlay();
        return;
      }
    }
    playFrame = requestAnimationFrame(tick);
  }
  function togglePlay(): void {
    if (playing) stopPlay();
    else startPlay();
  }
  function toggleLoop(): void {
    loop = !loop;
  }
  function toggleDiff(): void {
    diffOn = !diffOn;
  }

  // Tear down the rAF loop if the component unmounts mid-play.
  $effect(() => () => {
    if (playFrame) cancelAnimationFrame(playFrame);
    if (dragRaf) cancelAnimationFrame(dragRaf);
  });

  // ── Keyboard: ←/→ step over snap points ──────────────────────────────────
  function onKeydown(e: KeyboardEvent): void {
    if (e.key === "ArrowRight") {
      e.preventDefault();
      stepNext();
    } else if (e.key === "ArrowLeft") {
      e.preventDefault();
      stepPrev();
    }
  }

  // ── as-of readout ─────────────────────────────────────────────────────────
  const asOfLabel = $derived(
    value
      ? value.toLocaleDateString("en-US", {
          month: "short",
          day: "numeric",
          year: "numeric",
        })
      : "—",
  );
</script>

<svelte:window onkeydown={onKeydown} />

<div class="timewrap">
  <div class="tctl">
    <button
      class="play"
      onclick={togglePlay}
      title="Play / pause time-lapse"
      aria-label={playing ? "Pause" : "Play"}
    >{playing ? "⏸" : "▶"}</button>
    <button class="stepb" onclick={stepPrev} title="Previous change event" aria-label="Previous event">‹</button>
    <button class="stepb" onclick={stepNext} title="Next change event" aria-label="Next event">›</button>
    <button
      class="stepb"
      class:on={loop}
      onclick={toggleLoop}
      title="Loop playback"
      aria-pressed={loop}
    >⟳</button>
    <button
      class="stepb diff"
      class:on={diffOn}
      onclick={toggleDiff}
      title="Show before → after while scrubbing"
      aria-pressed={diffOn}
    >⇄ diff: {diffOn ? "on" : "off"}</button>
  </div>

  <div class="histo">
    <div class="track">
      <!-- inline-SVG density segments (no chart lib) -->
      <svg class="bars" viewBox="0 0 {Math.max(1, density.length)} 1" preserveAspectRatio="none" aria-hidden="true">
        {#each density as b, i (i)}
          <rect
            x={i}
            y="0"
            width="1"
            height="1"
            fill="var(--col, #2e5d7d)"
            opacity={bucketOpacity(b.changeCount)}
          />
        {/each}
      </svg>
      <div class="fill" style="width:{pct}%"></div>
    </div>
    <div class="playhead" style="left:{pct}%"></div>
    <input
      class="scrub"
      type="range"
      min="0"
      max="1000"
      step="1"
      value={Math.round((pct / 100) * 1000)}
      oninput={onScrubInput}
      aria-label="Scrub time"
    />
  </div>

  <div class="asof"><span class="lbl">as of</span><span class="asof-date">{asOfLabel}</span></div>
</div>

<style>
  .timewrap {
    margin: 0 0 14px;
    display: flex;
    align-items: center;
    gap: 14px;
    background: var(--card, #fff);
    border: 1px solid var(--line, #e7e9ee);
    border-radius: var(--r-pill, 999px);
    padding: 7px 12px 7px 8px;
    box-shadow: var(--shadow);
  }
  .tctl {
    display: flex;
    align-items: center;
    gap: 6px;
    flex: none;
  }
  .play {
    width: 34px;
    height: 34px;
    border-radius: 50%;
    border: none;
    background: var(--accent, #2563eb);
    color: #fff;
    font-size: 12px;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    flex: none;
    padding: 0;
    box-shadow: 0 2px 6px rgba(37, 99, 235, 0.32);
    transition: background 0.15s, transform 0.1s;
  }
  .play:hover {
    background: var(--accent-h, #1d4ed8);
  }
  .play:active {
    transform: scale(0.95);
  }
  .stepb {
    min-width: 32px;
    height: 32px;
    border-radius: var(--r-sm, 8px);
    border: 1px solid var(--line-strong, #d7dae1);
    background: var(--card, #fff);
    color: var(--text, #334155);
    cursor: pointer;
    font-size: 14px;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 0 6px;
    transition: background 0.15s, color 0.15s, border-color 0.15s;
  }
  .stepb:hover {
    background: var(--hover, #f3f5f8);
    color: var(--ink, #0f172a);
  }
  .stepb.diff {
    width: auto;
    padding: 0 12px;
    font-size: 12px;
    font-weight: 600;
  }
  .stepb.on {
    background: var(--accent, #2563eb);
    color: #fff;
    border-color: var(--accent, #2563eb);
  }
  .asof {
    font-variant-numeric: tabular-nums;
    font-size: 13px;
    font-weight: 600;
    color: var(--ink, #0f172a);
    background: var(--accent-bg, #eff5ff);
    border: 1px solid var(--accent-border, #bfd3fb);
    border-radius: var(--r-sm, 8px);
    padding: 6px 12px;
    display: flex;
    align-items: center;
  }
  .asof-date {
    font-family: var(--mono, ui-monospace, monospace);
  }
  .asof .lbl {
    color: var(--accent, #2563eb);
    font-weight: 700;
    font-size: 9.5px;
    margin-right: 8px;
    text-transform: uppercase;
    letter-spacing: 1px;
    font-family: var(--sans, sans-serif);
  }

  .histo {
    position: relative;
    height: 26px;
    flex: 1;
    min-width: 0;
  }
  .asof {
    flex: none;
  }
  .track {
    position: absolute;
    inset: 0;
    border-radius: var(--r-pill, 999px);
    border: 1px solid var(--line, #e7e9ee);
    background: var(--bg-sunken, #eef0f3);
    overflow: hidden;
  }
  .bars {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
  }
  .fill {
    position: absolute;
    top: 0;
    bottom: 0;
    left: 0;
    background: rgba(37, 99, 235, 0.12);
    z-index: 1;
  }
  .playhead {
    position: absolute;
    top: 50%;
    transform: translate(-50%, -50%);
    width: 20px;
    height: 20px;
    border-radius: 50%;
    background: var(--accent, #2563eb);
    border: 3px solid #fff;
    box-shadow: 0 2px 8px rgba(37, 99, 235, 0.45);
    pointer-events: none;
    z-index: 5;
  }
  .scrub {
    position: absolute;
    top: 0;
    left: 0;
    width: 100%;
    height: 28px;
    margin: 0;
    -webkit-appearance: none;
    appearance: none;
    background: transparent;
    cursor: pointer;
    z-index: 6;
  }
  .scrub::-webkit-slider-runnable-track {
    height: 28px;
    background: transparent;
  }
  .scrub::-webkit-slider-thumb {
    -webkit-appearance: none;
    width: 22px;
    height: 28px;
    background: transparent;
    cursor: grab;
  }
  .scrub::-moz-range-track {
    height: 28px;
    background: transparent;
  }
  .scrub::-moz-range-thumb {
    width: 22px;
    height: 28px;
    background: transparent;
    border: none;
    cursor: grab;
  }
</style>
