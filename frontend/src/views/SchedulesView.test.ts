import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import SchedulesView from './SchedulesView.vue'

const mocks = vi.hoisted(() => ({
  schedules: vi.fn(),
  sessions: vi.fn(),
  scheduleRuns: vi.fn(),
  saveSchedule: vi.fn(),
  scheduleAction: vi.fn(),
  runSchedule: vi.fn(),
  deleteSchedule: vi.fn(),
}))

vi.mock('../api/client', () => ({
  ApiError: class ApiError extends Error {},
  api: mocks,
}))

const schedule = {
  id: 'schedule-1',
  session_id: 'session-1',
  created_by: 'user-1',
  title: '早间天气',
  instruction: '告诉我北京天气',
  source_text: '每天早上八点告诉我北京天气',
  timezone: 'Asia/Shanghai',
  dtstart: '2026-07-29T08:00:00+08:00',
  rrule: 'FREQ=DAILY',
  status: 'active' as const,
  revision: 3,
  next_run_at: '2026-07-30T00:00:00Z',
  consecutive_failures: 0,
  created_at: '2026-07-29T00:00:00Z',
  updated_at: '2026-07-29T00:00:00Z',
}

describe('SchedulesView', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    mocks.schedules.mockReset().mockResolvedValue([{ ...schedule }])
    mocks.sessions.mockReset().mockResolvedValue([
      {
        id: 'session-1',
        platform: 'web-simulator',
        account_id: 'ija-local',
        external_chat_id: 'private-1',
        chat_type: 'private',
        display_name: '我的私聊',
        participants: [],
        revision: 1,
        created_at: '2026-07-29T00:00:00Z',
        updated_at: '2026-07-29T00:00:00Z',
      },
    ])
    mocks.scheduleRuns.mockReset().mockResolvedValue([
      {
        id: 'run-1',
        schedule_id: 'schedule-1',
        scheduled_for: '2026-07-29T00:00:00Z',
        status: 'completed',
        missed_occurrences: 0,
      },
    ])
    mocks.saveSchedule.mockReset().mockResolvedValue({ ...schedule, revision: 4 })
    mocks.scheduleAction.mockReset().mockResolvedValue({})
    mocks.runSchedule.mockReset().mockResolvedValue({})
    mocks.deleteSchedule.mockReset().mockResolvedValue({})
  })

  it('展示任务并以冻结 revision 执行暂停和立即运行', async () => {
    const wrapper = mount(SchedulesView)
    await flushPromises()

    expect(wrapper.text()).toContain('早间天气')
    expect(wrapper.text()).toContain('我的私聊')

    const buttons = wrapper.findAll('.schedule-actions button')
    await buttons.find((item) => item.text() === '暂停')!.trigger('click')
    await flushPromises()
    expect(mocks.scheduleAction).toHaveBeenCalledWith(
      'schedule-1',
      'pause',
      3,
    )
    expect(wrapper.text()).toContain('任务已暂停')

    await buttons.find((item) => item.text() === '立即执行')!.trigger('click')
    await flushPromises()
    expect(mocks.runSchedule).toHaveBeenCalledWith('schedule-1')
  })

  it('编辑任务、按需加载运行记录并经确认删除', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const wrapper = mount(SchedulesView)
    await flushPromises()

    await wrapper.findAll('.schedule-actions button')
      .find((item) => item.text() === '编辑')!
      .trigger('click')
    await wrapper.get('.schedule-form input').setValue('更新后的天气')
    await wrapper.get('.schedule-form').trigger('submit')
    await flushPromises()
    expect(mocks.saveSchedule).toHaveBeenCalledWith(
      expect.objectContaining({
        id: 'schedule-1',
        title: '更新后的天气',
        revision: 3,
      }),
    )

    const details = wrapper.get<HTMLDetailsElement>('details')
    details.element.open = true
    await details.trigger('toggle')
    await flushPromises()
    expect(mocks.scheduleRuns).toHaveBeenCalledWith('schedule-1')
    expect(wrapper.text()).toContain('completed')

    await wrapper.findAll('.schedule-actions button')
      .find((item) => item.text() === '删除')!
      .trigger('click')
    await flushPromises()
    expect(mocks.deleteSchedule).toHaveBeenCalledWith('schedule-1', 3)
  })

  it('操作失败时保留任务并显示错误', async () => {
    mocks.scheduleAction.mockRejectedValueOnce(new Error('scheduler down'))
    const wrapper = mount(SchedulesView)
    await flushPromises()

    await wrapper.findAll('.schedule-actions button')
      .find((item) => item.text() === '暂停')!
      .trigger('click')
    await flushPromises()

    expect(wrapper.get('.error').text()).toBe('操作失败')
    expect(wrapper.text()).toContain('早间天气')
  })
})
