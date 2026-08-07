import { mount, flushPromises } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import LogsView from './LogsView.vue'

vi.mock('../api/client', () => ({
  api: {
    logs: vi.fn(async () => ({
      items: [
        {
          id: '1',
          ts: '2024-01-01T00:00:00.000Z',
          level: 'INFO',
          logger: 'bootstrap',
          message: 'iJA 服务启动',
          extra: { session_id: '-', turn_id: '-' },
        },
        {
          id: '2',
          ts: '2024-01-01T00:00:01.000Z',
          level: 'WARNING',
          logger: 'application.tool_loop',
          message: '工具调用失败',
          extra: { tool_name: 'get_weather' },
        },
      ],
    })),
    modelAttempts: vi.fn(async () => ({
      items: [
        {
          id: 'attempt_1',
          invocation_id: 'call_1',
          attempt_number: 1,
          task: 'chat.reply',
          provider: 'openai_chat',
          profile: 'chat',
          model: 'chat-model',
          streamed: false,
          tool_call_count: 0,
          input_tokens: 12,
          output_tokens: 8,
          total_tokens: 20,
          usage_source: 'provider',
          latency_ms: 45,
          success: true,
          started_at: '2024-01-01T00:00:02.000Z',
          completed_at: '2024-01-01T00:00:02.045Z',
        },
      ],
    })),
    modelAttemptSummary: vi.fn(async () => ({
      attempt_count: 1,
      success_count: 1,
      error_count: 0,
      usage_unknown_count: 0,
      input_tokens: 12,
      output_tokens: 8,
      total_tokens: 20,
      average_latency_ms: 45,
      known_cost_microusd: 0,
      cost_unknown_count: 1,
    })),
  },
}))

describe('LogsView', () => {
  it('显示历史日志条目与级别标签', async () => {
    const wrapper = mount(LogsView)
    await flushPromises()
    expect(wrapper.text()).toContain('运行日志')
    expect(wrapper.text()).toContain('iJA 服务启动')
    expect(wrapper.text()).toContain('工具调用失败')
    expect(wrapper.text()).toContain('tool_name=get_weather')
    expect(wrapper.text()).toContain('模型调用概览')
    expect(wrapper.text()).toContain('chat.reply')
  })

  it('收到实时 log.appended 事件后追加显示', async () => {
    const wrapper = mount(LogsView)
    await flushPromises()
    window.dispatchEvent(
      new CustomEvent('ija:event', {
        detail: {
          type: 'log.appended',
          payload: {
            id: '3',
            ts: '2024-01-01T00:00:02.000Z',
            level: 'ERROR',
            logger: 'application.service',
            message: '模型生成失败',
          },
        },
      }),
    )
    await flushPromises()
    expect(wrapper.text()).toContain('模型生成失败')
  })
})
