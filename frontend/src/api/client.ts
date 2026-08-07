import type {
  BlacklistEntry,
  BehaviorPattern,
  ChatMessage,
  ConfigStatus,
  Decision,
  DetectionModelConfigInput,
  EmbeddingConfigInput,
  ExpressionAsset,
  EngagementPolicy,
  FeedSource,
  GroupExpressionPattern,
  GroupParticipationPolicy,
  ImageModelConfigInput,
  JargonTerm,
  LogEntry,
  MemoryConsolidationRun,
  MemoryKind,
  MemoryRecord,
  MessageComponent,
  ModelAttempt,
  ModelAttemptSummary,
  ModelConfigInput,
  OneBotGroupAccessConfig,
  OneBotGroupAccessStatus,
  VisionModelConfigInput,
  Persona,
  PersonaCatalog,
  PluginGenerationStatus,
  PlatformCapabilityStatus,
  ProactiveCandidate,
  ProactiveRun,
  ProfileFact,
  Schedule,
  ScheduleRun,
  Session,
  SocialLearningIndexes,
  SocialLearningRun,
  DriftRun,
  ToolExecution,
} from '../types'

export class ApiError extends Error {
  constructor(message: string, public code = 'request_failed', public details?: unknown) {
    super(message)
  }
}

const CONTROL_TOKEN_HANDOFF_KEY = 'ija.control_token'

let controlSessionEstablished = false

/** 控制面会话是否已通过 bootstrapControlSession 恢复；未恢复前不应请求 /api/* 或建立事件流。 */
export function isControlSessionEstablished(): boolean {
  return controlSessionEstablished
}

function markControlSessionEstablished(): void {
  controlSessionEstablished = true
}

/**
 * 从显式本机登录链接或 sessionStorage 接收 Token，换成 HttpOnly 控制会话。
 *
 * URL fragment 不会发给服务端，但仍可能被截图或复制，因此必须在首次请求前同步清除。
 */
export async function bootstrapControlSession(): Promise<void> {
  const fragment = new URLSearchParams(window.location.hash.slice(1))
  const fragmentToken = fragment.get('control_token')
  if (fragment.has('control_token')) {
    fragment.delete('control_token')
    const remainingFragment = fragment.toString()
    window.history.replaceState(
      window.history.state,
      '',
      `${window.location.pathname}${window.location.search}${remainingFragment ? `#${remainingFragment}` : ''}`,
    )
  }
  const storedToken = window.sessionStorage.getItem(CONTROL_TOKEN_HANDOFF_KEY)
  if (storedToken) window.sessionStorage.removeItem(CONTROL_TOKEN_HANDOFF_KEY)
  const token = fragmentToken || storedToken
  const response = await fetch('/api/control/session', {
    method: 'POST',
    credentials: 'same-origin',
    headers: token ? { Authorization: `Bearer ${token}` } : undefined,
  })
  if (response.ok) {
    markControlSessionEstablished()
    return
  }
  const data = await response.json().catch(() => undefined)
  throw new ApiError(
    data?.error?.message ?? '控制面会话初始化失败',
    data?.error?.code,
  )
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, {
    ...init,
    credentials: 'same-origin',
    headers: init?.body instanceof FormData ? init.headers : { 'Content-Type': 'application/json', ...init?.headers },
  })
  const data = await response.json()
  if (!response.ok) {
    throw new ApiError(
      data?.error?.message ?? '请求失败',
      data?.error?.code,
      data?.details ?? data?.error?.details,
    )
  }
  return data as T
}

export const api = {
  sessions: () => request<Session[]>('/sessions'),
  createSession: (payload: object) => request<Session>('/simulations/sessions', { method: 'POST', body: JSON.stringify(payload) }),
  deleteSession: (id: string) => request<Record<string, unknown>>(`/sessions/${encodeURIComponent(id)}`, {
    method: 'DELETE',
  }),
  clearSessionChat: (id: string) => request<Record<string, unknown>>(`/sessions/${encodeURIComponent(id)}/messages`, {
    method: 'DELETE',
  }),
  clearAllChat: () => request<Record<string, unknown>>('/messages', {
    method: 'DELETE',
  }),
  deleteAllLocalData: (confirmation: string) => request<Record<string, unknown>>('/local-data', {
    method: 'DELETE',
    body: JSON.stringify({ confirmation }),
  }),
  messages: (id: string) => request<ChatMessage[]>(`/sessions/${id}/messages`),
  searchMessages: (id: string, query: string, limit = 100) => request<ChatMessage[]>(
    `/sessions/${encodeURIComponent(id)}/messages/search?query=${encodeURIComponent(query)}&limit=${limit}`,
  ),
  messageContext: (
    sessionId: string,
    messageId: string,
    before = 50,
    after = 50,
  ) => request<ChatMessage[]>(
    `/sessions/${encodeURIComponent(sessionId)}/messages/${encodeURIComponent(messageId)}/context?before=${before}&after=${after}`,
  ),
  decisions: (id: string) => request<Decision[]>(`/sessions/${id}/decisions`),
  retryReply: (sessionId: string, turnId: string) => request<{ status: string }>(`/sessions/${encodeURIComponent(sessionId)}/turns/${encodeURIComponent(turnId)}/retry-reply`, {
    method: 'POST',
  }),
  send: (id: string, payload: object) => request(`/simulations/sessions/${id}/messages`, { method: 'POST', body: JSON.stringify(payload) }),
  saveMembers: (id: string, payload: object) => request<Session>(`/simulations/sessions/${id}/members`, { method: 'PUT', body: JSON.stringify(payload) }),
  upload: async (file: File) => {
    const form = new FormData()
    form.append('file', file)
    return request<MessageComponent>('/uploads', { method: 'POST', body: form })
  },
  profiles: () => request<ProfileFact[]>('/profiles'),
  memories: (sessionId?: string) => request<MemoryRecord[]>(`/memories${sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : ''}`),
  recallMemories: (sessionId: string, query: string) => request<MemoryRecord[]>(`/sessions/${encodeURIComponent(sessionId)}/memories/recall?query=${encodeURIComponent(query)}`),
  createMemory: (sessionId: string, payload: { content: string; kind: MemoryKind; subject_id?: string; importance: number }) => request<MemoryRecord>(`/sessions/${encodeURIComponent(sessionId)}/memories`, {
    method: 'POST', body: JSON.stringify(payload),
  }),
  forgetMemory: (memory: MemoryRecord, reason: string) => request<MemoryRecord>(`/sessions/${encodeURIComponent(memory.session_id)}/memories/${encodeURIComponent(memory.id)}/forget`, {
    method: 'POST', body: JSON.stringify({ reason }),
  }),
  correctMemory: (memory: MemoryRecord, correctedContent: string, reason: string) => request<MemoryRecord>(`/sessions/${encodeURIComponent(memory.session_id)}/memories/${encodeURIComponent(memory.id)}/correct`, {
    method: 'POST', body: JSON.stringify({ corrected_content: correctedContent, reason }),
  }),
  clearSessionMemories: (sessionId: string) => request<Record<string, unknown>>(`/sessions/${encodeURIComponent(sessionId)}/memories`, {
    method: 'DELETE',
  }),
  clearAllMemories: () => request<Record<string, unknown>>('/memories', {
    method: 'DELETE',
  }),
  memoryConsolidations: (sessionId?: string) => request<MemoryConsolidationRun[]>(`/memory-consolidations${sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : ''}`),
  retryMemoryConsolidation: (runId: string) => request<MemoryConsolidationRun>(`/memory-consolidations/${encodeURIComponent(runId)}/retry`, {
    method: 'POST',
  }),
  learnedJargons: (sessionId: string) => request<JargonTerm[]>(
    `/sessions/${encodeURIComponent(sessionId)}/learning/jargons`,
  ),
  learnedExpressions: (sessionId: string) => request<GroupExpressionPattern[]>(
    `/sessions/${encodeURIComponent(sessionId)}/learning/expressions`,
  ),
  learnedBehaviors: (sessionId: string) => request<BehaviorPattern[]>(
    `/sessions/${encodeURIComponent(sessionId)}/learning/behaviors`,
  ),
  socialLearningIndexes: (sessionId: string) => request<SocialLearningIndexes>(
    `/sessions/${encodeURIComponent(sessionId)}/learning/indexes`,
  ),
  socialLearningRuns: (sessionId: string) => request<SocialLearningRun[]>(
    `/social-learning-runs?session_id=${encodeURIComponent(sessionId)}`,
  ),
  persona: () => request<Persona>('/persona'),
  personas: () => request<PersonaCatalog>('/personas'),
  activatePersona: (characterId: string) => request<Persona>(
    `/personas/${encodeURIComponent(characterId)}/activate`,
    { method: 'POST' },
  ),
  savePersonaAssignments: (assignments: { private: string, group: string }) => request<{ private: string, group: string }>('/personas/assignments', {
    method: 'PUT',
    body: JSON.stringify(assignments),
  }),
  savePersona: (persona: Persona) => request<Persona>(`/persona?character_id=${encodeURIComponent(persona.character_id)}`, {
    method: 'PUT',
    body: JSON.stringify({
      name: persona.name,
      persona_prompt: persona.persona_prompt,
      expected_revision: persona.revision,
    }),
  }),
  savePersonaPortrait: async (characterId: string, file: File, revision: number, cropToNineSixteen = false) => {
    const form = new FormData()
    form.append('file', file)
    return request<Persona>(`/persona/portrait?expected_revision=${revision}&crop_to_nine_sixteen=${cropToNineSixteen}&character_id=${encodeURIComponent(characterId)}`, {
      method: 'POST', body: form,
    })
  },
  deletePersonaPortrait: (characterId: string, revision: number) => request<Persona>(`/persona/portrait?character_id=${encodeURIComponent(characterId)}`, {
    method: 'DELETE', body: JSON.stringify({ expected_revision: revision }),
  }),
  config: () => request<ConfigStatus>('/config/status'),
  pluginGenerations: () => request<PluginGenerationStatus>('/platform-plugins/generations'),
  pluginCapabilities: () => request<PlatformCapabilityStatus>('/platform-plugins/capabilities'),
  oneBotGroupAccess: () => request<OneBotGroupAccessStatus>('/platform-plugins/onebot/groups'),
  saveOneBotGroupAccess: (groups: OneBotGroupAccessConfig[]) => request<OneBotGroupAccessStatus>('/platform-plugins/onebot/groups', {
    method: 'PUT',
    body: JSON.stringify({ groups }),
  }),
  saveModelConfig: (payload: ModelConfigInput) => request<ConfigStatus>('/config/model', {
    method: 'PUT',
    body: JSON.stringify(payload),
  }),
  saveImageModelConfig: (payload: ImageModelConfigInput) => request<ConfigStatus>('/config/image-model', {
    method: 'PUT',
    body: JSON.stringify(payload),
  }),
  saveDetectionModelConfig: (payload: DetectionModelConfigInput) => request<ConfigStatus>('/config/detection-model', {
    method: 'PUT',
    body: JSON.stringify(payload),
  }),
  saveEmbeddingConfig: (payload: EmbeddingConfigInput) => request<ConfigStatus>('/config/embedding', {
    method: 'PUT', body: JSON.stringify(payload),
  }),
  expressions: (characterId?: string) => request<ExpressionAsset[]>(`/expressions${characterId ? `?character_id=${encodeURIComponent(characterId)}` : ''}`),
  deleteExpression: (id: string, characterId?: string) => request<ExpressionAsset>(`/expressions/${encodeURIComponent(id)}${characterId ? `?character_id=${encodeURIComponent(characterId)}` : ''}`, {
    method: 'DELETE',
  }),
  saveVisionModelConfig: (payload: VisionModelConfigInput) => request<ConfigStatus>('/config/vision-model', {
    method: 'PUT',
    body: JSON.stringify(payload),
  }),
  uploadExpression: async (file: File, name: string, emotion: string, characterId?: string) => {
    const form = new FormData()
    form.append('file', file)
    form.append('name', name)
    form.append('emotion', emotion)
    return request<ExpressionAsset>(`/expressions${characterId ? `?character_id=${encodeURIComponent(characterId)}` : ''}`, { method: 'POST', body: form })
  },
  renameExpression: (id: string, name: string, characterId?: string) => request<ExpressionAsset>(`/expressions/${encodeURIComponent(id)}${characterId ? `?character_id=${encodeURIComponent(characterId)}` : ''}`, {
    method: 'PATCH',
    body: JSON.stringify({ name }),
  }),
  probe: () => request<Record<string, unknown>>('/model/probe', { method: 'POST' }),
  schedules: (sessionId?: string) => request<Schedule[]>(`/schedules${sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : ''}`),
  scheduleRuns: (id: string) => request<ScheduleRun[]>(`/schedules/${id}/runs`),
  saveSchedule: (schedule: Schedule) => request<Schedule>(`/schedules/${schedule.id}`, {
    method: 'PUT',
    body: JSON.stringify({
      title: schedule.title,
      instruction: schedule.instruction,
      source_text: schedule.source_text,
      timezone: schedule.timezone,
      dtstart: schedule.dtstart,
      rrule: schedule.rrule,
      expected_revision: schedule.revision,
    }),
  }),
  scheduleAction: (id: string, action: 'pause' | 'resume', revision: number) => request<Schedule>(`/schedules/${id}/${action}`, {
    method: 'POST', body: JSON.stringify({ expected_revision: revision }),
  }),
  runSchedule: (id: string) => request<ScheduleRun>(`/schedules/${id}/run-now`, { method: 'POST' }),
  deleteSchedule: (id: string, revision: number) => request<Schedule>(`/schedules/${id}`, {
    method: 'DELETE', body: JSON.stringify({ expected_revision: revision }),
  }),
  toolExecutions: (sessionId: string) => request<ToolExecution[]>(`/tool-executions?session_id=${encodeURIComponent(sessionId)}`),
  engagementPolicy: (sessionId: string) => request<EngagementPolicy>(`/sessions/${encodeURIComponent(sessionId)}/engagement-policy`),
  saveEngagementPolicy: (policy: EngagementPolicy) => request<EngagementPolicy>(`/sessions/${encodeURIComponent(policy.session_id)}/engagement-policy`, {
    method: 'PUT',
    body: JSON.stringify({
      proactive_enabled: policy.proactive_enabled,
      drift_enabled: policy.drift_enabled,
      timezone: policy.timezone,
      quiet_start: policy.quiet_start,
      quiet_end: policy.quiet_end,
      minimum_interval_minutes: policy.minimum_interval_minutes,
    }),
  }),
  groupParticipationPolicy: (sessionId: string) => request<GroupParticipationPolicy>(`/sessions/${encodeURIComponent(sessionId)}/group-participation-policy`),
  saveGroupParticipationPolicy: (policy: GroupParticipationPolicy) => request<GroupParticipationPolicy>(`/sessions/${encodeURIComponent(policy.session_id)}/group-participation-policy`, {
    method: 'PUT',
    body: JSON.stringify({
      mode: policy.mode,
      trigger_count: policy.trigger_count,
      frequency_factor: policy.frequency_factor,
      cooldown_seconds: policy.cooldown_seconds,
      expected_revision: policy.revision,
    }),
  }),
  feeds: (sessionId: string) => request<FeedSource[]>(`/sessions/${encodeURIComponent(sessionId)}/feeds`),
  createFeed: (sessionId: string, payload: { url: string; title: string; poll_interval_minutes: number }) => request<FeedSource>(`/sessions/${encodeURIComponent(sessionId)}/feeds`, {
    method: 'POST', body: JSON.stringify(payload),
  }),
  saveFeed: (feed: FeedSource) => request<FeedSource>(`/sessions/${encodeURIComponent(feed.session_id)}/feeds/${encodeURIComponent(feed.id)}`, {
    method: 'PUT',
    body: JSON.stringify({
      url: feed.url,
      title: feed.title,
      enabled: feed.enabled,
      poll_interval_minutes: feed.poll_interval_minutes,
    }),
  }),
  deleteFeed: (feed: FeedSource) => request<FeedSource>(`/sessions/${encodeURIComponent(feed.session_id)}/feeds/${encodeURIComponent(feed.id)}`, {
    method: 'DELETE',
  }),
  refreshFeed: (feed: FeedSource) => request<FeedSource>(`/sessions/${encodeURIComponent(feed.session_id)}/feeds/${encodeURIComponent(feed.id)}/refresh`, {
    method: 'POST',
  }),
  proactiveCandidates: (sessionId: string) => request<ProactiveCandidate[]>(`/sessions/${encodeURIComponent(sessionId)}/proactive/candidates`),
  proactiveRuns: (sessionId: string) => request<ProactiveRun[]>(`/sessions/${encodeURIComponent(sessionId)}/proactive/runs`),
  runProactive: (sessionId: string) => request<ProactiveRun>(`/sessions/${encodeURIComponent(sessionId)}/proactive/run-now`, { method: 'POST' }),
  driftRuns: (sessionId: string) => request<DriftRun[]>(`/sessions/${encodeURIComponent(sessionId)}/drift/runs`),
  runDrift: (sessionId: string) => request<DriftRun>(`/sessions/${encodeURIComponent(sessionId)}/drift/run-now`, { method: 'POST' }),
  logs: (params?: { level?: string; logger_name?: string; limit?: number }) => {
    const query = new URLSearchParams()
    if (params?.level) query.set('level', params.level)
    if (params?.logger_name) query.set('logger_name', params.logger_name)
    if (params?.limit) query.set('limit', String(params.limit))
    const suffix = query.toString() ? `?${query.toString()}` : ''
    return request<{ items: LogEntry[] }>(`/logs${suffix}`)
  },
  modelAttempts: (params?: { session_id?: string; task?: string; limit?: number }) => {
    const query = new URLSearchParams()
    if (params?.session_id) query.set('session_id', params.session_id)
    if (params?.task) query.set('task', params.task)
    if (params?.limit) query.set('limit', String(params.limit))
    const suffix = query.toString() ? `?${query.toString()}` : ''
    return request<{ items: ModelAttempt[] }>(`/model-attempts${suffix}`)
  },
  modelAttemptSummary: (params?: { session_id?: string; task?: string }) => {
    const query = new URLSearchParams()
    if (params?.session_id) query.set('session_id', params.session_id)
    if (params?.task) query.set('task', params.task)
    const suffix = query.toString() ? `?${query.toString()}` : ''
    return request<ModelAttemptSummary>(`/model-attempts/summary${suffix}`)
  },
  blacklist: () => request<BlacklistEntry[]>('/blacklist'),
  createBlacklist: (payload: {
    platform: string
    account_id: string
    external_user_id: string
    display_name: string
    reason: string
    session_id?: string
  }) => request<BlacklistEntry>('/blacklist', { method: 'POST', body: JSON.stringify(payload) }),
  deleteBlacklist: (id: string) => request<BlacklistEntry>(`/blacklist/${encodeURIComponent(id)}`, {
    method: 'DELETE',
  }),
}
