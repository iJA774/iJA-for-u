<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { isControlSessionEstablished } from './api/client'
import {
  EventSocketClient,
  RUNTIME_EVENT_NAME,
  SNAPSHOT_EVENT_NAME,
  type RuntimeEvent,
} from './realtime/eventSocket'
import { useChatStore } from './stores/chat'

interface NavigationItem {
  to: string
  label: string
  short: string
  description: string
}

const workspaceNavigation: NavigationItem[] = [
  { to: '/', label: '会话模拟', short: '聊', description: '对话与策略判定' },
  { to: '/profiles', label: '长期记忆', short: '忆', description: '事实与关系沉淀' },
  { to: '/persona', label: '自定义人格', short: '格', description: '身份与表达方式' },
  { to: '/schedules', label: '周期任务', short: '程', description: '自动化任务管理' },
  { to: '/chains', label: '链路管理', short: '链', description: '主动触达与订阅' },
]

const systemNavigation: NavigationItem[] = [
  { to: '/learning', label: '学习资源', short: '学', description: '黑话、表达与行为' },
  { to: '/plugins', label: '插件状态', short: '插', description: '运行代际与租约' },
  { to: '/blacklist', label: '本地黑名单', short: '禁', description: '消息受理规则' },
  { to: '/settings', label: '模型设置', short: '模', description: '模型与能力配置' },
  { to: '/logs', label: '运行日志', short: '志', description: '状态与故障审计' },
]

const chat = useChatStore()
const route = useRoute()
const mobileNavigationOpen = ref(false)
const connectionState = ref<'connecting' | 'online' | 'offline'>('connecting')

const currentNavigation = computed(() => (
  [...workspaceNavigation, ...systemNavigation].find(item => item.to === route.path)
  ?? workspaceNavigation[0]
))

watch(() => route.path, () => {
  mobileNavigationOpen.value = false
})

function toggleNavigation() {
  mobileNavigationOpen.value = !mobileNavigationOpen.value
}

/**
 * 为所有可点击控件提供一次短暂的确认动画，覆盖异步请求开始前的感知空窗。
 */
function confirmInteraction(event: MouseEvent) {
  const target = (event.target as HTMLElement).closest<HTMLElement>(
    'button:not(:disabled), a[href], summary, label.file-button, label.upload',
  )
  if (!target) return
  target.classList.remove('interaction-confirmed')
  void target.offsetWidth
  target.classList.add('interaction-confirmed')
  window.setTimeout(() => target.classList.remove('interaction-confirmed'), 420)
}

async function refreshSnapshot(notify = true) {
  try {
    await chat.refreshSessions()
    if (chat.selectedId) await chat.refreshCurrent()
  } finally {
    // 各只读页面拥有独立 REST 快照；聊天快照失败不应阻止它们自行恢复。
    if (notify) window.dispatchEvent(new CustomEvent(SNAPSHOT_EVENT_NAME))
  }
}

async function handleRuntimeEvent(message: RuntimeEvent) {
  window.dispatchEvent(new CustomEvent(RUNTIME_EVENT_NAME, { detail: message }))
  if (message.type === 'session.updated') await chat.refreshSessions()
  if (message.type === 'session.deleted') {
    const deletedId = message.payload?.session_id
    chat.sessions = chat.sessions.filter((item) => item.id !== deletedId)
    if (chat.selectedId === deletedId) {
      chat.selectedId = chat.sessions[0]?.id ?? ''
      chat.messages = []
      chat.decisions = []
      chat.toolExecutions = []
      if (chat.selectedId) await chat.refreshCurrent()
    }
  }
  if (message.type === 'local_data.deleted') {
    chat.sessions = []
    chat.selectedId = ''
    chat.messages = []
    chat.decisions = []
    chat.toolExecutions = []
  }
  if (
    message.type !== 'model.delta'
    && message.payload?.session_id === chat.selectedId
  ) {
    await chat.refreshCurrent()
  }
}

const eventSocket = new EventSocketClient({
  url: () => {
    const protocol = location.protocol === 'https:' ? 'wss' : 'ws'
    return `${protocol}://${location.host}/api/events`
  },
  onEvent: (message) => void handleRuntimeEvent(message),
  onStateChange: state => {
    connectionState.value = state
  },
  onReconnectSnapshot: () => refreshSnapshot(),
})

onMounted(async () => {
  // 控制面会话未恢复前不拉取快照、不建立事件流，避免认证失败时刷屏 401 与反复重连。
  if (!isControlSessionEstablished()) return
  try {
    await refreshSnapshot(false)
  } finally {
    eventSocket.start()
  }
})

onBeforeUnmount(() => eventSocket.stop())
</script>

<template>
  <div
    class="app-shell"
    @click.capture="confirmInteraction"
  >
    <div
      :class="['navigation-scrim', { visible: mobileNavigationOpen }]"
      aria-hidden="true"
      @click="mobileNavigationOpen = false"
    />

    <aside :class="['app-sidebar', { open: mobileNavigationOpen }]">
      <div class="brand">
        <span
          class="brand-mark"
          aria-hidden="true"
        >
          <i />
        </span>
        <div class="brand-copy">
          <strong>iJA <span>for u</span></strong>
          <small>智能陪伴工作台</small>
        </div>
        <button
          class="sidebar-close"
          type="button"
          aria-label="关闭导航"
          @click="mobileNavigationOpen = false"
        >
          ×
        </button>
      </div>

      <nav
        class="sidebar-navigation"
        aria-label="主导航"
      >
        <div class="navigation-group">
          <p>工作空间</p>
          <RouterLink
            v-for="item in workspaceNavigation"
            :key="item.to"
            :to="item.to"
          >
            <span
              class="navigation-icon"
              aria-hidden="true"
            >{{ item.short }}</span>
            <span class="navigation-copy">
              <strong>{{ item.label }}</strong>
              <small>{{ item.description }}</small>
            </span>
            <span
              class="navigation-arrow"
              aria-hidden="true"
            >›</span>
          </RouterLink>
        </div>

        <div class="navigation-group">
          <p>系统管理</p>
          <RouterLink
            v-for="item in systemNavigation"
            :key="item.to"
            :to="item.to"
          >
            <span
              class="navigation-icon"
              aria-hidden="true"
            >{{ item.short }}</span>
            <span class="navigation-copy">
              <strong>{{ item.label }}</strong>
              <small>{{ item.description }}</small>
            </span>
            <span
              class="navigation-arrow"
              aria-hidden="true"
            >›</span>
          </RouterLink>
        </div>
      </nav>

      <div class="runtime-card">
        <div class="runtime-card-head">
          <span :class="['runtime-dot', connectionState]" />
          <strong>{{ connectionState === 'online' ? '本地服务在线' : connectionState === 'connecting' ? '正在连接服务' : '服务连接中断' }}</strong>
        </div>
        <p>数据存储在本机；已启用的外部服务可能处理所选内容</p>
        <div class="runtime-meta">
          <span>127.0.0.1</span>
          <span>四链路运行</span>
        </div>
      </div>
    </aside>

    <section class="app-content">
      <header class="mobile-topbar">
        <button
          class="menu-button"
          type="button"
          aria-label="打开导航"
          :aria-expanded="mobileNavigationOpen"
          @click="toggleNavigation"
        >
          <span />
          <span />
          <span />
        </button>
        <div>
          <strong>{{ currentNavigation.label }}</strong>
          <small>{{ currentNavigation.description }}</small>
        </div>
        <span
          :class="['mobile-status', connectionState]"
          :title="connectionState"
        />
      </header>

      <main class="main">
        <RouterView v-slot="{ Component }">
          <Transition
            name="page-fade"
            mode="out-in"
          >
            <component
              :is="Component"
              :key="route.path"
            />
          </Transition>
        </RouterView>
      </main>
    </section>
  </div>
</template>
