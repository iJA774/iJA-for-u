<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { ApiError, api } from '../api/client'
import type { BlacklistEntry } from '../types'

const entries = ref<BlacklistEntry[]>([])
const busy = ref(false)
const error = ref('')
const notice = ref('')

const draft = ref({
  platform: 'web-simulator',
  account_id: 'ija-local',
  external_user_id: '',
  display_name: '',
  reason: '',
})

async function load() {
  error.value = ''
  try {
    entries.value = await api.blacklist()
  } catch (caught) {
    error.value = caught instanceof ApiError ? caught.message : '黑名单加载失败'
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
    error.value = caught instanceof ApiError ? caught.message : '黑名单操作失败'
  } finally {
    busy.value = false
  }
}

function createEntry() {
  if (!draft.value.external_user_id.trim() || !draft.value.display_name.trim() || !draft.value.reason.trim()) return
  void act(
    () =>
      api.createBlacklist({
        platform: draft.value.platform.trim(),
        account_id: draft.value.account_id.trim(),
        external_user_id: draft.value.external_user_id.trim(),
        display_name: draft.value.display_name.trim(),
        reason: draft.value.reason.trim(),
      }),
    '已加入黑名单',
  )
  draft.value.external_user_id = ''
  draft.value.display_name = ''
  draft.value.reason = ''
}

function removeEntry(entry: BlacklistEntry) {
  const confirmed = window.confirm(
    `确定将“${entry.display_name}”从黑名单移除吗？移除后将恢复受理其后续消息。`,
  )
  if (!confirmed) return
  void act(() => api.deleteBlacklist(entry.id), '已从黑名单移除')
}

function sourceLabel(source: string): string {
  return source === 'agent' ? 'Agent 拉黑' : '手动拉黑'
}

function formatTime(value?: string): string {
  return value ? new Date(value).toLocaleString() : '-'
}

function onRuntimeEvent(event: Event) {
  const detail = (event as CustomEvent).detail
  if (detail?.type === 'blacklist.blocked' || detail?.type === 'blacklist.unblocked') {
    void load()
  }
}

onMounted(async () => {
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
          LOCAL BLACKLIST
        </p>
        <h1>本地黑名单</h1>
        <p>查看被 Agent 自动或手动拉黑的用户，可随时解除拉黑以恢复受理其消息。拉黑后对方在任何会话的消息都不再受理。</p>
      </div>
      <button
        class="primary"
        :disabled="busy"
        @click="load"
      >
        {{ busy ? '加载中…' : '刷新' }}
      </button>
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

    <section class="card">
      <header>
        <div>
          <p class="eyebrow">
            MANUAL
          </p>
          <h2>手动拉黑用户</h2>
        </div>
        <small>共 {{ entries.length }} 条记录</small>
      </header>
      <div class="feed-create blacklist-create">
        <input
          v-model="draft.platform"
          placeholder="平台"
          title="平台"
        >
        <input
          v-model="draft.account_id"
          placeholder="机器人账号"
          title="机器人账号"
        >
        <input
          v-model="draft.external_user_id"
          placeholder="用户外部 ID"
          title="用户外部 ID"
        >
        <input
          v-model="draft.display_name"
          placeholder="展示名"
          title="展示名"
        >
        <input
          v-model="draft.reason"
          placeholder="拉黑原因"
          title="拉黑原因"
        >
        <button
          class="primary"
          :disabled="busy || !draft.external_user_id.trim() || !draft.display_name.trim() || !draft.reason.trim()"
          :aria-busy="busy"
          @click="createEntry"
        >
          {{ busy ? '处理中…' : '拉黑' }}
        </button>
      </div>
    </section>

    <div class="fact-grid">
      <article
        v-for="entry in entries"
        :key="entry.id"
        class="card fact"
      >
        <div>
          <span>{{ sourceLabel(entry.source) }}</span>
          <span class="status">{{ entry.platform }}</span>
        </div>
        <h2>{{ entry.display_name }}</h2>
        <p>用户 ID：{{ entry.external_user_id }}</p>
        <p>机器人账号：{{ entry.account_id }}</p>
        <p class="scope">
          原因：{{ entry.reason }}
        </p>
        <small>拉黑时间：{{ formatTime(entry.created_at) }}</small>
        <small v-if="entry.session_id">来源会话：{{ entry.session_id }}</small>
        <div>
          <button
            class="danger-link"
            :disabled="busy"
            :aria-busy="busy"
            @click="removeEntry(entry)"
          >
            {{ busy ? '处理中…' : '解除拉黑' }}
          </button>
        </div>
      </article>
    </div>

    <div
      v-if="!entries.length"
      class="card empty large"
    >
      当前黑名单为空。当 Agent 在对话中遇到严重违规且屡次提醒不改的用户时会自动拉黑；也可在此手动添加。
    </div>
  </section>
</template>
