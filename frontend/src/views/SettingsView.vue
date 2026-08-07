<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ApiError, api } from '../api/client'
import type { ConfigStatus, DetectionModelConfigInput, EmbeddingConfigInput, ImageModelConfigInput, ModelConfigInput, VisionModelConfigInput } from '../types'

const config = ref<ConfigStatus>()
const probe = ref('')
const notice = ref('')
const error = ref('')
const saving = ref(false)
const apiKey = ref('')
const profileApiKey = ref('')
const imageApiKey = ref('')
const imageNotice = ref('')
const imageError = ref('')
const imageSaving = ref(false)
const visionApiKey = ref('')
const visionNotice = ref('')
const visionError = ref('')
const visionSaving = ref(false)
const detectionApiKey = ref('')
const detectionNotice = ref('')
const detectionError = ref('')
const detectionSaving = ref(false)
const embeddingApiKey = ref('')
const embeddingNotice = ref('')
const embeddingError = ref('')
const embeddingSaving = ref(false)
const localDataConfirmation = ref('')
const localDataNotice = ref('')
const localDataError = ref('')
const deletingLocalData = ref(false)
const form = ref<ModelConfigInput>({
  mode: 'fake',
  protocol: 'openai_chat',
  base_url: 'https://api.openai.com/v1',
  name: '',
  profile_protocol: 'openai_chat',
  profile_base_url: '',
  profile_name: '',
  clear_api_key: false,
  clear_profile_api_key: false,
  supports_json_object: true,
  supports_tools: true,
  supports_vision: false,
  supports_streaming: false,
})
const imageForm = ref<ImageModelConfigInput>({
  enabled: false,
  base_url: 'https://api.openai.com/v1',
  name: '',
  timeout_seconds: 120,
  clear_api_key: false,
})
const visionForm = ref<VisionModelConfigInput>({
  mode: 'main',
  protocol: 'openai_chat',
  base_url: 'https://api.openai.com/v1',
  name: '',
  timeout_seconds: 60,
  wait_seconds: 5,
  clear_api_key: false,
})
const detectionForm = ref<DetectionModelConfigInput>({
  enabled: false,
  base_url: 'https://api.openai.com/v1',
  name: '',
  timeout_seconds: 60,
  clear_api_key: false,
})
const embeddingForm = ref<EmbeddingConfigInput>({
  enabled: true,
  base_url: 'https://api.openai.com/v1',
  name: '',
  timeout_seconds: 30,
  clear_api_key: false,
})

function syncForm(status: ConfigStatus) {
  form.value = {
    mode: status.model.mode,
    protocol: status.model.protocol,
    base_url: status.model.base_url,
    name: status.model.name,
    profile_protocol: status.model.profile_protocol,
    profile_base_url: status.model.profile_base_url,
    profile_name: status.model.profile_name,
    clear_api_key: false,
    clear_profile_api_key: false,
    supports_json_object: status.model.supports_json_object,
    supports_tools: status.model.supports_tools,
    supports_vision: status.model.supports_vision,
    supports_streaming: status.model.supports_streaming,
  }
  apiKey.value = ''
  profileApiKey.value = ''
  imageForm.value = {
    enabled: status.image_model.enabled,
    base_url: status.image_model.base_url,
    name: status.image_model.name,
    timeout_seconds: status.image_model.timeout_seconds,
    clear_api_key: false,
  }
  imageApiKey.value = ''
  visionForm.value = {
    mode: status.vision_model.mode,
    protocol: status.vision_model.protocol,
    base_url: status.vision_model.base_url,
    name: status.vision_model.name,
    timeout_seconds: status.vision_model.timeout_seconds,
    wait_seconds: status.vision_model.wait_seconds,
    clear_api_key: false,
  }
  visionApiKey.value = ''
  detectionForm.value = {
    enabled: status.detection_model.enabled,
    base_url: status.detection_model.base_url,
    name: status.detection_model.name,
    timeout_seconds: status.detection_model.timeout_seconds,
    clear_api_key: false,
  }
  detectionApiKey.value = ''
  embeddingForm.value = {
    enabled: status.embedding.enabled,
    base_url: status.embedding.base_url,
    name: status.embedding.name,
    timeout_seconds: status.embedding.timeout_seconds,
    clear_api_key: false,
  }
  embeddingApiKey.value = ''
}

async function saveEmbedding() {
  embeddingSaving.value = true
  embeddingError.value = ''
  embeddingNotice.value = ''
  try {
    const payload: EmbeddingConfigInput = { ...embeddingForm.value }
    if (embeddingApiKey.value.trim()) payload.api_key = embeddingApiKey.value.trim()
    config.value = await api.saveEmbeddingConfig(payload)
    syncForm(config.value)
    embeddingNotice.value = config.value.embedding.available
      ? 'Embedding 模型已启用；将回填本地记忆和群体表达索引。'
      : payload.enabled
        ? 'Embedding 默认开启，填写模型名和 API Key 后生效；当前继续使用本地选择器。'
        : 'Embedding 已禁用；记忆与群体表达使用本地选择器。'
  } catch (reason) {
    embeddingError.value = reason instanceof ApiError ? reason.message : '保存 embedding 配置失败'
  } finally {
    embeddingSaving.value = false
  }
}

async function saveImageModel() {
  imageSaving.value = true
  imageError.value = ''
  imageNotice.value = ''
  try {
    const payload: ImageModelConfigInput = { ...imageForm.value }
    if (imageApiKey.value.trim()) payload.api_key = imageApiKey.value.trim()
    config.value = await api.saveImageModelConfig(payload)
    syncForm(config.value)
    imageNotice.value = payload.enabled ? '图片模型已启用。' : '图片模型已禁用；已有表情仍可复用。'
  } catch (reason) {
    imageError.value = reason instanceof ApiError ? reason.message : '保存图片模型配置失败'
  } finally {
    imageSaving.value = false
  }
}

async function saveVisionModel() {
  visionSaving.value = true
  visionError.value = ''
  visionNotice.value = ''
  try {
    const payload: VisionModelConfigInput = { ...visionForm.value }
    if (visionApiKey.value.trim()) payload.api_key = visionApiKey.value.trim()
    config.value = await api.saveVisionModelConfig(payload)
    syncForm(config.value)
    visionNotice.value = payload.mode === 'external'
      ? '独立视觉模型已启用；图片先转换为不可信派生描述再交给主模型。'
      : '未使用外挂视觉模型；图片将由声明支持视觉的主 LLM 直接处理。'
  } catch (reason) {
    visionError.value = reason instanceof ApiError ? reason.message : '保存视觉模型配置失败'
  } finally {
    visionSaving.value = false
  }
}

async function saveDetectionModel() {
  detectionSaving.value = true
  detectionError.value = ''
  detectionNotice.value = ''
  try {
    const payload: DetectionModelConfigInput = { ...detectionForm.value }
    if (detectionApiKey.value.trim()) payload.api_key = detectionApiKey.value.trim()
    config.value = await api.saveDetectionModelConfig(payload)
    syncForm(config.value)
    detectionNotice.value = payload.enabled ? '检测模型已启用。' : '检测模型已禁用；标准化命中将直接拦截。'
  } catch (reason) {
    detectionError.value = reason instanceof ApiError ? reason.message : '保存检测模型配置失败'
  } finally {
    detectionSaving.value = false
  }
}

onMounted(async () => {
  try {
    config.value = await api.config()
    syncForm(config.value)
  } catch (reason) {
    error.value = reason instanceof ApiError ? reason.message : '读取当前配置失败'
  }
})

async function saveModel() {
  saving.value = true
  error.value = ''
  notice.value = ''
  try {
    const payload: ModelConfigInput = { ...form.value }
    if (apiKey.value.trim()) payload.api_key = apiKey.value.trim()
    if (profileApiKey.value.trim()) payload.profile_api_key = profileApiKey.value.trim()
    config.value = await api.saveModelConfig(payload)
    syncForm(config.value)
    notice.value = '模型配置已保存，后续消息将使用新配置。'
  } catch (reason) {
    error.value = reason instanceof ApiError ? reason.message : '保存模型配置失败'
  } finally {
    saving.value = false
  }
}

async function testModel() {
  probe.value = '检测中…'
  try {
    probe.value = JSON.stringify(await api.probe())
  } catch (reason) {
    probe.value = reason instanceof Error ? reason.message : '模型检测失败'
  }
}

async function deleteAllLocalData() {
  if (
    deletingLocalData.value
    || localDataConfirmation.value !== '删除全部本地用户数据'
  ) return
  if (!window.confirm('这是不可逆的全局删除。确定继续吗？')) return
  deletingLocalData.value = true
  localDataNotice.value = ''
  localDataError.value = ''
  try {
    await api.deleteAllLocalData(localDataConfirmation.value)
    localDataConfirmation.value = ''
    localDataNotice.value = '全部聊天用户数据与用户媒体已删除；操作者配置、凭据、日志和独立备份仍保留。'
  } catch (reason) {
    localDataError.value = reason instanceof ApiError ? reason.message : '删除全部本地用户数据失败'
  } finally {
    deletingLocalData.value = false
  }
}
</script>

<template>
  <section class="page">
    <header class="page-head">
      <div>
        <p class="eyebrow">
          RUNTIME
        </p><h1>模型设置</h1><p>保存到本机后立即生效；密钥不会通过 API 返回。</p>
      </div><div class="model-settings-actions">
        <a
          class="ghost"
          href="#detection-model"
        >配置输出审核模型</a><button
          class="primary"
          @click="testModel"
        >
          检测模型能力
        </button>
      </div>
    </header>
    <div class="settings-grid">
      <article class="card">
        <h2>模型连接</h2>
        <form
          class="model-form"
          @submit.prevent="saveModel"
        >
          <label>Provider<select v-model="form.mode"><option value="fake">Fake（本地演示）</option><option value="openai">OpenAI-compatible</option></select></label>
          <label>聊天协议<select v-model="form.protocol"><option value="openai_chat">OpenAI Chat Completions</option><option value="openai_responses">OpenAI Responses</option><option value="anthropic_messages">Anthropic Messages</option></select></label>
          <label>Base URL<input
            v-model.trim="form.base_url"
            type="url"
            placeholder="https://api.openai.com/v1"
            required
          ></label>
          <label>聊天模型 <span>填写 API 模型 ID，不是控制台展示名称</span><input
            v-model.trim="form.name"
            :required="form.mode === 'openai'"
            placeholder="例如 gpt-4o-mini 或 doubao-seed-2.0-mini"
          ></label>
          <label>画像协议<select v-model="form.profile_protocol"><option value="openai_chat">OpenAI Chat Completions</option><option value="openai_responses">OpenAI Responses</option><option value="anthropic_messages">Anthropic Messages</option></select></label>
          <label>画像 Base URL <span>可独立部署；留空继承聊天端点</span><input
            v-model.trim="form.profile_base_url"
            type="url"
            placeholder="留空继承聊天 Base URL"
          ></label>
          <label>画像模型 <span>填写 API 模型 ID；留空则使用聊天模型</span><input
            v-model.trim="form.profile_name"
            placeholder="可选"
          ></label>
          <label class="checkbox-label"><input
            v-model="form.supports_json_object"
            type="checkbox"
          > JSON 对象输出（画像提取与部分判断需要）</label>
          <label class="checkbox-label"><input
            v-model="form.supports_tools"
            type="checkbox"
          > 原生 tool_calls（时间、天气、周期任务与表情 Skill 需要）</label>
          <label class="checkbox-label"><input
            v-model="form.supports_vision"
            type="checkbox"
          > 视觉输入（QQ/网页聊天中的真实图片会发送给聊天模型）</label>
          <label class="checkbox-label"><input
            v-model="form.supports_streaming"
            type="checkbox"
            :disabled="form.supports_tools || form.protocol !== 'openai_chat'"
          > 流式文本输出（仅 OpenAI Chat 且关闭原生 tool_calls 时可用）</label>
          <label>API Key <span>{{ config?.model.api_key_saved_locally ? '已保存；留空则保留现有密钥' : '仅保存在本机配置文件中' }}</span><input
            v-model="apiKey"
            type="password"
            autocomplete="new-password"
            :required="form.mode === 'openai' && !config?.model.api_key_saved_locally && !form.clear_api_key"
            placeholder="sk-..."
          ></label>
          <label
            v-if="config?.model.api_key_saved_locally"
            class="checkbox-label"
          >
            <input
              v-model="form.clear_api_key"
              type="checkbox"
            > 清除已保存的 API Key
          </label>
          <label>画像 API Key <span>{{ config?.model.profile_api_key_saved_locally ? '已单独保存；留空则保留' : '留空继承聊天 API Key' }}</span><input
            v-model="profileApiKey"
            type="password"
            autocomplete="new-password"
            placeholder="可选"
          ></label>
          <label
            v-if="config?.model.profile_api_key_saved_locally"
            class="checkbox-label"
          >
            <input
              v-model="form.clear_profile_api_key"
              type="checkbox"
            > 清除独立画像 API Key（恢复继承）
          </label>
          <div class="form-actions">
            <button
              class="primary"
              :disabled="saving"
            >
              {{ saving ? '保存中…' : '保存并应用' }}
            </button><p
              v-if="notice"
              class="success"
            >
              {{ notice }}
            </p>
          </div>
          <p
            v-if="error"
            class="error"
          >
            {{ error }}
          </p>
        </form>
      </article><article class="card">
        <h2>连接探测</h2><p>{{ probe || '尚未检测' }}</p><p class="scope">
          当前：{{ config?.model.mode === 'openai' ? 'OpenAI-compatible' : 'Fake Provider' }}<br>API Key：{{ config?.model.api_key_configured ? '已配置' : '未配置' }}
        </p>
      </article><article class="card image-model-card">
        <h2>视觉理解模型（可选）</h2>
        <p class="privacy-note">
          默认由主 LLM 直接读取图片。选择独立视觉模型后，图片只发送给该模型，主 LLM 接收带“不可信派生”标记的文字描述；该能力也用于理解和命名自动收集的 QQ 表情包。
        </p>
        <form
          class="model-form"
          @submit.prevent="saveVisionModel"
        >
          <label>处理方式<select v-model="visionForm.mode"><option value="main">主 LLM 直接看图</option><option value="external">独立视觉模型</option></select></label>
          <template v-if="visionForm.mode === 'external'">
            <label>协议<select v-model="visionForm.protocol"><option value="openai_chat">OpenAI Chat Completions</option><option value="openai_responses">OpenAI Responses</option><option value="anthropic_messages">Anthropic Messages</option></select></label>
            <label>Base URL<input
              v-model.trim="visionForm.base_url"
              type="url"
              required
            ></label>
            <label>视觉模型<input
              v-model.trim="visionForm.name"
              required
              placeholder="例如 gpt-4o-mini"
            ></label>
            <label>请求超时（秒）<input
              v-model.number="visionForm.timeout_seconds"
              type="number"
              min="1"
              max="300"
              step="1"
              required
            ></label>
            <label>聊天最长等待（秒）<input
              v-model.number="visionForm.wait_seconds"
              type="number"
              min="0"
              max="30"
              step="0.5"
              required
            ></label>
            <label>API Key <span>{{ config?.vision_model.api_key_saved_locally ? '已保存；留空则保留' : '仅保存在本机配置文件中' }}</span><input
              v-model="visionApiKey"
              type="password"
              autocomplete="new-password"
              :required="!config?.vision_model.api_key_saved_locally && !visionForm.clear_api_key"
              placeholder="sk-..."
            ></label>
            <label
              v-if="config?.vision_model.api_key_saved_locally"
              class="checkbox-label"
            ><input
              v-model="visionForm.clear_api_key"
              type="checkbox"
            > 清除已保存的视觉 API Key</label>
          </template>
          <div class="form-actions">
            <button
              class="primary"
              :disabled="visionSaving"
            >
              {{ visionSaving ? '保存中…' : '保存视觉设置' }}
            </button><p
              v-if="visionNotice"
              class="success"
            >
              {{ visionNotice }}
            </p>
          </div>
          <p
            v-if="visionError"
            class="error"
          >
            {{ visionError }}
          </p>
        </form>
      </article><article class="card image-model-card">
        <h2>图片生成模型（可选）</h2>
        <p class="privacy-note">
          启用后，Agent 可在需要表达情绪时自动把当前 base_image 和最小化生成描述发送到该服务；请求可能产生费用，不再逐次确认。聊天正文不会随图片请求发送。
        </p>
        <form
          class="model-form"
          @submit.prevent="saveImageModel"
        >
          <label class="checkbox-label"><input
            v-model="imageForm.enabled"
            type="checkbox"
          > 启用表情图片生成</label>
          <label>Base URL<input
            v-model.trim="imageForm.base_url"
            type="url"
            required
          ></label>
          <label>图片模型<input
            v-model.trim="imageForm.name"
            :required="imageForm.enabled"
            placeholder="例如 gpt-image-1"
          ></label>
          <label>请求超时（秒）<input
            v-model.number="imageForm.timeout_seconds"
            type="number"
            min="1"
            max="600"
            step="1"
            required
          ></label>
          <label>API Key <span>{{ config?.image_model.api_key_saved_locally ? '已保存；留空则保留' : '仅保存在本机配置文件中' }}</span><input
            v-model="imageApiKey"
            type="password"
            autocomplete="new-password"
            :required="imageForm.enabled && !config?.image_model.api_key_saved_locally && !imageForm.clear_api_key"
            placeholder="sk-..."
          ></label>
          <label
            v-if="config?.image_model.api_key_saved_locally"
            class="checkbox-label"
          ><input
            v-model="imageForm.clear_api_key"
            type="checkbox"
          > 清除已保存的图片 API Key</label>
          <div class="form-actions">
            <button
              class="primary"
              :disabled="imageSaving"
            >
              {{ imageSaving ? '保存中…' : '保存图片模型' }}
            </button><p
              v-if="imageNotice"
              class="success"
            >
              {{ imageNotice }}
            </p>
          </div>
          <p
            v-if="imageError"
            class="error"
          >
            {{ imageError }}
          </p>
        </form>
      </article><article
        id="detection-model"
        class="card detection-model-card"
      >
        <h2>输出检测模型（可选）</h2>
        <p class="privacy-note">
          启用后，当 filter 插件在标准化文本中命中敏感词但原文未命中时，会把原文和触发词交给该模型进行语境判断。若未启用，标准化命中将直接拦截。
        </p>
        <form
          class="model-form"
          @submit.prevent="saveDetectionModel"
        >
          <label class="checkbox-label"><input
            v-model="detectionForm.enabled"
            type="checkbox"
          > 启用检测模型</label>
          <label>Base URL<input
            v-model.trim="detectionForm.base_url"
            type="url"
            required
          ></label>
          <label>检测模型<input
            v-model.trim="detectionForm.name"
            :required="detectionForm.enabled"
            placeholder="例如 gpt-4o-mini"
          ></label>
          <label>请求超时（秒）<input
            v-model.number="detectionForm.timeout_seconds"
            type="number"
            min="1"
            max="300"
            step="1"
            required
          ></label>
          <label>API Key <span>{{ config?.detection_model.api_key_saved_locally ? '已保存；留空则保留' : '仅保存在本机配置文件中' }}</span><input
            v-model="detectionApiKey"
            type="password"
            autocomplete="new-password"
            :required="detectionForm.enabled && !config?.detection_model.api_key_saved_locally && !detectionForm.clear_api_key"
            placeholder="sk-..."
          ></label>
          <label
            v-if="config?.detection_model.api_key_saved_locally"
            class="checkbox-label"
          ><input
            v-model="detectionForm.clear_api_key"
            type="checkbox"
          > 清除已保存的检测 API Key</label>
          <div class="form-actions">
            <button
              class="primary"
              :disabled="detectionSaving"
            >
              {{ detectionSaving ? '保存中…' : '保存检测模型' }}
            </button><p
              v-if="detectionNotice"
              class="success"
            >
              {{ detectionNotice }}
            </p>
          </div>
          <p
            v-if="detectionError"
            class="error"
          >
            {{ detectionError }}
          </p>
        </form>
      </article>
      <article class="card">
        <h2>Embedding 模型</h2>
        <p class="privacy-note">
          默认启用并由这里的 Embedding 模型统一服务长期记忆和群体表达。模型名或 API Key 尚未配置时自动保持本地召回；配置完成后，记忆正文、群体表达“情境+风格”及各自查询会发送到该服务。
        </p>
        <form
          class="model-form"
          @submit.prevent="saveEmbedding"
        >
          <label class="checkbox-label"><input
            v-model="embeddingForm.enabled"
            type="checkbox"
          > 启用向量检索</label>
          <label>Base URL<input
            v-model.trim="embeddingForm.base_url"
            type="url"
            required
          ></label>
          <label>Embedding 模型<input
            v-model.trim="embeddingForm.name"
            :required="embeddingForm.enabled"
            placeholder="例如 text-embedding-3-small"
          ></label>
          <label>请求超时（秒）<input
            v-model.number="embeddingForm.timeout_seconds"
            type="number"
            min="1"
            max="300"
            step="1"
            required
          ></label>
          <label>API Key <span>{{ config?.embedding.api_key_saved_locally ? '已保存；留空则保留' : '仅保存在本机配置文件中' }}</span><input
            v-model="embeddingApiKey"
            type="password"
            autocomplete="new-password"
            :required="embeddingForm.enabled && !config?.embedding.api_key_saved_locally && !embeddingForm.clear_api_key"
            placeholder="sk-..."
          ></label>
          <label
            v-if="config?.embedding.api_key_saved_locally"
            class="checkbox-label"
          ><input
            v-model="embeddingForm.clear_api_key"
            type="checkbox"
          > 清除已保存的 embedding API Key</label>
          <div class="form-actions">
            <button
              class="primary"
              :disabled="embeddingSaving"
            >
              {{ embeddingSaving ? '保存中…' : '保存向量检索配置' }}
            </button><p
              v-if="embeddingNotice"
              class="success"
            >
              {{ embeddingNotice }}
            </p>
          </div>
          <p
            v-if="embeddingError"
            class="error"
          >
            {{ embeddingError }}
          </p>
        </form>
      </article>
      <article class="card">
        <h2>本地用户数据</h2>
        <p class="privacy-note">
          该操作会删除全部 Session、身份、黑名单、聊天正文、长期知识、运行审计、上传与人格图片。项目人格名称与 Prompt、场景指派、模型/插件配置、API 凭据、日志和独立备份属于操作者数据，不会随之删除。
        </p>
        <label>输入“删除全部本地用户数据”以确认<input
          v-model="localDataConfirmation"
          autocomplete="off"
          placeholder="删除全部本地用户数据"
        ></label>
        <div class="form-actions">
          <button
            class="danger-button"
            :disabled="deletingLocalData || localDataConfirmation !== '删除全部本地用户数据'"
            :aria-busy="deletingLocalData"
            @click="deleteAllLocalData"
          >
            {{ deletingLocalData ? '删除中…' : '删除全部本地用户数据' }}
          </button>
          <p
            v-if="localDataNotice"
            class="success"
          >
            {{ localDataNotice }}
          </p>
        </div>
        <p
          v-if="localDataError"
          class="error"
        >
          {{ localDataError }}
        </p>
      </article>
    </div>
  </section>
</template>
