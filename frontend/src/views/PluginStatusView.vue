<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { api } from '../api/client'
import { SNAPSHOT_EVENT_NAME } from '../realtime/eventSocket'
import type {
  CapabilityStatus,
  OneBotGroupAccessConfig,
  PlatformCapability,
  PlatformCapabilityStatus,
  PluginGenerationStatus,
} from '../types'

type OneBotGroupDraft = {
  group_id: string
  require_at: boolean
  allow_from_text: string
}

const status = ref<PluginGenerationStatus>()
const capabilities = ref<PlatformCapabilityStatus>()
const oneBotGroups = ref<OneBotGroupDraft[]>([])
const loading = ref(false)
const savingOneBotGroups = ref(false)
const error = ref('')
const oneBotNotice = ref('')

const current = computed(() => (
  status.value?.generations.find(generation => generation.current)
))

function stateText(value: boolean, active: string, inactive: string) {
  return value ? active : inactive
}

function capabilityText(value: CapabilityStatus) {
  return {
    supported: '支持',
    placeholder: '占位',
    unsupported: '不支持',
  }[value]
}

function capabilityStages(platform: PlatformCapability) {
  return [
    { label: 'Ingress canonical', values: platform.ingress },
    { label: '处理 / 派生', values: platform.processing },
    { label: 'Prompt 投影', values: platform.prompt_projection },
    { label: 'Egress', values: platform.egress },
  ]
}

function groupDraft(group: OneBotGroupAccessConfig): OneBotGroupDraft {
  return {
    group_id: group.group_id,
    require_at: group.require_at,
    allow_from_text: group.allow_from.join(', '),
  }
}

function normalizedOneBotGroups(): OneBotGroupAccessConfig[] {
  return oneBotGroups.value.map(group => ({
    group_id: group.group_id.trim(),
    require_at: group.require_at,
    allow_from: group.allow_from_text
      .split(/[\s,，]+/)
      .map(value => value.trim())
      .filter(Boolean),
  }))
}

function addOneBotGroup() {
  oneBotGroups.value.push({ group_id: '', require_at: true, allow_from_text: '' })
}

function removeOneBotGroup(index: number) {
  oneBotGroups.value.splice(index, 1)
}

async function saveOneBotGroups() {
  if (savingOneBotGroups.value) return
  savingOneBotGroups.value = true
  error.value = ''
  oneBotNotice.value = ''
  try {
    const saved = await api.saveOneBotGroupAccess(normalizedOneBotGroups())
    oneBotGroups.value = saved.groups.map(groupDraft)
    oneBotNotice.value = `群准入配置已保存并切换到插件 generation #${saved.generation ?? '新'}。`
    await loadStatus(false)
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '保存 OneBot 群准入配置失败'
  } finally {
    savingOneBotGroups.value = false
  }
}

async function loadStatus(showLoading = true) {
  if (showLoading) loading.value = true
  error.value = ''
  try {
    const [generationStatus, capabilityStatus, oneBotAccess] = await Promise.all([
      api.pluginGenerations(),
      api.pluginCapabilities(),
      api.oneBotGroupAccess(),
    ])
    status.value = generationStatus
    capabilities.value = capabilityStatus
    oneBotGroups.value = oneBotAccess.groups.map(groupDraft)
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '插件状态加载失败'
  } finally {
    if (showLoading) loading.value = false
  }
}

function onSnapshot() {
  void loadStatus()
}

onMounted(async () => {
  window.addEventListener(SNAPSHOT_EVENT_NAME, onSnapshot)
  await loadStatus()
})

onBeforeUnmount(() => {
  window.removeEventListener(SNAPSHOT_EVENT_NAME, onSnapshot)
})
</script>

<template>
  <div class="page plugin-page">
    <header class="page-header">
      <div>
        <p class="eyebrow">
          PLATFORM PLUGINS
        </p>
        <h1>插件状态</h1>
        <p>管理 OneBot 群准入，并查看平台插件代际、接单状态和正在占用的运行租约。</p>
      </div>
      <button
        class="ghost"
        type="button"
        :disabled="loading"
        @click="() => loadStatus()"
      >
        {{ loading ? '刷新中…' : '刷新快照' }}
      </button>
    </header>

    <p
      v-if="error"
      class="error"
      role="alert"
    >
      {{ error }}
    </p>

    <section
      class="card onebot-group-access"
      aria-label="OneBot 群准入配置"
    >
      <header class="section-heading">
        <div>
          <p class="eyebrow">
            ONEBOT GROUP ACCESS
          </p>
          <h2>Agent 可参与群聊</h2>
        </div>
        <p>
          只有列在这里的 QQ 群会进入 OneBot 入站链路；空列表表示不处理任何群聊。
        </p>
      </header>
      <div class="onebot-group-list">
        <div
          v-for="(group, index) in oneBotGroups"
          :key="index"
          class="onebot-group-row"
        >
          <label>群号<input
            v-model="group.group_id"
            placeholder="987654321"
          ></label>
          <label class="onebot-require-at"><input
            v-model="group.require_at"
            type="checkbox"
          >必须 @ Agent</label>
          <label>允许触发的成员 <span>留空表示该群所有成员</span><input
            v-model="group.allow_from_text"
            placeholder="111111, 222222"
          ></label>
          <button
            class="ghost"
            type="button"
            @click="removeOneBotGroup(index)"
          >
            移除
          </button>
        </div>
        <p
          v-if="!oneBotGroups.length"
          class="empty"
        >
          当前没有放行任何 OneBot 群聊。
        </p>
      </div>
      <div class="onebot-group-actions">
        <button
          class="ghost"
          type="button"
          @click="addOneBotGroup"
        >
          添加群聊
        </button>
        <button
          class="primary"
          type="button"
          :disabled="savingOneBotGroups"
          :aria-busy="savingOneBotGroups"
          @click="saveOneBotGroups"
        >
          {{ savingOneBotGroups ? '保存并重载中…' : '保存并立即生效' }}
        </button>
      </div>
      <p
        v-if="oneBotNotice"
        class="notice"
      >
        {{ oneBotNotice }}
      </p>
    </section>

    <section
      v-if="status"
      class="plugin-overview"
      aria-label="当前插件代际概览"
    >
      <article class="card plugin-current">
        <div>
          <p class="eyebrow">
            CURRENT GENERATION
          </p>
          <strong>#{{ status.current_generation }}</strong>
        </div>
        <div class="plugin-current-copy">
          <h2>{{ current?.ready ? '当前代际已就绪' : '当前代际尚未就绪' }}</h2>
          <p>
            {{ current?.plugins.length ?? 0 }} 个插件 ·
            {{ current?.lease_count ?? 0 }} 个运行租约 ·
            {{ current?.accepting ? '正在接收新任务' : '不接收新任务' }}
          </p>
        </div>
      </article>
    </section>

    <section
      v-if="capabilities"
      class="plugin-capabilities"
      aria-label="平台模态能力矩阵"
    >
      <header class="section-heading">
        <div>
          <p class="eyebrow">
            CAPABILITY MATRIX
          </p>
          <h2>平台模态边界</h2>
        </div>
        <p>清单来自内置声明或插件 manifest；“占位”不等于可调用能力。</p>
      </header>
      <div class="plugin-capability-grid">
        <article
          v-for="platform in capabilities.platforms"
          :key="platform.platform"
          class="card plugin-capability"
        >
          <header>
            <div>
              <h3>{{ platform.display_name }}</h3>
              <code>{{ platform.platform }}</code>
            </div>
            <span :class="['generation-badge', { current: platform.enabled }]">
              {{ platform.enabled ? '已启用' : '未启用' }}
            </span>
          </header>
          <div
            v-for="stage in capabilityStages(platform)"
            :key="stage.label"
            class="capability-stage"
          >
            <strong>{{ stage.label }}</strong>
            <div>
              <span
                v-for="(value, name) in stage.values"
                :key="String(name)"
                :class="['capability-chip', value]"
              >
                {{ name }} · {{ capabilityText(value) }}
              </span>
            </div>
          </div>
          <ul>
            <li
              v-for="limitation in platform.limitations"
              :key="limitation"
            >
              {{ limitation }}
            </li>
          </ul>
        </article>
      </div>
    </section>

    <section
      v-if="status"
      class="plugin-generations"
    >
      <article
        v-for="generation in status.generations"
        :key="generation.generation"
        :class="['card', 'plugin-generation', { current: generation.current }]"
      >
        <header>
          <div>
            <p class="eyebrow">
              GENERATION
            </p>
            <h2>#{{ generation.generation }}</h2>
          </div>
          <span :class="['generation-badge', { current: generation.current }]">
            {{ generation.current ? '当前代际' : '历史代际' }}
          </span>
        </header>
        <div class="plugin-state-grid">
          <span :class="{ active: generation.prepared }">
            {{ stateText(generation.prepared, '已准备', '未准备') }}
          </span>
          <span :class="{ active: generation.ready }">
            {{ stateText(generation.ready, '已就绪', '未就绪') }}
          </span>
          <span :class="{ active: generation.accepting }">
            {{ stateText(generation.accepting, '接收任务', '停止接单') }}
          </span>
          <span :class="{ active: generation.running }">
            {{ stateText(generation.running, '正在运行', '未运行') }}
          </span>
          <span
            :class="{
              active: !generation.cleanup_pending,
              'cleanup-pending': generation.cleanup_pending,
            }"
          >
            {{ generation.cleanup_pending ? '清理失败，待重试' : '无需清理' }}
          </span>
        </div>
        <p class="plugin-lease">
          <strong>{{ generation.lease_count }}</strong>
          <span>运行租约</span>
        </p>
        <div class="plugin-list">
          <span
            v-for="plugin in generation.plugins"
            :key="plugin"
          >{{ plugin }}</span>
          <p v-if="!generation.plugins.length">
            此代际没有注册插件。
          </p>
        </div>
      </article>
    </section>

    <section
      v-if="!status && !loading && !error"
      class="card empty-panel"
    >
      <div class="empty-copy">
        <h2>暂无插件代际</h2>
        <p>平台插件管理器尚未返回可展示的运行状态。</p>
      </div>
    </section>

    <p
      v-if="status"
      class="plugin-readonly-note"
    >
      能力清单和 generation 历史仍为诊断信息；上方 OneBot 群准入是此页面唯一可写配置。
    </p>
  </div>
</template>
