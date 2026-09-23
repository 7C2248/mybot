// Isolated lifecycle smoke test: no business database, model calls or login startup.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdir } from 'node:fs/promises';
import { createServer } from 'node:net';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const executable = resolve(frontend, 'src-tauri/target/release/mybot-desktop.exe');
const output = resolve(frontend, 'test-results/service');
await mkdir(output, { recursive: true });
const wait = (ms) => new Promise((done) => setTimeout(done, ms));
async function freePort() {
  const server = createServer();
  await new Promise((done, fail) => { server.once('error', fail); server.listen(0, '127.0.0.1', done); });
  const port = server.address().port;
  await new Promise((done) => server.close(done));
  return port;
}
async function until(read, predicate, label) {
  const end = Date.now() + 35_000;
  let last;
  while (Date.now() < end) {
    try { last = await read(); if (predicate(last)) return last; } catch (error) { last = error.message; }
    await wait(150);
  }
  throw new Error(`${label}: ${JSON.stringify(last)}`);
}
const apiPort = await freePort();
const base = `http://127.0.0.1:${apiPort}`;
const env = { ...process.env, MYBOT_PROJECT_ROOT: resolve(frontend, '..'),
  MYBOT_API_PORT: String(apiPort), DB_URL: '', MYBOT_API_RUNS: '0', MYBOT_API_MEMORY_WORKER: '0' };
const apps = [];
async function launch(tag) {
  const debugPort = await freePort();
  const app = { child: spawn(executable, [], { cwd: frontend, windowsHide: true, stdio: 'ignore',
    env: { ...env, WEBVIEW2_USER_DATA_FOLDER: resolve(output, `${tag}-${Date.now()}`),
      WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS: `--remote-debugging-address=127.0.0.1 --remote-debugging-port=${debugPort}` } }) };
  apps.push(app);
  await until(async () => (await fetch(`http://127.0.0.1:${debugPort}/json/version`, { signal: AbortSignal.timeout(1000) })).ok,
    Boolean, 'WebView2 startup');
  app.browser = await chromium.connectOverCDP(`http://127.0.0.1:${debugPort}`);
  app.page = await until(() => app.browser.contexts().flatMap((context) => context.pages())
    .find((page) => page.url().includes('tauri.localhost')), Boolean, 'Packaged page');
  app.invoke = (command, args = {}) => app.page.evaluate(({ command, args }) =>
    window.__TAURI_INTERNALS__.invoke(command, args), { command, args });
  return app;
}
async function close(app) {
  await app.invoke('plugin:window|close', { label: 'main' }).catch(() => {});
  await until(() => app.child.exitCode, (code) => code === 0, 'Normal desktop exit');
}
async function healthy() {
  try { return (await fetch(`${base}/api/health`, { signal: AbortSignal.timeout(1000) })).ok; }
  catch { return false; }
}
try {
  const owner = await launch('owner');
  const status = await until(() => owner.invoke('local_service_status'),
    (value) => value.status === 'connected', 'Managed service startup');
  assert.equal(status.managed, true);
  assert.equal(status.base_url, base);
  await until(() => owner.page.locator('.environment-label').textContent(), (value) => value?.includes('本机服务'), 'Frontend defaults to local service');
  await owner.page.getByRole('button', { name: '角色', exact: true }).click();
  await until(() => owner.page.locator('.character-option').count(), (value) => value > 0, 'Real characters load through packaged CSP');
  const state = await (await fetch(`${base}/api/service`)).json();
  assert.equal(state.startup_mode, 'desktop');
  assert.equal(state.runtime_ready, false); // This test deliberately disables inference.
  const denied = await fetch(`${base}/api/service/shutdown`, {
    method: 'POST', headers: { 'X-Mybot-Client': 'mybot-desktop' },
  });
  assert.equal(denied.status, 403);
  const guest = await launch('guest');
  const reused = await until(() => guest.invoke('local_service_status'),
    (value) => value.status === 'connected', 'Reuse existing service');
  assert.equal(reused.managed, false);
  await close(guest);
  assert.equal(await healthy(), true, 'Guest exit must leave the reused service running');
  await close(owner);
  await until(healthy, (value) => !value, 'Owned Python process exits with desktop');
  console.log('Desktop frontend connection, real character loading, service startup/reuse, owner protection and owned-child cleanup passed.');
} finally {
  for (const app of apps.reverse()) {
    if (app.child.exitCode === null && app.invoke) await close(app).catch(() => {});
    if (app.browser) await app.browser.close().catch(() => {});
    if (app.child.exitCode === null) {
      // Scope forced cleanup to this test's app and its own process tree.
      const cleanup = spawn('taskkill', ['/PID', String(app.child.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' });
      await new Promise((done) => { cleanup.once('exit', done); cleanup.once('error', done); });
    }
  }
}
