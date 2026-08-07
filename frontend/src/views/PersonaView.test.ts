import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, api } from '../api/client'
import PersonaView from './PersonaView.vue'

vi.mock('../api/client', () => ({
  ApiError: class ApiError extends Error {
    constructor(message: string, public code = 'request_failed', public details?: unknown) {
      super(message)
    }
  },
  api: {
    personas: vi.fn(async () => ({
      active_character_id: 'default',
      assignments: {
        private: 'default',
        group: 'uzi',
      },
      personas: [
        {
          character_id: 'default',
          name: '小佳',
          persona_prompt: '自然说话。',
          prompt_sha256: '0'.repeat(64),
          revision: 1,
          updated_at: '2026-07-22T00:00:00Z',
        },
        {
          character_id: 'uzi',
          name: '苏柚',
          persona_prompt: '短句聊天。',
          prompt_sha256: '1'.repeat(64),
          revision: 1,
          updated_at: '2026-07-22T00:00:00Z',
        },
      ],
    })),
    activatePersona: vi.fn(),
    savePersonaAssignments: vi.fn(async (
      assignments: { private: string, group: string },
    ) => assignments),
    savePersona: vi.fn(),
    savePersonaPortrait: vi.fn(),
    deletePersonaPortrait: vi.fn(),
    expressions: vi.fn(async () => []),
    deleteExpression: vi.fn(),
  },
}))

describe('PersonaView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('显示名称、人格提示词和形象上传入口', async () => {
    const wrapper = mount(PersonaView)
    await flushPromises()
    expect(wrapper.text()).toContain('自定义人格')
    expect(wrapper.find('input[type="file"]').exists()).toBe(true)
    expect(wrapper.find('textarea').element.value).toBe('自然说话。')
    expect(wrapper.text()).toContain('character_id: default')
    expect(wrapper.text()).toContain('苏柚（uzi）')
    expect(wrapper.findAll<HTMLSelectElement>('.persona-assignments select')[0].element.value).toBe('default')
    expect(wrapper.findAll<HTMLSelectElement>('.persona-assignments select')[1].element.value).toBe('uzi')
    expect(wrapper.text()).toContain('0 / 200')
  })

  it('切换人格后刷新编辑内容和表情图库', async () => {
    const wrapper = mount(PersonaView)
    await flushPromises()

    await wrapper.get('.persona-switcher select').setValue('uzi')
    await wrapper.get('.persona-switcher button').trigger('click')
    await flushPromises()

    expect(api.activatePersona).not.toHaveBeenCalled()
    expect(wrapper.find('textarea').element.value).toBe('短句聊天。')
    expect(wrapper.text()).toContain('character_id: uzi')
    expect(wrapper.text()).toContain('正在编辑「苏柚」')
    expect(api.expressions).toHaveBeenLastCalledWith('uzi')
  })

  it('分别保存私聊与群聊人格指派', async () => {
    const wrapper = mount(PersonaView)
    await flushPromises()
    const selects = wrapper.findAll<HTMLSelectElement>('.persona-assignments select')
    await selects[0].setValue('uzi')
    await selects[1].setValue('default')
    await wrapper.get('.persona-assignments button').trigger('click')
    await flushPromises()

    expect(api.savePersonaAssignments).toHaveBeenCalledWith({
      private: 'uzi',
      group: 'default',
    })
    expect(wrapper.text()).toContain('私聊与群聊人格指派已保存')
  })

  it('比例不合规时先提示并仅在确认后携带裁剪参数重试', async () => {
    vi.mocked(api.savePersonaPortrait)
      .mockRejectedValueOnce(new ApiError(
        '人物形象必须为 9:16',
        'portrait_aspect_ratio_mismatch',
        { width: 800, height: 800 },
      ))
      .mockResolvedValueOnce({
        character_id: 'default',
        name: '小佳',
        persona_prompt: '自然说话。',
        prompt_sha256: '0'.repeat(64),
        portrait: {
          storage_path: 'hidden', filename: 'base_image.png', sha256: '1'.repeat(64),
          mime_type: 'image/png', size: 100, width: 450, height: 800, aspect_valid: true,
        },
        revision: 2,
        updated_at: '2026-07-22T00:00:00Z',
      })
    const wrapper = mount(PersonaView)
    await flushPromises()
    const input = wrapper.find<HTMLInputElement>('input[type="file"]')
    const file = new File(['square'], 'square.png', { type: 'image/png' })
    Object.defineProperty(input.element, 'files', { value: [file] })
    await input.trigger('change')
    await flushPromises()
    expect(wrapper.text()).toContain('原始尺寸：800 × 800')
    expect(api.savePersonaPortrait).toHaveBeenLastCalledWith('default', file, 1, false)

    await wrapper.get('.crop-warning .primary').trigger('click')
    await flushPromises()
    expect(api.savePersonaPortrait).toHaveBeenLastCalledWith('default', file, 1, true)
    expect(wrapper.find('.crop-warning').exists()).toBe(false)
  })

  it('展示表情图库并支持按 ID 删除', async () => {
    vi.mocked(api.expressions).mockResolvedValueOnce([{
      id: 'expression-1',
      character_id: 'default',
      name: '开心挥手',
      emotion: '开心',
      description: '笑着挥手',
      source_portrait_sha256: '1'.repeat(64),
      mime_type: 'image/png',
      size: 100,
      sha256: '2'.repeat(64),
      width: 576,
      height: 1024,
      use_count: 3,
      source_kind: 'generated',
      created_at: '2026-07-22T00:00:00Z',
    }])
    const wrapper = mount(PersonaView)
    await flushPromises()
    expect(wrapper.text()).toContain('开心挥手')
    expect(wrapper.text()).toContain('已发送 3 次')
    expect(wrapper.get('.expression-card img').attributes('src')).toContain(
      '/api/expressions/expression-1/image',
    )
    await wrapper.get('.danger-link').trigger('click')
    await flushPromises()
    expect(api.deleteExpression).toHaveBeenCalledWith('expression-1', 'default')
  })
})
