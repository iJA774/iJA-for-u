import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { SNAPSHOT_EVENT_NAME } from '../realtime/eventSocket'
import PluginStatusView from './PluginStatusView.vue'

vi.mock('../api/client', () => ({
  api: {
    pluginGenerations: vi.fn(async () => ({
      current_generation: 3,
      generations: [
        {
          generation: 3,
          current: true,
          prepared: true,
          ready: true,
          accepting: true,
          running: true,
          cleanup_pending: false,
          lease_count: 2,
          plugins: ['web-simulator', 'telegram'],
        },
        {
          generation: 2,
          current: false,
          prepared: true,
          ready: true,
          accepting: false,
          running: false,
          cleanup_pending: true,
          lease_count: 1,
          plugins: ['web-simulator'],
        },
      ],
    })),
    oneBotGroupAccess: vi.fn(async () => ({
      groups: [{ group_id: '987654321', require_at: true, allow_from: ['111111'] }],
    })),
    saveOneBotGroupAccess: vi.fn(async (groups: Array<{ group_id: string; require_at: boolean; allow_from: string[] }>) => ({ groups, generation: 4 })),
    pluginCapabilities: vi.fn(async () => ({
      platforms: [
        {
          platform: 'web-simulator',
          display_name: 'Web 模拟器',
          source: 'builtin',
          enabled: true,
          ingress: { text: 'supported', image: 'supported' },
          processing: { image_description: 'supported', audio_transcript: 'unsupported' },
          prompt_projection: { text: 'supported', image_description: 'supported' },
          egress: { text: 'supported', image: 'supported', audio: 'supported', file: 'supported' },
          limitations: ['尚未提供 ASR。'],
        },
        {
          platform: 'qq',
          display_name: 'QQ 官方机器人',
          source: 'plugin_manifest',
          plugin_id: 'qq',
          enabled: false,
          ingress: { text: 'supported', image: 'placeholder' },
          processing: { image_description: 'unsupported' },
          prompt_projection: { text: 'supported' },
          egress: { text: 'supported', image: 'unsupported', audio: 'unsupported', file: 'unsupported' },
          limitations: ['官方出站当前只实现文本消息。'],
        },
      ],
    })),
  },
}))

describe('PluginStatusView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('只读展示当前与历史代际、插件及租约', async () => {
    const wrapper = mount(PluginStatusView)
    await flushPromises()

    expect(wrapper.text()).toContain('插件状态')
    expect(wrapper.text()).toContain('当前代际已就绪')
    expect(wrapper.text()).toContain('#3')
    expect(wrapper.text()).toContain('telegram')
    expect(wrapper.text()).toContain('2 个运行租约')
    expect(wrapper.text()).toContain('历史代际')
    expect(wrapper.text()).toContain('清理失败，待重试')
    expect(wrapper.text()).toContain('OneBot 群准入是此页面唯一可写配置')
    expect(wrapper.text()).toContain('Agent 可参与群聊')
    expect(wrapper.get<HTMLInputElement>('input[placeholder="987654321"]').element.value).toBe('987654321')
    expect(wrapper.text()).toContain('平台模态边界')
    expect(wrapper.text()).toContain('QQ 官方机器人')
    expect(wrapper.text()).toContain('image · 占位')
    expect(wrapper.text()).toContain('官方出站当前只实现文本消息')

    wrapper.unmount()
  })

  it('收到重连快照事件后刷新插件状态', async () => {
    const wrapper = mount(PluginStatusView)
    await flushPromises()
    expect(api.pluginGenerations).toHaveBeenCalledTimes(1)
    expect(api.pluginCapabilities).toHaveBeenCalledTimes(1)
    expect(api.oneBotGroupAccess).toHaveBeenCalledTimes(1)

    window.dispatchEvent(new CustomEvent(SNAPSHOT_EVENT_NAME))
    await flushPromises()
    expect(api.pluginGenerations).toHaveBeenCalledTimes(2)
    expect(api.pluginCapabilities).toHaveBeenCalledTimes(2)
    expect(api.oneBotGroupAccess).toHaveBeenCalledTimes(2)

    wrapper.unmount()
  })

  it('可在 WebUI 修改 OneBot 群准入并保存后热重载', async () => {
    const wrapper = mount(PluginStatusView)
    await flushPromises()

    const row = wrapper.get('.onebot-group-row')
    await row.get<HTMLInputElement>('input[placeholder="987654321"]').setValue('123456789')
    await row.get<HTMLInputElement>('input[placeholder="111111, 222222"]').setValue('222222, 333333')
    await row.get<HTMLInputElement>('input[type="checkbox"]').setValue(false)
    const saveButton = wrapper.findAll('button').find(button => button.text().includes('保存并立即生效'))
    expect(saveButton).toBeTruthy()
    await saveButton!.trigger('click')
    await flushPromises()

    expect(api.saveOneBotGroupAccess).toHaveBeenCalledWith([{
      group_id: '123456789',
      require_at: false,
      allow_from: ['222222', '333333'],
    }])
    expect(wrapper.text()).toContain('generation #4')

    wrapper.unmount()
  })
})
