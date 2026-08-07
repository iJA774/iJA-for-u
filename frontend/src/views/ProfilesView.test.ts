import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import ProfilesView from './ProfilesView.vue'

vi.mock('../api/client', () => ({
  ApiError: class ApiError extends Error {},
  api: {
    sessions: vi.fn(async () => [{
      id: 'session-memory',
      platform: 'web-simulator',
      account_id: 'ija-local',
      external_chat_id: 'memory',
      chat_type: 'private',
      display_name: '记忆私聊',
      participants: [],
      revision: 1,
      created_at: '2026-07-24T00:00:00Z',
      updated_at: '2026-07-24T00:00:00Z',
    }]),
    memories: vi.fn(async () => [{
      id: 'memory-1',
      session_id: 'session-memory',
      scope_key: 'private:session-memory',
      subject_id: 'u1',
      kind: 'preference',
      content: '喜欢爵士乐',
      confidence: 0.95,
      importance: 0.8,
      status: 'active',
      source_chain: 'reactive',
      source_message_ids: ['msg-1'],
      source_refs: [],
      reinforcement: 2,
      recall_count: 1,
      created_at: '2026-07-24T00:00:00Z',
      updated_at: '2026-07-24T00:00:00Z',
    }]),
    memoryConsolidations: vi.fn(async () => []),
    recallMemories: vi.fn(async () => []),
    createMemory: vi.fn(async () => ({})),
    forgetMemory: vi.fn(async () => ({})),
    correctMemory: vi.fn(async () => ({})),
    clearSessionMemories: vi.fn(async () => ({})),
    clearAllMemories: vi.fn(async () => ({})),
    retryMemoryConsolidation: vi.fn(async () => ({})),
  },
}))

describe('ProfilesView', () => {
  it('显示分域长期记忆与来源链路', async () => {
    const wrapper = mount(ProfilesView)
    await flushPromises()

    expect(wrapper.text()).toContain('长期记忆')
    expect(wrapper.text()).toContain('喜欢爵士乐')
    expect(wrapper.text()).toContain('private:session-memory')
    expect(wrapper.text()).toContain('来源链路：reactive')
  })

  it('可以手动保存当前 Session 记忆', async () => {
    const wrapper = mount(ProfilesView)
    await flushPromises()
    const input = wrapper.get<HTMLInputElement>('input[placeholder="明确写入一条当前会话记忆"]')
    await input.setValue('周末整理相册')
    await wrapper.get('button.primary').trigger('click')
    await flushPromises()

    expect(api.createMemory).toHaveBeenCalledWith('session-memory', {
      content: '周末整理相册',
      kind: 'event',
      importance: 0.8,
    })
  })

  it('可以清空当前聊天或所有聊天的记忆', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const wrapper = mount(ProfilesView)
    await flushPromises()

    const buttons = wrapper.findAll('button.danger-button')
    await buttons[1].trigger('click')
    await flushPromises()
    expect(api.clearSessionMemories).toHaveBeenCalledWith('session-memory')

    await buttons[0].trigger('click')
    await flushPromises()
    expect(api.clearAllMemories).toHaveBeenCalled()
  })
})
