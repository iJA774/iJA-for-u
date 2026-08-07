<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ApiError, api } from '../api/client'
import type {
  MemoryConsolidationRun,
  MemoryKind,
  MemoryRecord,
  Session,
} from '../types'

const sessions = ref<Session[]>([])
const selectedId = ref('')
const memories = ref<MemoryRecord[]>([])
const runs = ref<MemoryConsolidationRun[]>([])
const query = ref('')
const busy = ref(false)
const error = ref('')
const notice = ref('')
const draft = ref<{ content: string; kind: MemoryKind; importance: number }>({
  content: '',
  kind: 'event',
  importance: 0.8,
})

const selectedSession = computed(() => sessions.value.find((item) => item.id === selectedId.value))
const activeCount = computed(() => memories.value.filter((item) => item.status === 'active').length)

const kindLabels: Record<MemoryKind, string> = {
  profile: '人物画像',
  preference: '偏好边界',
  event: '事件',
  episode: '互动情节',
  commitment: '约定待办',
  relationship: '人物关系',
  procedure: '可复用步骤',
  summary: '阶段摘要',
}

async function load() {
  error.value = ''
  try {
    ;[memories.value, runs.value] = await Promise.all([
      api.memories(selectedId.value || undefined),
      api.memoryConsolidations(selectedId.value || undefined),
    ])
  } catch (caught) {
    error.value = caught instanceof ApiError ? caught.message : '长期记忆加载失败'
  }
}

async function act(action: () => Promise<unknown>, message: string) {
  if (busy.value) return
  busy.value = true
  error.value = ''
  notice.value = ''
  try {
    await action()
    notice.value = message
    await load()
  } catch (caught) {
    error.value = caught instanceof ApiError ? caught.message : '记忆操作失败'
  } finally {
    busy.value = false
  }
}

function createMemory() {
  if (!selectedId.value || !draft.value.content.trim()) return
  const payload = {
    content: draft.value.content.trim(),
    kind: draft.value.kind,
    importance: draft.value.importance,
  }
  void act(() => api.createMemory(selectedId.value, payload), '记忆已保存')
  draft.value.content = ''
}

function forget(memory: MemoryRecord) {
  const reason = window.prompt('撤回原因（记录会保留，但不再召回）')
  if (!reason?.trim()) return
  void act(() => api.forgetMemory(memory, reason.trim()), '记忆已撤回')
}

function correct(memory: MemoryRecord) {
  const content = window.prompt('输入纠正后的记忆', memory.content)
  if (!content?.trim() || content.trim() === memory.content) return
  const reason = window.prompt('纠正原因')
  if (!reason?.trim()) return
  void act(() => api.correctMemory(memory, content.trim(), reason.trim()), '已创建纠正版并保留旧版本')
}

function clearSessionMemories() {
  if (!selectedSession.value) return
  const confirmed = window.confirm(
    `确定清空“${selectedSession.value.display_name}”的全部长期记忆吗？画像、记忆、黑话和已学表达会物理删除；聊天正文与自含审计保留，但旧正文不再提供给模型或重新学习。`,
  )
  if (!confirmed) return
  void act(
    () => api.clearSessionMemories(selectedSession.value!.id),
    '当前聊天的长期记忆已清除；聊天正文仍保留',
  )
}

function clearAllMemories() {
  const confirmed = window.confirm(
    '确定清空所有聊天的长期记忆吗？画像、记忆、黑话和已学表达会物理删除；聊天正文与自含审计保留，但旧正文不再提供给模型或重新学习。',
  )
  if (!confirmed) return
  void act(
    () => api.clearAllMemories(),
    '所有聊天的长期记忆已清除；聊天正文仍保留',
  )
}

async function recall() {
  if (!selectedId.value || !query.value.trim()) return
  busy.value = true
  error.value = ''
  notice.value = ''
  try {
    memories.value = await api.recallMemories(selectedId.value, query.value.trim())
    notice.value = '已按当前会话可见域召回'
  } catch (caught) {
    error.value = caught instanceof ApiError ? caught.message : '记忆召回失败'
  } finally {
    busy.value = false
  }
}

function formatTime(value?: string) {
  return value ? new Date(value).toLocaleString() : '—'
}

function onRuntimeEvent(event: Event) {
  const detail = (event as CustomEvent).detail
  if (
    detail?.payload?.session_id === selectedId.value
    && /^memory\./.test(detail.type ?? '')
  ) void load()
}

watch(selectedId, () => void load())
onMounted(async () => {
  sessions.value = await api.sessions()
  selectedId.value = sessions.value[0]?.id ?? ''
  await load()
  window.addEventListener('ija:event', onRuntimeEvent)
})
onBeforeUnmount(() => window.removeEventListener('ija:event', onRuntimeEvent))
</script>

<template>
  <section class="page">
    <header class="page-head">
      <div>
        <p class="eyebrow">
          LONG-TERM MEMORY
        </p>
        <h1>长期记忆</h1>
        <p>记忆按 Session 严格分域，保留证据、产生链路、纠正版本与召回统计。</p>
      </div>
      <div class="memory-page-actions">
        <select v-model="selectedId">
          <option
            v-for="session in sessions"
            :key="session.id"
            :value="session.id"
          >
            {{ session.display_name }} · {{ session.chat_type === 'private' ? '私聊' : '群聊' }}
          </option>
        </select>
        <button
          class="danger-button"
          :disabled="busy || !sessions.length"
          :aria-busy="busy"
          @click="clearAllMemories"
        >
          {{ busy ? '处理中…' : '清空所有记忆' }}
        </button>
      </div>
    </header>

    <p
      v-if="error"
      class="error"
    >
      {{ error }}
    </p>
    <p
      v-if="notice"
      class="notice"
    >
      {{ notice }}
    </p>

    <section
      v-if="selectedSession"
      class="card"
    >
      <header>
        <div>
          <p class="eyebrow">
            CONTROL
          </p>
          <h2>手动记忆与按需召回</h2>
        </div>
        <small>活跃 {{ activeCount }} / 总计 {{ memories.length }}</small>
      </header>
      <div class="feed-create">
        <input
          v-model="draft.content"
          placeholder="明确写入一条当前会话记忆"
        >
        <select v-model="draft.kind">
          <option
            v-for="(label, value) in kindLabels"
            :key="value"
            :value="value"
          >
            {{ label }}
          </option>
        </select>
        <input
          v-model.number="draft.importance"
          type="number"
          min="0"
          max="1"
          step="0.1"
          title="重要度"
        >
        <button
          class="primary"
          :disabled="busy || !draft.content.trim()"
          :aria-busy="busy"
          @click="createMemory"
        >
          {{ busy ? '保存中…' : '保存' }}
        </button>
      </div>
      <div class="feed-create memory-recall-row">
        <input
          v-model="query"
          placeholder="输入问题或当前话题，测试真实召回结果"
          @keyup.enter="recall"
        >
        <button
          class="ghost"
          :disabled="busy || !query.trim()"
          :aria-busy="busy"
          @click="recall"
        >
          {{ busy ? '召回中…' : '召回' }}
        </button>
        <button
          class="ghost"
          :disabled="busy"
          :aria-busy="busy"
          @click="load"
        >
          {{ busy ? '加载中…' : '显示全部' }}
        </button>
        <button
          class="danger-button"
          :disabled="busy || !selectedSession"
          :aria-busy="busy"
          @click="clearSessionMemories"
        >
          {{ busy ? '处理中…' : '清空当前聊天记忆' }}
        </button>
      </div>
    </section>

    <div class="fact-grid">
      <article
        v-for="memory in memories"
        :key="memory.id"
        class="card fact"
      >
        <div>
          <span>{{ kindLabels[memory.kind] }}</span>
          <span :class="memory.status">{{ memory.status }}</span>
        </div>
        <h2>{{ memory.content }}</h2>
        <p>主体：{{ memory.subject_id || '会话整体' }} · 来源链路：{{ memory.source_chain }}</p>
        <p class="scope">
          {{ memory.scope_key }}
        </p>
        <small>
          置信度 {{ Math.round(memory.confidence * 100) }}% · 重要度
          {{ Math.round(memory.importance * 100) }}% · 增强 {{ memory.reinforcement }} 次 ·
          召回 {{ memory.recall_count }} 次
        </small>
        <small>发生：{{ formatTime(memory.happened_at || memory.created_at) }} · 证据 {{ memory.source_message_ids.join(', ') || '手动记录' }}</small>
        <div v-if="memory.status === 'active' || memory.status === 'conflicted'">
          <button
            class="ghost"
            :disabled="busy"
            :aria-busy="busy"
            @click="correct(memory)"
          >
            {{ busy ? '处理中…' : '纠正' }}
          </button>
          <button
            class="danger-link"
            :disabled="busy"
            :aria-busy="busy"
            @click="forget(memory)"
          >
            {{ busy ? '处理中…' : '撤回' }}
          </button>
        </div>
      </article>
    </div>

    <div
      v-if="!memories.length"
      class="card empty large"
    >
      当前会话尚无长期记忆。对话归档、四条链路的真实结果或手动保存都会在这里留下可追溯记录。
    </div>

    <section class="card">
      <header>
        <div>
          <p class="eyebrow">
            CONSOLIDATION
          </p>
          <h2>归档任务审计</h2>
        </div>
      </header>
      <article
        v-for="run in runs"
        :key="run.id"
        class="run-row"
      >
        <span :class="`status ${run.status}`">{{ run.status }}</span>
        <div>
          <strong>{{ run.source_chain }} · 产出 {{ run.produced_memory_ids.length }} 条</strong>
          <small>{{ formatTime(run.updated_at) }} · 尝试 {{ run.attempt_count }} 次</small>
          <span v-if="run.error_message">{{ run.error_message }}</span>
        </div>
        <button
          v-if="run.status === 'failed'"
          class="ghost"
          :disabled="busy"
          :aria-busy="busy"
          @click="act(() => api.retryMemoryConsolidation(run.id), '归档任务已重试')"
        >
          {{ busy ? '重试中…' : '重试' }}
        </button>
      </article>
      <p
        v-if="!runs.length"
        class="empty"
      >
        暂无归档任务。
      </p>
    </section>
  </section>
</template>
