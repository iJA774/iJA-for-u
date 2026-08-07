<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ApiError, api } from '../api/client'
import type {
  DriftRun,
  EngagementPolicy,
  FeedSource,
  ProactiveCandidate,
  ProactiveRun,
  Session,
} from '../types'

const sessions = ref<Session[]>([])
const selectedId = ref('')
const policy = ref<EngagementPolicy>()
const feeds = ref<FeedSource[]>([])
const candidates = ref<ProactiveCandidate[]>([])
const proactiveRuns = ref<ProactiveRun[]>([])
const driftRuns = ref<DriftRun[]>([])
const busy = ref(false)
const error = ref('')
const notice = ref('')
const newFeed = ref({ url: '', title: '', poll_interval_minutes: 30 })
let refreshTimer: number | undefined

const privateSessions = computed(() => sessions.value.filter((item) => item.chat_type === 'private'))

async function loadSessions() {
  sessions.value = await api.sessions()
  if (!selectedId.value || !privateSessions.value.some((item) => item.id === selectedId.value)) {
    selectedId.value = privateSessions.value[0]?.id ?? ''
  }
}

async function loadCurrent() {
  if (!selectedId.value) {
    policy.value = undefined
    feeds.value = []
    candidates.value = []
    proactiveRuns.value = []
    driftRuns.value = []
    return
  }
  error.value = ''
  try {
    ;[
      policy.value,
      feeds.value,
      candidates.value,
      proactiveRuns.value,
      driftRuns.value,
    ] = await Promise.all([
      api.engagementPolicy(selectedId.value),
      api.feeds(selectedId.value),
      api.proactiveCandidates(selectedId.value),
      api.proactiveRuns(selectedId.value),
      api.driftRuns(selectedId.value),
    ])
  } catch (caught) {
    error.value = caught instanceof ApiError ? caught.message : '链路状态加载失败'
  }
}

async function act(action: () => Promise<unknown>, success: string) {
  if (busy.value) return
  busy.value = true
  error.value = ''
  notice.value = ''
  try {
    await action()
    notice.value = success
    await loadCurrent()
  } catch (caught) {
    error.value = caught instanceof ApiError ? caught.message : '操作失败'
  } finally {
    busy.value = false
  }
}

function savePolicy() {
  if (!policy.value) return
  void act(() => api.saveEngagementPolicy(policy.value!), '主动策略已保存')
}

function addFeed() {
  if (!selectedId.value || !newFeed.value.url.trim()) return
  void act(
    () => api.createFeed(selectedId.value, {
      url: newFeed.value.url.trim(),
      title: newFeed.value.title.trim(),
      poll_interval_minutes: newFeed.value.poll_interval_minutes,
    }),
    '订阅已添加并等待首次抓取',
  )
  newFeed.value = { url: '', title: '', poll_interval_minutes: 30 }
}

function formatTime(value?: string) {
  return value ? new Date(value).toLocaleString() : '—'
}

function statusText(value: string) {
  const labels: Record<string, string> = {
    pending: '待判断',
    deferred: '已推迟',
    skipped: '已跳过',
    sent: '已发送',
    expired: '已过期',
    failed: '失败',
    gated: '门控阻止',
    prepared: '已准备',
    unknown: '结果不确定',
    running: '运行中',
    completed: '已完成',
    paused: '已暂停',
    selecting: '选择活动',
    executing: '执行活动',
    committing: '提交候选',
    finished: '已收尾',
  }
  return labels[value] ?? value
}

function onRuntimeEvent(event: Event) {
  const detail = (event as CustomEvent).detail
  if (
    detail?.payload?.session_id === selectedId.value
    && /^(engagement|feed|proactive|drift)\./.test(detail.type ?? '')
  ) {
    if (refreshTimer !== undefined) window.clearTimeout(refreshTimer)
    // 一个状态机运行会连续发布多个 checkpoint；尾沿合并避免并发请求覆盖新状态。
    refreshTimer = window.setTimeout(() => {
      refreshTimer = undefined
      void loadCurrent()
    }, 80)
  }
}

watch(selectedId, () => void loadCurrent())
onMounted(async () => {
  await loadSessions()
  await loadCurrent()
  window.addEventListener('ija:event', onRuntimeEvent)
})
onBeforeUnmount(() => {
  window.removeEventListener('ija:event', onRuntimeEvent)
  if (refreshTimer !== undefined) window.clearTimeout(refreshTimer)
})
</script>

<template>
  <div class="page chains-page">
    <header class="page-header">
      <div class="chain-heading">
        <p class="eyebrow">
          FOUR CHAINS
        </p>
        <h1>链路管理</h1>
        <p>编排主动触达、信息订阅与 Drift 思考链路。</p>
      </div>
      <label class="page-selector">
        <span>ACTIVE SESSION</span>
        <select
          v-model="selectedId"
          :disabled="!privateSessions.length"
        >
          <option
            v-if="!privateSessions.length"
            value=""
          >
            暂无可用私聊
          </option>
          <option
            v-for="session in privateSessions"
            :key="session.id"
            :value="session.id"
          >
            {{ session.display_name }}
          </option>
        </select>
      </label>
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
      v-if="!privateSessions.length"
      class="card empty-panel chain-empty"
    >
      <div
        class="empty-visual chain-visual"
        aria-hidden="true"
      >
        <span />
        <i />
        <b />
      </div>
      <div class="empty-copy">
        <p class="eyebrow">
          WAITING FOR A PRIVATE CHAT
        </p>
        <h2>先连接一段私聊</h2>
        <p>请先在“会话模拟”中创建私聊。为了避免意外打扰，群聊不会开放主动触达或 Drift。</p>
        <a
          class="primary"
          href="/"
        >
          前往会话模拟
        </a>
      </div>
    </section>

    <template v-if="policy">
      <section class="card chain-policy">
        <header>
          <div>
            <p class="eyebrow">
              POLICY
            </p><h2>主动行为硬门控</h2>
          </div>
        </header>
        <div class="policy-grid">
          <label class="switch-row"><input
            v-model="policy.proactive_enabled"
            type="checkbox"
          >允许 Proactive 主动触达</label>
          <label class="switch-row"><input
            v-model="policy.drift_enabled"
            type="checkbox"
          >允许 Drift 产生内部候选</label>
          <label>时区<input v-model="policy.timezone"></label>
          <label>静默开始<input
            v-model="policy.quiet_start"
            type="time"
          ></label>
          <label>静默结束<input
            v-model="policy.quiet_end"
            type="time"
          ></label>
          <label>最小间隔（分钟）<input
            v-model.number="policy.minimum_interval_minutes"
            type="number"
            min="5"
          ></label>
        </div>
        <button
          class="primary"
          :disabled="busy"
          :aria-busy="busy"
          @click="savePolicy"
        >
          {{ busy ? '保存中…' : '保存策略' }}
        </button>
      </section>

      <section class="card">
        <header>
          <div>
            <p class="eyebrow">
              RSS / ATOM
            </p><h2>当前私聊的候选来源</h2>
          </div>
        </header>
        <div class="feed-create">
          <input
            v-model="newFeed.url"
            placeholder="https://example.com/feed.xml"
          >
          <input
            v-model="newFeed.title"
            placeholder="显示名称（可选）"
          >
          <input
            v-model.number="newFeed.poll_interval_minutes"
            type="number"
            min="15"
            max="1440"
            title="轮询分钟"
          >
          <button
            class="primary"
            :disabled="busy || !newFeed.url.trim()"
            :aria-busy="busy"
            @click="addFeed"
          >
            {{ busy ? '添加中…' : '添加公开订阅' }}
          </button>
        </div>
        <article
          v-for="feed in feeds"
          :key="feed.id"
          class="feed-row"
        >
          <div>
            <strong>{{ feed.title || feed.url }}</strong>
            <a
              :href="feed.url"
              target="_blank"
              rel="noreferrer"
            >{{ feed.url }}</a>
            <small>下次抓取：{{ formatTime(feed.next_poll_at) }} · 连续失败 {{ feed.consecutive_failures }} 次</small>
            <span
              v-if="feed.error_message"
              class="error"
            >{{ feed.error_message }}</span>
          </div>
          <label class="switch-row"><input
            v-model="feed.enabled"
            type="checkbox"
            @change="act(() => api.saveFeed(feed), '订阅状态已保存')"
          >启用</label>
          <button
            class="ghost"
            :disabled="busy"
            :aria-busy="busy"
            @click="act(() => api.refreshFeed(feed), '抓取已完成')"
          >
            {{ busy ? '抓取中…' : '立即抓取' }}
          </button>
          <button
            class="danger-link"
            :disabled="busy"
            :aria-busy="busy"
            @click="act(() => api.deleteFeed(feed), '订阅已删除')"
          >
            {{ busy ? '处理中…' : '删除' }}
          </button>
        </article>
        <p
          v-if="!feeds.length"
          class="empty"
        >
          尚未添加来源；仅启用 Proactive 不会自动产生消息。
        </p>
      </section>

      <section class="chain-actions">
        <button
          class="primary"
          :disabled="busy || !policy.proactive_enabled"
          :aria-busy="busy"
          @click="act(() => api.runProactive(policy!.session_id), 'Proactive 已运行')"
        >
          {{ busy ? '运行中…' : '立即运行 Proactive' }}
        </button>
        <button
          class="ghost"
          :disabled="busy || !policy.drift_enabled"
          :aria-busy="busy"
          @click="act(() => api.runDrift(policy!.session_id), 'Drift 已运行')"
        >
          {{ busy ? '运行中…' : '立即运行 Drift' }}
        </button>
      </section>

      <section class="card">
        <header>
          <div>
            <p class="eyebrow">
              CANDIDATES
            </p><h2>候选与最终处理</h2>
          </div>
        </header>
        <article
          v-for="candidate in candidates"
          :key="candidate.id"
          class="audit-row"
        >
          <span :class="`status ${candidate.status}`">{{ statusText(candidate.status) }}</span>
          <div>
            <strong>{{ candidate.title }}</strong>
            <p>{{ candidate.summary || '无摘要' }}</p>
            <small>{{ candidate.source_kind }} · {{ formatTime(candidate.published_at || candidate.created_at) }}</small>
            <span v-if="candidate.decision_reason">{{ candidate.decision_reason }}</span>
          </div>
        </article>
        <p
          v-if="!candidates.length"
          class="empty"
        >
          暂无候选。
        </p>
      </section>

      <div class="audit-grid">
        <section class="card">
          <header>
            <div>
              <p class="eyebrow">
                PROACTIVE RUNS
              </p><h2>主动运行审计</h2>
            </div>
          </header>
          <article
            v-for="run in proactiveRuns"
            :key="run.id"
            class="run-row"
          >
            <span :class="`status ${run.status}`">{{ statusText(run.status) }}</span>
            <div><strong>{{ run.gate_reason || run.decision_reason || '已完成判断' }}</strong><small>{{ formatTime(run.created_at) }} · {{ statusText(run.stage) }}<template v-if="run.manual_triggered"> · 人工触发</template><template v-if="run.score != null"> · 分数 {{ run.score.toFixed(2) }}</template><template v-if="run.candidate_ids.length"> · 快照 {{ run.candidate_ids.length }} 条</template></small><span v-if="run.error_message">{{ run.error_message }}</span></div>
          </article>
          <p
            v-if="!proactiveRuns.length"
            class="empty"
          >
            暂无主动运行。
          </p>
        </section>
        <section class="card">
          <header>
            <div>
              <p class="eyebrow">
                DRIFT RUNS
              </p><h2>Drift 运行审计</h2>
            </div>
          </header>
          <article
            v-for="run in driftRuns"
            :key="run.id"
            class="run-row"
          >
            <span :class="`status ${run.status}`">{{ statusText(run.status) }}</span>
            <div><strong>{{ run.activity || 'idle' }}</strong><small>{{ formatTime(run.created_at) }} · {{ statusText(run.stage) }} · 自动续接 {{ run.auto_resume_count }} 次<template v-if="run.resumed_from_run_id"> · 已从暂停点续接</template></small><span>{{ run.error_message || run.decision_reason }}</span></div>
          </article>
          <p
            v-if="!driftRuns.length"
            class="empty"
          >
            暂无 Drift 运行。
          </p>
        </section>
      </div>
    </template>
  </div>
</template>
