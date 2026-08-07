import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import SettingsView from './SettingsView.vue'

const mocks = vi.hoisted(() => {
  const config = {
    server: {
      host: '127.0.0.1',
      port: 8000,
      control_authentication: 'disabled_local_compatibility',
      trusted_hosts: ['127.0.0.1', 'localhost', '::1'],
      trusted_origins: [],
      event_history_capacity: 1000,
    },
    model: {
      mode: 'fake', protocol: 'openai_chat', base_url: 'https://api.openai.com/v1', name: '',
      profile_protocol: 'openai_chat', profile_base_url: 'https://api.openai.com/v1', profile_name: '',
      api_key_configured: false, api_key_saved_locally: false, supports_json_object: true,
      profile_api_key_configured: false, profile_api_key_saved_locally: false,
      supports_tools: true,
      supports_vision: false,
      supports_streaming: false,
    },
    vision_model: {
      mode: 'main', protocol: 'openai_chat', base_url: 'https://api.openai.com/v1', name: '',
      timeout_seconds: 60, wait_seconds: 5,
      api_key_configured: false, api_key_saved_locally: false,
    },
    image_model: {
      enabled: false, base_url: 'https://api.openai.com/v1', name: '',
      timeout_seconds: 120,
      api_key_configured: false, api_key_saved_locally: false,
    },
    detection_model: {
      enabled: false, base_url: 'https://api.openai.com/v1', name: '',
      timeout_seconds: 60,
      api_key_configured: false, api_key_saved_locally: false,
    },
    embedding: {
      enabled: true, available: false, base_url: 'https://api.openai.com/v1', name: '',
      timeout_seconds: 30,
      api_key_configured: false, api_key_saved_locally: false,
    },
    chat: {},
  }
  return {
    config,
    deleteAllLocalData: vi.fn(async () => ({})),
    saveDetectionModelConfig: vi.fn(async (payload) => ({
      ...config,
      detection_model: {
        enabled: payload.enabled,
        base_url: payload.base_url,
        name: payload.name,
        timeout_seconds: payload.timeout_seconds,
        api_key_configured: Boolean(payload.api_key),
        api_key_saved_locally: Boolean(payload.api_key),
      },
    })),
  }
})

vi.mock('../api/client', () => ({
  ApiError: class ApiError extends Error {},
  api: {
    config: vi.fn(async () => mocks.config),
    saveModelConfig: vi.fn(),
    saveImageModelConfig: vi.fn(),
    saveVisionModelConfig: vi.fn(),
    saveDetectionModelConfig: mocks.saveDetectionModelConfig,
    saveEmbeddingConfig: vi.fn(),
    deleteAllLocalData: mocks.deleteAllLocalData,
    probe: vi.fn(async () => ({ ok: true })),
  },
}))

describe('SettingsView', () => {
  beforeEach(() => {
    mocks.saveDetectionModelConfig.mockClear()
    mocks.deleteAllLocalData.mockClear()
  })

  it('显示聊天、图片、输出检测和向量检索配置表单', async () => {
    const wrapper = mount(SettingsView)
    await flushPromises()

    expect(wrapper.text()).toContain('模型设置')
    expect(wrapper.text()).toContain('填写 API 模型 ID，不是控制台展示名称')
    expect(wrapper.text()).toContain('图片生成模型（可选）')
    expect(wrapper.text()).toContain('视觉理解模型（可选）')
    expect(wrapper.text()).toContain('输出检测模型（可选）')
    expect(wrapper.text()).toContain('Embedding 模型')
    expect(wrapper.get('a[href="#detection-model"]').text()).toBe('配置输出审核模型')
    // 独立视觉模型未启用时不展示其密钥框。
    expect(wrapper.findAll('input[type="password"]')).toHaveLength(5)
  })

  it('保存输出检测模型并提示已启用', async () => {
    const wrapper = mount(SettingsView)
    await flushPromises()
    const card = wrapper.get('.detection-model-card')

    await card.get('input[type="checkbox"]').setValue(true)
    await card.get('input[type="url"]').setValue('https://audit.example/v1')
    await card.get('input[placeholder="例如 gpt-4o-mini"]').setValue('audit-model')
    await card.get('input[type="number"]').setValue('45')
    await card.get('input[type="password"]').setValue('audit-secret')
    await card.get('form').trigger('submit')
    await flushPromises()

    expect(mocks.saveDetectionModelConfig).toHaveBeenCalledWith({
      enabled: true,
      base_url: 'https://audit.example/v1',
      name: 'audit-model',
      timeout_seconds: 45,
      api_key: 'audit-secret',
      clear_api_key: false,
    })
    expect(wrapper.text()).toContain('检测模型已启用。')
  })

  it('只有精确确认词才允许删除全部本地用户数据', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const wrapper = mount(SettingsView)
    await flushPromises()
    const card = wrapper.findAll('.card').find((item) => item.text().includes('本地用户数据'))
    expect(card).toBeDefined()
    const button = card!.get('button.danger-button')
    expect(button.attributes('disabled')).toBeDefined()

    await card!.get('input[placeholder="删除全部本地用户数据"]').setValue('删除全部本地用户数据')
    expect(button.attributes('disabled')).toBeUndefined()
    await button.trigger('click')
    await flushPromises()

    expect(mocks.deleteAllLocalData).toHaveBeenCalledWith('删除全部本地用户数据')
    expect(card!.text()).toContain('操作者配置、凭据、日志和独立备份仍保留')
  })
})
