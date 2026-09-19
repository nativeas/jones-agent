/**
 * Renderer-side mirror of the domain objects in docs/design/00-foundation.md §5
 * and the RPC v0 envelope in §4. Kept intentionally narrow to the fields the UI
 * actually reads/writes — this is not a 1:1 copy of the SQLite schema.
 */

export type SessionMode = 'chat' | 'task' | 'auto'
export type SessionStatus = 'idle' | 'running' | 'terminated'

export interface Project {
  id: string
  path: string
  name: string
}

export interface Agent {
  id: string
  project_id: string | null
  name: string
  persona: string
  tone: string
  principles: string
  tool_allowlist: string[]
  skills: string[]
  model_pref: { provider?: string; model?: string } | null
}

export interface Session {
  id: string
  project_id: string
  agent_id: string
  parent_id: string | null
  is_main: boolean
  mode: SessionMode
  title: string
  status: SessionStatus
}

export type MessageRole = 'user' | 'assistant' | 'system' | 'tool'

export interface Message {
  id: string
  session_id: string
  turn_id: string
  role: MessageRole
  content: string
  seq: number
  /** true while a message is still receiving `message.delta` notifications. */
  streaming?: boolean
}

export type StepStatus = 'running' | 'completed' | 'failed'

export interface Step {
  id: string
  run_id: string
  seq: number
  tool: string
  args_summary: string
  result_summary: string | null
  duration_ms: number | null
  status: StepStatus
  permission_id?: string | null
}

export type TerminationKind = 'user' | 'error' | 'budget'

export interface TerminationCard {
  run_id: string
  kind: TerminationKind
  reason: string
  /** Free-form structured detail the three card renderings each read differently
   * (error: step/message; budget: used/limit; user: nothing extra required). */
  card: Record<string, unknown>
}

export type PermissionGate = 'rule' | 'review' | 'user'
export type PermissionRiskLevel = 'low' | 'medium' | 'high'

export interface PermissionRequest {
  id: string
  session_id: string
  gate: PermissionGate
  risk: PermissionRiskLevel
  action_description: string
  tool: string
  args_summary: string
  requested_at: string
}

export type PermissionDecisionValue = 'allow' | 'deny'
export type PermissionRemember = 'session' | 'project'

export interface QueueItem {
  id: string
  session_id: string
  text: string
  position: number
  state: 'pending' | 'sent'
}

export interface Provider {
  provider: string
  has_key: boolean
  key_hint: string | null
  default_model: string | null
}

export interface Model {
  provider: string
  id: string
  label: string
}

/** The six BYOK providers named in PRD 8.1 FR04 / 00-foundation §3 B. */
export const KNOWN_PROVIDERS = ['anthropic', 'openai', 'deepseek', 'qwen', 'gemini', 'ollama'] as const
export type KnownProvider = (typeof KNOWN_PROVIDERS)[number]
