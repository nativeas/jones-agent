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

/** 评审第 3 轮 important：`args_summary: string` never existed on the wire —
 * `steps` table column is `args_json` (`store/migrations/001_init.sql`),
 * `sessions/queries.py::_d()` decodes it under the key `args` into a plain
 * object, never a pre-summarized string (verified against source, same
 * "renderer type vs. daemon `_d()` reality" class of bug #34 was). Same
 * fix as #34: align the type with what daemon actually sends instead of an
 * imagined field. Whether daemon should ALSO send a human-summarized string
 * is a contract decision outside this branch's `domain/types.ts`/`StepCard.tsx`
 * ownership (steps 广播 owned by A/G, see this PR's report) — this only
 * stops the renderer from reading a field that was always `undefined`. */
export interface Step {
  id: string
  run_id: string
  seq: number
  tool: string
  args: Record<string, unknown>
  result_summary: string | null
  duration_ms: number | null
  status: StepStatus
  permission_id?: string | null
}

export type TerminationKind = 'user' | 'error' | 'budget'

/** Fixed 9-value enum, `daemon/src/jones_daemon/errors/classify.py::ErrorKind`'s
 * mirror (Issue #22, 04-w5-interfaces.md §4) — `"user"` is NOT one of these
 * (a user-initiated stop never goes through `ErrorKind`, see that module's
 * `build_user_card`), it's `TerminationCard.kind` at the outer level only. */
export type ErrorKind =
  | 'network'
  | 'provider_auth'
  | 'provider_quota'
  | 'provider_error'
  | 'tool_exception'
  | 'worker_crash'
  | 'approval_timeout'
  | 'budget'
  | 'internal'

export type CardAction = 'retry' | 'switch_model' | 'abandon'

/** `ErrorCard.to_dict()`'s exact shape (04-w5-interfaces.md §4: "{kind, title,
 * message, step_seq?, raw_excerpt(≤2KB, 已脱敏), actions, retryable}") — the
 * `kind="user"` pass-through card `build_user_card()` produces has the same
 * shape (empty `actions`), so this one interface covers every `card.card`
 * value `run.terminated` can carry, no separate "budget card"/"error card"
 * union needed. */
export interface ErrorCard {
  kind: ErrorKind | 'user'
  title: string
  message: string
  step_seq: number | null
  raw_excerpt: string
  actions: CardAction[]
  retryable: boolean
}

export interface TerminationCard {
  run_id: string
  /** Added by Issue #22 (`_terminate_run`'s broadcast, 04-w5-interfaces.md
   * §4) — the only way the "重试/换模型/放弃" actions can name which Turn to
   * act on via `session.retry {id, turn_id, action, model_override?}`. */
  turn_id: string
  kind: TerminationKind
  reason: string
  card: ErrorCard
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
 * `rawInput` is left untyped (`unknown`) because it's a discriminated-by-shape
 * payload, not because it's unread: for the `write_file`/`patch` edit-approval
 * shape (`{"tool","arguments"}`) this app still doesn't decode it — real
 * structured args land there directly from Hermes and decoding it would be
 * genuine shape-guessing (03-w4-interfaces.md §5); but for the plugin-
 * approval-rule shape (`{"command","description"}`, every other escalated
 * tool) `description` carries a `JONES_REVIEW_V1:{...}` payload whose format
 * Jones itself defines (`_review_payload.py`, docs/design/02-w3-interfaces.md
 * §1.2) — `PermissionPanel.tsx`'s `decodeReviewPayload` decodes that one
 * (评审第 3 轮 critical), which is why this field stays `unknown` rather than
 * fully unread. */
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
