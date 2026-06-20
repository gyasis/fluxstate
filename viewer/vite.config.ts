/// <reference types="vitest/config" />
import { defineConfig } from 'vite'
import { svelte } from '@sveltejs/vite-plugin-svelte'

// https://vite.dev/config/
export default defineConfig({
  plugins: [svelte()],
  // DuckDB-WASM ships a web-worker + .wasm and uses top-level await / BigInt.
  // It must NOT be dep-pre-bundled (esbuild mangles its worker glue), and the
  // build/dev targets need to be modern enough for top-level await.
  // The worker + .wasm themselves are loaded from the jsDelivr CDN at runtime
  // (see getDuckDB() in src/lib/duckdb.ts), so no local asset copying is needed.
  optimizeDeps: {
    exclude: ['@duckdb/duckdb-wasm'],
    esbuildOptions: { target: 'esnext' },
  },
  build: { target: 'esnext' },
  worker: { format: 'es' },
  // Vitest runs the UNIT/PARITY suites only (`*.test.ts`). The Playwright
  // interaction spec (`*.spec.ts`, T022) imports `@playwright/test` and is run
  // by `npx playwright test`, NOT vitest — so it's excluded here.
  test: {
    include: ['tests/**/*.test.ts'],
  },
})
