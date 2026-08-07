import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import type { ChatMessage } from '../types'
import { useChatStore } from '../stores/chat'
import ChatView from './ChatView.vue'

describe('ChatView', () => {
  it('按组件顺序渲染图文，并在历史图片缺失时显示占位', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const chat = useChatStore()
    chat.sessions = [{
      id: 'session-1',
      platform: 'web-simulator',
      account_id: 'ija-local',
      external_chat_id: 'u1',
      chat_type: 'private',
      display_name: '图文会话',
      participants: [{ external_user_id: 'u1', display_name: '小明', role: 'owner' }],
      revision: 1,
      created_at: '2026-07-22T00:00:00Z',
      updated_at: '2026-07-22T00:00:00Z',
    }]
    chat.selectedId = 'session-1'
    chat.messages = [{
      id: 'msg-1',
      session_id: 'session-1',
      role: 'assistant',
      sender_id: 'agent',
      sender_name: '小佳',
      components: [
        { type: 'text', text: '一起开心！' },
        {
          type: 'image_ref',
          attachment_id: 'expression-1',
          filename: '开心挥手.png',
          mime_type: 'image/png',
          size: 100,
          sha256: '0'.repeat(64),
          storage_path: 'server-only',
          description: '开心挥手',
        },
        {
          type: 'audio_ref',
          attachment_id: 'audio-1',
          filename: '问候.amr',
          mime_type: 'audio/amr',
          size: 20,
          sha256: '1'.repeat(64),
          storage_path: 'server-only',
        },
        {
          type: 'file_ref',
          attachment_id: 'file-1',
          filename: '清单.pdf',
          mime_type: 'application/pdf',
          size: 20,
          sha256: '2'.repeat(64),
          storage_path: 'server-only',
        },
      ],
      created_at: '2026-07-22T00:00:00Z',
    }]
    const wrapper = mount(ChatView, { global: { plugins: [pinia] } })
    expect(wrapper.get('.component-text').text()).toBe('一起开心！')
    const image = wrapper.get<HTMLImageElement>('.message-image')
    expect(image.attributes('src')).toContain('/api/messages/msg-1/components/1/image')
    expect(wrapper.get('audio').attributes('src')).toContain('/api/messages/msg-1/components/2/attachment')
    expect(wrapper.get('.message-file').attributes('href')).toContain('/api/messages/msg-1/components/3/attachment')
    expect(wrapper.get('.message-file').text()).toContain('清单.pdf')
    await image.trigger('error')
    expect(wrapper.text()).toContain('图片文件缺失或已损坏')
  })

  it('点击删除按钮经确认后会删除聊天窗并清空当前选中', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const chat = useChatStore()
    const deleteSpy = vi.spyOn(api, 'deleteSession').mockResolvedValue({})
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    chat.sessions = [{
      id: 'session-del',
      platform: 'web-simulator',
      account_id: 'ija-local',
      external_chat_id: 'del',
      chat_type: 'private',
      display_name: '待删除',
      participants: [{ external_user_id: 'u1', display_name: '小明', role: 'owner' }],
      revision: 1,
      created_at: '2026-07-24T00:00:00Z',
      updated_at: '2026-07-24T00:00:00Z',
    }]
    chat.selectedId = 'session-del'
    const wrapper = mount(ChatView, { global: { plugins: [pinia] } })
    await wrapper.get('.session-remove').trigger('click')
    await flushPromises()
    expect(deleteSpy).toHaveBeenCalledWith('session-del')
    expect(chat.sessions).toHaveLength(0)
    expect(chat.selectedId).toBe('')
  })

  it('清空正文会明确提示保留长期知识并刷新当前会话', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const chat = useChatStore()
    const clearSpy = vi.spyOn(api, 'clearSessionChat').mockResolvedValue({})
    vi.spyOn(api, 'messages').mockResolvedValue([])
    vi.spyOn(api, 'decisions').mockResolvedValue([])
    vi.spyOn(api, 'toolExecutions').mockResolvedValue([])
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    chat.sessions = [{
      id: 'session-clear',
      platform: 'web-simulator',
      account_id: 'ija-local',
      external_chat_id: 'clear',
      chat_type: 'private',
      display_name: '只清正文',
      participants: [{ external_user_id: 'u1', display_name: '小明', role: 'owner' }],
      revision: 1,
      created_at: '2026-07-24T00:00:00Z',
      updated_at: '2026-07-24T00:00:00Z',
    }]
    chat.selectedId = 'session-clear'
    const wrapper = mount(ChatView, { global: { plugins: [pinia] } })

    await wrapper.get('.conversation header .danger-link').trigger('click')
    await flushPromises()

    expect(confirmSpy.mock.calls[0][0]).toContain('画像、长期记忆和已学表达摘要仍会保留')
    expect(confirmSpy.mock.calls[0][0]).toContain('彻底忘记')
    expect(clearSpy).toHaveBeenCalledWith('session-clear')
    expect(chat.messages).toEqual([])
  })

  it('流式增量只临时展示，并在投递失败时清除', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const chat = useChatStore()
    chat.sessions = [{
      id: 'session-stream',
      platform: 'web-simulator',
      account_id: 'ija-local',
      external_chat_id: 'stream',
      chat_type: 'private',
      display_name: '流式会话',
      participants: [{ external_user_id: 'u1', display_name: '小明', role: 'owner' }],
      revision: 1,
      created_at: '2026-07-22T00:00:00Z',
      updated_at: '2026-07-22T00:00:00Z',
    }]
    chat.selectedId = 'session-stream'
    const wrapper = mount(ChatView, { global: { plugins: [pinia] } })

    window.dispatchEvent(new CustomEvent('ija:event', {
      detail: { type: 'model.delta', payload: { session_id: 'session-stream', delta: '正在生成' } },
    }))
    await flushPromises()
    expect(wrapper.text()).toContain('正在生成')

    window.dispatchEvent(new CustomEvent('ija:event', {
      detail: { type: 'reply.delivery_failed', payload: { session_id: 'session-stream' } },
    }))
    await flushPromises()
    expect(wrapper.text()).not.toContain('正在生成')
  })

  it('模拟会话保持用户在右侧和可输入的既有行为', () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const chat = useChatStore()
    chat.sessions = [{
      id: 'session-simulator-layout',
      platform: 'web-simulator',
      account_id: 'ija-local',
      external_chat_id: 'sim-layout',
      chat_type: 'private',
      display_name: '模拟布局',
      participants: [{ external_user_id: 'u1', display_name: '小明', role: 'owner' }],
      revision: 1,
      created_at: '2026-08-01T00:00:00Z',
      updated_at: '2026-08-01T00:00:00Z',
    }]
    chat.selectedId = 'session-simulator-layout'
    chat.messages = [
      {
        id: 'sim-user', session_id: chat.selectedId, role: 'user', sender_id: 'u1', sender_name: '小明',
        components: [{ type: 'text', text: '你好' }], created_at: '2026-08-01T00:00:00Z',
      },
      {
        id: 'sim-agent', session_id: chat.selectedId, role: 'assistant', sender_id: 'agent', sender_name: '小佳',
        components: [{ type: 'text', text: '你好呀' }], created_at: '2026-08-01T00:00:01Z',
      },
    ]

    const wrapper = mount(ChatView, { global: { plugins: [pinia] } })

    expect(wrapper.get('.bubble-row.user').classes()).toContain('message-right')
    expect(wrapper.get('.bubble-row.assistant').classes()).toContain('message-left')
    expect(wrapper.find('textarea').exists()).toBe(true)
    expect(wrapper.text()).toContain('模拟 · 1 位成员')
  })

  it('真实对话反转消息方向、流式 agent 消息在右侧且只读', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const chat = useChatStore()
    chat.sessions = [{
      id: 'session-real-layout',
      platform: 'telegram',
      account_id: 'telegram-bot',
      external_chat_id: 'real-layout',
      chat_type: 'group',
      display_name: '真实群聊',
      participants: [{ external_user_id: 'u1', display_name: '小明', role: 'owner' }],
      revision: 1,
      created_at: '2026-08-01T00:00:00Z',
      updated_at: '2026-08-01T00:00:00Z',
    }]
    chat.selectedId = 'session-real-layout'
    chat.messages = [
      {
        id: 'real-user', session_id: chat.selectedId, role: 'user', sender_id: 'u1', sender_name: '小明',
        components: [{ type: 'text', text: '群成员消息' }], created_at: '2026-08-01T00:00:00Z',
      },
      {
        id: 'real-agent', session_id: chat.selectedId, role: 'assistant', sender_id: 'agent', sender_name: '小佳',
        components: [{ type: 'text', text: 'agent 消息' }], created_at: '2026-08-01T00:00:01Z',
      },
    ]
    const wrapper = mount(ChatView, { global: { plugins: [pinia] } })

    window.dispatchEvent(new CustomEvent('ija:event', {
      detail: { type: 'model.delta', payload: { session_id: chat.selectedId, delta: '流式回复' } },
    }))
    await flushPromises()

    expect(wrapper.get('.conversation').classes()).toContain('real-conversation')
    expect(wrapper.get('.bubble-row.user').classes()).toContain('message-left')
    expect(wrapper.get('.bubble-row.assistant').classes()).toContain('message-right')
    expect(wrapper.get('.bubble-row.streaming').classes()).toContain('message-right')
    expect(wrapper.find('textarea').exists()).toBe(false)
    expect(wrapper.findAll('button').some((button) => button.text() === '发送')).toBe(false)
    expect(wrapper.get('.readonly-notice').text()).toContain('真实对话，只读展示')
    expect(wrapper.text()).toContain('真实 · 1 位成员')
  })

  it('群聊参与策略可加载并用当前 revision 保存', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const chat = useChatStore()
    const policy = {
      session_id: 'session-group-policy',
      mode: 'normal' as const,
      trigger_count: 3,
      frequency_factor: 0.9,
      cooldown_seconds: 60,
      idle_streak: 2,
      last_ordinary_reply_at: null,
      last_external_message_at: null,
      external_interval_ewma_seconds: null,
      external_interval_sample_count: 0,
      revision: 3,
      state_version: 8,
      updated_at: '2026-07-29T00:00:00Z',
    }
    vi.spyOn(api, 'groupParticipationPolicy').mockResolvedValue(policy)
    const saveSpy = vi.spyOn(api, 'saveGroupParticipationPolicy').mockImplementation(async (payload) => ({
      ...payload,
      revision: payload.revision + 1,
    }))
    chat.sessions = [{
      id: policy.session_id,
      platform: 'web-simulator',
      account_id: 'ija-local',
      external_chat_id: 'group-policy',
      chat_type: 'group',
      display_name: '参与策略群',
      participants: [{ external_user_id: 'u1', display_name: '小明', role: 'owner' }],
      revision: 1,
      created_at: '2026-07-29T00:00:00Z',
      updated_at: '2026-07-29T00:00:00Z',
    }]
    chat.selectedId = policy.session_id

    const wrapper = mount(ChatView, { global: { plugins: [pinia] } })
    await flushPromises()
    expect(wrapper.get('.participation-editor').text()).toContain('连续 idle：2')
    expect(wrapper.get('.participation-editor').text()).toContain('待采样（0 个样本）')
    await wrapper.get<HTMLSelectElement>('.participation-editor select').setValue('focused')
    await wrapper.get('.participation-editor button').trigger('click')
    await flushPromises()

    expect(saveSpy).toHaveBeenCalledWith(expect.objectContaining({
      mode: 'focused',
      revision: 3,
    }))

    window.dispatchEvent(new CustomEvent('ija:event', {
      detail: {
        type: 'turn.decision',
        payload: { session_id: policy.session_id },
      },
    }))
    await flushPromises()
    expect(api.groupParticipationPolicy).toHaveBeenCalledTimes(2)
  })

  it('群聊实时刷新不会覆盖尚未保存的参与策略草稿', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const chat = useChatStore()
    const policy = {
      session_id: 'session-group-draft',
      mode: 'normal' as const,
      trigger_count: 3,
      frequency_factor: 0.9,
      cooldown_seconds: 60,
      idle_streak: 1,
      last_ordinary_reply_at: null,
      last_external_message_at: null,
      external_interval_ewma_seconds: null,
      external_interval_sample_count: 0,
      revision: 3,
      state_version: 8,
      updated_at: '2026-07-29T00:00:00Z',
    }
    vi.spyOn(api, 'groupParticipationPolicy')
      .mockResolvedValueOnce(policy)
      .mockResolvedValueOnce({
        ...policy,
        mode: 'silent',
        trigger_count: 100,
        frequency_factor: 0.5,
        cooldown_seconds: 5,
        idle_streak: 4,
        external_interval_ewma_seconds: 42,
        external_interval_sample_count: 3,
        revision: 4,
        state_version: 9,
      })
    const saveSpy = vi.spyOn(api, 'saveGroupParticipationPolicy').mockImplementation(async payload => ({
      ...payload,
      revision: payload.revision + 1,
    }))
    chat.sessions = [{
      id: policy.session_id,
      platform: 'web-simulator',
      account_id: 'ija-local',
      external_chat_id: 'group-draft',
      chat_type: 'group',
      display_name: '草稿保护群',
      participants: [{ external_user_id: 'u1', display_name: '小明', role: 'owner' }],
      revision: 1,
      created_at: '2026-07-29T00:00:00Z',
      updated_at: '2026-07-29T00:00:00Z',
    }]
    chat.selectedId = policy.session_id

    const wrapper = mount(ChatView, { global: { plugins: [pinia] } })
    await flushPromises()
    const editor = wrapper.get('.participation-editor')
    const inputs = editor.findAll<HTMLInputElement>('input[type="number"]')
    await editor.get<HTMLSelectElement>('select').setValue('focused')
    await inputs[0].setValue('4')
    await inputs[1].setValue('0.8')
    await inputs[2].setValue('120')

    window.dispatchEvent(new CustomEvent('ija:event', {
      detail: { type: 'turn.decision', payload: { session_id: policy.session_id } },
    }))
    await flushPromises()

    expect(editor.get<HTMLSelectElement>('select').element.value).toBe('focused')
    expect(inputs[0].element.value).toBe('4')
    expect(inputs[1].element.value).toBe('0.8')
    expect(inputs[2].element.value).toBe('120')
    expect(editor.text()).toContain('连续 idle：4')
    expect(editor.text()).toContain('42 秒（3 个样本）')

    await editor.get('button').trigger('click')
    await flushPromises()
    expect(saveSpy).toHaveBeenCalledWith(expect.objectContaining({
      mode: 'focused',
      trigger_count: 4,
      frequency_factor: 0.8,
      cooldown_seconds: 120,
      revision: 4,
    }))

    wrapper.unmount()
  })

  it('部分气泡已送达时仍按后端 retryable 状态展示重试入口', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const chat = useChatStore()
    const retrySpy = vi.spyOn(api, 'retryReply').mockResolvedValue({
      status: 'retried',
    })
    vi.spyOn(api, 'messages').mockResolvedValue([])
    vi.spyOn(api, 'decisions').mockResolvedValue([])
    vi.spyOn(api, 'toolExecutions').mockResolvedValue([])
    chat.sessions = [{
      id: 'session-partial-retry',
      platform: 'web-simulator',
      account_id: 'ija-local',
      external_chat_id: 'partial-retry',
      chat_type: 'private',
      display_name: '部分送达',
      participants: [{ external_user_id: 'u1', display_name: '小明', role: 'owner' }],
      revision: 1,
      created_at: '2026-07-30T00:00:00Z',
      updated_at: '2026-07-30T00:00:00Z',
    }]
    chat.selectedId = 'session-partial-retry'
    chat.messages = [{
      id: 'first-bubble',
      session_id: 'session-partial-retry',
      role: 'assistant',
      sender_id: 'agent',
      sender_name: '小佳',
      components: [{ type: 'text', text: '第一气泡' }],
      origin: 'reactive',
      origin_run_id: 'turn-partial',
      created_at: '2026-07-30T00:00:01Z',
    }]
    chat.decisions = [{
      id: 'turn-partial',
      action: 'reply',
      score: 90,
      threshold: 80,
      reason: '需要回复',
      score_detail: {},
      retryable: true,
      created_at: '2026-07-30T00:00:00Z',
    }]

    const wrapper = mount(ChatView, { global: { plugins: [pinia] } })
    const retryButton = wrapper.get('.decision .ghost')
    expect(retryButton.text()).toContain('重试未送达回复')
    await retryButton.trigger('click')
    await flushPromises()

    expect(retrySpy).toHaveBeenCalledWith(
      'session-partial-retry',
      'turn-partial',
    )
  })

  it('搜索旧消息时加载上下文，并可切换结果后恢复原消息列表', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const chat = useChatStore()

    const recentMessage: ChatMessage = {
      id: 'recent-message',
      session_id: 'session-search',
      role: 'user',
      sender_id: 'u1',
      sender_name: '小明',
      components: [{ type: 'text', text: '最近显示的消息' }],
      created_at: '2026-08-05T12:10:00Z',
    }
    const firstResult: ChatMessage = {
      id: 'old-result-1',
      session_id: 'session-search',
      role: 'user',
      sender_id: 'u1',
      sender_name: '阿澄',
      components: [{ type: 'text', text: '第一条旧搜索结果' }],
      created_at: '2026-08-05T10:00:00Z',
    }
    const secondResult: ChatMessage = {
      id: 'old-result-2',
      session_id: 'session-search',
      role: 'assistant',
      sender_id: 'agent',
      sender_name: '小佳',
      components: [{ type: 'text', text: '第二条旧搜索结果' }],
      created_at: '2026-08-05T10:05:00Z',
    }

    chat.sessions = [{
      id: 'session-search',
      platform: 'web-simulator',
      account_id: 'ija-local',
      external_chat_id: 'search-user',
      chat_type: 'private',
      display_name: '消息搜索',
      participants: [
        {
          external_user_id: 'u1',
          display_name: '小明',
          role: 'owner',
        },
      ],
      revision: 1,
      created_at: '2026-08-05T10:00:00Z',
      updated_at: '2026-08-05T12:10:00Z',
    }]
    chat.selectedId = 'session-search'
    chat.messages = [recentMessage]

    const searchSpy = vi.spyOn(api, 'searchMessages').mockResolvedValue([
      firstResult,
      secondResult,
    ])
    const contextSpy = vi.spyOn(api, 'messageContext').mockImplementation(
      async (_sessionId, messageId) => (
        messageId === firstResult.id
          ? [firstResult]
          : [secondResult]
      ),
    )

    const wrapper = mount(ChatView, {
      attachTo: document.body,
      global: { plugins: [pinia] },
    })

    const searchInput = wrapper.get<HTMLInputElement>(
      '.message-search input',
    )
    const shortcutEvent = new KeyboardEvent('keydown', {
      key: 'f',
      ctrlKey: true,
      cancelable: true,
    })

    window.dispatchEvent(shortcutEvent)

    expect(shortcutEvent.defaultPrevented).toBe(true)
    expect(document.activeElement).toBe(searchInput.element)

    await searchInput.setValue('旧搜索结果')
    await wrapper.get('.message-search').trigger('submit')
    await flushPromises()

    expect(searchSpy).toHaveBeenCalledWith(
      'session-search',
      '旧搜索结果',
      500,
    )
    expect(contextSpy).toHaveBeenCalledWith(
      'session-search',
      'old-result-1',
      50,
      50,
    )
    expect(wrapper.get('.message-search-position').text()).toBe('1 / 2')
    expect(wrapper.get('.message-search-active').attributes('id')).toBe(
      'chat-message-old-result-1',
    )
    expect(wrapper.text()).toContain('第一条旧搜索结果')

    await wrapper
      .get('[aria-label="下一个搜索结果"]')
      .trigger('click')
    await flushPromises()

    expect(contextSpy).toHaveBeenCalledWith(
      'session-search',
      'old-result-2',
      50,
      50,
    )
    expect(wrapper.get('.message-search-position').text()).toBe('2 / 2')
    expect(wrapper.get('.message-search-active').attributes('id')).toBe(
      'chat-message-old-result-2',
    )
    expect(wrapper.text()).toContain('第二条旧搜索结果')

    // 搜索上下文只是临时投影，不能覆盖 Pinia 保存的正常消息列表。
    expect(chat.messages).toEqual([recentMessage])

    await wrapper
      .get('[aria-label="关闭消息搜索"]')
      .trigger('click')

    expect(wrapper.text()).toContain('最近显示的消息')
    expect(wrapper.find('.message-search-active').exists()).toBe(false)

    wrapper.unmount()
  })
})
