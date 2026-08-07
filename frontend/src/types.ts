export type ChatType = 'private' | 'group'
export type ParticipantRole = 'owner' | 'admin' | 'member'
export type ComponentType = 'text' | 'mention' | 'quote' | 'image_ref' | 'audio_ref' | 'file_ref'

export interface Participant {
  external_user_id: string
  display_name: string
  role: ParticipantRole
}

export interface MessageComponent {
  type: ComponentType
  text?: string
  target_id?: string
  target_name?: string
  message_id?: string
  attachment_id?: string
  filename?: string
  mime_type?: string
  size?: number
  sha256?: string
  storage_path?: string
  description?: string
  is_expression?: boolean
}

export interface Session {
  id: string
  platform: string
  account_id: string
  external_chat_id: string
  chat_type: ChatType
  display_name: string
  participants: Participant[]
  revision: number
  created_at: string
  updated_at: string
}

export interface ChatMessage {
  id: string
  session_id: string
  role: 'user' | 'assistant'
  sender_id: string
  sender_name: string
  components: MessageComponent[]
  created_at: string
  origin?: 'reactive' | 'scheduled' | 'proactive' | 'unknown'
  origin_run_id?: string
  source_refs?: string[]
}

export interface Decision {
  id: string
  action: 'reply' | 'silence'
  score: number
  threshold: number
  reason: string
  score_detail: Record<string, unknown>
  created_at: string
  retryable?: boolean
}

export interface ProfileFact {
  id: string
  subject_id: string
  scope_key: string
  category: string
  content: string
  confidence: number
  status: string
  source_message_ids: string[]
}

export type MemoryKind = 'profile' | 'preference' | 'event' | 'episode' | 'commitment' | 'relationship' | 'procedure' | 'summary'
export type MemoryStatus = 'active' | 'conflicted' | 'superseded' | 'retracted'

export interface MemoryRecord {
  id: string
  session_id: string
  scope_key: string
  subject_id?: string
  kind: MemoryKind
  content: string
  confidence: number
  importance: number
  status: MemoryStatus
  source_chain: 'reactive' | 'scheduled' | 'proactive' | 'drift' | 'manual' | 'imported'
  source_run_id?: string
  source_message_ids: string[]
  source_refs: string[]
  supersedes_id?: string
  happened_at?: string
  reinforcement: number
  recall_count: number
  last_recalled_at?: string
  created_at: string
  updated_at: string
}

export interface MemoryConsolidationRun {
  id: string
  session_id: string
  source_chain: MemoryRecord['source_chain']
  status: 'pending' | 'running' | 'completed' | 'failed'
  source_message_ids: string[]
  produced_memory_ids: string[]
  error_code?: string
  error_message?: string
  attempt_count: number
  created_at: string
  updated_at: string
}

export type LearnedItemStatus = 'candidate' | 'active' | 'rejected' | 'disabled'

export interface JargonTerm {
  id: string
  session_id: string
  term: string
  meaning: string
  status: LearnedItemStatus
  confidence: number
  occurrence_count: number
  inference_count: number
  evidence_message_ids: string[]
  last_seen_at: string
  last_inferred_at?: string
  updated_at: string
}

export interface GroupExpressionPattern {
  id: string
  session_id: string
  situation: string
  style: string
  status: LearnedItemStatus
  confidence: number
  occurrence_count: number
  selection_count: number
  evidence_message_ids: string[]
  last_reinforced_at: string
  last_selected_at?: string
  updated_at: string
}

export interface BehaviorPattern {
  id: string
  session_id: string
  scene_summary: string
  scene_tags: string[]
  need_tags: string[]
  other_traits: string[]
  action: string
  expected_outcome: string
  actor_type: 'other_user' | 'group_collective' | 'agent_self' | 'unknown'
  learning_type: 'observed_behavior' | 'self_reflection'
  status: LearnedItemStatus
  confidence: number
  occurrence_count: number
  activation_count: number
  success_count: number
  failure_count: number
  score: number
  evidence_message_ids: string[]
  last_reinforced_at: string
  last_selected_at?: string
  last_feedback_at?: string
  updated_at: string
}

export interface SocialLearningRun {
  id: string
  session_id: string
  source_run_id?: string
  source_message_ids: string[]
  status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled' | 'stale'
  produced_jargon_ids: string[]
  produced_expression_ids: string[]
  produced_behavior_ids: string[]
  error_code?: string
  error_message?: string
  attempt_count: number
  created_at: string
  updated_at: string
}

export interface SocialLearningIndexes {
  expression_clusters: Array<{
    profile_marker: string
    model_name: string
    index_fingerprint: string
    cluster_id: number
    dimension: number
    member_count: number
    updated_at: string
  }>
  behavior_tag_aliases: Array<{
    kind: string
    tag: string
    cluster_key: string
    source_count: number
  }>
  behavior_scene_clusters: Array<{
    scene_cluster_id: string
    tag_distribution: Record<string, number>
    source_count: number
    updated_at: string
  }>
}

export interface PluginGeneration {
  generation: number
  current: boolean
  prepared: boolean
  ready: boolean
  accepting: boolean
  running: boolean
  cleanup_pending: boolean
  lease_count: number
  plugins: string[]
}

export interface PluginGenerationStatus {
  current_generation: number
  generations: PluginGeneration[]
}

export interface OneBotGroupAccessConfig {
  group_id: string
  require_at: boolean
  allow_from: string[]
}

export interface OneBotGroupAccessStatus {
  groups: OneBotGroupAccessConfig[]
  generation?: number
}

export type CapabilityStatus = 'supported' | 'placeholder' | 'unsupported'

export interface PlatformCapability {
  platform: string
  display_name: string
  source: 'builtin' | 'plugin_manifest'
  plugin_id?: string
  enabled: boolean
  ingress: Record<string, CapabilityStatus>
  processing: Record<string, CapabilityStatus>
  prompt_projection: Record<string, CapabilityStatus>
  egress: Record<string, CapabilityStatus>
  limitations: string[]
}

export interface PlatformCapabilityStatus {
  platforms: PlatformCapability[]
}

export interface Persona {
  character_id: string
  name: string
  persona_prompt: string
  prompt_sha256: string
  portrait?: {
    storage_path: string
    filename: string
    sha256: string
    mime_type: string
    size: number
    width?: number
    height?: number
    aspect_valid: boolean
  }
  revision: number
  updated_at: string
}

export interface PersonaCatalog {
  active_character_id: string
  assignments: {
    private: string
    group: string
  }
  personas: Persona[]
}

export interface ModelConfigStatus {
  mode: 'fake' | 'openai'
  protocol: ModelProtocol
  base_url: string
  name: string
  profile_protocol: ModelProtocol
  profile_base_url: string
  profile_name: string
  api_key_configured: boolean
  api_key_saved_locally: boolean
  profile_api_key_configured: boolean
  profile_api_key_saved_locally: boolean
  supports_json_object: boolean
  supports_tools: boolean
  supports_vision: boolean
  supports_streaming: boolean
  task_profiles: Record<string, ModelTaskProfile>
}

export interface ModelTaskProfile {
  models: string[]
  temperature?: number | null
  max_tokens?: number | null
  hard_timeout_seconds?: number | null
  selection_policy: 'primary' | 'ordered_fallback'
}

export interface ConfigStatus {
  server: {
    host: string
    port: number
    control_authentication: 'required' | 'disabled_local_compatibility'
    trusted_hosts: string[]
    trusted_origins: string[]
    event_history_capacity: number
  }
  model: ModelConfigStatus
  vision_model: VisionModelConfigStatus
  image_model: ImageModelConfigStatus
  detection_model: DetectionModelConfigStatus
  embedding: EmbeddingConfigStatus
  chat: Record<string, unknown>
}

export interface VisionModelConfigStatus {
  mode: 'main' | 'external'
  protocol: ModelProtocol
  base_url: string
  name: string
  timeout_seconds: number
  wait_seconds: number
  api_key_configured: boolean
  api_key_saved_locally: boolean
}

export interface EmbeddingConfigStatus {
  enabled: boolean
  available: boolean
  base_url: string
  name: string
  timeout_seconds: number
  api_key_configured: boolean
  api_key_saved_locally: boolean
}

export interface DetectionModelConfigStatus {
  enabled: boolean
  base_url: string
  name: string
  timeout_seconds: number
  api_key_configured: boolean
  api_key_saved_locally: boolean
}

export interface ImageModelConfigStatus {
  enabled: boolean
  base_url: string
  name: string
  timeout_seconds: number
  api_key_configured: boolean
  api_key_saved_locally: boolean
}

export type ModelProtocol = 'openai_chat' | 'openai_responses' | 'anthropic_messages'

export interface ModelConfigInput {
  mode: 'fake' | 'openai'
  protocol: ModelProtocol
  base_url: string
  name: string
  profile_protocol?: ModelProtocol
  profile_base_url: string
  profile_name: string
  api_key?: string
  profile_api_key?: string
  clear_api_key: boolean
  clear_profile_api_key: boolean
  supports_json_object: boolean
  supports_tools: boolean
  supports_vision: boolean
  supports_streaming: boolean
}

export interface ImageModelConfigInput {
  enabled: boolean
  base_url: string
  name: string
  timeout_seconds: number
  api_key?: string
  clear_api_key: boolean
}

export interface VisionModelConfigInput {
  mode: 'main' | 'external'
  protocol: ModelProtocol
  base_url: string
  name: string
  timeout_seconds: number
  wait_seconds: number
  api_key?: string
  clear_api_key: boolean
}

export interface DetectionModelConfigInput {
  enabled: boolean
  base_url: string
  name: string
  timeout_seconds: number
  api_key?: string
  clear_api_key: boolean
}

export interface EmbeddingConfigInput {
  enabled: boolean
  base_url: string
  name: string
  timeout_seconds: number
  api_key?: string
  clear_api_key: boolean
}

export interface ExpressionAsset {
  id: string
  character_id: string
  name: string
  emotion: string
  description: string
  source_kind: 'generated' | 'uploaded' | 'collected'
  source_portrait_sha256?: string
  mime_type: 'image/png'
  size: number
  sha256: string
  width: number
  height: number
  use_count: number
  created_at: string
  last_used_at?: string
}

export interface Schedule {
  id: string
  session_id: string
  created_by: string
  title: string
  instruction: string
  source_text: string
  timezone: string
  dtstart: string
  rrule: string
  status: 'active' | 'paused' | 'completed' | 'deleted'
  revision: number
  next_run_at?: string
  last_run_at?: string
  consecutive_failures: number
  created_at: string
  updated_at: string
}

export interface ScheduleRun {
  id: string
  schedule_id: string
  scheduled_for: string
  status: 'pending' | 'running' | 'completed' | 'failed'
  missed_occurrences: number
  error_code?: string
  error_message?: string
  completed_at?: string
}

export interface ToolExecution {
  id: string
  session_id: string
  tool_name: string
  status: 'running' | 'completed' | 'failed'
  turn_id?: string
  schedule_run_id?: string
  error_message?: string
  started_at: string
  completed_at?: string
}

export interface LogEntry {
  id: string
  ts: string
  level: string
  logger: string
  message: string
  extra?: Record<string, unknown>
  exception?: string
}

export interface ModelAttempt {
  id: string
  invocation_id: string
  attempt_number: number
  task: string
  provider: string
  profile: string
  model: string
  session_id?: string | null
  turn_id?: string | null
  run_id?: string | null
  streamed: boolean
  tool_call_count: number
  input_tokens?: number | null
  output_tokens?: number | null
  total_tokens?: number | null
  usage_source: 'provider' | 'unknown'
  latency_ms: number
  success: boolean
  error_type?: string | null
  error_code?: string | null
  cost_microusd?: number | null
  started_at: string
  completed_at: string
}

export interface ModelAttemptSummary {
  attempt_count: number
  success_count: number
  error_count: number
  usage_unknown_count: number
  input_tokens: number
  output_tokens: number
  total_tokens: number
  average_latency_ms?: number | null
  known_cost_microusd: number
  cost_unknown_count: number
}

export interface EngagementPolicy {
  session_id: string
  proactive_enabled: boolean
  drift_enabled: boolean
  timezone: string
  quiet_start: string
  quiet_end: string
  minimum_interval_minutes: number
  updated_at: string
}

export type GroupParticipationMode = 'silent' | 'normal' | 'focused'

export interface GroupParticipationPolicy {
  session_id: string
  mode: GroupParticipationMode
  trigger_count: number
  frequency_factor: number
  cooldown_seconds: number
  idle_streak: number
  last_ordinary_reply_at: string | null
  last_external_message_at: string | null
  external_interval_ewma_seconds: number | null
  external_interval_sample_count: number
  revision: number
  state_version: number
  updated_at: string
}

export interface FeedSource {
  id: string
  session_id: string
  url: string
  title: string
  enabled: boolean
  poll_interval_minutes: number
  consecutive_failures: number
  next_poll_at?: string
  last_polled_at?: string
  error_code?: string
  error_message?: string
  created_at: string
  updated_at: string
}

export interface ProactiveCandidate {
  id: string
  session_id: string
  source_kind: 'alert' | 'content' | 'context' | 'rss' | 'drift_aggregation' | 'drift_conversation'
  title: string
  summary: string
  url: string
  source_refs: string[]
  parent_candidate_ids: string[]
  status: 'pending' | 'deferred' | 'prepared' | 'skipped' | 'sent' | 'expired' | 'failed'
  decision_reason?: string
  attempt_count: number
  published_at?: string
  created_at: string
  updated_at: string
}

export interface ProactiveRun {
  id: string
  session_id: string
  status: 'running' | 'gated' | 'skipped' | 'prepared' | 'sent' | 'failed' | 'unknown'
  stage: 'gating' | 'judging' | 'composing' | 'preparing' | 'prepared' | 'delivering' | 'finished'
  gate_reason: string
  decision_code: string
  decision_reason: string
  score?: number
  candidate_ids: string[]
  candidate_id?: string
  snapshot_at: string
  snapshot_message_id?: string
  manual_triggered: boolean
  outbound_id?: string
  error_code?: string
  error_message?: string
  created_at: string
  updated_at: string
}

export interface DriftRun {
  id: string
  session_id: string
  status: 'running' | 'completed' | 'gated' | 'paused'
  stage: 'selecting' | 'executing' | 'committing' | 'finished'
  activity: string
  decision_reason: string
  evidence_refs: string[]
  produced_candidate_id?: string
  snapshot_at: string
  snapshot_message_id?: string
  resumed_from_run_id?: string
  auto_resume_count: number
  error_code?: string
  error_message?: string
  created_at: string
  updated_at: string
}

export interface BlacklistEntry {
  id: string
  platform: string
  account_id: string
  external_user_id: string
  display_name: string
  reason: string
  source: 'agent' | 'manual'
  session_id?: string
  created_at: string
}
