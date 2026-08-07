<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api, ApiError } from '../api/client'
import { useChatStore } from '../stores/chat'
import type { ChatMessage, ChatType, GroupParticipationPolicy, MessageComponent, Participant, ParticipantRole } from '../types'

const chat = useChatStore()
const showCreate = ref(false)
const chatType = ref<ChatType>('private')
const title = ref('和小佳聊天')
const membersText = ref('u001:小明:owner')
const showMembers = ref(false)
const editMembersText = ref('')
const input = ref('')
const mentionAgent = ref(false)
const quoted = ref<ChatMessage>()
const attachment = ref<MessageComponent>()
const error = ref('')
const sending = ref(false)
const creating = ref(false)
const savingMembers = ref(false)
const deletingSession = ref('')
const clearingChat = ref(false)
const senderId = ref('')
const brokenImages = ref(new Set<string>())
const streamingText = ref('')
const retryingTurn = ref('')
const participationPolicy = ref<GroupParticipationPolicy>()
type ParticipationDraft = Pick<GroupParticipationPolicy, 'mode' | 'trigger_count' | 'frequency_factor' | 'cooldown_seconds'>
const participationDraft = ref<ParticipationDraft>()
const savingParticipation = ref(false)
const messageSearchQuery = ref('')
const messageSearchInput = ref<HTMLInputElement>()
const messageSearchResults = ref<ChatMessage[]>([])
const messageContextWindow = ref<ChatMessage[]>()
const activeMessageSearchIndex = ref(-1)
const searchingMessages = ref(false)
let messageSearchRequestVersion = 0

function attachmentLabel(component: MessageComponent) {
  if (component.type === 'audio_ref') return `语音：${component.filename || '未命名'}`
  if (component.type === 'file_ref') return `文件：${component.filename || '未命名'}`
  return `图片：${component.filename || '未命名'}`
}

const selected = computed(() => chat.selected)
// web-simulator 是前端唯一可编辑的会话来源；其他平台消息由外部渠道驱动。
const isSimulator = computed(() => selected.value?.platform === 'web-simulator')
const activeSender = computed(() => selected.value?.participants.find((item) => item.external_user_id === senderId.value) ?? selected.value?.participants[0])
const retryableTurns = computed(() => chat.decisions.filter((decision) => decision.retryable))
const displayedMessages = computed(
  () => messageContextWindow.value ?? chat.messages,
)

const activeMessageSearchResult = computed(() => {
  if (activeMessageSearchIndex.value < 0) return undefined
  return messageSearchResults.value[activeMessageSearchIndex.value]
})

const messageSearchPosition = computed(() => {
  if (!messageSearchResults.value.length) return '0 / 0'
  return `${activeMessageSearchIndex.value + 1} / ${messageSearchResults.value.length}`
})

function isMessageRight(role: ChatMessage['role']) {
  return isSimulator.value ? role === 'user' : role === 'assistant'
}

function resetMessageSearch() {
  messageSearchRequestVersion += 1
  messageSearchQuery.value = ''
  messageSearchResults.value = []
  messageContextWindow.value = undefined
  activeMessageSearchIndex.value = -1
  searchingMessages.value = false
}

async function runMessageSearch() {
  const sessionId = chat.selectedId
  const query = messageSearchQuery.value.trim()

  if (!sessionId || !query) {
    resetMessageSearch()
    return
  }

  const requestVersion = ++messageSearchRequestVersion
  messageContextWindow.value = undefined
  searchingMessages.value = true
  error.value = ''

  try {
    const results = await api.searchMessages(sessionId, query, 500)

    // 用户可能已经切换会话或输入了新的搜索词，旧请求结果不能覆盖新状态。
    if (
      requestVersion !== messageSearchRequestVersion
      || chat.selectedId !== sessionId
    ) return

    messageSearchResults.value = results
    activeMessageSearchIndex.value = results.length ? 0 : -1
  } catch (reason) {
    if (requestVersion !== messageSearchRequestVersion) return
    error.value = reason instanceof ApiError
      ? reason.message
      : '搜索聊天消息失败'
    messageSearchResults.value = []
    activeMessageSearchIndex.value = -1
  } finally {
    if (requestVersion === messageSearchRequestVersion) {
      searchingMessages.value = false
    }
  }
}

function moveMessageSearchResult(direction: -1 | 1) {
  const resultCount = messageSearchResults.value.length
  if (!resultCount) return

  activeMessageSearchIndex.value = (
    activeMessageSearchIndex.value
    + direction
    + resultCount
  ) % resultCount
}

function showPreviousMessageSearchResult() {
  moveMessageSearchResult(-1)
}

function showNextMessageSearchResult() {
  moveMessageSearchResult(1)
}

function handleMessageSearchShortcut(event: KeyboardEvent) {
  if (
    !selected.value
    || (!event.ctrlKey && !event.metaKey)
    || event.key.toLowerCase() !== 'f'
  ) return

  event.preventDefault()
  messageSearchInput.value?.focus()
  messageSearchInput.value?.select()
}

watch(selected, (session) => {
  resetMessageSearch()
  senderId.value = session?.participants[0]?.external_user_id ?? ''
  streamingText.value = ''
  participationPolicy.value = undefined
  participationDraft.value = undefined
  if (session?.chat_type === 'group') {
    void loadParticipationPolicy(session.id)
  }
}, { immediate: true })

watch(activeMessageSearchResult, async (result) => {
  if (!result) return

  const sessionId = chat.selectedId
  const searchVersion = messageSearchRequestVersion

  // 等待 Vue 根据新的 active class 更新 DOM，再尝试定位目标消息。
  await nextTick()

  if (
    activeMessageSearchResult.value?.id !== result.id
    || chat.selectedId !== sessionId
    || messageSearchRequestVersion !== searchVersion
  ) return

  let target = document.getElementById(`chat-message-${result.id}`)

  if (!target) {
    try {
      const context = await api.messageContext(
        sessionId,
        result.id,
        50,
        50,
      )

      // 上下文请求期间可能切换了结果、会话或搜索词。
      if (
        activeMessageSearchResult.value?.id !== result.id
        || chat.selectedId !== sessionId
        || messageSearchRequestVersion !== searchVersion
      ) return

      messageContextWindow.value = context
      await nextTick()

      if (
        activeMessageSearchResult.value?.id !== result.id
        || chat.selectedId !== sessionId
        || messageSearchRequestVersion !== searchVersion
      ) return

      target = document.getElementById(`chat-message-${result.id}`)
    } catch (reason) {
      if (
        activeMessageSearchResult.value?.id !== result.id
        || chat.selectedId !== sessionId
        || messageSearchRequestVersion !== searchVersion
      ) return

      error.value = reason instanceof ApiError
        ? reason.message
        : '加载搜索结果上下文失败'
      return
    }
  }

  if (!target) {
    error.value = '搜索结果上下文中缺少目标消息'
    return
  }

  target.scrollIntoView?.({
    behavior: 'smooth',
    block: 'center',
    inline: 'nearest',
  })
})

function draftFromPolicy(policy: GroupParticipationPolicy): ParticipationDraft {
  return {
    mode: policy.mode,
    trigger_count: policy.trigger_count,
    frequency_factor: policy.frequency_factor,
    cooldown_seconds: policy.cooldown_seconds,
  }
}

async function loadParticipationPolicy(sessionId: string, preserveDraft = false) {
  try {
    const policy = await api.groupParticipationPolicy(sessionId)
    if (chat.selectedId !== sessionId) return
    participationPolicy.value = policy
    if (!preserveDraft || !participationDraft.value) {
      participationDraft.value = draftFromPolicy(policy)
    }
  } catch (reason) {
    error.value = reason instanceof ApiError ? reason.message : '读取群聊参与策略失败'
  }
}

async function saveParticipationPolicy() {
  if (!participationPolicy.value || !participationDraft.value || savingParticipation.value) return
  savingParticipation.value = true
  error.value = ''
  try {
    const saved = await api.saveGroupParticipationPolicy({
      ...participationPolicy.value,
      ...participationDraft.value,
    })
    participationPolicy.value = saved
    participationDraft.value = draftFromPolicy(saved)
  } catch (reason) {
    error.value = reason instanceof ApiError ? reason.message : '保存群聊参与策略失败'
  } finally {
    savingParticipation.value = false
  }
}

function markImageBroken(key: string) {
  brokenImages.value = new Set(brokenImages.value).add(key)
}

async function handleRuntimeEvent(event: Event) {
  const message = (event as CustomEvent<{ type?: string; payload?: Record<string, unknown> }>).detail
  if (message?.payload?.session_id !== chat.selectedId) return
  if (message.type === 'model.delta') {
    streamingText.value += String(message.payload?.delta ?? '')
  }
  if (message.type === 'message.committed' || message.type === 'model.failed' || message.type === 'reply.delivery_failed') {
    streamingText.value = ''
  }
  if (
    selected.value?.chat_type === 'group'
    && (
      message.type === 'turn.decision'
      || (
        message.type === 'message.committed'
        && message.payload?.role === 'assistant'
      )
    )
  ) {
    await loadParticipationPolicy(selected.value.id, true)
  }
}

onMounted(() => {
  window.addEventListener('ija:event', handleRuntimeEvent)
  window.addEventListener('keydown', handleMessageSearchShortcut)
})

onBeforeUnmount(() => {
  window.removeEventListener('ija:event', handleRuntimeEvent)
  window.removeEventListener('keydown', handleMessageSearchShortcut)
})

async function retryReply(turnId: string) {
  if (!selected.value || retryingTurn.value) return
  retryingTurn.value = turnId
  error.value = ''
  try {
    await api.retryReply(selected.value.id, turnId)
    await chat.refreshCurrent()
    if (selected.value.chat_type === 'group') {
      await loadParticipationPolicy(selected.value.id, true)
    }
  } catch (reason) {
    error.value = reason instanceof ApiError ? reason.message : '重试回复失败'
  } finally {
    retryingTurn.value = ''
  }
}

function parseMembers(value: string): Participant[] {
  return value.split(',').map((item) => item.trim()).filter(Boolean).map((item, index) => {
    const [external_user_id, display_name, rawRole] = item.split(':')
    const role: ParticipantRole = ['owner', 'admin', 'member'].includes(rawRole) ? rawRole as ParticipantRole : index === 0 ? 'owner' : 'member'
    return { external_user_id, display_name: display_name || external_user_id, role }
  })
}

async function createSession() {
  if (creating.value) return
  creating.value = true
  error.value = ''
  const participants = parseMembers(membersText.value)
  try {
    const session = await api.createSession({
      chat_type: chatType.value,
      display_name: title.value,
      external_chat_id: `web_${crypto.randomUUID()}`,
      participants,
    })
    showCreate.value = false
    await chat.refreshSessions()
    await chat.select(session.id)
  } catch (reason) {
    error.value = reason instanceof ApiError ? reason.message : '创建会话失败'
  } finally {
    creating.value = false
  }
}

function beginEditMembers() {
  if (!selected.value) return
  editMembersText.value = selected.value.participants.map((item) => `${item.external_user_id}:${item.display_name}:${item.role}`).join(',')
  showMembers.value = true
}

async function saveMembers() {
  if (!selected.value || savingMembers.value) return
  savingMembers.value = true
  error.value = ''
  try {
    await api.saveMembers(selected.value.id, {
      participants: parseMembers(editMembersText.value),
      expected_revision: selected.value.revision,
    })
    showMembers.value = false
    await chat.refreshSessions()
  } catch (reason) {
    error.value = reason instanceof ApiError ? reason.message : '更新成员失败'
  } finally {
    savingMembers.value = false
  }
}

async function removeSession(id: string, name: string) {
  if (!window.confirm(`确定删除聊天窗“${name}”吗？将清除该会话的全部聊天记录与记忆，操作不可恢复。`)) return
  deletingSession.value = id
  error.value = ''
  try {
    await chat.deleteSession(id)
  } catch (reason) {
    error.value = reason instanceof ApiError ? reason.message : '删除会话失败'
  } finally {
    deletingSession.value = ''
  }
}

async function clearChat() {
  if (!selected.value || clearingChat.value) return
  if (!window.confirm(
    `确定清空“${selected.value.display_name}”的聊天正文吗？消息、正文型审计和独占媒体会物理删除；画像、长期记忆和已学表达摘要仍会保留。若要彻底忘记，还需在“长期记忆”页清空记忆。`,
  )) return
  clearingChat.value = true
  error.value = ''
  try {
    await api.clearSessionChat(selected.value.id)
    streamingText.value = ''
    quoted.value = undefined
    await chat.refreshCurrent()
  } catch (reason) {
    error.value = reason instanceof ApiError ? reason.message : '清空聊天正文失败'
  } finally {
    clearingChat.value = false
  }
}

async function upload(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0]
  if (!file) return
  try { attachment.value = await api.upload(file) } catch (reason) { error.value = (reason as Error).message }
}

async function send() {
  if (!isSimulator.value) {
    error.value = '真实对话仅供查看，不能从控制台发送消息'
    return
  }
  if (sending.value || !selected.value || !activeSender.value || (!input.value.trim() && !attachment.value)) return
  sending.value = true
  error.value = ''
  const components: MessageComponent[] = []
  if (mentionAgent.value) components.push({ type: 'mention', target_id: 'agent', target_name: '小佳' })
  if (quoted.value) components.push({ type: 'quote', message_id: quoted.value.id })
  if (input.value.trim()) components.push({ type: 'text', text: input.value.trim() })
  if (attachment.value) components.push(attachment.value)
  try {
    await api.send(selected.value.id, {
      sender_id: activeSender.value.external_user_id,
      sender_name: activeSender.value.display_name,
      components,
    })
    input.value = ''
    mentionAgent.value = false
    quoted.value = undefined
    attachment.value = undefined
    await chat.refreshCurrent()
    if (selected.value.chat_type === 'group') {
      await loadParticipationPolicy(selected.value.id, true)
    }
    await nextTick()
  } catch (reason) {
    error.value = reason instanceof ApiError ? reason.message : '发送失败'
  } finally { sending.value = false }
}

function handleComposerKeydown(event: KeyboardEvent) {
  if (event.isComposing || event.shiftKey) return
  event.preventDefault()
  void send()
}
</script>

<template>
  <section class="page chat-page">
    <header class="page-head">
      <div>
        <p class="eyebrow">
          SIMULATOR
        </p><h1>会话模拟</h1>
      </div><button
        class="primary"
        @click="showCreate = !showCreate"
      >
        新建会话
      </button>
    </header>
    <div
      v-if="showCreate"
      class="create-card card"
    >
      <label>类型<select v-model="chatType"><option value="private">私聊</option><option value="group">群聊</option></select></label>
      <label>名称<input v-model="title"></label>
      <label>成员 <span>格式：ID:昵称:owner/admin/member</span><input v-model="membersText"></label>
      <button
        class="primary"
        :disabled="creating"
        :aria-busy="creating"
        @click="createSession"
      >
        {{ creating ? '创建中…' : '创建' }}
      </button>
    </div>
    <div class="chat-grid">
      <aside class="session-list card">
        <div
          v-for="session in chat.sessions"
          :key="session.id"
          :class="['session-item', {active: session.id === chat.selectedId}]"
          @click="chat.select(session.id)"
        >
          <span>{{ session.chat_type === 'group' ? '群' : '私' }}</span>
          <div class="session-item-main">
            <strong>{{ session.display_name }}</strong>
            <small>{{ session.platform === 'web-simulator' ? '模拟' : '真实' }} · {{ session.participants.length }} 位成员</small>
          </div>
          <button
            class="session-remove"
            title="删除聊天窗"
            :disabled="Boolean(deletingSession)"
            :aria-busy="deletingSession === session.id"
            @click.stop="removeSession(session.id, session.display_name)"
          >
            {{ deletingSession === session.id ? '…' : '×' }}
          </button>
        </div>
        <p
          v-if="!chat.sessions.length"
          class="empty"
        >
          先创建一个模拟会话
        </p>
      </aside>
      <div :class="['conversation', 'card', { 'real-conversation': selected && !isSimulator }]">
        <div
          v-if="!selected"
          class="conversation-welcome"
        >
          <span
            class="welcome-orb"
            aria-hidden="true"
          >
            <i />
          </span>
          <p class="eyebrow">
            START A CONVERSATION
          </p>
          <h2>从一段自然的对话开始</h2>
          <p>创建私聊或群聊会话，体验人格、记忆、工具调用与主动触达的完整链路。</p>
          <div
            class="welcome-features"
            aria-label="会话能力"
          >
            <span><strong>01</strong> 独立会话记忆</span>
            <span><strong>02</strong> 实时策略判定</span>
            <span><strong>03</strong> 工具调用审计</span>
          </div>
          <button
            class="primary"
            @click="showCreate = true"
          >
            创建第一个会话
          </button>
        </div>
        <header v-if="selected">
          <div><strong>{{ selected.display_name }}</strong><small>{{ isSimulator ? '模拟会话 · ' : '真实对话 · ' }}{{ selected.chat_type === 'group' ? '群聊必要性门控' : '私聊默认回复' }}</small></div><button
            class="member-edit"
            @click="beginEditMembers"
          >
            成员与角色
          </button><button
            class="danger-link"
            :disabled="clearingChat"
            :aria-busy="clearingChat"
            @click="clearChat"
          >
            {{ clearingChat ? '清空中…' : '清空正文' }}
          </button><span class="status">运行中</span>
        </header>
        <form
          v-if="selected"
          class="message-search"
          role="search"
          @submit.prevent="runMessageSearch"
        >
          <label>
            <span>搜索消息</span>
            <input
              ref="messageSearchInput"
              v-model="messageSearchQuery"
              type="search"
              maxlength="500"
              autocomplete="off"
              placeholder="输入用户名或聊天内容"
              @keydown.esc.prevent="resetMessageSearch"
            >
          </label>
          <button
            type="submit"
            class="ghost"
            :disabled="searchingMessages || !messageSearchQuery.trim()"
            :aria-busy="searchingMessages"
          >
            {{ searchingMessages ? '搜索中…' : '搜索' }}
          </button>
          <span
            class="message-search-position"
            aria-live="polite"
          >
            {{ messageSearchPosition }}
          </span>
          <button
            type="button"
            class="ghost"
            aria-label="上一个搜索结果"
            :disabled="searchingMessages || !messageSearchResults.length"
            @click="showPreviousMessageSearchResult"
          >
            ↑
          </button>
          <button
            type="button"
            class="ghost"
            aria-label="下一个搜索结果"
            :disabled="searchingMessages || !messageSearchResults.length"
            @click="showNextMessageSearchResult"
          >
            ↓
          </button>
          <button
            v-if="messageSearchQuery || messageSearchResults.length"
            type="button"
            class="ghost"
            aria-label="关闭消息搜索"
            @click="resetMessageSearch"
          >
            ×
          </button>
        </form>
        <div class="messages">
          <button
            v-for="message in displayedMessages"
            :id="`chat-message-${message.id}`"
            :key="message.id"
            :class="[
              'bubble-row',
              message.role,
              isMessageRight(message.role) ? 'message-right' : 'message-left',
              {
                'message-search-active':
                  message.id === activeMessageSearchResult?.id,
              },
            ]"
            :aria-current="
              message.id === activeMessageSearchResult?.id
                ? 'true'
                : undefined
            "
            @click="isSimulator && (quoted = message)"
          >
            <span class="avatar">{{ message.sender_name.slice(0, 1) }}</span><span class="bubble"><small>{{ message.sender_name }}<em
              v-if="message.origin === 'proactive' || message.origin === 'scheduled'"
              class="origin-badge"
            >{{ message.origin === 'proactive' ? '主动触达' : '周期任务' }}</em></small><template
              v-for="(component, componentIndex) in message.components"
              :key="`${message.id}-${componentIndex}`"
            ><span
              v-if="component.type === 'text'"
              class="component-text"
            >{{ component.text }}</span><span
              v-else-if="component.type === 'mention'"
              class="component-text"
            >@{{ component.target_name || component.target_id }}</span><span
              v-else-if="component.type === 'quote'"
              class="component-text component-meta"
            >[引用消息]</span><img
              v-else-if="component.type === 'image_ref' && !brokenImages.has(`${message.id}-${componentIndex}`)"
              class="message-image"
              :src="`/api/messages/${encodeURIComponent(message.id)}/components/${componentIndex}/image?sha256=${component.sha256}`"
              :alt="component.description || component.filename || '消息图片'"
              @error="markImageBroken(`${message.id}-${componentIndex}`)"
            ><span
              v-else-if="component.type === 'image_ref'"
              class="image-missing"
            >图片文件缺失或已损坏</span><audio
              v-else-if="component.type === 'audio_ref'"
              controls
              preload="metadata"
              :src="`/api/messages/${encodeURIComponent(message.id)}/components/${componentIndex}/attachment?sha256=${component.sha256}`"
              @click.stop
            /><a
              v-else-if="component.type === 'file_ref'"
              class="message-file"
              :href="`/api/messages/${encodeURIComponent(message.id)}/components/${componentIndex}/attachment?sha256=${component.sha256}`"
              download
              @click.stop
            >下载 {{ component.filename || '文件' }}</a></template></span>
          </button>
          <div
            v-if="streamingText"
            :class="['bubble-row', 'assistant', 'streaming', isMessageRight('assistant') ? 'message-right' : 'message-left']"
          >
            <span class="avatar">小</span><span class="bubble"><small>小佳</small><span class="component-text">{{ streamingText }}</span></span>
          </div>
          <p
            v-if="selected && !displayedMessages.length"
            class="empty"
          >
            {{ isSimulator ? '发送第一条消息吧' : '等待外部渠道的新消息' }}
          </p>
        </div>
        <footer v-if="selected">
          <div
            v-if="showMembers"
            class="member-editor"
          >
            <label>成员角色<input v-model="editMembersText"></label>
            <button
              :disabled="savingMembers"
              :aria-busy="savingMembers"
              @click="saveMembers"
            >
              {{ savingMembers ? '保存中…' : '保存' }}
            </button><button @click="showMembers = false">
              取消
            </button>
          </div>
          <div
            v-if="selected.chat_type === 'group' && participationPolicy && participationDraft"
            class="participation-editor"
          >
            <label>参与模式<select v-model="participationDraft.mode"><option value="silent">静默</option><option value="normal">普通</option><option value="focused">专注</option></select></label>
            <label>积压触发条数<input
              v-model.number="participationDraft.trigger_count"
              type="number"
              min="1"
              max="999999"
              step="1"
            ></label>
            <label>评分倍率<input
              v-model.number="participationDraft.frequency_factor"
              type="number"
              min="0"
              max="1"
              step="0.05"
            ></label>
            <label>冷却秒数<input
              v-model.number="participationDraft.cooldown_seconds"
              type="number"
              min="0"
              max="3600"
            ></label>
            <span>连续 idle：{{ participationPolicy.idle_streak }}</span>
            <span>群消息平均间隔：{{ participationPolicy.external_interval_ewma_seconds == null ? '待采样' : `${Math.round(participationPolicy.external_interval_ewma_seconds)} 秒` }}（{{ participationPolicy.external_interval_sample_count }} 个样本）</span>
            <button
              :disabled="savingParticipation"
              :aria-busy="savingParticipation"
              @click="saveParticipationPolicy"
            >
              {{ savingParticipation ? '保存中…' : '保存参与策略' }}
            </button>
          </div>
          <template v-if="isSimulator">
            <div
              v-if="quoted || attachment"
              class="composer-note"
            >
              <span v-if="quoted">引用：{{ quoted.sender_name }}</span><span v-if="attachment">{{ attachmentLabel(attachment) }}</span><button @click="quoted = undefined; attachment = undefined">
                清除
              </button>
            </div>
            <label
              v-if="selected.chat_type === 'group'"
              class="sender-picker"
            >当前发言人<select v-model="senderId"><option
              v-for="member in selected.participants"
              :key="member.external_user_id"
              :value="member.external_user_id"
            >{{ member.display_name }}</option></select></label>
            <textarea
              v-model="input"
              placeholder="Enter 发送，Shift+Enter 换行；点击历史消息可引用"
              @keydown.enter="handleComposerKeydown"
            />
            <div class="composer-actions">
              <label v-if="selected.chat_type === 'group'"><input
                v-model="mentionAgent"
                type="checkbox"
              > @小佳</label><label class="upload">添加附件<input
                type="file"
                accept="image/*,audio/*,.pdf,.zip,.txt,.json"
                @change="upload"
              ></label><button
                class="primary"
                :disabled="sending"
                @click="send"
              >
                {{ sending ? '已接收…' : '发送' }}
              </button>
            </div>
          </template>
          <p
            v-else
            class="readonly-notice"
          >
            此为真实对话，只读展示；请在对应外部平台继续发送消息。
          </p>
          <p
            v-if="error"
            class="error"
          >
            {{ error }}
          </p>
        </footer>
      </div>
      <aside class="decision-panel card">
        <h2>策略判定</h2>
        <article
          v-for="item in chat.decisions"
          :key="item.id"
          class="decision"
        >
          <span :class="item.action">{{ item.action === 'reply' ? '回复' : '沉默' }}</span><strong>{{ item.score }} / {{ item.threshold }}</strong><p>{{ item.reason }}</p><details><summary>评分明细</summary><pre>{{ JSON.stringify(item.score_detail, null, 2) }}</pre></details>
          <button
            v-if="retryableTurns.some((turn) => turn.id === item.id)"
            class="ghost"
            :disabled="Boolean(retryingTurn)"
            @click="retryReply(item.id)"
          >
            {{ retryingTurn === item.id ? '重试中…' : '重试未送达回复' }}
          </button>
        </article>
        <p
          v-if="!chat.decisions.length"
          class="empty"
        >
          暂无判定
        </p>
        <h2>工具审计</h2>
        <article
          v-for="item in chat.toolExecutions"
          :key="item.id"
          class="decision tool-audit"
        >
          <span :class="item.status">{{ item.status }}</span><strong>{{ item.tool_name }}</strong>
          <p>{{ item.error_message || new Date(item.started_at).toLocaleString('zh-CN') }}</p>
        </article>
      </aside>
    </div>
  </section>
</template>
