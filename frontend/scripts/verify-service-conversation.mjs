// A packaged WebView conversation against the runner's isolated API/database.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdir } from 'node:fs/promises';
import { createServer } from 'node:net';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium, expect } from '@playwright/test';

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const base = process.env.MYBOT_E2E_API_URL;
assert(base, 'Use node scripts/test-service-ui.mjs --desktop [--live]');
const output = resolve(frontend, 'test-results/service-conversation');
await mkdir(output, { recursive: true });
const reservation = createServer();
await new Promise((done, fail) => { reservation.once('error', fail); reservation.listen(0, '127.0.0.1', done); });
const port = reservation.address().port;
await new Promise((done) => reservation.close(done));
const child = spawn(resolve(frontend, 'src-tauri/target/release/mybot-desktop.exe'), [], {
  cwd: frontend, windowsHide: true, stdio: 'ignore', env: { ...process.env,
    MYBOT_PROJECT_ROOT: resolve(frontend, '..'), MYBOT_API_PORT: new URL(base).port,
    DB_URL: '', MYBOT_API_RUNS: '0', MYBOT_API_MEMORY_WORKER: '0',
    WEBVIEW2_USER_DATA_FOLDER: resolve(output, `profile-${Date.now()}`),
    WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS: `--remote-debugging-address=127.0.0.1 --remote-debugging-port=${port}`,
  },
});
const wait = (ms) => new Promise((done) => setTimeout(done, ms));
async function until(read, predicate, label, timeout = 30_000) {
  const end = Date.now() + timeout; let last;
  while (Date.now() < end) {
    try { last = await read(); if (predicate(last)) return last; } catch (error) { last = error.message; }
    await wait(150);
  }
  throw new Error(`${label}: ${JSON.stringify(last)}`);
}
let browser, page;
try {
  await until(async () => (await fetch(`http://127.0.0.1:${port}/json/version`, { signal: AbortSignal.timeout(1000) })).ok, Boolean, 'WebView2 startup');
  browser = await chromium.connectOverCDP(`http://127.0.0.1:${port}`);
  page = await until(() => browser.contexts().flatMap((context) => context.pages()).find((item) => item.url().includes('tauri.localhost')), Boolean, 'Packaged page');
  page.setDefaultTimeout(15_000);
  const errors = []; page.on('pageerror', (error) => errors.push(error.message));
  const status = await until(() => page.evaluate(() => window.__TAURI_INTERNALS__.invoke('local_service_status')), (value) => value.status === 'connected', 'Reuse isolated API');
  assert.equal(status.managed, false); assert.equal(status.base_url, base);
  await expect(page.locator('.environment-label')).toContainText('本机服务');
  await page.getByRole('button', { name: '新建对话', exact: true }).click();
  await page.getByLabel('新会话角色').selectOption('Alpha');
  await page.getByLabel('会话标题').fill('桌面真实服务联调');
  await page.getByRole('button', { name: '开始对话', exact: true }).click();
  await expect(page.getByRole('heading', { name: '桌面真实服务联调', exact: true })).toBeVisible();
  const text = '你好，请用一句简短中文向我打招呼。';
  await page.getByLabel('消息输入', { exact: true }).fill(text);
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await expect(page.locator('.message.assistant')).toHaveCount(1, { timeout: process.env.MYBOT_E2E_LIVE === '1' ? 210_000 : 20_000 });
  await expect(page.locator('.run-status')).toContainText('本轮已完成', { timeout: 30_000 });
  const reply = await page.locator('.message.assistant .message-text').textContent();
  assert(reply?.trim().length > 0);
  await page.screenshot({ path: resolve(output, 'conversation.png') });
  await page.getByRole('button', { name: '简化模式', exact: true }).click();
  await expect(page.locator('.compact-history')).toBeHidden();
  await page.getByLabel('消息输入', { exact: true }).click();
  await expect(page.locator('.compact-history .message.assistant')).toHaveCount(1);
  assert.equal(await page.locator('.message.assistant .message-text').textContent(), reply);
  await page.getByRole('button', { name: '返回工作台', exact: true }).click();
  await page.reload();
  await expect(page.locator('.message.assistant')).toHaveCount(1);
  assert.equal(await page.locator('.message.assistant .message-text').textContent(), reply);
  assert.equal(await page.locator('.message.user .message-text').textContent(), text);
  assert.deepEqual(errors, []);
  await page.evaluate(() => window.__TAURI_INTERNALS__.invoke('plugin:window|close', { label: 'main' })).catch(() => {});
  await until(() => child.exitCode, (code) => code === 0, 'Normal desktop exit');
  assert((await fetch(`${base}/api/health`)).ok, 'Reused isolated service survives desktop exit');
  console.log(`Packaged desktop conversation, compact mode, history recovery and normal exit passed (${process.env.MYBOT_E2E_LIVE === '1' ? 'live configured model' : 'controlled model'}).`);
} catch (error) {
  if (page) {
    await page.screenshot({ path: resolve(output, 'failure.png') }).catch(() => {});
    const status = await page.locator('.run-status').textContent().catch(() => null);
    console.error('Desktop run status:', status);
  }
  throw error;
} finally {
  if (child.exitCode === null && page) await page.evaluate(() => window.__TAURI_INTERNALS__.invoke('plugin:window|close', { label: 'main' })).catch(() => {});
  if (browser) await browser.close().catch(() => {});
  if (child.exitCode === null) {
    const cleanup = spawn('taskkill', ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' });
    await new Promise((done) => { cleanup.once('exit', done); cleanup.once('error', done); });
  }
}
