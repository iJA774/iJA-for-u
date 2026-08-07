<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ApiError, api } from '../api/client'
import type { ExpressionAsset, Persona } from '../types'

const persona = ref<Persona>()
const availablePersonas = ref<Persona[]>([])
const selectedCharacterId = ref('')
const privateCharacterId = ref('')
const groupCharacterId = ref('')
const notice = ref('')
const busy = ref(false)
const expressions = ref<ExpressionAsset[]>([])
const pendingCropFile = ref<File>()
const mismatchSize = ref<{ width?: number, height?: number }>()
const showUpload = ref(false)
const uploadFile = ref<File>()
const uploadName = ref('')
const uploadEmotion = ref('')
const uploadError = ref('')
const renamingId = ref<string>()
const renameValue = ref('')
const renameError = ref('')
const portraitUrl = computed(() => (
  persona.value?.portrait
    ? `/api/persona/portrait?character_id=${encodeURIComponent(persona.value.character_id)}&sha256=${persona.value.portrait.sha256}`
    : ''
))

async function loadExpressions() {
  expressions.value = await api.expressions(persona.value?.character_id)
}

function replacePersona(updated: Persona) {
  persona.value = updated
  availablePersonas.value = availablePersonas.value.map(
    item => item.character_id === updated.character_id ? updated : item,
  )
}

onMounted(async () => {
  try {
    const catalog = await api.personas()
    availablePersonas.value = catalog.personas
    selectedCharacterId.value = catalog.active_character_id
    privateCharacterId.value = catalog.assignments.private
    groupCharacterId.value = catalog.assignments.group
    persona.value = catalog.personas.find(
      item => item.character_id === catalog.active_character_id,
    )
    if (!persona.value) throw new Error('当前人格不在可用列表中')
    await loadExpressions()
  } catch (error) {
    notice.value = error instanceof ApiError ? error.message : '人格加载失败'
  }
})

async function switchPersona() {
  if (
    !persona.value
    || !selectedCharacterId.value
    || selectedCharacterId.value === persona.value.character_id
    || busy.value
  ) return
  busy.value = true
  try {
    const selected = availablePersonas.value.find(
      item => item.character_id === selectedCharacterId.value,
    )
    if (!selected) throw new Error('所选人格不在可用列表中')
    persona.value = {
      ...selected,
      portrait: selected.portrait ? { ...selected.portrait } : undefined,
    }
    pendingCropFile.value = undefined
    mismatchSize.value = undefined
    await loadExpressions()
    notice.value = `正在编辑「${selected.name}」`
  } catch (error) {
    selectedCharacterId.value = persona.value.character_id
    notice.value = error instanceof ApiError ? error.message : '人格切换失败'
  } finally {
    busy.value = false
  }
}

async function saveAssignments() {
  if (!privateCharacterId.value || !groupCharacterId.value || busy.value) return
  busy.value = true
  try {
    const saved = await api.savePersonaAssignments({
      private: privateCharacterId.value,
      group: groupCharacterId.value,
    })
    privateCharacterId.value = saved.private
    groupCharacterId.value = saved.group
    notice.value = '私聊与群聊人格指派已保存'
  } catch (error) {
    notice.value = error instanceof ApiError ? error.message : '人格指派保存失败'
  } finally {
    busy.value = false
  }
}

async function save() {
  if (!persona.value || busy.value) return
  busy.value = true
  try {
    replacePersona(await api.savePersona(persona.value))
    notice.value = `人格已保存，revision ${persona.value.revision}`
  } catch (error) {
    notice.value = error instanceof ApiError ? error.message : '人格保存失败'
  } finally {
    busy.value = false
  }
}

async function uploadPortrait(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file || !persona.value || busy.value) return
  await savePortraitFile(file, false)
  input.value = ''
}

async function savePortraitFile(file: File, cropToNineSixteen: boolean) {
  if (!persona.value || busy.value) return
  busy.value = true
  try {
    replacePersona(
      await api.savePersonaPortrait(
        persona.value.character_id,
        file,
        persona.value.revision,
        cropToNineSixteen,
      ),
    )
    pendingCropFile.value = undefined
    mismatchSize.value = undefined
    await loadExpressions()
    notice.value = `形象已更新，revision ${persona.value.revision}`
  } catch (error) {
    if (error instanceof ApiError && error.code === 'portrait_aspect_ratio_mismatch') {
      pendingCropFile.value = file
      mismatchSize.value = (error.details ?? {}) as { width?: number, height?: number }
      notice.value = error.message
    } else {
      notice.value = error instanceof ApiError ? error.message : '形象上传失败'
    }
  } finally {
    busy.value = false
  }
}

async function cropPendingPortrait() {
  if (pendingCropFile.value) await savePortraitFile(pendingCropFile.value, true)
}

function chooseAgain() {
  pendingCropFile.value = undefined
  mismatchSize.value = undefined
  notice.value = '请选择一张 9:16 图片。'
}

async function removePortrait() {
  if (!persona.value?.portrait || busy.value) return
  busy.value = true
  try {
    replacePersona(
      await api.deletePersonaPortrait(
        persona.value.character_id,
        persona.value.revision,
      ),
    )
    await loadExpressions()
    notice.value = `形象已移除，revision ${persona.value.revision}`
  } catch (error) {
    notice.value = error instanceof ApiError ? error.message : '形象移除失败'
  } finally {
    busy.value = false
  }
}

async function deleteExpression(asset: ExpressionAsset) {
  if (busy.value) return
  busy.value = true
  try {
    await api.deleteExpression(asset.id, persona.value?.character_id)
    await loadExpressions()
    notice.value = `已删除表情「${asset.name}」；历史消息中的图片不受影响。`
  } catch (error) {
    notice.value = error instanceof ApiError ? error.message : '删除表情失败'
  } finally {
    busy.value = false
  }
}

function pickExpressionFile(event: Event) {
  const input = event.target as HTMLInputElement
  uploadFile.value = input.files?.[0]
}

async function submitUpload() {
  if (!uploadFile.value || !uploadName.value.trim() || !uploadEmotion.value.trim() || busy.value) return
  busy.value = true
  uploadError.value = ''
  try {
    await api.uploadExpression(
      uploadFile.value,
      uploadName.value.trim(),
      uploadEmotion.value.trim(),
      persona.value?.character_id,
    )
    await loadExpressions()
    showUpload.value = false
    uploadFile.value = undefined
    uploadName.value = ''
    uploadEmotion.value = ''
    notice.value = '表情已上传，Agent 现在可以复用该表情。'
  } catch (error) {
    uploadError.value = error instanceof ApiError ? error.message : '表情上传失败'
  } finally {
    busy.value = false
  }
}

function startRename(asset: ExpressionAsset) {
  renamingId.value = asset.id
  renameValue.value = asset.name
  renameError.value = ''
}

function cancelRename() {
  renamingId.value = undefined
  renameValue.value = ''
  renameError.value = ''
}

async function confirmRename(asset: ExpressionAsset) {
  if (busy.value || !renameValue.value.trim()) return
  const newName = renameValue.value.trim()
  if (newName === asset.name) {
    cancelRename()
    return
  }
  busy.value = true
  renameError.value = ''
  try {
    await api.renameExpression(asset.id, newName, persona.value?.character_id)
    await loadExpressions()
    renamingId.value = undefined
    renameValue.value = ''
    notice.value = `表情已重命名为「${newName}」`
  } catch (error) {
    renameError.value = error instanceof ApiError ? error.message : '重命名失败'
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <section class="page">
    <header class="page-head">
      <div>
        <p class="eyebrow">
          PERSONA
        </p><h1>自定义人格</h1><p>名称与形象属于角色身份；人格提示词只定义这个角色如何表达。</p>
      </div><button
        class="primary"
        :disabled="busy || !persona"
        :aria-busy="busy"
        @click="save"
      >
        {{ busy ? '保存中…' : '保存名称与提示词' }}
      </button>
    </header>
    <section
      v-if="availablePersonas.length"
      class="persona-assignments card"
    >
      <div>
        <p class="eyebrow">
          CHAT PERSONAS
        </p>
        <h2>聊天场景人格</h2>
        <p class="form-note">
          私聊和群聊会分别使用这里指派的人格；下方编辑不会自动改变指派。
        </p>
      </div>
      <label>私聊人格<select
        v-model="privateCharacterId"
        :disabled="busy"
      >
        <option
          v-for="item in availablePersonas"
          :key="`private-${item.character_id}`"
          :value="item.character_id"
        >
          {{ item.name }}（{{ item.character_id }}）
        </option>
      </select></label>
      <label>群聊人格<select
        v-model="groupCharacterId"
        :disabled="busy"
      >
        <option
          v-for="item in availablePersonas"
          :key="`group-${item.character_id}`"
          :value="item.character_id"
        >
          {{ item.name }}（{{ item.character_id }}）
        </option>
      </select></label>
      <button
        class="primary"
        :disabled="busy || !privateCharacterId || !groupCharacterId"
        @click="saveAssignments"
      >
        保存场景指派
      </button>
    </section>
    <div
      v-if="persona"
      class="persona-layout"
    >
      <div class="persona-form card">
        <div class="persona-switcher">
          <label>当前编辑人格<select
            v-model="selectedCharacterId"
            :disabled="busy"
          >
            <option
              v-for="item in availablePersonas"
              :key="item.character_id"
              :value="item.character_id"
            >
              {{ item.name }}（{{ item.character_id }}）
            </option>
          </select></label>
          <button
            class="ghost"
            :disabled="busy || selectedCharacterId === persona.character_id"
            :aria-busy="busy && selectedCharacterId !== persona.character_id"
            @click="switchPersona"
          >
            {{ busy && selectedCharacterId !== persona.character_id ? '切换中…' : '切换编辑对象' }}
          </button>
        </div>
        <div class="portrait-editor">
          <div class="portrait-preview portrait-nine-sixteen">
            <img
              v-if="portraitUrl"
              :src="portraitUrl"
              :alt="`${persona.name}的形象`"
            >
            <span v-else>尚未上传形象</span>
          </div>
          <div class="portrait-actions">
            <label class="file-button">上传图片<input
              type="file"
              accept="image/png,image/jpeg,image/gif,image/webp"
              :disabled="busy"
              @change="uploadPortrait"
            ></label>
            <button
              v-if="persona.portrait"
              class="ghost"
              :disabled="busy"
              :aria-busy="busy"
              @click="removePortrait"
            >
              {{ busy ? '处理中…' : '移除形象' }}
            </button>
            <small>必须为精确 9:16；GIF 取首帧，保存后统一转为 base_image.png。</small>
          </div>
        </div>
        <div
          v-if="pendingCropFile"
          class="crop-warning"
        >
          <strong>图片比例不是 9:16</strong>
          <p>原始尺寸：{{ mismatchSize?.width ?? '?' }} × {{ mismatchSize?.height ?? '?' }}。可以重新选择，或确认自动居中裁剪。</p>
          <div>
            <button
              class="ghost"
              @click="chooseAgain"
            >
              重新选择
            </button><button
              class="primary"
              :disabled="busy"
              @click="cropPendingPortrait"
            >
              自动居中裁剪
            </button>
          </div>
        </div>
        <label>人物名称<input
          v-model="persona.name"
          maxlength="80"
        ></label>
        <label>人格提示词<textarea
          v-model="persona.persona_prompt"
          class="persona-prompt"
          maxlength="20000"
        /></label>
        <p class="form-note">
          character_id: {{ persona.character_id }} · {{ notice || `当前 revision ${persona.revision}` }}
        </p>
      </div>
      <section class="expression-library card">
        <header>
          <div>
            <p class="eyebrow">
              EXPRESSIONS
            </p><h2>表情图库</h2>
          </div>
          <div class="expression-header-actions">
            <strong>{{ expressions.length }} / 200</strong>
            <button
              class="ghost"
              :disabled="busy"
              @click="showUpload = !showUpload"
            >
              {{ showUpload ? '取消上传' : '上传表情' }}
            </button>
          </div>
        </header>
        <div
          v-if="showUpload"
          class="expression-upload-form"
        >
          <label>名称<input
            v-model.trim="uploadName"
            maxlength="40"
            placeholder="如：微笑"
            :disabled="busy"
          ></label>
          <label>情绪<input
            v-model.trim="uploadEmotion"
            maxlength="120"
            placeholder="如：开心、害羞"
            :disabled="busy"
          ></label>
          <label class="file-button">选择图片<input
            type="file"
            accept="image/png,image/jpeg,image/gif,image/webp"
            :disabled="busy"
            @change="pickExpressionFile"
          ></label>
          <span
            v-if="uploadFile"
            class="form-note"
          >{{ uploadFile.name }}</span>
          <button
            class="primary"
            :disabled="busy || !uploadFile || !uploadName || !uploadEmotion"
            @click="submitUpload"
          >
            {{ busy ? '上传中…' : '确认上传' }}
          </button>
          <p
            v-if="uploadError"
            class="error"
          >
            {{ uploadError }}
          </p>
          <p class="form-note">
            图片会去除元数据并转为静态 PNG，不限制宽高比；手动上传和聊天收集表情不会因形象替换而清空。
          </p>
        </div>
        <div
          v-if="expressions.length"
          class="expression-grid"
        >
          <article
            v-for="asset in expressions"
            :key="asset.id"
            class="expression-card"
          >
            <img
              :src="`/api/expressions/${encodeURIComponent(asset.id)}/image?character_id=${encodeURIComponent(persona.character_id)}&sha256=${asset.sha256}`"
              :alt="asset.name"
            >
            <div>
              <template v-if="renamingId === asset.id">
                <input
                  v-model="renameValue"
                  class="rename-input"
                  maxlength="40"
                  :disabled="busy"
                  @keyup.enter="confirmRename(asset)"
                >
                <div class="rename-actions">
                  <button
                    class="ghost"
                    :disabled="busy"
                    @click="cancelRename"
                  >
                    取消
                  </button><button
                    class="primary small"
                    :disabled="busy || !renameValue.trim()"
                    @click="confirmRename(asset)"
                  >
                    确认
                  </button>
                </div>
                <p
                  v-if="renameError"
                  class="error"
                >
                  {{ renameError }}
                </p>
              </template>
              <template v-else>
                <strong>{{ asset.name }}</strong><span>{{ asset.emotion }}</span><small>{{ asset.source_kind === 'collected' ? '聊天收集' : asset.source_kind === 'uploaded' ? '手动上传' : '角色生成' }} · 已发送 {{ asset.use_count }} 次</small>
                <div class="expression-card-actions">
                  <button
                    class="ghost"
                    :disabled="busy"
                    @click="startRename(asset)"
                  >
                    重命名
                  </button><button
                    class="danger-link"
                    :disabled="busy"
                    @click="deleteExpression(asset)"
                  >
                    删除
                  </button>
                </div>
              </template>
            </div>
          </article>
        </div>
        <p
          v-else
          class="empty"
        >
          暂无可复用表情。可手动上传，或由 Agent 首次需要时生成。
        </p>
      </section>
    </div>
  </section>
</template>
