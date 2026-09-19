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

/** jones-agent#34: this MUST match what daemon `sessions/queries.py::_d()`
 * actually decodes `messages.content_json` into — `{"kind": "text"|"thinking",
 * "text": "..."}` (see that module and `test_sessions_service.py`'s
 * `m["content"]["text"]` assertions), not a bare string. Rendering the whole
 * object as a React child is exactly the white-screen crash issue #34 files. */
export interface MessageContent {
  kind: 'text' | 'thinking'
  text: string
}

export interface Message {
  id: string
  session_id: string
  turn_id: string
  role: MessageRole
  content: MessageContent
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
/** `unclassified` is real: `sessions/service.py::permission_pending()`'s W2-era
 * snapshot path (pre-dates the W3 review gate) still reports it verbatim for
 * anything already pending when a client asks — see this interface's doc
 * comment for where that was checked against the daemon's actual code. */
export type PermissionRiskLevel = 'low' | 'medium' | 'high' | 'unclassified'

/** The ACP `toolCall` object the request rode in on, forwarded mostly as-is
 * by the daemon (`sessions/service.py`'s `permission.requested` broadcast /
 * `permission_pending()` both do `tool_call = params.get("toolCall") or {}`
 * and pass it straight through) — every field is optional because nothing on
 * the renderer side may assume Hermes populated a given one for a given tool.
 * `rawInput` is intentionally left untyped/unread by this app: per
 * 03-w4-interfaces.md §5 ("不要在 renderer 里猜测解析"), decoding it (it can
 * be either a `{"tool","arguments"}` shape or a plugin-approval-rule shape
 * with the real tool/args packed into `description`, see
 * docs/design/02-w3-interfaces.md §1.2) is daemon-side work that hasn't
 * landed — see this PR's report. */
export interface ToolCallInfo {
  toolCallId?: string
  title?: string
  kind?: string
  rawInput?: unknown
  rawOutput?: unknown
}

/** jones-agent (no filed issue number — found auditing this same "renderer
 * type vs. daemon `_d()`/broadcast reality" class of bug for #34): the actual
 * `permission.requested` / `permission.pending` payload
 * (`sessions/service.py`, verified against source, not assumed) is
 * `{request_id, session_id, gate, risk, reasons?, tool_call, options?}` — it
 * has never had `id`/`action_description`/`tool`/`args_summary`/
 * `requested_at`. This interface previously described an aspirational shape
 * (chatStore.ts used to say so directly: "assumed to carry session_id
 * directly ... real shape TBD"); this branch aligns it with what the daemon
 * on `main` actually sends today. See this PR's report for the daemon-side
 * gap this leaves open (no decoded tool name/args reach the renderer). */
export interface PermissionRequest {
  request_id: string
  session_id: string
  gate: PermissionGate
  risk: PermissionRiskLevel
  reasons?: string[]
  tool_call: ToolCallInfo
  options?: unknown
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

/** `skill.list`'s per-entry shape (00-foundation.md §4.1's row added by this
 * branch; `daemon/src/jones_daemon/skills/service.py::SkillEntry.to_dict()`). */
export type SkillTier = 'project' | 'user' | 'builtin'

export interface SkillEntry {
  name: string
  description: string
  tier: SkillTier
  source_path: string
  valid: boolean
  error: string | null
}

/** `capability.list`'s per-tool shape (03-w4-interfaces.md §2, H/#17 — not yet
 * implemented server-side as of this branch, see rpcMethods.ts's comment on
 * `capability.list`). `hidden_reason` is only ever set for an entry NOT
 * currently enabled; `actually_loaded` is the "real 装配" half of G21 —
 * whether the worker's own tool registry actually has this tool, independent
 * of whether Jones's registry thinks it should. */
export type CapabilitySource = 'builtin' | 'mcp' | 'skill'
export type CapabilityHiddenReason =
  | 'not_in_allowlist'
  | 'denied_by_rule'
  | 'mode_chat'
  | 'mcp_server_down'
  | 'unknown_tool'

export interface CapabilityToolEntry {
  name: string
  source: CapabilitySource
  enabled: boolean
  hidden_reason?: CapabilityHiddenReason
  actually_loaded: boolean
}

export interface CapabilityListResult {
  tools: CapabilityToolEntry[]
  /** Non-empty = G21 failure signal (期望装配 ≠ 实际装配) — the transparency
   * page must show this prominently, not bury it in the table. */
  drift: string[]
}
