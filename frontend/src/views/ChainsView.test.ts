import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import ChainsView from './ChainsView.vue'

vi.mock('../api/client', () => ({
  ApiError: class ApiError extends Error {},
  api: {
    sessions: vi.fn(async () => [{
      id: 'session-private',
      platform: 'web-simulator',
      account_id: 'ija-local',
      external_chat_id: 'private',
      chat_type: 'private',
      display_name: '测试私聊',
      participants: [],
      revision: 1,
      created_at: '2026-07-23T00:00:00Z',
      updated_at: '2026-07-23T00:00:00Z',
    }]),
    engagementPolicy: vi.fn(async () => ({
      session_id: 'session-private',
      proactive_enabled: true,
      drift_enabled: true,
      timezone: 'Asia/Shanghai',
      quiet_start: '22:00',
      quiet_end: '08:00',
      minimum_interval_minutes: 240,
      updated_at: '2026-07-23T00:00:00Z',
    })),
    feeds: vi.fn(async () => []),
    proactiveCandidates: vi.fn(async () => [{
      id: 'candidate-1',
      session_id: 'session-private',
      source_kind: 'rss',
      title: '候选标题',
      summary: '候选摘要',
      url: 'https://example.com/1',
      source_refs: [],
      parent_candidate_ids: [],
      status: 'deferred',
      decision_reason: 'quiet_hours',
      attempt_count: 0,
      created_at: '2026-07-23T00:00:00Z',
      updated_at: '2026-07-23T00:00:00Z',
    }]),
    proactiveRuns: vi.fn(async () => [{
      id: 'run-1',
      session_id: 'session-private',
      status: 'gated',
      stage: 'finished',
      gate_reason: 'quiet_hours',
      decision_code: 'quiet_hours',
      decision_reason: 'quiet_hours',
      score: null,
      candidate_ids: ['candidate-1'],
      snapshot_at: '2026-07-23T00:00:00Z',
      manual_triggered: false,
      created_at: '2026-07-23T00:00:00Z',
      updated_at: '2026-07-23T00:00:00Z',
    }]),
    driftRuns: vi.fn(async () => []),
    saveEngagementPolicy: vi.fn(async (policy) => policy),
    createFeed: vi.fn(async () => ({})),
    saveFeed: vi.fn(),
    deleteFeed: vi.fn(),
    refreshFeed: vi.fn(),
    runProactive: vi.fn(),
    runDrift: vi.fn(),
  },
}))

describe('ChainsView', () => {
  it('显示策略、候选与明确门控原因', async () => {
    const wrapper = mount(ChainsView)
    await flushPromises()

    expect(wrapper.text()).toContain('链路管理')
    expect(wrapper.text()).toContain('测试私聊')
    expect(wrapper.text()).toContain('候选标题')
    expect(wrapper.text()).toContain('quiet_hours')
    expect(wrapper.text()).toContain('已推迟')
    expect(wrapper.text()).not.toContain('分数')
  })

  it('保存策略并提交公开 Feed', async () => {
    const wrapper = mount(ChainsView)
    await flushPromises()

    await wrapper.get('.chain-policy .primary').trigger('click')
    await flushPromises()
    expect(api.saveEngagementPolicy).toHaveBeenCalled()

    const urlInput = wrapper.get<HTMLInputElement>('.feed-create input')
    await urlInput.setValue('https://example.com/feed.xml')
    await wrapper.get('.feed-create .primary').trigger('click')
    await flushPromises()
    expect(api.createFeed).toHaveBeenCalledWith('session-private', {
      url: 'https://example.com/feed.xml',
      title: '',
      poll_interval_minutes: 30,
    })
  })
})
