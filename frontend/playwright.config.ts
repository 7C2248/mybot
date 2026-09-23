import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './e2e', fullyParallel: true, workers: 2,
  use: { baseURL: 'http://127.0.0.1:5173', viewport: { width: 1440, height: 900 }, channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge', trace: 'retain-on-failure', screenshot: 'only-on-failure' },
  webServer: { command: 'node node_modules/vite/bin/vite.js --host 127.0.0.1', url: 'http://127.0.0.1:5173', reuseExistingServer: !process.env.CI, timeout: 30_000 },
});
