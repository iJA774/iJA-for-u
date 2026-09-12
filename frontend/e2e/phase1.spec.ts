import { expect, test, type Page } from '@playwright/test'
import { execFileSync } from 'node:child_process'
import path from 'node:path'

async function createSession(page: Page, type: 'private' | 'group', name: string, members: string) {
  await page.goto('/')
  await page.getByRole('button', { name: '新建会话' }).click()
  await page.getByLabel('类型').selectOption(type)
  await page.getByLabel('名称').fill(name)
  await page.getByLabel(/成员/).fill(members)
  await page.getByRole('button', { name: '创建', exact: true }).click()
  await expect(page.locator('.conversation header').getByText(name, { exact: true })).toBeVisible()
}

async function sendText(page: Page, text: string) {
  const composer = page.getByPlaceholder('Enter 发送，Shift+Enter 换行；点击历史消息可引用')
  await composer.fill(text)
  await composer.press('Enter')
}

test('私聊默认回复', async ({ page }) => {
  await createSession(page, 'private', 'E2E 私聊', 'u-private:小明')
  await sendText(page, '你好，小佳')
  await expect(page.getByText(/我听到了：你好，小佳/)).toBeVisible({ timeout: 6_000 })
  const userBubble = page.locator('.bubble-row.user')
  const agentBubble = page.locator('.bubble-row.assistant')
  await expect(userBubble).toHaveCount(1)
  await expect(agentBubble).toHaveCount(1)
  expect((await userBubble.boundingBox())?.x).toBeGreaterThan((await agentBubble.boundingBox())?.x ?? 0)
})

test('Shift+Enter 换行，Enter 发送', async ({ page }) => {
  await createSession(page, 'private', 'E2E 输入快捷键', 'u-shortcut:小明')
  const composer = page.getByPlaceholder('Enter 发送，Shift+Enter 换行；点击历史消息可引用')
  await composer.fill('第一行')
  await composer.press('Shift+Enter')
  await composer.type('第二行')
  await expect(composer).toHaveValue('第一行\n第二行')
  await expect(page.locator('.bubble-row.user')).toHaveCount(0)
  await composer.press('Enter')
  await expect(page.getByText(/我听到了：第一行/)).toBeVisible({ timeout: 6_000 })
})

test('群聊普通短反应合法沉默', async ({ page }) => {
  await createSession(page, 'group', 'E2E 沉默群', 'u-one:小明,u-two:小红')
  await sendText(page, '哈哈')
  await expect(page.locator('.decision-panel').getByText('沉默', { exact: true })).toBeVisible({
    timeout: 6_000,
  })
  await expect(page.locator('.conversation').getByText('我听到了')).toHaveCount(0)
})

test('群聊 @Agent 强制回复', async ({ page }) => {
  await createSession(page, 'group', 'E2E @群', 'u-at:小明,u-two:小红')
  await page.getByLabel('@小佳').check()
  await sendText(page, '你怎么看？')
  await expect(page.locator('.decision-panel').getByText('回复', { exact: true })).toBeVisible({
    timeout: 6_000,
  })
  await expect(page.getByText(/我听到了/)).toBeVisible({ timeout: 6_000 })
})

test('长期记忆展示可见域、来源链路和证据消息', async ({ page }) => {
  await createSession(page, 'private', 'E2E 画像私聊', 'u-profile:小青')
  await sendText(page, '我喜欢爵士乐。')
  await expect(page.getByText(/我听到了/)).toBeVisible({ timeout: 6_000 })
  await page.getByRole('link', { name: '长期记忆' }).click()
  await expect(page.getByText('喜欢爵士乐', { exact: true })).toBeVisible({ timeout: 6_000 })
  await expect(page.getByText(/private:/)).toBeVisible()
  await expect(page.getByText(/来源链路：reactive/)).toBeVisible()
  await expect(page.getByText(/证据 msg_/)).toBeVisible()
})

test('自然语言创建周期任务并立即执行', async ({ page }) => {
  await createSession(page, 'private', 'E2E 周期私聊', 'u-schedule:小青')
  await sendText(page, '每隔5分钟提醒我现在几点')
  await expect(page.getByText(/已创建/)).toBeVisible({ timeout: 6_000 })
  await page.getByRole('link', { name: '周期任务' }).click()
  const card = page.locator('.schedule-card').filter({ hasText: '每隔5分钟提醒我现在几点' })
  await expect(card).toBeVisible()
  await card.getByRole('button', { name: '立即执行' }).click()
  await expect(page.getByText('已提交立即执行')).toBeVisible()
  await page.getByRole('link', { name: '会话模拟' }).click()
  await expect(page.getByText(/Asia\/Shanghai 当前时间/)).toBeVisible({ timeout: 6_000 })
})

test('添加 Feed 后候选经 Proactive 送达并标记来源', async ({ page }) => {
  await createSession(page, 'private', 'E2E 主动私聊', 'u-proactive:小青')
  await page.getByRole('link', { name: '链路管理' }).click()
  await expect(page.getByRole('heading', { name: '链路管理' })).toBeVisible()
  await page.locator('.feed-create input').first().fill('https://1.1.1.1/feed.xml')
  await page.getByRole('button', { name: '添加公开订阅' }).click()
  await expect(page.getByText(/订阅已添加/)).toBeVisible()
  const feedRow = page.locator('.feed-row').filter({ hasText: '1.1.1.1' })
  await expect(feedRow).toBeVisible()
  await feedRow.getByRole('button', { name: '立即抓取' }).click()
  await expect(page.getByText('E2E 主动候选')).toBeVisible({ timeout: 8_000 })
  const candidate = page.locator('.audit-row').filter({ hasText: 'E2E 主动候选' })
  if (!(await candidate.getByText('已发送', { exact: true }).isVisible())) {
    await page.getByRole('button', { name: '立即运行 Proactive' }).click()
  }
  await expect(candidate.getByText('已发送', { exact: true })).toBeVisible({ timeout: 8_000 })

  await page.getByRole('link', { name: '会话模拟' }).click()
  const message = page.locator('.bubble-row.assistant').filter({
    hasText: '看到一条可能和你有关的内容：E2E 主动候选',
  })
  await expect(message).toBeVisible()
  await expect(message.getByText('主动触达', { exact: true })).toBeVisible()
})

test('低价值候选经 Drift 聚合后重新通过 Proactive 送达', async ({ page }) => {
  await createSession(page, 'private', 'E2E Drift 私聊', 'u-drift:小青')
  await page.getByRole('link', { name: '链路管理' }).click()
  await page.getByLabel('允许 Drift 产生内部候选').check()
  await page.getByRole('button', { name: '保存策略' }).click()
  await expect(page.getByText('主动策略已保存')).toBeVisible()

  await page.locator('.feed-create input').first().fill('https://1.1.1.2/drift-feed.xml')
  await page.getByPlaceholder('显示名称（可选）').fill('E2E Drift Feed')
  await page.getByRole('button', { name: '添加公开订阅' }).click()
  const feedRow = page.locator('.feed-row').filter({ hasText: 'E2E Drift Feed' })
  await feedRow.getByRole('button', { name: '立即抓取' }).click()
  await expect(page.getByText('E2E Drift 原料 1')).toBeVisible({ timeout: 8_000 })

  const rawCandidate = page.locator('.audit-row').filter({ hasText: 'E2E Drift 原料 1' })
  if (!(await rawCandidate.getByText('已跳过', { exact: true }).isVisible())) {
    await page.getByRole('button', { name: '立即运行 Proactive' }).click()
  }
  await expect(rawCandidate.getByText('已跳过', { exact: true })).toBeVisible({ timeout: 8_000 })

  if (!(await page.getByText('近期订阅内容小结', { exact: true }).isVisible())) {
    await page.getByRole('button', { name: '立即运行 Drift' }).click()
  }
  const aggregate = page.locator('.audit-row').filter({ hasText: '近期订阅内容小结' })
  await expect(aggregate).toBeVisible({ timeout: 8_000 })
  await page.getByRole('button', { name: '立即运行 Proactive' }).click()
  await expect(aggregate.getByText('已发送', { exact: true })).toBeVisible({ timeout: 8_000 })
  await expect(page.locator('.run-row').filter({ hasText: 'candidate_aggregation' })).toBeVisible()

  await page.getByRole('link', { name: '会话模拟' }).click()
  const message = page.locator('.bubble-row.assistant').filter({
    hasText: '看到一条可能和你有关的内容：近期订阅内容小结',
  })
  await expect(message).toBeVisible()
  await expect(message.getByText('主动触达', { exact: true })).toBeVisible()
})

test('表情首次生成后按名称复用且聊天渲染真实图片', async ({ page }) => {
  await page.goto('/persona')
  const portrait = execFileSync(
    path.resolve('../.venv/Scripts/python.exe'),
    [
      '-c',
      "import sys; from io import BytesIO; from PIL import Image; output=BytesIO(); Image.new('RGB',(90,160),'#5379aa').save(output,format='PNG'); sys.stdout.buffer.write(output.getvalue())",
    ],
  )
  await page.locator('input[type="file"]').setInputFiles({
    name: 'base.png',
    mimeType: 'image/png',
    buffer: portrait,
  })
  await expect(page.getByText(/形象已更新/)).toBeVisible()

  await createSession(page, 'private', 'E2E 表情私聊', 'u-expression:小明')
  await sendText(page, '发个开心表情庆祝一下')
  await expect(page.locator('.message-image')).toHaveCount(1, { timeout: 6_000 })
  await sendText(page, '再发个开心表情')
  await expect(page.locator('.message-image')).toHaveCount(2, { timeout: 6_000 })

  const response = await page.request.get('/api/expressions')
  const expressions = await response.json()
  expect(expressions).toHaveLength(1)
  expect(expressions[0].name).toBe('开心挥手')
  expect(expressions[0].use_count).toBe(2)
})
