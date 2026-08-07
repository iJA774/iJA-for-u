import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { SNAPSHOT_EVENT_NAME } from '../realtime/eventSocket'
import LearningResourcesView from './LearningResourcesView.vue'

vi.mock('../api/client', () => ({
  api: {
    sessions: vi.fn(async () => [{
      id: 'session-group',
      platform: 'web-simulator',
      account_id: 'ija-local',
      external_chat_id: 'group',
      chat_type: 'group',
      display_name: '研发群',
      participants: [],
      revision: 1,
      created_at: '2026-07-29T00:00:00Z',
      updated_at: '2026-07-29T00:00:00Z',
    }]),
    learnedJargons: vi.fn(async () => [{
      id: 'jargon-1',
      session_id: 'session-group',
      term: '先对齐',
      meaning: '先确认目标和边界',
      status: 'active',
      confidence: 0.91,
      occurrence_count: 4,
      inference_count: 2,
      evidence_message_ids: ['message-1'],
      last_seen_at: '2026-07-29T00:00:00Z',
      updated_at: '2026-07-29T00:00:00Z',
    }]),
    learnedExpressions: vi.fn(async () => [{
      id: 'expression-1',
      session_id: 'session-group',
      situation: '确认收到任务',
      style: '短句回应，结尾带“收到🫡”',
      status: 'active',
      confidence: 0.88,
      occurrence_count: 3,
      selection_count: 1,
      evidence_message_ids: ['message-2'],
      last_reinforced_at: '2026-07-29T00:00:00Z',
      updated_at: '2026-07-29T00:00:00Z',
    }]),
    learnedBehaviors: vi.fn(async () => [{
      id: 'behavior-1',
      session_id: 'session-group',
      scene_summary: '需求存在歧义',
      scene_tags: ['澄清'],
      need_tags: ['目标'],
      other_traits: [],
      action: '先复述目标，再问一个关键问题',
      expected_outcome: '降低返工',
      actor_type: 'group_collective',
      learning_type: 'observed_behavior',
      status: 'active',
      confidence: 0.86,
      occurrence_count: 3,
      activation_count: 2,
      success_count: 2,
      failure_count: 0,
      score: 0.82,
      evidence_message_ids: ['message-3'],
      last_reinforced_at: '2026-07-29T00:00:00Z',
      updated_at: '2026-07-29T00:00:00Z',
    }]),
    socialLearningRuns: vi.fn(async () => [{
      id: 'run-1',
      session_id: 'session-group',
      source_message_ids: ['message-1'],
      status: 'completed',
      produced_jargon_ids: ['jargon-1'],
      produced_expression_ids: ['expression-1'],
      produced_behavior_ids: ['behavior-1'],
      attempt_count: 1,
      created_at: '2026-07-29T00:00:00Z',
      updated_at: '2026-07-29T00:00:00Z',
    }]),
    socialLearningIndexes: vi.fn(async () => ({
      expression_clusters: [{
        profile_marker: 'default',
        model_name: 'embedding-test',
        index_fingerprint: 'fingerprint',
        cluster_id: 1,
        dimension: 8,
        member_count: 2,
        updated_at: '2026-07-29T00:00:00Z',
      }],
      behavior_tag_aliases: [{
        kind: 'scene',
        tag: '澄清',
        cluster_key: 'clarify',
        source_count: 2,
      }],
      behavior_scene_clusters: [],
    })),
  },
}))

describe('LearningResourcesView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('只读展示黑话、表达、行为、运行和派生索引', async () => {
    const wrapper = mount(LearningResourcesView)
    await flushPromises()

    expect(wrapper.text()).toContain('学习资源')
    expect(wrapper.text()).toContain('先对齐')
    expect(wrapper.text()).toContain('收到🫡')
    expect(wrapper.text()).toContain('先复述目标，再问一个关键问题')
    expect(wrapper.text()).toContain('产出 3 项')
    expect(wrapper.text()).toContain('表达向量簇')
    expect(wrapper.text()).toContain('不会读取或展示原始 embedding 向量')
    expect(wrapper.find('form').exists()).toBe(false)

    wrapper.unmount()
  })

  it('收到重连快照事件后重新拉取 REST 数据', async () => {
    const wrapper = mount(LearningResourcesView)
    await flushPromises()
    expect(api.learnedJargons).toHaveBeenCalledTimes(1)

    window.dispatchEvent(new CustomEvent(SNAPSHOT_EVENT_NAME))
    await flushPromises()
    expect(api.learnedJargons).toHaveBeenCalledTimes(2)
    expect(api.socialLearningIndexes).toHaveBeenCalledTimes(2)

    wrapper.unmount()
  })
})
