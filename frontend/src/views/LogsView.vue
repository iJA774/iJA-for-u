<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api } from '../api/client'
import type { LogEntry, ModelAttempt, ModelAttemptSummary } from '../types'

const MAX_VIEW = 500
const logs = ref<LogEntry[]>([])
const modelAttempts = ref<ModelAttempt[]>([])
const modelSummary = ref<ModelAttemptSummary | null>(null)
const levelFilter = ref<'ALL' | 'INFO' | 'WARNING' | 'ERROR' | 'CRITICAL'>('ALL')
const loggerFilter = ref('')
const paused = ref(false)
const error = ref('')
const loading = ref(false)
const logList = ref<HTMLElement>()

let listener: ((event: Event) => void) | undefined

const filtered = computed(() => {
  let items = logs.value
  if (levelFilter.value !== 'ALL') {
    // ERROR 过滤同时包含 ERROR 与 CRITICAL
    if (levelFilter.value === 'ERROR') {
      items = items.filter((item) => item.level === 'ERROR' || item.level === 'CRITICAL')
    } else {
      items = items.filter((item) => item.level === levelFilter.value)
    }
  }
  if (loggerFilter.value.trim()) {
    const needle = loggerFilter.value.trim().toLowerCase()
    items = items.filter(
      (item) => item.logger.toLowerCase().includes(needle) || item.message.toLowerCase().includes(needle),
    )
  }
  return items.slice(-MAX_VIEW)
})

function formatTime(ts: string): string {
  const date = new Date(ts)
  if (Number.isNaN(date.getTime())) return ts
  return date.toLocaleTimeString('zh-CN', { hour12: false }) + '.' + String(date.getMilliseconds()).padStart(3, '0')
}

function levelClass(level: string): string {
  return level.toLowerCase()
}

function formatTokens(value: number | null | undefined): string {
  return value == null ? 'unknown' : value.toLocaleString('zh-CN')
}

function formatCost(value: number | null | undefined): string {
  return value == null ? 'unknown' : `$${(value / 1_000_000).toFixed(6)}`
}

function extraSummary(entry: LogEntry): string {
  const extra = entry.extra
  if (!extra || Object.keys(extra).length === 0) return ''
  return Object.entries(extra)
    .map(([key, value]) => `${key}=${value}`)
    .join(' ')
}

function append(entry: LogEntry) {
  logs.value.push(entry)
  // 防止内存无限增长
  if (logs.value.length > MAX_VIEW * 2) {
    logs.value = logs.value.slice(-MAX_VIEW)
  }
}

async function refresh() {
  loading.value = true
  error.value = ''
  try {
    const [logResult, attemptResult, summaryResult] = await Promise.all([
      api.logs({ limit: 300 }),
      api.modelAttempts({ limit: 50 }),
      api.modelAttemptSummary(),
    ])
    logs.value = logResult.items
    modelAttempts.value = attemptResult.items
    modelSummary.value = summaryResult
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '加载日志失败'
  } finally {
    loading.value = false
  }
}

function scrollToBottom() {
  if (paused.value) return
  nextTick(() => {
    const el = logList.value
    if (el) el.scrollTop = el.scrollHeight
  })
}

watch(filtered, () => scrollToBottom(), { flush: 'post' })

onMounted(async () => {
  await refresh()
  scrollToBottom()
  listener = (event: Event) => {
    const detail = (event as CustomEvent).detail as { type: string; payload?: LogEntry }
    if (detail?.type === 'log.appended' && detail.payload) {
      append(detail.payload)
    }
  }
  window.addEventListener('ija:event', listener)
})

onBeforeUnmount(() => {
  if (listener) window.removeEventListener('ija:event', listener)
})
</script>

<template>
  <section class="page">
    <header class="page-head">
      <div>
        <p class="eyebrow">
          OBSERVABILITY
        </p><h1>运行日志</h1><p>实时显示启动、连接、异常、插件加载、数据库错误、工具调用与 Skill 使用等运行事件。</p>
      </div>
      <div class="log-toolbar">
        <button
          class="primary"
          :disabled="loading"
          @click="refresh"
        >
          {{ loading ? '加载中…' : '刷新' }}
        </button>
        <button
          class="ghost"
          @click="paused = !paused"
        >
          {{ paused ? '继续滚动' : '暂停滚动' }}
        </button>
        <button
          class="ghost"
          @click="logs = []"
        >
          清空显示
        </button>
      </div>
    </header>

    <div class="log-filters">
      <label>级别
        <select v-model="levelFilter">
          <option value="ALL">
            全部
          </option><option value="INFO">
            INFO
          </option><option value="WARNING">
            WARNING
          </option><option value="ERROR">
            ERROR / CRITICAL
          </option><option value="CRITICAL">
            CRITICAL
          </option>
        </select>
      </label>
      <label>过滤
        <input
          v-model.trim="loggerFilter"
          placeholder="logger 名称或消息关键字"
        >
      </label>
      <span class="log-count">{{ filtered.length }} 条显示 · 共 {{ logs.length }} 条</span>
    </div>

    <p
      v-if="error"
      class="error"
    >
      {{ error }}
    </p>

    <section class="model-observability">
      <div class="section-heading">
        <div>
          <p class="eyebrow">
            MODEL ATTEMPTS
          </p>
          <h2>模型调用概览</h2>
        </div>
        <small>只展示任务、模型安全标识、真实 usage、耗时和结果；不保存 Prompt 或输出正文。</small>
      </div>
      <div class="attempt-metrics">
        <article class="card attempt-metric">
          <span>调用 / 错误</span>
          <strong>{{ modelSummary?.attempt_count ?? 0 }} / {{ modelSummary?.error_count ?? 0 }}</strong>
        </article>
        <article class="card attempt-metric">
          <span>已知 token</span>
          <strong>{{ formatTokens(modelSummary?.total_tokens) }}</strong>
          <small>{{ modelSummary?.usage_unknown_count ?? 0 }} 次 usage unknown</small>
        </article>
        <article class="card attempt-metric">
          <span>平均耗时</span>
          <strong>{{ modelSummary?.average_latency_ms == null ? '—' : `${modelSummary.average_latency_ms} ms` }}</strong>
        </article>
        <article class="card attempt-metric">
          <span>已知成本</span>
          <strong>{{ formatCost(modelSummary?.known_cost_microusd) }}</strong>
          <small>{{ modelSummary?.cost_unknown_count ?? 0 }} 次成本未知</small>
        </article>
      </div>
      <div class="attempt-table card">
        <table>
          <thead>
            <tr>
              <th>时间</th>
              <th>任务</th>
              <th>Provider / Profile / Model</th>
              <th>尝试</th>
              <th>Usage</th>
              <th>耗时</th>
              <th>结果</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="attempt in modelAttempts"
              :key="attempt.id"
            >
              <td>{{ formatTime(attempt.started_at) }}</td>
              <td>{{ attempt.task }}</td>
              <td>{{ attempt.provider }} / {{ attempt.profile }} / {{ attempt.model }}</td>
              <td>#{{ attempt.attempt_number }}{{ attempt.streamed ? ' · stream' : '' }}</td>
              <td>{{ attempt.usage_source === 'unknown' ? 'unknown' : formatTokens(attempt.total_tokens) }}</td>
              <td>{{ attempt.latency_ms }} ms</td>
              <td :class="attempt.success ? 'attempt-ok' : 'attempt-error'">
                {{ attempt.success ? '成功' : (attempt.error_type || '失败') }}
              </td>
            </tr>
            <tr v-if="!modelAttempts.length">
              <td
                colspan="7"
                class="empty"
              >
                暂无模型调用记录。
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <div
      ref="logList"
      class="log-stream card"
    >
      <div
        v-for="entry in filtered"
        :key="entry.id"
        class="log-row"
      >
        <span class="log-ts">{{ formatTime(entry.ts) }}</span>
        <span
          class="log-level"
          :class="levelClass(entry.level)"
        >{{ entry.level }}</span>
        <span class="log-logger">{{ entry.logger }}</span>
        <span class="log-message">{{ entry.message }}</span>
        <small
          v-if="extraSummary(entry)"
          class="log-extra"
        >{{ extraSummary(entry) }}</small>
        <details v-if="entry.exception">
          <summary>异常堆栈</summary>
          <pre>{{ entry.exception }}</pre>
        </details>
      </div>
      <div
        v-if="!filtered.length"
        class="empty large"
      >
        暂无日志记录。发送消息或触发周期任务后，运行事件将在此实时显示。
      </div>
    </div>
  </section>
</template>

<style scoped>
.model-observability {
  display: grid;
  gap: 14px;
  margin-bottom: 22px;
}

.section-heading {
  align-items: end;
  display: flex;
  justify-content: space-between;
  gap: 18px;
}

.section-heading h2 {
  margin: 0;
}

.section-heading > small {
  color: var(--muted);
  max-width: 620px;
}

.attempt-metrics {
  display: grid;
  gap: 12px;
  grid-template-columns: repeat(4, minmax(0, 1fr));
}

.attempt-metric {
  display: grid;
  gap: 5px;
  padding: 16px;
}

.attempt-metric span,
.attempt-metric small {
  color: var(--muted);
}

.attempt-metric strong {
  font-size: 1.35rem;
}

.attempt-table {
  overflow-x: auto;
  padding: 0;
}

.attempt-table table {
  border-collapse: collapse;
  min-width: 880px;
  width: 100%;
}

.attempt-table th,
.attempt-table td {
  border-bottom: 1px solid var(--line);
  padding: 10px 12px;
  text-align: left;
}

.attempt-table th {
  color: var(--muted);
  font-size: 0.8rem;
}

.attempt-ok {
  color: #3b8c62;
}

.attempt-error {
  color: #c44949;
}

@media (max-width: 900px) {
  .attempt-metrics {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .section-heading {
    align-items: start;
    flex-direction: column;
  }
}
</style>
