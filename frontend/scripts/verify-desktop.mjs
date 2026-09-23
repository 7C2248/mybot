// Smoke-test the packaged WebView2 app, using a temporary debugging port and
// isolated browser data. Neither setting is included in the shipped program.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { mkdir, writeFile } from 'node:fs/promises';
import { createServer } from 'node:net';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const executable = resolve(frontend, process.argv[2] ?? 'src-tauri/target/release/mybot-desktop.exe');
assert(existsSync(executable), `Build the desktop app first: ${executable}`);
const output = resolve(frontend, 'test-results/desktop');
await mkdir(output, { recursive: true });
const port = 19223;
for (const debugPort of [port, port + 1]) {
  const reservation = createServer();
  await new Promise((done, fail) => { reservation.once('error', fail); reservation.listen(debugPort, '127.0.0.1', done); });
  await new Promise((done) => reservation.close(done));
}
const children = [];
const apiReservation = createServer();
await new Promise((done, fail) => { apiReservation.once('error', fail); apiReservation.listen(0, '127.0.0.1', done); });
const apiPort = apiReservation.address().port;
await new Promise((done) => apiReservation.close(done));
const wait = (ms) => new Promise((done) => setTimeout(done, ms));
async function until(read, predicate, label, timeout = 15_000) {
  const start = Date.now(); let last;
  while (Date.now() - start < timeout) {
    try { last = await read(); if (predicate(last)) return last; } catch (error) { last = error.message; }
    await wait(100);
  }
  throw new Error(`${label}: ${JSON.stringify(last)}`);
}
function launch(tag, debugPort) {
  const env = { ...process.env, WEBVIEW2_USER_DATA_FOLDER: resolve(output, `profile-${tag}-${Date.now()}`),
    MYBOT_PROJECT_ROOT: resolve(frontend, '..'), MYBOT_API_PORT: String(apiPort),
    DB_URL: '', MYBOT_API_RUNS: '0', MYBOT_API_MEMORY_WORKER: '0' };
  delete env.WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS;
  if (debugPort) env.WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS = `--remote-debugging-address=127.0.0.1 --remote-debugging-port=${debugPort}`;
  const child = spawn(executable, [], { cwd: dirname(executable), env, windowsHide: true, stdio: 'ignore' });
  children.push(child);
  return child;
}
let browser, helperBrowser, page;
try {
  const mainProcess = launch('main', port);
  await until(async () => (await fetch(`http://127.0.0.1:${port}/json/version`, { signal: AbortSignal.timeout(1000) })).ok, Boolean, 'WebView2 debugging endpoint');
  browser = await chromium.connectOverCDP(`http://127.0.0.1:${port}`);
  page = await until(() => browser.contexts().flatMap((context) => context.pages()).find((page) => page.url().includes('tauri.localhost')), Boolean, 'Packaged app page');
  await page.evaluate(() => localStorage.setItem('mybot.frontend.connection.v1', JSON.stringify({ mode: 'demo', baseUrl: 'http://127.0.0.1:8765' })));
  await page.reload();
  // Playwright enables focus emulation when attaching. Disable it to exercise
  // document.hasFocus() and unread handling against actual OS activation.
  await (await page.context().newCDPSession(page)).send('Emulation.setFocusEmulationEnabled', { enabled: false });
  page.setDefaultTimeout(10_000);
  const errors = []; page.on('pageerror', (error) => errors.push(error.message));
  const invoke = (command, args = {}) => page.evaluate(({ command, args }) => window.__TAURI_INTERNALS__.invoke(`plugin:window|${command}`, { label: 'main', ...args }), { command, args });
  const snapshot = async () => {
    const [size, position, scale, decorated, topmost] = await Promise.all(['inner_size', 'outer_position', 'scale_factor', 'is_decorated', 'is_always_on_top'].map((name) => invoke(name)));
    return { size, position, scale, decorated, topmost };
  };
  const near = (a, b, label) => assert(Math.abs(a - b) < 3, `${label}: ${a} vs ${b}`);
  const inputAnchor = async () => {
    const native = await snapshot();
    const bottom = await page.locator('.compact-composer').evaluate((element) => element.getBoundingClientRect().bottom);
    return native.position.y + bottom * native.scale;
  };
  const trackInput = () => page.evaluate(() => {
    window.__inputFrames = []; window.__trackInput = true;
    function frame() {
      const rect = document.querySelector('.compact-composer')?.getBoundingClientRect();
      if (rect) window.__inputFrames.push({ bottom: screenY + rect.bottom, inset: innerHeight - rect.bottom, screenY, viewport: innerHeight, time: performance.now() });
      if (window.__trackInput) requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
  });
  const checkInputFrames = async (label) => {
    await wait(100);
    const frames = await page.evaluate(() => { window.__trackInput = false; return window.__inputFrames; });
    assert(frames.length > 0);
    // screenY and the Blink viewport update independently in WebView2. Check
    // the CSS bottom anchor in every layout, and the physical anchor separately.
    const insets = frames.map((frame) => frame.inset);
    assert(Math.max(...insets) - Math.min(...insets) < 3, `${label}: input bottom inset changed ${Math.max(...insets) - Math.min(...insets)} CSS px`);
  };
  await page.getByRole('button', { name: '简化模式', exact: true }).waitFor();
  await invoke('set_focus');
  const workbench = await snapshot();
  let rememberedWidth = 676, rememberedHistory = 280;
  await page.screenshot({ path: resolve(output, 'workbench.png') });
  for (const placement of ['bubble', 'inline']) {
    if (placement === 'inline') {
      await page.getByRole('button', { name: '设置', exact: true }).click();
      await page.getByRole('button', { name: '简化模式', exact: true }).click();
      await page.getByLabel('简化模式回复位置').selectOption('inline');
      await page.getByRole('button', { name: '进入简化模式' }).click();
    } else await page.getByRole('button', { name: '简化模式', exact: true }).click();
    await page.locator('.compact-scene.native .collapsed').waitFor();
    await until(snapshot, (value) => value.size.height / value.scale < 110, 'Compact starts as input bar');
    await page.getByRole('button', { name: '拖动输入条', exact: true }).focus();
    assert.equal(await page.locator('.compact-history').isVisible(), false, 'Drag handle focus does not open history');
    await page.getByRole('button', { name: '返回工作台', exact: true }).focus();
    assert.equal(await page.locator('.compact-history').isVisible(), false, 'Return button focus does not open history');
    const collapsedAnchor = await inputAnchor();
    await trackInput();
    await page.getByLabel('消息输入', { exact: true }).click();
    await page.locator('.compact-scene.native .expanded').waitFor();
    await until(() => page.locator('.message-list').evaluate((element) => element.clientHeight), (height) => height > 200, 'Expanded history height');
    await wait(350);
    near(await inputAnchor(), collapsedAnchor, 'Input stays anchored when history opens');
    await checkInputFrames('Opening history');
    const expanded = await snapshot();
    assert.equal(expanded.decorated, false); assert.equal(expanded.topmost, true);
    assert.equal(await page.locator('.global-notice').count(), 0, 'No desktop permission errors');
    const initialHistory = await page.locator('.message-list').evaluate((element) => element.clientHeight);
    near(initialHistory, rememberedHistory, 'History height retained across modes');
    near(expanded.size.width / expanded.scale, rememberedWidth, 'Compact width retained across modes');
    const width = expanded.size.width / expanded.scale + 100;
    const height = expanded.size.height / expanded.scale + 60;
    await invoke('set_size', { value: { Logical: { width, height } } });
    await until(() => page.locator('.message-list').evaluate((element) => element.clientHeight), (value) => Math.abs(value - initialHistory - 60) < 3, 'History follows native window resize');
    const resized = await snapshot();
    const history = page.locator('.message-list');
    const distanceFromLatest = () => history.evaluate((element) => element.scrollHeight - element.clientHeight - element.scrollTop);
    await until(distanceFromLatest, (value) => value < 3, 'Latest reply stays visible after native resize');
    await history.evaluate((element) => { element.scrollTop = 40; });
    await page.getByRole('button', { name: '回到最新', exact: true }).waitFor();
    await invoke('set_size', { value: { Logical: { width, height: height - 20 } } });
    await until(() => history.evaluate((element) => element.clientHeight), (value) => Math.abs(value - initialHistory - 40) < 3, 'Scrolled history resize');
    near(await history.evaluate((element) => element.scrollTop), 40, 'Scrollback preserved');
    await page.getByRole('button', { name: '回到最新', exact: true }).click();
    await invoke('set_size', { value: { Logical: { width, height: 1 } } });
    await until(() => history.evaluate((element) => element.clientHeight), (value) => Math.abs(value - 72) < 3, 'Native minimum readable history');
    await invoke('set_size', { value: { Logical: { width, height } } });
    await until(() => history.evaluate((element) => element.clientHeight), (value) => Math.abs(value - initialHistory - 60) < 3, 'Native resize restored');
    const messageHeight = await page.locator('.message-list').evaluate((element) => element.clientHeight);
    await page.getByLabel('消息输入', { exact: true }).fill('第一行\n\n  原生窗口草稿');
    await wait(250);
    const beforeCollapseAnchor = await inputAnchor();
    await trackInput();
    await page.getByLabel('消息输入', { exact: true }).press('Escape');
    await until(snapshot, (value) => value.size.height / value.scale < 110, 'Collapsed OS window bounds');
    near(await inputAnchor(), beforeCollapseAnchor, 'History collapses down to stationary input');
    await checkInputFrames('Collapsing history');
    await page.getByLabel('消息输入', { exact: true }).click();
    await until(() => page.locator('.message-list').evaluate((element) => element.clientHeight), (value) => Math.abs(value - messageHeight) < 3, 'History height restores after focus');
    await page.getByLabel('消息输入', { exact: true }).fill('');
    await wait(250);
    await until(distanceFromLatest, (value) => value < 3, 'Latest reply remains visible after multiline collapse');
    const beforeLeaving = await snapshot();
    rememberedWidth = beforeLeaving.size.width / beforeLeaving.scale;
    rememberedHistory = await page.locator('.message-list').evaluate((element) => element.clientHeight);
    await page.screenshot({ path: resolve(output, `${placement}.png`) });
    await page.getByLabel('消息输入', { exact: true }).press('Escape');
    await until(snapshot, (value) => value.size.height / value.scale < 110, 'Input bar before returning');
    const back = await page.getByRole('button', { name: '返回工作台' }).boundingBox();
    await page.mouse.move(back.x + back.width / 2, back.y + back.height / 2);
    await page.mouse.down();
    await wait(100);
    assert.equal(await page.locator('.compact-history').isVisible(), false, 'Return pointer down does not open history');
    assert((await snapshot()).size.height / expanded.scale < 110, 'Return pointer down keeps input-sized window');
    await page.mouse.up();
    await page.getByRole('heading', { name: '雨后的傍晚' }).waitFor();
    const restored = await until(snapshot, (value) => value.decorated && value.size.height === workbench.size.height, 'Workbench window restored');
    assert(restored.decorated && !restored.topmost);
    near(restored.size.width, workbench.size.width, 'Workbench width restored');
    near(restored.size.height, workbench.size.height, 'Workbench height restored');
    near(restored.position.x, workbench.position.x, 'Workbench horizontal position restored');
    near(restored.position.y, workbench.position.y, 'Workbench vertical position restored');
    console.log(`${placement}: native size ${Math.round(resized.size.width / resized.scale)} × ${Math.round(resized.size.height / resized.scale)}, focus collapse and workbench restore passed`);
  }
  // Returning while expanded must remember the input's position, rather than
  // the top edge of the message window, for the next compact session.
  await page.getByRole('button', { name: '简化模式', exact: true }).click();
  await page.locator('.compact-scene.native .collapsed').waitFor();
  await page.getByLabel('消息输入', { exact: true }).click();
  await until(() => page.locator('.message-list').evaluate((element) => element.clientHeight), (value) => Math.abs(value - rememberedHistory) < 3, 'Expanded window before switching modes');
  const rememberedInputAnchor = await inputAnchor();
  await page.getByRole('button', { name: '返回工作台' }).click();
  await until(snapshot, (value) => value.decorated && value.size.height === workbench.size.height, 'Workbench restored from expanded mode');
  await page.getByRole('button', { name: '简化模式', exact: true }).click();
  await page.locator('.compact-scene.native .collapsed').waitFor();
  await until(snapshot, (value) => value.size.height / value.scale < 110, 'Reopened input bar');
  near(await inputAnchor(), rememberedInputAnchor, 'Input position retained across workbench');
  await page.getByRole('button', { name: '返回工作台' }).click();
  await until(snapshot, (value) => value.decorated && value.size.height === workbench.size.height, 'Workbench before focus test');
  // A second instance takes actual OS focus, while the first receives a reply.
  launch('focus-helper', port + 1);
  await until(async () => (await fetch(`http://127.0.0.1:${port + 1}/json/version`, { signal: AbortSignal.timeout(1000) })).ok, Boolean, 'Focus helper endpoint');
  helperBrowser = await chromium.connectOverCDP(`http://127.0.0.1:${port + 1}`);
  const helperPage = await until(() => helperBrowser.contexts().flatMap((context) => context.pages()).find((candidate) => candidate.url().includes('tauri.localhost')), Boolean, 'Focus helper page');
  await (await helperPage.context().newCDPSession(helperPage)).send('Emulation.setFocusEmulationEnabled', { enabled: false });
  await invoke('set_focus');
  await page.getByRole('button', { name: '简化模式', exact: true }).click();
  await page.locator('.compact-scene.native .collapsed').waitFor();
  await page.getByLabel('消息输入', { exact: true }).click();
  await page.locator('.compact-scene.native .expanded').waitFor();
  await until(() => page.locator('.message-list').evaluate((element) => element.clientHeight), (value) => Math.abs(value - rememberedHistory) < 3, 'Final compact height restored');
  await page.getByLabel('消息输入', { exact: true }).fill('验证后台回复');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await helperPage.evaluate(() => window.__TAURI_INTERNALS__.invoke('plugin:window|set_focus', { label: 'main' }));
  await until(() => page.evaluate(() => document.hasFocus()), (focused) => !focused, 'Actual native window focus lost');
  await page.locator('.compact-history').waitFor({ state: 'hidden' });
  await page.locator('.unread-label').waitFor();
  assert.equal(await page.locator('.compact-history').isVisible(), false, 'Background reply does not reopen history');
  await invoke('set_focus');
  await until(() => page.evaluate(() => document.hasFocus()), Boolean, 'Window reactivated');
  await wait(100);
  assert.equal(await page.locator('.compact-history').isVisible(), false, 'Window activation alone does not open history');
  await page.getByRole('button', { name: '拖动输入条', exact: true }).focus();
  assert.equal(await page.locator('.compact-history').isVisible(), false, 'Drag handle activation preserves collapsed history');
  await page.getByLabel('消息输入', { exact: true }).click();
  await page.locator('.compact-history').waitFor({ state: 'visible' });
  assert.equal(await page.locator('.global-notice').count(), 0);
  assert.deepEqual(errors, []);
  console.log('Native focus loss, background unread reply, packaged assets and JavaScript runtime passed.');
  await page.getByLabel('消息输入', { exact: true }).press('Escape');
  await until(snapshot, (value) => value.size.height / value.scale < 110, 'Input bar before exiting');
  await page.getByRole('button', { name: '退出程序', exact: true }).focus();
  assert.equal(await page.locator('.compact-history').isVisible(), false, 'Exit button does not open history');
  await page.getByRole('button', { name: '退出程序', exact: true }).click();
  await until(() => mainProcess.exitCode, (code) => code === 0, 'Exit button terminates application');
  console.log('Input anchoring during expansion/collapse and direct application exit passed.');
} catch (error) {
  if (page) {
    await page.screenshot({ path: resolve(output, 'failure.png') }).catch(() => {});
    const diagnostics = await page.evaluate(() => ({ inputFrames: window.__inputFrames, viewport: [innerWidth, innerHeight], focus: document.hasFocus(), notices: document.querySelector('.global-notice')?.textContent, storage: { ...localStorage },
      elements: ['.compact-group', '.compact-history', '.message-list-wrap', '.message-list', '.compact-composer'].map((selector) => { const element = document.querySelector(selector); return { selector, rect: element?.getBoundingClientRect().toJSON(), className: element?.className, display: element ? getComputedStyle(element).display : null }; }) })).catch(() => ({}));
    await writeFile(resolve(output, 'failure.json'), JSON.stringify(diagnostics, null, 2));
    console.error(`Desktop diagnostics saved to ${resolve(output, 'failure.json')}`);
  }
  throw error;
} finally {
  if (browser) await browser.close().catch(() => {});
  if (helperBrowser) await helperBrowser.close().catch(() => {});
  for (const child of children) {
    if (child.exitCode !== null) continue;
    const cleanup = spawn('taskkill', ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' });
    await new Promise((done) => { cleanup.once('exit', done); cleanup.once('error', done); });
  }
}
