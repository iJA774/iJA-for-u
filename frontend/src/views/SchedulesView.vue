<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { ApiError, api } from '../api/client'
import type { Schedule, ScheduleRun, Session } from '../types'

const schedules = ref<Schedule[]>([])
const sessions = ref<Session[]>([])
const runs = ref<Record<string, ScheduleRun[]>>({})
const editing = ref('')
const busy = ref('')
const error = ref('')
const notice = ref('')

function sessionName(id: string) {
  return sessions.value.find((item) => item.id === id)?.display_name ?? id
}

function formatTime(value?: string) {
  return value ? new Date(value).toLocaleString('zh-CN') : '—'
}

async function refresh() {
  ;[schedules.value, sessions.value] = await Promise.all([api.schedules(), api.sessions()])
}

async function loadRuns(schedule: Schedule) {
  runs.value[schedule.id] = await api.scheduleRuns(schedule.id)
}

async function perform(id: string, action: () => Promise<unknown>, message: string) {
  busy.value = id
  error.value = ''
  notice.value = ''
  try {
    await action()
    notice.value = message
    await refresh()
    if (runs.value[id]) await loadRuns(schedules.value.find((item) => item.id === id) ?? { id } as Schedule)
  } catch (reason) {
    error.value = reason instanceof ApiError ? reason.message : '操作失败'
  } finally {
    busy.value = ''
  }
}

async function save(schedule: Schedule) {
  await perform(schedule.id, () => api.saveSchedule(schedule), '任务已更新')
  editing.value = ''
}

function deleteTask(schedule: Schedule) {
  if (window.confirm('删除后仅保留审计记录，确定吗？')) {
    void perform(
      schedule.id,
      () => api.deleteSchedule(schedule.id, schedule.revision),
      '任务已删除',
    )
  }
}

function handleRuntimeEvent(event: Event) {
  const message = (event as CustomEvent).detail
  if (String(message?.type || '').startsWith('schedule.')) void refresh()
}

onMounted(() => {
  void refresh()
  window.addEventListener('ija:event', handleRuntimeEvent)
})
onBeforeUnmount(() => window.removeEventListener('ija:event', handleRuntimeEvent))
</script>

<template>
  <section class="page">
    <header class="page-head">
      <div>
        <p class="eyebrow">
          SCHEDULER
        </p><h1>周期任务</h1>
        <p>任务从聊天中的自然语言直接创建；这里负责审计和管理。</p>
      </div>
    </header>
    <p
      v-if="notice"
      class="success"
    >
      {{ notice }}
    </p>
    <p
      v-if="error"
      class="error"
    >
      {{ error }}
    </p>
    <div class="schedule-list">
      <article
        v-for="schedule in schedules"
        :key="schedule.id"
        class="card schedule-card"
      >
        <header>
          <div>
            <span :class="['schedule-status', schedule.status]">{{ schedule.status }}</span>
            <h2>{{ schedule.title }}</h2>
            <p>{{ sessionName(schedule.session_id) }} · 创建者 {{ schedule.created_by }}</p>
          </div>
          <div class="schedule-actions">
            <button @click="editing = editing === schedule.id ? '' : schedule.id">
              编辑
            </button>
            <button
              v-if="schedule.status === 'active'"
              :disabled="busy === schedule.id"
              :aria-busy="busy === schedule.id"
              @click="perform(schedule.id, () => api.scheduleAction(schedule.id, 'pause', schedule.revision), '任务已暂停')"
            >
              {{ busy === schedule.id ? '处理中…' : '暂停' }}
            </button>
            <button
              v-else-if="schedule.status === 'paused'"
              :disabled="busy === schedule.id"
              :aria-busy="busy === schedule.id"
              @click="perform(schedule.id, () => api.scheduleAction(schedule.id, 'resume', schedule.revision), '任务已恢复')"
            >
              {{ busy === schedule.id ? '处理中…' : '恢复' }}
            </button>
            <button
              :disabled="busy === schedule.id"
              :aria-busy="busy === schedule.id"
              @click="perform(schedule.id, () => api.runSchedule(schedule.id), '已提交立即执行')"
            >
              {{ busy === schedule.id ? '提交中…' : '立即执行' }}
            </button>
            <button
              class="danger"
              :disabled="busy === schedule.id"
              :aria-busy="busy === schedule.id"
              @click="deleteTask(schedule)"
            >
              {{ busy === schedule.id ? '处理中…' : '删除' }}
            </button>
          </div>
        </header>
        <div class="schedule-meta">
          <span>时区<br><strong>{{ schedule.timezone }}</strong></span>
          <span>下次执行<br><strong>{{ formatTime(schedule.next_run_at) }}</strong></span>
          <span>上次执行<br><strong>{{ formatTime(schedule.last_run_at) }}</strong></span>
          <span>连续失败<br><strong>{{ schedule.consecutive_failures }}</strong></span>
        </div>
        <p class="schedule-instruction">
          {{ schedule.instruction }}
        </p>
        <code>{{ schedule.rrule }}</code>
        <form
          v-if="editing === schedule.id"
          class="schedule-form"
          @submit.prevent="save(schedule)"
        >
          <label>标题<input
            v-model="schedule.title"
            required
          ></label>
          <label>执行指令<textarea
            v-model="schedule.instruction"
            required
          /></label>
          <label>原始自然语言<textarea
            v-model="schedule.source_text"
            required
          /></label>
          <label>时区<input
            v-model="schedule.timezone"
            required
          ></label>
          <label>开始时间<input
            v-model="schedule.dtstart"
            required
          ></label>
          <label>RRULE<input
            v-model="schedule.rrule"
            required
          ></label>
          <button
            class="primary"
            :disabled="busy === schedule.id"
            :aria-busy="busy === schedule.id"
          >
            {{ busy === schedule.id ? '保存中…' : `保存 revision ${schedule.revision}` }}
          </button>
        </form>
        <details @toggle="($event.target as HTMLDetailsElement).open && loadRuns(schedule)">
          <summary>运行记录</summary>
          <div
            v-for="run in runs[schedule.id]"
            :key="run.id"
            class="run-row"
          >
            <span>{{ formatTime(run.scheduled_for) }}</span><strong :class="run.status">{{ run.status }}</strong>
            <span>合并遗漏 {{ run.missed_occurrences }}</span><span>{{ run.error_message || '' }}</span>
          </div>
          <p
            v-if="runs[schedule.id] && !runs[schedule.id].length"
            class="empty"
          >
            暂无运行记录
          </p>
        </details>
      </article>
      <div
        v-if="!schedules.length"
        class="card empty-panel schedule-empty"
      >
        <div
          class="empty-visual schedule-visual"
          aria-hidden="true"
        >
          <span />
          <i />
        </div>
        <div class="empty-copy">
          <p class="eyebrow">
            NO AUTOMATIONS YET
          </p>
          <h2>让下一次提醒自动发生</h2>
          <p>尚无周期任务。你不需要填写复杂表单，直接在私聊中告诉小佳想在何时做什么。</p>
          <div class="command-hint">
            <span>TRY</span>
            <code>每天早上八点告诉我北京天气</code>
          </div>
        </div>
      </div>
    </div>
  </section>
</template>
