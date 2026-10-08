import { defineConfig } from 'vitest/config';

export default defineConfig({
  root: 'frontend', base: './',
  server: { host: '127.0.0.1', port: 1420, strictPort: true },
  build: { outDir: '../dist/frontend', emptyOutDir: true },
  test: { environment: 'jsdom', pool: 'threads', maxWorkers: 1, setupFiles: ['./test/setup.ts'], include: ['**/*.test.ts', '**/*.test.tsx'] },
});
