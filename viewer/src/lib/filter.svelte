<script lang="ts" module>
  // File: viewer/src/lib/filter.svelte  (T038 / US5 — dev-spec §5)
  //
  // A collapsible filter pane with TWO modes that compile to the SAME predicate:
  //   • SIMPLE — selects/range controls over a few tracked columns + min-changes +
  //     id-contains + a `flagged` (immutable-violation) toggle + deleted/resurrected
  //     toggles.
  //   • SQL    — a `WHERE` expression over tracked columns + meta-fields
  //     (`changes`, `deleted`, `resurrected`, `flagged`, `id`). Invalid syntax →
  //     inline error; the prior result is preserved.
  //
  // Both modes emit ONE predicate `(row: FilterRow) => boolean`. The parent
  // (App.svelte) evaluates that predicate ONCE over the per-entity "as-of-now"
  // row views to produce a STABLE row set (entity_id Set), then scrubs THAT set —
  // the filter is NOT re-evaluated per T (dev-spec §5 default = stable set).
  //
  // The SQL shim mirrors the prototype's `sqlToFn`: string-substitution onto the
  // row object, JS-evaluated. Production swaps this for DuckDB's real WHERE parser.

  /** The per-row "as-of-now" view a predicate is evaluated against. */
  export interface FilterRow {
    /** entity-id string (the key column, stringified). */
    id: string;
    /** Total change count for the row (meta `changes`). */
    changes: number;
    /** Lifecycle: ever-deleted / resurrected as-of-now (meta). */
    deleted: boolean;
    resurrected: boolean;
    /** Immutable-column violation (meta `flagged`) — shadows any column named `flagged`. */
    flagged: boolean;
    /** Tracked column values (latest as-of-now), keyed by column name. */
    [col: string]: string | number | boolean;
  }

  export type FilterPredicate = (row: FilterRow) => boolean;

  /** Meta-field names that are always available to predicates. */
  const META_FIELDS = ["changes", "deleted", "resurrected", "flagged", "id"];

  /**
   * Compile a SQL-ish `WHERE` expression to a JS predicate over a FilterRow.
   * Mirrors the prototype shim: LIKE → startsWith/endsWith/includes; =→==;
   * AND/OR→&&/||; bare column/meta names → `r.<name>`. Throws on invalid syntax.
   */
  export function compileSql(expr: string, columns: string[]): FilterPredicate {
    const s0 = expr.trim();
    if (!s0) return () => true;
    let s = s0;
    // LIKE 'pat' → string match (kept BEFORE quote-stripping of operators).
    s = s.replace(/(\w+)\s+LIKE\s+'([^']*)'/gi, (_m, col: string, pat: string) => {
      if (pat.startsWith("%") && pat.endsWith("%"))
        return `String(r.${col}).includes('${pat.slice(1, -1)}')`;
      if (pat.endsWith("%")) return `String(r.${col}).startsWith('${pat.slice(0, -1)}')`;
      if (pat.startsWith("%")) return `String(r.${col}).endsWith('${pat.slice(1)}')`;
      return `String(r.${col})==='${pat}'`;
    });
    // Comparison operators (protect multi-char ops before turning `=`→`==`).
    s = s.replace(/<=/g, "@LE@").replace(/>=/g, "@GE@").replace(/!=/g, "@NE@").replace(/<>/g, "@NE@");
    s = s.replace(/=/g, "==").replace(/@LE@/g, "<=").replace(/@GE@/g, ">=").replace(/@NE@/g, "!=");
    s = s.replace(/\bAND\b/gi, "&&").replace(/\bOR\b/gi, "||");
    // Bind bare identifiers (meta first so `flagged`/`id` win, then columns).
    for (const name of [...META_FIELDS, ...columns]) {
      s = s.replace(new RegExp("\\b" + name + "\\b", "g"), "r." + name);
    }
    s = s.replace(/r\.r\./g, "r."); // de-double any r.r. from overlapping names
    // eslint-disable-next-line no-new-func
    const fn = new Function("r", "return (" + s + ");") as (r: FilterRow) => unknown;
    return (r: FilterRow) => Boolean(fn(r));
  }
</script>

<script lang="ts">
  // ── Props ────────────────────────────────────────────────────────────────
  interface Props {
    /** Tracked columns available to the filter (schema columns, key excluded). */
    columns: string[];
    /** Manifest schema map (column → dtype tag) — picks select vs range controls. */
    schema: Record<string, string>;
    /** Distinct values per low-cardinality string column (for the simple selects). */
    distinct: Record<string, string[]>;
    /** Total entity count N (for "showing X of N"). */
    total: number;
    /** Count currently shown (after the filter) — parent feeds this back. */
    shown: number;
    /** Out: the compiled predicate (bindable so the parent re-evaluates on change). */
    predicate?: FilterPredicate;
  }

  let {
    columns,
    schema,
    distinct,
    total,
    shown,
    predicate = $bindable<FilterPredicate>(() => true),
  }: Props = $props();

  const norm = (d: string): string => (d ?? "").trim().toLowerCase();
  const isNumeric = (col: string): boolean => {
    const d = norm(schema[col]);
    return d.startsWith("int") || d.startsWith("float");
  };
  /** String columns with a small distinct set → render as a <select>. */
  const selectCols = $derived(
    columns.filter((c) => (distinct[c]?.length ?? 0) > 0 && (distinct[c]?.length ?? 0) <= 40),
  );
  /** Numeric columns → render a min/max range pair. */
  const rangeCols = $derived(columns.filter((c) => isNumeric(c)));

  // ── UI state ───────────────────────────────────────────────────────────────
  let tab = $state<"simple" | "sql">("simple");

  // Collapsible-section state (Status open by default; rest collapsed — noise
  // reduction per the minimal redesign). Keyed by section name.
  let open = $state<Record<string, boolean>>({
    status: true,
    columns: false,
    ranges: false,
    refine: false,
  });
  function toggleSection(name: string): void {
    open[name] = !open[name];
  }

  // Simple-mode controls.
  let selVals = $state<Record<string, string>>({}); // column → selected value ("" = any)
  let rangeMin = $state<Record<string, string>>({}); // column → min text ("" = none)
  let rangeMax = $state<Record<string, string>>({});
  let minChanges = $state<string>("");
  let idContains = $state<string>("");
  let onlyFlagged = $state(false);
  let onlyDeleted = $state(false);
  let onlyResurrected = $state(false);

  // SQL-mode controls.
  let sql = $state<string>("");
  let sqlErr = $state<string>("");

  /** Build the SIMPLE-mode predicate from the controls. */
  function simplePredicate(): FilterPredicate {
    const sels = { ...selVals };
    const mins = { ...rangeMin };
    const maxs = { ...rangeMax };
    const mc = minChanges.trim() === "" ? NaN : Number(minChanges);
    const idq = idContains.trim();
    const wantFlagged = onlyFlagged;
    const wantDeleted = onlyDeleted;
    const wantResurrected = onlyResurrected;
    return (r: FilterRow) => {
      for (const c of Object.keys(sels)) {
        if (sels[c] && String(r[c]) !== sels[c]) return false;
      }
      for (const c of Object.keys(mins)) {
        if (mins[c] !== "" && !Number.isNaN(Number(mins[c])) && Number(r[c]) < Number(mins[c]))
          return false;
      }
      for (const c of Object.keys(maxs)) {
        if (maxs[c] !== "" && !Number.isNaN(Number(maxs[c])) && Number(r[c]) > Number(maxs[c]))
          return false;
      }
      if (!Number.isNaN(mc) && r.changes < mc) return false;
      if (idq && !r.id.includes(idq)) return false;
      if (wantFlagged && !r.flagged) return false;
      if (wantDeleted && !r.deleted) return false;
      if (wantResurrected && !r.resurrected) return false;
      return true;
    };
  }

  /** Recompile + publish the predicate from whichever tab is active. */
  function apply(): void {
    if (tab === "sql") {
      try {
        const fn = compileSql(sql, columns);
        // Smoke-test the compiled fn against a dummy row to surface syntax errors
        // BEFORE publishing (so a throw doesn't escape into the parent's eval loop).
        fn({
          id: "",
          changes: 0,
          deleted: false,
          resurrected: false,
          flagged: false,
        });
        sqlErr = "";
        predicate = fn;
      } catch (err) {
        // Invalid syntax → inline error, PRESERVE the prior result (don't republish).
        sqlErr = "⚠ " + (err instanceof Error ? err.message : String(err));
      }
    } else {
      sqlErr = "";
      predicate = simplePredicate();
    }
  }

  function clear(): void {
    selVals = {};
    rangeMin = {};
    rangeMax = {};
    minChanges = "";
    idContains = "";
    onlyFlagged = false;
    onlyDeleted = false;
    onlyResurrected = false;
    sql = "";
    sqlErr = "";
    predicate = () => true;
  }

  /** A quick chip → switches to SQL with a canned WHERE and applies. */
  function chip(expr: string): void {
    tab = "sql";
    sql = expr;
    apply();
  }
</script>

<section class="filter">
  <div class="fhead">
    <h2 class="side-title">Filter</h2>
    <span class="count"
      >showing <b>{shown.toLocaleString()}</b> of <b>{total.toLocaleString()}</b></span
    >
  </div>

  <div class="fbody">
    <div class="tabs">
      <button class="tab" class:on={tab === "simple"} type="button" onclick={() => (tab = "simple")}
        >Simple</button
      >
      <button class="tab" class:on={tab === "sql"} type="button" onclick={() => (tab = "sql")}
        >SQL</button
      >
    </div>

    {#if tab === "simple"}
      <div class="simple">
        <!-- Status — open by default; boolean filters as green toggle switches. -->
        <div class="grp" class:open={open.status}>
          <button class="grp-head" type="button" aria-expanded={open.status} onclick={() => toggleSection("status")}>
            <span class="caret" aria-hidden="true">▸</span>
            <span class="grp-title">Status</span>
          </button>
          {#if open.status}
            <div class="grp-body">
              <div class="toggles">
                <label class="tg">
                  <span class="tg-lbl">flagged <span class="i-warn">⚠</span></span>
                  <span class="switch"><input type="checkbox" bind:checked={onlyFlagged} /><span class="slider"></span></span>
                </label>
                <label class="tg">
                  <span class="tg-lbl">deleted</span>
                  <span class="switch"><input type="checkbox" bind:checked={onlyDeleted} /><span class="slider"></span></span>
                </label>
                <label class="tg">
                  <span class="tg-lbl">resurrected <span class="i-res">✦</span></span>
                  <span class="switch"><input type="checkbox" bind:checked={onlyResurrected} /><span class="slider"></span></span>
                </label>
              </div>
            </div>
          {/if}
        </div>

        {#if selectCols.length}
          <div class="grp" class:open={open.columns}>
            <button class="grp-head" type="button" aria-expanded={open.columns} onclick={() => toggleSection("columns")}>
              <span class="caret" aria-hidden="true">▸</span>
              <span class="grp-title">Columns</span>
            </button>
            {#if open.columns}
              <div class="grp-body">
                {#each selectCols as c (c)}
                  <label class="fld">
                    <span>{c}</span>
                    <select bind:value={selVals[c]}>
                      <option value="">any</option>
                      {#each distinct[c] as v (v)}
                        <option value={v}>{v}</option>
                      {/each}
                    </select>
                  </label>
                {/each}
              </div>
            {/if}
          </div>
        {/if}

        {#if rangeCols.length}
          <div class="grp" class:open={open.ranges}>
            <button class="grp-head" type="button" aria-expanded={open.ranges} onclick={() => toggleSection("ranges")}>
              <span class="caret" aria-hidden="true">▸</span>
              <span class="grp-title">Ranges</span>
            </button>
            {#if open.ranges}
              <div class="grp-body">
                {#each rangeCols as c (c)}
                  <label class="fld range">
                    <span>{c}</span>
                    <span class="rr">
                      <input type="number" placeholder="min" bind:value={rangeMin[c]} />
                      <input type="number" placeholder="max" bind:value={rangeMax[c]} />
                    </span>
                  </label>
                {/each}
              </div>
            {/if}
          </div>
        {/if}

        <div class="grp" class:open={open.refine}>
          <button class="grp-head" type="button" aria-expanded={open.refine} onclick={() => toggleSection("refine")}>
            <span class="caret" aria-hidden="true">▸</span>
            <span class="grp-title">Refine</span>
          </button>
          {#if open.refine}
            <div class="grp-body">
              <label class="fld">
                <span>min changes</span>
                <input type="number" placeholder="≥" bind:value={minChanges} />
              </label>
              <label class="fld">
                <span>id contains</span>
                <input type="text" placeholder="substring" bind:value={idContains} />
              </label>
            </div>
          {/if}
        </div>
      </div>
    {:else}
      <div class="sqlpane">
        <label class="fld">
          <span>WHERE expression</span>
          <textarea
            rows="3"
            placeholder="risk > 0.5 AND flagged"
            bind:value={sql}
            onkeydown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                apply();
              }
            }}
          ></textarea>
        </label>
        <div class="hint">
          Columns: {columns.join(", ")}<br />
          Meta: <code>changes</code> <code>deleted</code> <code>resurrected</code>
          <code>flagged</code> <code>id</code><br />
          Ops: = != &lt; &gt; &lt;= &gt;= AND OR LIKE 'x%'
        </div>
        {#if sqlErr}
          <div class="sqlerr">{sqlErr}</div>
        {/if}
      </div>
    {/if}

    <div class="chips">
      <button class="chip" type="button" onclick={() => chip("flagged")}>⚠ violations</button>
      <button class="chip" type="button" onclick={() => chip("deleted = true")}>deleted</button>
      <button class="chip" type="button" onclick={() => chip("resurrected = true")}
        >resurrected</button
      >
    </div>

    <div class="actions">
      <button class="apply" type="button" onclick={apply}>Apply</button>
      <button class="clearbtn" type="button" onclick={clear}>Clear</button>
    </div>
  </div>
</section>

<style>
  .filter {
    font-size: 13px;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .fhead {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 4px;
  }
  .side-title {
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    color: var(--mut);
    margin: 0;
  }
  .count {
    color: var(--mut);
    font-size: 11.5px;
    font-variant-numeric: tabular-nums;
  }
  .count b {
    color: var(--ink);
  }
  .fbody {
    display: flex;
    flex-direction: column;
    gap: 14px;
  }
  .tabs {
    display: flex;
    gap: 4px;
    padding: 3px;
    background: var(--bg-sunken);
    border-radius: var(--r-sm);
  }
  .tab {
    appearance: none;
    flex: 1;
    border: none;
    background: transparent;
    border-radius: 6px;
    padding: 6px 12px;
    font: inherit;
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
    color: var(--mut);
    transition: background 0.15s, color 0.15s;
  }
  .tab:hover {
    color: var(--ink);
  }
  .tab.on {
    background: var(--card);
    color: var(--accent);
    box-shadow: var(--shadow-sm);
  }
  .simple {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  /* Collapsible section: a clickable header row + a body that mounts when open. */
  .grp {
    display: flex;
    flex-direction: column;
    border-bottom: 1px solid var(--line);
    padding-bottom: 6px;
  }
  .grp:last-child {
    border-bottom: none;
  }
  .grp-head {
    appearance: none;
    border: none;
    background: transparent;
    display: flex;
    align-items: center;
    gap: 7px;
    padding: 8px 4px;
    cursor: pointer;
    font: inherit;
    width: 100%;
    text-align: left;
  }
  .grp-head:hover .grp-title {
    color: var(--ink);
  }
  .caret {
    font-size: 9px;
    color: var(--dim);
    transition: transform 0.15s;
    transform-origin: center;
    display: inline-block;
  }
  .grp.open .caret {
    transform: rotate(90deg);
    color: var(--accent);
  }
  .grp-body {
    display: flex;
    flex-direction: column;
    gap: 10px;
    padding: 2px 4px 6px;
  }
  .grp-title {
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.7px;
    color: var(--mut);
    font-weight: 700;
  }
  .fld {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .fld > span {
    font-size: 10.5px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: var(--mut);
    font-weight: 600;
  }
  .fld select,
  .fld input,
  .sqlpane textarea {
    font: inherit;
    font-size: 12.5px;
    padding: 7px 9px;
    border: 1px solid var(--line-strong);
    border-radius: var(--r-sm);
    background: var(--card);
    color: var(--ink);
    width: 100%;
    transition: border-color 0.15s, box-shadow 0.15s;
  }
  .fld select:focus,
  .fld input:focus,
  .sqlpane textarea:focus {
    outline: none;
    border-color: var(--accent);
    box-shadow: 0 0 0 3px var(--accent-bg);
  }
  .fld.range .rr {
    display: flex;
    gap: 8px;
  }
  .toggles {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .tg {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    font-size: 13px;
    color: var(--text);
    cursor: pointer;
    padding: 3px 0;
  }
  .tg-lbl {
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }
  /* Green toggle switch (reference look). */
  .switch {
    position: relative;
    display: inline-block;
    width: 34px;
    height: 19px;
    flex: none;
  }
  .switch input {
    position: absolute;
    opacity: 0;
    width: 100%;
    height: 100%;
    margin: 0;
    cursor: pointer;
  }
  .slider {
    position: absolute;
    inset: 0;
    background: var(--line-strong);
    border-radius: 999px;
    transition: background 0.18s;
  }
  .slider::before {
    content: "";
    position: absolute;
    top: 2px;
    left: 2px;
    width: 15px;
    height: 15px;
    background: #fff;
    border-radius: 50%;
    box-shadow: 0 1px 2px rgba(15, 23, 42, 0.3);
    transition: transform 0.18s;
  }
  .switch input:checked + .slider {
    background: var(--add);
  }
  .switch input:checked + .slider::before {
    transform: translateX(15px);
  }
  .switch input:focus-visible + .slider {
    box-shadow: 0 0 0 3px var(--accent-bg);
  }
  .i-warn {
    color: var(--del);
    font-weight: 700;
  }
  .i-res {
    color: var(--add);
  }
  .sqlpane {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .sqlpane textarea {
    font-family: var(--mono);
    resize: vertical;
  }
  .hint {
    font-size: 11px;
    color: var(--mut);
    line-height: 1.6;
  }
  .hint code {
    font-family: var(--mono);
    background: var(--code-bg);
    padding: 1px 5px;
    border-radius: 4px;
    color: var(--text);
  }
  .sqlerr {
    color: var(--del);
    font-size: 12px;
    font-family: var(--mono);
    background: var(--del-bg);
    border: 1px solid var(--del-border);
    border-radius: var(--r-sm);
    padding: 6px 9px;
  }
  .chips {
    display: flex;
    gap: 7px;
    flex-wrap: wrap;
  }
  .chip {
    appearance: none;
    border: 1px solid var(--line-strong);
    background: var(--card);
    border-radius: var(--r-pill);
    padding: 4px 12px;
    font: inherit;
    font-size: 11.5px;
    cursor: pointer;
    color: var(--mut);
    transition: background 0.15s, color 0.15s, border-color 0.15s;
  }
  .chip:hover {
    background: var(--accent-bg);
    color: var(--accent);
    border-color: var(--accent-border);
  }
  .actions {
    display: flex;
    gap: 8px;
  }
  .apply,
  .clearbtn {
    appearance: none;
    border-radius: var(--r-sm);
    padding: 8px 16px;
    font: inherit;
    font-size: 12.5px;
    font-weight: 600;
    cursor: pointer;
    transition: filter 0.15s, background 0.15s;
  }
  .apply {
    flex: 1;
    background: var(--accent);
    color: #fff;
    border: 1px solid var(--accent);
  }
  .apply:hover {
    background: var(--accent-h);
  }
  .clearbtn {
    background: var(--card);
    color: var(--mut);
    border: 1px solid var(--line-strong);
  }
  .clearbtn:hover {
    background: var(--hover);
    color: var(--ink);
  }
</style>
