<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { api } from '../api/client'
import { RUNTIME_EVENT_NAME, SNAPSHOT_EVENT_NAME } from '../realtime/eventSocket'
import type {
  BehaviorPattern,
  GroupExpressionPattern,
  JargonTerm,
  Session,
  SocialLearningIndexes,
  SocialLearningRun,
} from '../types'

const sessions = ref<Session[]>([])
const selectedId = ref('')
const jargons = ref<JargonTerm[]>([])
const expressions = ref<GroupExpressionPattern[]>([])
const behaviors = ref<BehaviorPattern[]>([])
const runs = ref<SocialLearningRun[]>([])
const indexes = ref<SocialLearningIndexes>(emptyIndexes())
const loading = ref(false)
const error = ref('')
let requestVersion = 0
let refreshTimer: number | undefined

const selectedSession = computed(() => (
  sessions.value.find(session => session.id === selectedId.value)
))

function emptyIndexes(): SocialLearningIndexes {
  return {
    expression_clusters: [],
    behavior_tag_aliases: [],
    behavior_scene_clusters: [],
  }
}

function resetResources() {
  jargons.value = []
  expressions.value = []
  behaviors.value = []
  runs.value = []
  indexes.value = emptyIndexes()
}

function formatTime(value?: string) {
  if (!value) return '暂无'
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(value))
}

function statusText(status: string) {
  return {
    active: '已启用',
    candidate: '候选',
    rejected: '已拒绝',
    disabled: '已停用',
    pending: '等待中',
    running: '运行中',
    completed: '已完成',
    failed: '失败',
    cancelled: '已取消',
    stale: '已失效',
  }[status] ?? status
}

function formatConfidence(value: number) {
  return `${Math.round(value * 100)}%`
}

async function loadSessions() {
  const nextSessions = await api.sessions()
  sessions.value = nextSessions
  if (!nextSessions.some(session => session.id === selectedId.value)) {
    selectedId.value = nextSessions[0]?.id ?? ''
  }
}

async function loadCurrent() {
  const sessionId = selectedId.value
  const version = ++requestVersion
  if (!sessionId) {
    resetResources()
    return
  }
  loading.value = true
  error.value = ''
  try {
    const [
      nextJargons,
      nextExpressions,
      nextBehaviors,
      nextRuns,
      nextIndexes,
    ] = await Promise.all([
      api.learnedJargons(sessionId),
      api.learnedExpressions(sessionId),
      api.learnedBehaviors(sessionId),
      api.socialLearningRuns(sessionId),
      api.socialLearningIndexes(sessionId),
    ])
    if (version !== requestVersion || sessionId !== selectedId.value) return
    jargons.value = nextJargons
    expressions.value = nextExpressions
    behaviors.value = nextBehaviors
    runs.value = nextRuns
    indexes.value = nextIndexes
  } catch (reason) {
    if (version !== requestVersion) return
    error.value = reason instanceof Error ? reason.message : '学习资源加载失败'
  } finally {
    if (version === requestVersion) loading.value = false
  }
}

async function refreshAll() {
  error.value = ''
  try {
    await loadSessions()
    await loadCurrent()
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '学习资源加载失败'
  }
}

function scheduleCurrentRefresh() {
  if (refreshTimer !== undefined) window.clearTimeout(refreshTimer)
  // 一次学习任务会连续发布入队、完成等事件；尾沿合并保证页面采用最终快照。
  refreshTimer = window.setTimeout(() => {
    refreshTimer = undefined
    void loadCurrent()
  }, 80)
}

function onRuntimeEvent(event: Event) {
  const detail = (event as CustomEvent).detail
  if (
    detail?.payload?.session_id === selectedId.value
    && /^social_learning\./.test(detail.type ?? '')
  ) {
    scheduleCurrentRefresh()
  }
}

function onSnapshot() {
  void refreshAll()
}

async function onSessionChange() {
  await loadCurrent()
}

onMounted(async () => {
  window.addEventListener(RUNTIME_EVENT_NAME, onRuntimeEvent)
  window.addEventListener(SNAPSHOT_EVENT_NAME, onSnapshot)
  await refreshAll()
})

onBeforeUnmount(() => {
  window.removeEventListener(RUNTIME_EVENT_NAME, onRuntimeEvent)
  window.removeEventListener(SNAPSHOT_EVENT_NAME, onSnapshot)
  if (refreshTimer !== undefined) window.clearTimeout(refreshTimer)
})
</script>

<template>
  <div class="page learning-page">
    <header class="page-header">
      <div>
        <p class="eyebrow">
          SOCIAL LEARNING
        </p>
        <h1>学习资源</h1>
        <p>只读查看当前会话沉淀的黑话、表达方式、行为经验与索引状态。</p>
      </div>
      <div class="learning-toolbar">
        <label class="page-selector">
          <span>ACTIVE SESSION</span>
          <select
            v-model="selectedId"
            :disabled="!sessions.length || loading"
            @change="onSessionChange"
          >
            <option
              v-if="!sessions.length"
              value=""
            >
              暂无会话
            </option>
            <option
              v-for="session in sessions"
              :key="session.id"
              :value="session.id"
            >
              {{ session.display_name }} · {{ session.chat_type === 'group' ? '群聊' : '私聊' }}
            </option>
          </select>
        </label>
        <button
          class="ghost"
          type="button"
          :disabled="loading"
          @click="refreshAll"
        >
          {{ loading ? '刷新中…' : '刷新快照' }}
        </button>
      </div>
    </header>

    <p
      v-if="error"
      class="error"
      role="alert"
    >
      {{ error }}
    </p>

    <section
      v-if="!sessions.length && !loading"
      class="card empty-panel learning-empty"
    >
      <div class="empty-copy">
        <p class="eyebrow">
          NO SESSION
        </p>
        <h2>还没有可查看的学习资源</h2>
        <p>先在“会话模拟”中创建会话并进行对话，系统会按证据和置信度沉淀资源。</p>
      </div>
    </section>

    <template v-else-if="selectedSession">
      <section
        class="learning-summary"
        aria-label="学习资源概览"
      >
        <article class="card learning-metric">
          <span>黑话词条</span>
          <strong>{{ jargons.length }}</strong>
          <small>当前会话累计</small>
        </article>
        <article class="card learning-metric">
          <span>群体表达</span>
          <strong>{{ expressions.length }}</strong>
          <small>仅群聊会产生</small>
        </article>
        <article class="card learning-metric">
          <span>行为经验</span>
          <strong>{{ behaviors.length }}</strong>
          <small>观察与自我反思</small>
        </article>
        <article class="card learning-metric">
          <span>学习运行</span>
          <strong>{{ runs.length }}</strong>
          <small>含成功与失败审计</small>
        </article>
      </section>

      <div class="learning-grid">
        <section class="card learning-section">
          <header>
            <div>
              <p class="eyebrow">
                JARGON
              </p>
              <h2>黑话与语义</h2>
            </div>
            <small>{{ selectedSession.display_name }}</small>
          </header>
          <article
            v-for="item in jargons"
            :key="item.id"
            class="learning-row"
          >
            <div class="learning-row-heading">
              <strong>{{ item.term }}</strong>
              <span :class="['resource-status', item.status]">{{ statusText(item.status) }}</span>
            </div>
            <p>{{ item.meaning }}</p>
            <small>
              置信度 {{ formatConfidence(item.confidence) }} · 出现 {{ item.occurrence_count }} 次 ·
              推断 {{ item.inference_count }} 次 · 更新于 {{ formatTime(item.updated_at) }}
            </small>
          </article>
          <p
            v-if="!jargons.length"
            class="empty"
          >
            暂无已沉淀的黑话。
          </p>
        </section>

        <section class="card learning-section">
          <header>
            <div>
              <p class="eyebrow">
                EXPRESSION
              </p>
              <h2>群体表达方式</h2>
            </div>
            <small>{{ selectedSession.chat_type === 'group' ? '群聊会话' : '私聊不学习群体表达' }}</small>
          </header>
          <article
            v-for="item in expressions"
            :key="item.id"
            class="learning-row"
          >
            <div class="learning-row-heading">
              <strong>{{ item.situation }}</strong>
              <span :class="['resource-status', item.status]">{{ statusText(item.status) }}</span>
            </div>
            <p>{{ item.style }}</p>
            <small>
              置信度 {{ formatConfidence(item.confidence) }} · 强化 {{ item.occurrence_count }} 次 ·
              采用 {{ item.selection_count }} 次
            </small>
          </article>
          <p
            v-if="!expressions.length"
            class="empty"
          >
            暂无已沉淀的群体表达方式。
          </p>
        </section>

        <section class="card learning-section learning-section-wide">
          <header>
            <div>
              <p class="eyebrow">
                BEHAVIOR
              </p>
              <h2>场景与行为经验</h2>
            </div>
            <small>只展示结构化摘要，不展示原始消息正文</small>
          </header>
          <article
            v-for="item in behaviors"
            :key="item.id"
            class="learning-row behavior-row"
          >
            <div class="learning-row-heading">
              <strong>{{ item.scene_summary }}</strong>
              <span :class="['resource-status', item.status]">{{ statusText(item.status) }}</span>
            </div>
            <p><b>建议行动：</b>{{ item.action }}</p>
            <p><b>预期结果：</b>{{ item.expected_outcome }}</p>
            <div class="tag-list">
              <span
                v-for="tag in [...item.scene_tags, ...item.need_tags]"
                :key="tag"
              >{{ tag }}</span>
            </div>
            <small>
              {{ item.learning_type === 'self_reflection' ? '自我反思' : '观察行为' }} ·
              置信度 {{ formatConfidence(item.confidence) }} · 分数 {{ item.score.toFixed(2) }} ·
              成功 {{ item.success_count }} / 失败 {{ item.failure_count }}
            </small>
          </article>
          <p
            v-if="!behaviors.length"
            class="empty"
          >
            暂无已沉淀的行为经验。
          </p>
        </section>
      </div>

      <div class="learning-grid">
        <section class="card learning-section">
          <header>
            <div>
              <p class="eyebrow">
                INDEX
              </p>
              <h2>派生索引</h2>
            </div>
          </header>
          <dl class="index-summary">
            <div>
              <dt>表达向量簇</dt>
              <dd>{{ indexes.expression_clusters.length }}</dd>
            </div>
            <div>
              <dt>行为标签别名</dt>
              <dd>{{ indexes.behavior_tag_aliases.length }}</dd>
            </div>
            <div>
              <dt>行为场景簇</dt>
              <dd>{{ indexes.behavior_scene_clusters.length }}</dd>
            </div>
          </dl>
          <p class="learning-note">
            索引是可重建的派生数据；页面不会读取或展示原始 embedding 向量。
          </p>
        </section>

        <section class="card learning-section">
          <header>
            <div>
              <p class="eyebrow">
                RUN AUDIT
              </p>
              <h2>最近学习运行</h2>
            </div>
          </header>
          <article
            v-for="run in runs"
            :key="run.id"
            class="learning-run-row"
          >
            <span :class="['resource-status', run.status]">{{ statusText(run.status) }}</span>
            <div>
              <strong>
                产出 {{ run.produced_jargon_ids.length + run.produced_expression_ids.length + run.produced_behavior_ids.length }} 项
              </strong>
              <small>{{ formatTime(run.created_at) }} · 第 {{ run.attempt_count }} 次尝试</small>
              <p v-if="run.error_message">
                {{ run.error_message }}
              </p>
            </div>
          </article>
          <p
            v-if="!runs.length"
            class="empty"
          >
            暂无学习运行记录。
          </p>
        </section>
      </div>
    </template>
  </div>
</template>
