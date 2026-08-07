import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import BlacklistView from './BlacklistView.vue'

const mocks = vi.hoisted(() => ({
  blacklist: vi.fn(),
  createBlacklist: vi.fn(),
  deleteBlacklist: vi.fn(),
}))

vi.mock('../api/client', () => ({
  ApiError: class ApiError extends Error {},
  api: mocks,
}))

const entry = {
  id: 'blacklist-1',
  platform: 'onebot',
  account_id: 'bot-1',
  external_user_id: 'user-1',
  display_name: '测试用户',
  reason: '重复骚扰',
  source: 'manual' as const,
  session_id: 'session-1',
  created_at: '2026-07-29T12:00:00Z',
}

describe('BlacklistView', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    mocks.blacklist.mockReset().mockResolvedValue([entry])
    mocks.createBlacklist.mockReset().mockResolvedValue(entry)
    mocks.deleteBlacklist.mockReset().mockResolvedValue({})
  })

  it('展示隔离条目并提交经过裁剪的手动拉黑参数', async () => {
    const wrapper = mount(BlacklistView)
    await flushPromises()

    expect(wrapper.text()).toContain('测试用户')
    expect(wrapper.text()).toContain('重复骚扰')
    expect(wrapper.text()).toContain('来源会话：session-1')

    await wrapper.get('input[placeholder="平台"]').setValue(' onebot ')
    await wrapper.get('input[placeholder="机器人账号"]').setValue(' bot-2 ')
    await wrapper.get('input[placeholder="用户外部 ID"]').setValue(' user-2 ')
    await wrapper.get('input[placeholder="展示名"]').setValue(' 新用户 ')
    await wrapper.get('input[placeholder="拉黑原因"]').setValue(' 主动测试 ')
    await wrapper.get('.blacklist-create button').trigger('click')
    await flushPromises()

    expect(mocks.createBlacklist).toHaveBeenCalledWith({
      platform: 'onebot',
      account_id: 'bot-2',
      external_user_id: 'user-2',
      display_name: '新用户',
      reason: '主动测试',
    })
    expect(wrapper.text()).toContain('已加入黑名单')
    expect(mocks.blacklist).toHaveBeenCalledTimes(2)
  })

  it('只有显式确认后才解除拉黑', async () => {
    const confirm = vi.spyOn(window, 'confirm')
    confirm.mockReturnValueOnce(false).mockReturnValueOnce(true)
    const wrapper = mount(BlacklistView)
    await flushPromises()
    const button = wrapper.get('button.danger-link')

    await button.trigger('click')
    expect(mocks.deleteBlacklist).not.toHaveBeenCalled()

    await button.trigger('click')
    await flushPromises()
    expect(mocks.deleteBlacklist).toHaveBeenCalledWith('blacklist-1')
    expect(wrapper.text()).toContain('已从黑名单移除')
  })

  it('加载失败时显示明确错误而不伪造空列表成功', async () => {
    mocks.blacklist.mockRejectedValueOnce(new Error('database unavailable'))
    const wrapper = mount(BlacklistView)
    await flushPromises()

    expect(wrapper.get('.error').text()).toBe('黑名单加载失败')
  })
})
