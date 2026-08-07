import { flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { defineComponent } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import { describe, expect, it, vi } from 'vitest'
import App from './App.vue'

const socketMocks = vi.hoisted(() => ({
  start: vi.fn(),
  stop: vi.fn(),
}))

vi.mock('./realtime/eventSocket', () => ({
  EventSocketClient: class EventSocketClient {
    start = socketMocks.start
    stop = socketMocks.stop
  },
  RUNTIME_EVENT_NAME: 'ija:event',
  SNAPSHOT_EVENT_NAME: 'ija:snapshot',
}))

vi.mock('./api/client', () => ({
  api: {
    sessions: vi.fn(async () => []),
    messages: vi.fn(async () => []),
    decisions: vi.fn(async () => []),
    toolExecutions: vi.fn(async () => []),
  },
  isControlSessionEstablished: () => true,
}))

describe('App', () => {
  it('导航包含只读诊断页面，并展示准确的本机与外部服务隐私说明', async () => {
    const Placeholder = defineComponent({ template: '<div>页面内容</div>' })
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/', component: Placeholder },
        {
          path: '/:pathMatch(.*)*',
          component: Placeholder,
        },
      ],
    })
    await router.push('/')
    await router.isReady()

    const wrapper = mount(App, {
      global: {
        plugins: [createPinia(), router],
      },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('学习资源')
    expect(wrapper.text()).toContain('插件状态')
    expect(wrapper.text()).toContain('数据存储在本机；已启用的外部服务可能处理所选内容')
    expect(wrapper.find('a[href="/learning"]').exists()).toBe(true)
    expect(wrapper.find('a[href="/plugins"]').exists()).toBe(true)
    expect(socketMocks.start).toHaveBeenCalledTimes(1)

    wrapper.unmount()
    expect(socketMocks.stop).toHaveBeenCalledTimes(1)
  })
})
