import { expect, test, type Page } from '@playwright/test';

const base = process.env.MYBOT_E2E_API_URL ?? '';
const live = process.env.MYBOT_E2E_LIVE === '1';
const clientHeaders = { 'X-Mybot-Client': 'mybot-desktop' };
test.skip(!base, 'Run with npm run test:service-ui against an isolated API/database.');
test.beforeEach(async ({ page }) => {
  await page.addInitScript((baseUrl) => localStorage.setItem('mybot.frontend.connection.v1', JSON.stringify({ mode: 'service', baseUrl })), base);
  await page.goto('/');
  await expect(page.locator('.environment-label')).toContainText('本机服务');
});
async function create(page: Page, title: string) {
  await page.getByRole('button', { name: '新建对话', exact: true }).click();
  await page.getByLabel('新会话角色').selectOption('Alpha');
  await page.getByLabel('会话标题').fill(title);
  await page.getByRole('button', { name: '开始对话', exact: true }).click();
  await expect(page.getByRole('heading', { name: title, exact: true })).toBeVisible();
}
async function send(page: Page, text: string) {
  await page.getByLabel('消息输入', { exact: true }).fill(text);
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
}
async function services(page: Page) {
  await page.getByRole('button', { name: '设置', exact: true }).click();
  await page.getByRole('button', { name: '连接与语音', exact: true }).click();
}
test.describe('controlled model with real API and PostgreSQL', () => {
  test.skip(live, 'Controlled fault and fixture data scenarios');
  test('checkpoint confirmation recovers from conflict and refreshes another client', async ({ page, context }) => {
    const title = '检查点截断验证';
    await page.locator('.session').filter({ hasText: title }).click();
    await expect(page.locator('.message')).toHaveCount(4);
    await page.getByLabel('消息输入', { exact: true }).fill('同步后保留的草稿');
    const second = await context.newPage();
    try {
      await second.goto('/');
      await second.locator('.session').filter({ hasText: title }).click();
      await expect(second.locator('.message')).toHaveCount(4);
      await second.getByLabel('消息输入', { exact: true }).fill('另一客户端草稿');
      const list = await (await page.request.get(`${base}/api/threads`)).json();
      const thread = list.items.find((item: { title: string }) => item.title === title);
      await page.getByRole('button', { name: `${title} 的会话设置`, exact: true }).click();
      let releasePreview!: () => void;
      const previewGate = new Promise<void>((resolve) => { releasePreview = resolve; });
      await page.route(`${base}/api/threads/${thread.id}/checkpoint-sync`, async (route) => {
        await previewGate; await route.continue();
      }, { times: 1 });
      await page.getByRole('button', { name: '预览检查点差异', exact: true }).click();
      const dialog = page.getByRole('dialog', { name: '重新加载检查点', exact: true });
      await expect(dialog.getByRole('status')).toContainText('正在读取检查点差异');
      await page.keyboard.press('Escape');
      await expect(dialog).toBeVisible();
      releasePreview();
      await expect(dialog).toContainText('检查点保留回复');
      await expect(dialog).toContainText('将删除 2 条消息');
      await expect(dialog).toContainText('对应语音资源会排队清理');
      expect((await (await page.request.get(`${base}/api/threads/${thread.id}/messages`)).json()).items).toHaveLength(4);
      // A concurrent settings update invalidates the confirmed version.
      expect((await page.request.patch(`${base}/api/threads/${thread.id}`, {
        headers: clientHeaders, data: { expected_version: thread.version, title },
      })).ok()).toBe(true);
      await dialog.getByRole('button', { name: '确认截断', exact: true }).click();
      await expect(dialog.getByRole('alert')).toContainText('请重新预览后再确认');
      await expect(dialog.getByRole('button', { name: '确认截断', exact: true })).toBeDisabled();
      await dialog.getByRole('button', { name: '重新预览', exact: true }).click();
      await expect(dialog.getByRole('button', { name: '确认截断', exact: true })).toBeEnabled();
      await dialog.getByRole('button', { name: '确认截断', exact: true }).click();
      await expect(dialog).not.toBeVisible();
      await expect(page.getByText('已按检查点截断 2 条消息。', { exact: true })).toBeVisible();
      // Re-previewing must not authorize overwriting settings with an older edit.
      await page.getByRole('button', { name: '保存会话设置', exact: true }).click();
      await expect(page.getByRole('alert')).toContainText('会话设置已变化');
      await page.locator('.session').filter({ hasText: title }).click();
      await expect(page.locator('.message')).toHaveCount(2);
      await expect(page.getByLabel('消息输入', { exact: true })).toHaveValue('同步后保留的草稿');
      await expect(second.locator('.message')).toHaveCount(2, { timeout: 12_000 });
      await expect(second.getByLabel('消息输入', { exact: true })).toHaveValue('另一客户端草稿');
      expect((await (await page.request.get(`${base}/api/threads/${thread.id}/messages`)).json()).items).toHaveLength(2);
    } finally { await second.close(); }
  });
  test('background memory stays separate while the next conversation reply completes', async ({ page }) => {
    await page.getByRole('button', { name: '新建对话', exact: true }).click();
    await page.getByLabel('新会话角色').selectOption('Alpha');
    await page.getByLabel('会话标题').fill('后台并行');
    await page.getByLabel('将本会话内容存入长期记忆', { exact: true }).check();
    await page.getByRole('button', { name: '开始对话', exact: true }).click();
    await expect(page.getByRole('heading', { name: '后台并行', exact: true })).toBeVisible();
    await send(page, '触发后台记忆');
    await expect(page.locator('.run-status')).toContainText('本轮已完成');
    await expect(page.locator('.background-memory-status')).toContainText('正在整理', { timeout: 10000 });
    const threads = await (await page.request.get(`${base}/api/threads`)).json();
    const thread = threads.items.find((value: { title: string }) => value.title === '后台并行');
    await send(page, '记忆整理时继续对话');
    await expect(page.locator('.message.assistant')).toHaveCount(2, { timeout: 6000 });
    await expect(page.locator('.run-status')).toContainText('本轮已完成');
    expect((await (await page.request.get(`${base}/api/threads/${thread.id}/memory-status`)).json()).status).toBe('running');
    await page.getByLabel('消息输入', { exact: true }).fill('下一条草稿');
    await expect(page.getByRole('button', { name: '发送消息', exact: true })).toBeEnabled();
  });
  test('memory switches, CLI changes, conflict drafts, trash and restore share one conversation', async ({ page }) => {
    await page.getByRole('button', { name: '新建对话', exact: true }).click();
    await expect(page.getByLabel('检索长期记忆', { exact: true })).not.toBeChecked();
    await expect(page.getByLabel('将本会话内容存入长期记忆', { exact: true })).not.toBeChecked();
    await page.getByLabel('新会话角色').selectOption('Alpha');
    await page.getByLabel('会话标题').fill('策略与删除');
    await page.getByRole('button', { name: '开始对话', exact: true }).click();
    await send(page, '保留历史');
    await expect(page.locator('.run-status')).toContainText('本轮已完成');
    const list = await (await page.request.get(`${base}/api/threads`)).json();
    const thread = list.items.find((item: { title: string }) => item.title === '策略与删除');
    await page.getByRole('button', { name: '策略与删除 的会话设置', exact: true }).click();
    await page.getByLabel('修改会话标题').fill('保留本地编辑');
    // The exact endpoint used by CLIClient.update_policy.
    expect((await page.request.patch(`${base}/api/threads/${thread.id}`, { headers: clientHeaders, data: { expected_version: thread.version, memory_storage_enabled: true } })).ok()).toBe(true);
    await page.getByRole('button', { name: '保存会话设置', exact: true }).click();
    await expect(page.getByRole('alert')).toContainText('会话设置已变化');
    await expect(page.getByLabel('修改会话标题')).toHaveValue('保留本地编辑');
    await page.getByRole('button', { name: '放弃编辑并重读', exact: true }).click();
    await expect(page.getByLabel('将本会话内容存入长期记忆', { exact: true })).toBeChecked();
    await page.getByLabel('检索长期记忆', { exact: true }).check();
    await page.getByRole('button', { name: '保存会话设置', exact: true }).click();
    await expect(page.getByText('已保存，新的输入使用此记忆策略。')).toBeVisible();
    let row = page.locator('.managed-row').filter({ hasText: '策略与删除' });
    await expect(row).toContainText('检索开 / 存储开');
    await row.getByRole('button', { name: '删除会话 策略与删除', exact: true }).click();
    await expect(page.locator('.session').filter({ hasText: '策略与删除' })).toHaveCount(0);
    await page.getByRole('button', { name: '回收站', exact: true }).click();
    row = page.locator('.managed-row').filter({ hasText: '策略与删除' });
    await row.getByRole('button', { name: '恢复', exact: true }).click();
    await page.locator('.session').filter({ hasText: '策略与删除' }).click();
    await expect(page.locator('.message')).toHaveCount(2);
    await page.getByRole('button', { name: '策略与删除 的会话设置', exact: true }).click();
    await page.getByRole('button', { name: '会话列表', exact: true }).click();
    await page.getByRole('button', { name: '删除会话 策略与删除', exact: true }).click();
    await page.getByRole('button', { name: '回收站', exact: true }).click();
    await page.locator('.managed-row').filter({ hasText: '策略与删除' }).getByRole('button', { name: '永久删除', exact: true }).click();
    await page.getByRole('button', { name: '确认永久删除', exact: true }).click();
    await expect(page.locator('.managed-row').filter({ hasText: '策略与删除' })).toHaveCount(0);
    expect((await page.request.get(`${base}/api/threads/${thread.id}/messages`)).status()).toBe(404);
  });
  test('discovers CLI history automatically and associates an unknown role before continuing', async ({ page }) => {
    await expect(page.locator('.session').filter({ hasText: 'CLI · cli-known' })).toBeVisible({ timeout: 15000 });
    await page.locator('.session').filter({ hasText: 'CLI · cli-known' }).click();
    await expect(page.locator('.message')).toHaveCount(2);
    await expect(page.locator('.history-notice')).toContainText('可能存在无法恢复的历史');
    await page.getByRole('button', { name: '管理 CLI 历史', exact: true }).click();
    const row = page.locator('.legacy-row').filter({ hasText: 'cli-unlinked' });
    await expect(row.getByRole('button', { name: '关联并导入' })).toBeDisabled();
    await row.getByRole('button', { name: '预览历史', exact: true }).click();
    await expect(page.locator('.legacy-preview')).toContainText('cli-unlinked 正式回复');
    await page.getByRole('button', { name: '关闭', exact: true }).click();
    await row.getByRole('combobox').selectOption('Beta');
    await row.getByRole('button', { name: '关联并导入', exact: true }).click();
    const session = page.locator('.session').filter({ hasText: 'CLI · cli-unlinked' });
    await expect(session).toBeVisible({ timeout: 15000 });
    await session.click();
    await expect(page.locator('.message')).toHaveCount(2);
    await send(page, '导入后继续');
    await expect(page.locator('.message')).toHaveCount(4);
    await expect(page.locator('.run-status')).toContainText('本轮已完成');
    await page.reload();
    await expect(page.locator('.message')).toHaveCount(4);
    await expect(page.locator('.session').filter({ hasText: 'CLI · cli-unlinked' })).toHaveCount(1);
  });
  test('lost submission response and page reload preserve raw input and one committed reply', async ({ page }) => {
    await create(page, '断线恢复');
    const keys: string[] = []; let dropped = false;
    await page.route(`${base}/api/threads/*/runs`, async (route) => {
      if (route.request().method() !== 'POST') return route.continue();
      keys.push(route.request().postDataJSON().client_request_id);
      if (!dropped) { dropped = true; await route.fetch(); await route.abort(); } else await route.continue();
    });
    const text = '  第一行\n\n  第二行  ';
    await send(page, text);
    await expect.poll(() => dropped).toBe(true);
    await page.getByLabel('消息输入', { exact: true }).fill('保留下一条草稿');
    await page.reload();
    await expect(page.locator('.message.assistant')).toHaveCount(1);
    await expect(page.locator('.message.user .message-text')).toHaveText(text, { useInnerText: false });
    await expect(page.getByLabel('消息输入', { exact: true })).toHaveValue('保留下一条草稿');
    expect(keys.length).toBeGreaterThanOrEqual(2); expect(new Set(keys).size).toBe(1);
    const cache = await page.evaluate((url) => JSON.parse(localStorage.getItem(`mybot.frontend.service.v1:${url}`)!), base);
    expect(Object.values(cache.threads).some((thread) => Object.hasOwn(thread as object, 'messages'))).toBe(false);
    await page.getByRole('button', { name: '简化模式', exact: true }).click();
    await expect(page.locator('.compact-history')).toBeHidden();
    await page.getByLabel('消息输入', { exact: true }).click();
    await expect(page.locator('.compact-history .message.assistant')).toHaveCount(1);
    await page.getByRole('button', { name: '返回工作台', exact: true }).click();
    await expect(page.locator('.message.assistant')).toHaveCount(1);
  });
  test('failed run retries without a duplicate input; committed warning cannot regenerate', async ({ page }) => {
    await create(page, '明确重试'); await send(page, '触发失败');
    await page.getByRole('button', { name: '重试', exact: true }).click();
    await expect(page.locator('.message.assistant')).toHaveCount(1);
    await expect(page.locator('.message.user')).toHaveCount(1);
    await expect(page.locator('.run-status')).toContainText('本轮已完成');
    await send(page, '后处理异常');
    await expect(page.locator('.run-status')).toContainText('后续处理存在异常');
    await expect(page.locator('.message.assistant')).toHaveCount(2);
    await expect(page.getByRole('button', { name: '重试', exact: true })).toHaveCount(0);
    await page.reload(); await expect(page.locator('.run-status')).toContainText('后续处理存在异常');
  });
  test('history pagination preserves its visible anchor and real state has no demo examples', async ({ page }) => {
    await page.locator('.session').filter({ hasText: '历史分页' }).click();
    const viewport = page.getByRole('region', { name: '对话消息，可滚动阅读' });
    await expect(viewport.locator('.message')).toHaveCount(30);
    await viewport.evaluate((element) => { element.scrollTop = 0; });
    await page.waitForTimeout(150);
    const before = await viewport.evaluate((element) => { const first = element.querySelector<HTMLElement>('[data-message-id]')!; return { id: first.dataset.messageId, top: first.getBoundingClientRect().top - element.getBoundingClientRect().top }; });
    await page.getByRole('button', { name: '加载更早消息', exact: true }).click();
    await expect(viewport.locator('.message')).toHaveCount(60);
    const after = await viewport.evaluate((element, id) => { const anchor = [...element.querySelectorAll<HTMLElement>('[data-message-id]')].find((message) => message.dataset.messageId === id)!; return anchor.getBoundingClientRect().top - element.getBoundingClientRect().top; }, before.id);
    expect(Math.abs(after - before.top)).toBeLessThan(4);
    await expect(page.locator('.inspector')).not.toContainText('雨后，微凉');
    await expect(page.locator('.inspector')).toContainText('未记录');
  });
  test('queries original memories with cursor pages and role-scoped detail', async ({ page }) => {
    await page.getByRole('button', { name: '记忆', exact: true }).first().click();
    await expect(page.locator('.memory-row')).toHaveCount(20);
    await page.getByRole('button', { name: '下一页', exact: true }).click();
    await expect(page.locator('.memory-row')).toHaveCount(5);
    await page.locator('.memory-row').first().click();
    await expect(page.locator('.memory-original')).toContainText('完整缩进原文');
    await page.getByRole('button', { name: '关闭', exact: true }).click();
    await page.getByLabel('记忆所属角色').selectOption('Beta');
    await page.getByLabel('搜索记忆原文').fill('Beta 原始记忆 24');
    await expect(page.locator('.memory-row')).toHaveCount(1);
    await expect(page.locator('.memory-row')).toContainText('Beta');
    await expect(page.locator('.memory-list')).not.toContainText('Alpha');
  });
  test('profile conflict retains the draft and model settings save and apply independently', async ({ page }) => {
    await page.getByRole('button', { name: '角色', exact: true }).click();
    await page.getByRole('button', { name: '编辑档案', exact: true }).click();
    await page.getByLabel('角色档案草稿').fill('新档案\n\n  缩进');
    const current = await (await page.request.get(`${base}/api/characters/Alpha`)).json();
    expect((await page.request.put(`${base}/api/characters/Alpha/profiles/zh`, { headers: clientHeaders, data: { expected_version: current.version, text: '外部修改' } })).ok()).toBe(true);
    await page.getByRole('button', { name: '写回角色档案', exact: true }).click();
    await expect(page.getByRole('alert')).toContainText('档案已变化');
    await expect(page.getByLabel('角色档案草稿')).toHaveValue('新档案\n\n  缩进');
    await page.getByRole('button', { name: '读取最新版本', exact: true }).click();
    await expect(page.getByText('外部修改', { exact: true })).toBeVisible();
    await page.getByRole('button', { name: '写回角色档案', exact: true }).click();
    await expect(page.getByText('档案已写回，下一个任务使用新版本。')).toBeVisible();
    expect((await (await page.request.get(`${base}/api/characters/Alpha`)).json()).profiles.zh).toBe('新档案\n\n  缩进');
    await services(page);
    await page.getByLabel('角色回复模型', { exact: true }).fill('changed-model');
    await page.getByRole('button', { name: '保存模型设置', exact: true }).click();
    const saved = await (await page.request.get(`${base}/api/settings/models`)).json();
    expect(saved.pending_changes).toBe(true);
    await page.getByRole('button', { name: '应用已保存设置', exact: true }).click();
    await expect(page.getByText('已应用，下一个任务使用新设置。')).toBeVisible();
    expect((await (await page.request.get(`${base}/api/settings/models`)).json()).pending_changes).toBe(false);
  });
  test('generates and loads registered audio and keeps demo conversations isolated', async ({ page }) => {
    await create(page, '语音资源'); await send(page, '你好。');
    await page.getByRole('button', { name: '生成语音', exact: true }).click();
    const audio = page.getByLabel('角色回复语音'); await expect(audio).toBeVisible();
    await audio.evaluate((element: HTMLAudioElement) => element.load());
    await expect.poll(() => audio.evaluate((element: HTMLAudioElement) => element.readyState)).toBeGreaterThanOrEqual(1);
    const source = await audio.getAttribute('src'); expect(source).toContain(base + '/api/audio/resources/');
    await services(page); await page.getByLabel('数据来源').selectOption('demo');
    await page.getByRole('button', { name: '切换到演示', exact: true }).click();
    await expect(page.locator('.environment-label')).toContainText('演示模式');
    await expect(page.getByRole('button', { name: /雨后的傍晚/ })).toBeVisible();
    await expect(page.getByRole('button', { name: /语音资源/ })).toHaveCount(0);
  });
});
test('live configured model completes a real frontend conversation', async ({ page }) => {
  test.skip(!live, 'Explicit live model integration run'); test.setTimeout(240_000);
  await create(page, '真实模型联调');
  await send(page, '你好，请用一句简短中文向我打招呼。');
  await expect(page.locator('.message.assistant')).toHaveCount(1, { timeout: 210_000 });
  await expect(page.locator('.run-status')).toContainText('本轮已完成', { timeout: 30_000 });
  expect((await page.locator('.message.assistant .message-text').textContent())?.trim().length).toBeGreaterThan(0);
  await page.reload(); await expect(page.locator('.message.assistant')).toHaveCount(1);
});
