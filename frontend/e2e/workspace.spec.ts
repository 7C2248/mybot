import { expect, test, type Page } from '@playwright/test';

test.beforeEach(async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('button', { name: '简化模式', exact: true })).toBeVisible();
});
async function enterCompact(page: Page) {
  await page.getByRole('button', { name: '简化模式', exact: true }).click();
  await expect(page.locator('.compact-group')).toHaveClass(/collapsed/);
  await page.getByLabel('消息输入', { exact: true }).click();
  await expect(page.locator('.compact-group')).toHaveClass(/expanded/);
}
async function drag(page: Page, direction: string, dx: number, dy: number) {
  const handle = await page.locator(`[data-direction="${direction}"]`).boundingBox();
  if (!handle) throw new Error(`missing ${direction} handle`);
  const x = handle.x + handle.width / 2, y = handle.y + handle.height / 2;
  await page.mouse.move(x, y); await page.mouse.down(); await page.mouse.move(x + dx, y + dy, { steps: 10 }); await page.mouse.up();
}
test('creates a role-bound conversation and restores multiline input and preferences after reload', async ({ page }) => {
  await page.getByRole('button', { name: '新建对话', exact: true }).click();
  await expect(page.getByLabel('新会话角色')).toContainText('XiaoCe');
  await page.getByLabel('新会话角色').selectOption('XiaoCe'); await page.getByLabel('会话标题', { exact: true }).fill('窗口旁的对话');
  await page.getByRole('button', { name: '开始对话', exact: true }).click();
  await page.getByLabel('消息输入', { exact: true }).fill('第一行\n\n  缩进的第二行');
  await page.reload();
  await expect(page.getByRole('heading', { name: '窗口旁的对话' })).toBeVisible();
  await expect(page.getByLabel('消息输入', { exact: true })).toHaveValue('第一行\n\n  缩进的第二行');
  await page.getByLabel('消息输入', { exact: true }).press('Enter');
  await expect(page.locator('.message.user .message-text')).toHaveText('第一行\n\n  缩进的第二行');
  await expect(page.locator('.message.assistant')).toHaveCount(1);
  await page.getByRole('button', { name: '设置', exact: true }).click();
  await page.getByLabel('主题', { exact: true }).selectOption('dark'); await page.reload();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
});
test('IME enter does not send; Shift+Enter retains newlines across both layouts', async ({ page }) => {
  const input = page.getByLabel('消息输入', { exact: true });
  await input.fill('中文输入');
  await input.dispatchEvent('keydown', { key: 'Enter', code: 'Enter', isComposing: true, keyCode: 229 });
  await expect(page.locator('.message.user')).toHaveCount(2);
  await input.press('End'); await input.press('Shift+Enter'); await input.press('KeyA');
  const draft = await input.inputValue(); expect(draft).toContain('\n');
  await enterCompact(page); await expect(input).toHaveValue(draft);
  await page.getByRole('button', { name: '返回工作台' }).click(); await expect(input).toHaveValue(draft);
});
test('background reply stays hidden and internal controls do not collapse history', async ({ page }) => {
  await enterCompact(page);
  await page.getByLabel('消息输入', { exact: true }).fill('请继续\n\n保留换行');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await expect(page.locator('.compact-group')).toHaveClass(/expanded/);
  await page.mouse.click(30, 150);
  await expect(page.locator('.compact-history')).toBeHidden();
  await expect(page.locator('.unread-label')).toHaveText('有新回复');
  await expect(page.locator('.compact-history')).toBeHidden();
  await page.getByLabel('消息输入', { exact: true }).click();
  await expect(page.locator('.compact-history')).toBeVisible();
  await expect(page.locator('.message.assistant')).toHaveCount(3);
  await expect(page.locator('.unread-label')).toHaveCount(0);
});
for (const placement of ['bubble', 'inline']) {
  test(`resizes ${placement} on edges/corners, preserves reading and enforces minimums`, async ({ page }) => {
    if (placement === 'inline') {
      await page.getByRole('button', { name: '设置', exact: true }).click();
      await page.getByRole('button', { name: '简化模式', exact: true }).click();
      await page.getByLabel('简化模式回复位置').selectOption('inline');
      await page.getByRole('button', { name: '进入简化模式' }).click();
      await page.getByLabel('消息输入', { exact: true }).click();
    } else await enterCompact(page);
    const box = () => page.locator('.compact-group').boundingBox();
    const near = (a: number, b: number) => expect(Math.abs(a - b)).toBeLessThan(2);
    for (const [direction, dx, dy] of [['e', 45, 0], ['w', 25, 0], ['n', 0, -40], ['s', 0, -25], ['ne', 15, -15], ['nw', -15, -15], ['se', -15, -15], ['sw', 15, -15]] as const) {
      const a = (await box())!; await drag(page, direction, dx, dy); const b = (await box())!;
      near(b.width, a.width + (direction.includes('e') ? dx : direction.includes('w') ? -dx : 0));
      near(b.height, a.height + (direction.includes('s') ? dy : direction.includes('n') ? -dy : 0));
      if (direction.includes('n')) near(b.y + b.height, a.y + a.height);
      if (direction.includes('w')) near(b.x + b.width, a.x + a.width);
    }
    const history = page.locator('.message-list'); await history.evaluate((element) => { element.scrollTop = 40; });
    await expect(page.getByRole('button', { name: '回到最新', exact: true })).toBeVisible();
    await drag(page, 'n', 0, -20); near(await history.evaluate((element) => element.scrollTop), 40);
    await drag(page, 'n', 0, 1200); near((await history.boundingBox())!.height, 72);
    await expect(page.locator('.compact-group')).toHaveClass(/expanded/);
    await page.getByLabel('消息输入', { exact: true }).press('Escape');
    const a = (await box())!; await drag(page, 'e', 20, 0); near((await box())!.width, a.width + 20);
    await expect(page.locator('.compact-group')).toHaveClass(/collapsed/);
    await page.getByLabel('消息输入', { exact: true }).click();
    await drag(page, 'e', -1400, 0); near((await box())!.width, 320);
    await page.getByRole('button', { name: '返回工作台' }).click();
    await page.getByRole('button', { name: '设置', exact: true }).click(); await page.getByRole('button', { name: '简化模式', exact: true }).click();
    await expect(page.getByRole('slider')).toHaveCount(0);
    await expect(page.locator('output')).toContainText('320px');
  });
}

for (const placement of ['bubble', 'inline']) {
  test(`${placement} opens only from the input, never from dragging or returning`, async ({ page }) => {
    if (placement === 'inline') {
      await page.getByRole('button', { name: '设置', exact: true }).click();
      await page.getByRole('button', { name: '简化模式', exact: true }).click();
      await page.getByLabel('简化模式回复位置').selectOption('inline');
      await page.getByRole('button', { name: '进入简化模式' }).click();
    } else await page.getByRole('button', { name: '简化模式', exact: true }).click();
    const history = page.locator('.compact-history');
    await expect(history).toBeHidden();
    await page.getByRole('button', { name: '拖动输入条', exact: true }).focus();
    await expect(history).toBeHidden();
    const handle = (await page.locator('.move-handle').boundingBox())!;
    const before = (await page.locator('.compact-group').boundingBox())!;
    await page.mouse.move(handle.x + handle.width / 2, handle.y + handle.height / 2);
    await page.mouse.down(); await page.mouse.move(handle.x + handle.width / 2 + 60, handle.y + handle.height / 2 - 40, { steps: 10 });
    await expect(history).toBeHidden(); await page.mouse.up();
    const after = (await page.locator('.compact-group').boundingBox())!;
    expect(Math.abs(after.x - before.x - 60)).toBeLessThan(2);
    expect(after.height).toBe(before.height);
    await page.locator('.compact-composer .avatar').click();
    await expect(history).toBeHidden();
    const inputBottom = await page.locator('.compact-composer').evaluate((element) => element.getBoundingClientRect().bottom);
    await page.getByLabel('消息输入', { exact: true }).click();
    await expect(history).toBeVisible();
    expect(Math.abs(await page.locator('.compact-composer').evaluate((element) => element.getBoundingClientRect().bottom) - inputBottom)).toBeLessThan(2);
    await page.evaluate(() => window.dispatchEvent(new Event('blur')));
    await expect(history).toBeHidden();
    expect(Math.abs(await page.locator('.compact-composer').evaluate((element) => element.getBoundingClientRect().bottom) - inputBottom)).toBeLessThan(2);
    await expect(page.getByLabel('消息输入', { exact: true })).not.toBeFocused();
    await page.getByRole('button', { name: '返回工作台', exact: true }).focus();
    await expect(history).toBeHidden();
    const back = (await page.getByRole('button', { name: '返回工作台', exact: true }).boundingBox())!;
    await page.mouse.move(back.x + back.width / 2, back.y + back.height / 2);
    await page.mouse.down(); await expect(history).toBeHidden(); await page.mouse.up();
    await expect(page.getByRole('heading', { name: '雨后的傍晚' })).toBeVisible();
  });
}
test('browses raw memories and selects a real sprite', async ({ page }) => {
  await page.getByRole('button', { name: '记忆', exact: true }).first().click();
  await page.getByLabel('搜索记忆原文').fill('没有读完');
  await page.getByRole('button', { name: /记忆 #23/ }).click();
  await expect(page.locator('.memory-original')).toHaveText('我们约好下次继续看那本没有读完的书。\n\n书名还没有记录下来。');
  await page.getByRole('button', { name: '关闭', exact: true }).click();
  await page.getByRole('button', { name: '设置', exact: true }).click(); await page.getByRole('button', { name: '立绘显示', exact: true }).click();
  await page.getByLabel('立绘图片').selectOption({ label: 'Suli_校服.png' });
  await page.getByLabel('在对话工作台显示').check();
  await expect(page.getByAltText('当前立绘预览')).toBeVisible();
  await page.getByRole('button', { name: '对话', exact: true }).click();
  await expect(page.locator('.sprite-stage img')).toBeVisible();
  expect(await page.locator('.sprite-stage img').evaluate((image) => (image as HTMLImageElement).naturalWidth)).toBeGreaterThan(0);
});
test('narrow viewport keeps navigation, input and minimum-size compact window usable', async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 780 });
  await expect(page.getByLabel('消息输入', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '会话导航', exact: true }).click();
  await expect(page.getByRole('button', { name: '新建对话', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '对话', exact: true }).click();
  await enterCompact(page); await drag(page, 'n', 0, 1200);
  await page.setViewportSize({ width: 320, height: 780 });
  await expect.poll(async () => { const box = (await page.locator('.compact-group').boundingBox())!; return box.x >= 0 && box.x + box.width <= 320; }).toBe(true);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(320);
});
