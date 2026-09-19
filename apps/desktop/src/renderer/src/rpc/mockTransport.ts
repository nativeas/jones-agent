import type { RpcCallResult, RpcTransport } from './transport'
import type {
  Agent,
  CapabilityListResult,
  Message,
  PermissionRequest,
  Project,
  Provider,
  QueueItem,
  Session,
  SessionMode,
  SkillEntry,
  Step,
  TerminationCard
} from '../domain/types'

/**
 * In-memory fake daemon implementing the subset of RPC v0 (00-foundation.md §4)
 * this UI calls, plus a small scripted turn simulator so `session.send` produces
 * believable streaming output. Used by `pnpm dev:mock` (no daemon/Electron main
 * needed) and by vitest (01-w2-interfaces.md §5: "MockTransport（vitest 与
 * pnpm dev:mock 用）").
 *
 * Deterministic by construction: every async step goes through the injectable
 * `schedule` function (default: a real `setTimeout`, for a believable dev-mode
 * feel) — tests pass `schedule: (fn) => fn()` to run the whole scripted turn
 * synchronously, with no fake timers and no flakiness.
 *
 * Text-prefix commands let a developer (or a test) reach the three termination
 * kinds and a permission prompt without scripting a custom backend:
 *   `/error <reason>`      → run.terminated kind:"error"
 *   `/budget <reason>`     → run.terminated kind:"budget"
 *   `/permission <reason>` → emits permission.requested and waits for
 *                            permission.decide before continuing/terminating
 * Anything else completes normally (see `runScriptedTurn`).
 */

let idCounter = 0
function nextId(prefix: string): string {
  idCounter += 1
  return `${prefix}_mock_${idCounter}`
}

const MAIN_SESSION_ID = 'session_main'
const DEFAULT_PROJECT_ID = 'proj_default'
const DEFAULT_AGENT_ID = 'agent_default'

interface MockTransportOptions {
  schedule?: (fn: () => void) => void
}

export class MockTransport implements RpcTransport {
  private projects: Project[] = [
    { id: DEFAULT_PROJECT_ID, path: '~', name: '默认工作区' }
  ]
  private agents: Agent[] = [
    {
      id: DEFAULT_AGENT_ID,
      project_id: null,
      name: '默认 Agent',
      persona: '',
      tone: '',
      principles: '',
      tool_allowlist: [],
      skills: [],
      model_pref: null
    }
  ]
  private sessions: Session[] = [
    {
      id: MAIN_SESSION_ID,
      project_id: DEFAULT_PROJECT_ID,
      agent_id: DEFAULT_AGENT_ID,
      parent_id: null,
      is_main: true,
      mode: 'task',
      title: '主会话',
      status: 'idle'
    }
  ]
  private messages = new Map<string, Message[]>([[MAIN_SESSION_ID, []]])
  private steps = new Map<string, Step[]>() // keyed by run_id
  private queue = new Map<string, QueueItem[]>([[MAIN_SESSION_ID, []]])
  private permissions = new Map<string, PermissionRequest>()
  private permissionWaiters = new Map<string, (decision: 'allow' | 'deny') => void>()
  private providers: Provider[] = [
    { provider: 'anthropic', has_key: false, key_hint: null, default_model: null },
    { provider: 'openai', has_key: false, key_hint: null, default_model: null },
    { provider: 'deepseek', has_key: false, key_hint: null, default_model: null },
    { provider: 'qwen', has_key: false, key_hint: null, default_model: null },
    { provider: 'gemini', has_key: false, key_hint: null, default_model: null },
    { provider: 'ollama', has_key: false, key_hint: null, default_model: null }
  ]
  private settings: Record<string, unknown> = {}

  private listeners = new Map<string, Set<(params: unknown) => void>>()
  private schedule: (fn: () => void) => void

  constructor(options: MockTransportOptions = {}) {
    this.schedule = options.schedule ?? ((fn) => setTimeout(fn, 30))
  }

  on(method: string, callback: (params: unknown) => void): () => void {
    let set = this.listeners.get(method)
    if (!set) {
      set = new Set()
      this.listeners.set(method, set)
    }
    set.add(callback)
    return () => set!.delete(callback)
  }

  private emit(method: string, params: unknown): void {
    this.listeners.get(method)?.forEach((cb) => cb(params))
  }

  async call<T = unknown>(method: string, params: Record<string, unknown> = {}): Promise<RpcCallResult<T>> {
    try {
      const result = await this.dispatch(method, params)
      return { ok: true, result: result as T }
    } catch (err) {
      return { ok: false, message: err instanceof Error ? err.message : String(err) }
    }
  }

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  private async dispatch(method: string, params: Record<string, any>): Promise<unknown> {
    switch (method) {
      case 'daemon.ping':
        return { version: '0.0.0-mock', pid: 0, uptime_s: 0 }
      case 'daemon.status':
        return { sessions_active: this.sessions.filter((s) => s.status === 'running').length, workers: 0, memory_mb: 0 }

      case 'project.list':
        return this.projects
      case 'project.create': {
        const project: Project = { id: nextId('proj'), path: params.path, name: params.path.split('/').pop() || params.path }
        this.projects.push(project)
        return project
      }
      case 'project.delete': {
        this.projects = this.projects.filter((p) => p.id !== params.id)
        return { id: params.id }
      }

      case 'agent.list':
        return this.agents
      case 'agent.get': {
        const agent = this.agents.find((a) => a.id === params.id)
        if (!agent) throw new Error('agent not found')
        return agent
      }
      case 'agent.upsert': {
        const existingIdx = this.agents.findIndex((a) => a.id === params.id)
        const agent: Agent = {
          id: params.id ?? nextId('agent'),
          project_id: params.project_id ?? null,
          name: params.name ?? '未命名 Agent',
          persona: params.persona ?? '',
          tone: params.tone ?? '',
          principles: params.principles ?? '',
          tool_allowlist: params.tool_allowlist ?? [],
          skills: params.skills ?? [],
          model_pref: params.model_pref ?? null
        }
        if (existingIdx >= 0) this.agents[existingIdx] = agent
        else this.agents.push(agent)
        return agent
      }
      case 'agent.delete':
        this.agents = this.agents.filter((a) => a.id !== params.id)
        return { id: params.id }

      // The mock has no real per-connection routing — every `on()` listener
      // already receives every notification — so subscribe/unsubscribe are
      // accepted no-ops here purely so callers following RPC v0 §4.2 (always
      // subscribe before expecting session-scoped notifications) don't get an
      // unimplemented-method error.
      case 'session.subscribe':
      case 'session.unsubscribe':
        return {}

      case 'session.list':
        return params.project_id ? this.sessions.filter((s) => s.project_id === params.project_id) : this.sessions
      case 'session.create': {
        const parent = params.parent_id ? this.sessions.find((s) => s.id === params.parent_id) : undefined
        const session: Session = {
          id: nextId('session'),
          project_id: params.project_id,
          agent_id: params.agent_id,
          parent_id: params.parent_id ?? null,
          is_main: false,
          mode: params.mode ?? parent?.mode ?? 'task',
          title: params.title ?? '新会话',
          status: 'idle'
        }
        this.sessions.push(session)
        this.messages.set(session.id, [])
        this.queue.set(session.id, [])
        return session
      }
      case 'session.get': {
        // 00-foundation.md §4.1: `session.get` returns Session + 最近 Turn —
        // the "turn" here is trimmed to the one field chatStore's replay-on-
        // rebind path needs (run_id), since this mock has no separate turns
        // table to model the rest of it.
        const session = this.requireSession(params.id)
        const runId = this.activeRuns.get(session.id) ?? null
        return { ...session, messages: this.messages.get(session.id) ?? [], turn: runId ? { run_id: runId } : null }
      }
      case 'session.set_mode': {
        const session = this.requireSession(params.id)
        session.mode = params.mode as SessionMode
        return session
      }
      case 'session.send':
        return this.handleSend(params.id, params.text)
      case 'session.stop':
        return this.handleStop(params.id)
      case 'session.queue':
        return this.queue.get(params.id) ?? []
      case 'session.queue_remove': {
        const items = (this.queue.get(params.id) ?? []).filter((q) => q.id !== params.item_id)
        this.queue.set(params.id, items)
        this.emit('queue.changed', { session_id: params.id, items })
        return items
      }
      case 'session.queue_reorder': {
        const items = this.queue.get(params.id) ?? []
        const order = params.order as string[]
        const reordered = order
          .map((id, idx) => {
            const item = items.find((q) => q.id === id)
            return item ? { ...item, position: idx } : null
          })
          .filter((x): x is QueueItem => x !== null)
        this.queue.set(params.id, reordered)
        this.emit('queue.changed', { session_id: params.id, items: reordered })
        return reordered
      }

      case 'turn.messages':
        return this.messages.get(params.session_id) ?? []
      case 'run.steps':
        return this.steps.get(params.run_id) ?? []
      case 'run.get':
        return { id: params.run_id, steps: this.steps.get(params.run_id) ?? [] }

      case 'permission.pending':
        return Array.from(this.permissions.values()).filter(
          (p) => !params.session_id || p.session_id === params.session_id
        )
      case 'permission.decide': {
        const request = this.permissions.get(params.request_id)
        if (!request) throw new Error('permission request not found')
        this.permissions.delete(params.request_id)
        const decision = { request_id: params.request_id, decision: params.decision, remember: params.remember }
        this.emit('permission.decided', decision)
        this.permissionWaiters.get(params.request_id)?.(params.decision)
        this.permissionWaiters.delete(params.request_id)
        return decision
      }

      case 'provider.list':
        return this.providers
      case 'provider.set_key': {
        const provider = this.providers.find((p) => p.provider === params.provider)
        if (!provider) throw new Error('unknown provider')
        provider.has_key = true
        provider.key_hint = String(params.key).slice(-4)
        return provider
      }
      case 'provider.delete_key': {
        const provider = this.providers.find((p) => p.provider === params.provider)
        if (!provider) throw new Error('unknown provider')
        provider.has_key = false
        provider.key_hint = null
        return provider
      }
      case 'model.list':
        return this.mockModels(params.provider)

      case 'settings.get':
        return this.settings
      case 'settings.set':
        this.settings = { ...this.settings, ...params.patch }
        return this.settings

      // skill.list / capability.list: neither is real daemon behavior yet in
      // this dev-mode mock (skill.list is this branch's own new RPC;
      // capability.list is H/#17, not landed) — static, believable data so
      // the Capabilities settings tab is demoable via `pnpm dev:mock` without
      // a real daemon (03-w4-interfaces.md §5's transparency page).
      case 'skill.list':
        return { skills: this.mockSkills() }
      case 'capability.list':
        return this.mockCapabilities()

      default:
        throw new Error(`mock transport: 未实现的方法 ${method}`)
    }
  }

  private requireSession(id: string): Session {
    const session = this.sessions.find((s) => s.id === id)
    if (!session) throw new Error('session not found')
    return session
  }

  private mockSkills(): SkillEntry[] {
    return [
      {
        name: 'research',
        description: '带引用的深度调研（示例数据，见 mockTransport.ts）',
        tier: 'builtin',
        source_path: '<bundled>/research/SKILL.md',
        valid: true,
        error: null
      },
      {
        name: 'weekly-report',
        description: '整理本周 Run 生成周报',
        tier: 'user',
        source_path: '~/.jones/skills/weekly-report/SKILL.md',
        valid: true,
        error: null
      }
    ]
  }

  private mockCapabilities(): CapabilityListResult {
    return {
      tools: [
        { name: 'read_file', source: 'builtin', enabled: true, actually_loaded: true },
        { name: 'write_file', source: 'builtin', enabled: true, actually_loaded: true },
        { name: 'terminal', source: 'builtin', enabled: true, actually_loaded: true },
        {
          name: 'kanban_create',
          source: 'builtin',
          enabled: false,
          hidden_reason: 'not_in_allowlist',
          actually_loaded: false
        },
        { name: 'research', source: 'skill', enabled: true, actually_loaded: true }
      ],
      drift: []
    }
  }

  private mockModels(provider?: string): Array<{ provider: string; id: string; label: string }> {
    const table: Record<string, string[]> = {
      anthropic: ['claude-sonnet-4-5', 'claude-opus-4-1'],
      openai: ['gpt-5', 'gpt-5-mini'],
      deepseek: ['deepseek-chat'],
      qwen: ['qwen-max'],
      gemini: ['gemini-2.5-pro'],
      ollama: ['llama3']
    }
    const providers = provider ? [provider] : Object.keys(table)
    return providers.flatMap((p) => (table[p] ?? []).map((id) => ({ provider: p, id, label: id })))
  }

  private handleSend(sessionId: string, text: string): { turn_id: string; queued: boolean } {
    const session = this.requireSession(sessionId)
    if (session.status === 'running') {
      const items = this.queue.get(sessionId) ?? []
      const item: QueueItem = {
        id: nextId('queue'),
        session_id: sessionId,
        text,
        position: items.length,
        state: 'pending'
      }
      const next = [...items, item]
      this.queue.set(sessionId, next)
      this.emit('queue.changed', { session_id: sessionId, items: next })
      return { turn_id: item.id, queued: true }
    }
    const turnId = nextId('turn')
    this.startTurn(session, turnId, text)
    return { turn_id: turnId, queued: false }
  }

  private handleStop(sessionId: string): { stopped: boolean } {
    const session = this.requireSession(sessionId)
    if (session.status !== 'running') return { stopped: false }
    const runId = this.activeRunId(sessionId)
    this.cancelledRuns.add(runId)
    this.activeRuns.delete(sessionId)
    session.status = 'idle'
    const card: TerminationCard = { run_id: runId, kind: 'user', reason: '用户手动停止', card: {} }
    this.emit('run.terminated', card)
    return { stopped: true }
  }

  private activeRuns = new Map<string, string>() // session_id -> run_id
  /** run_ids a stop()/error/budget termination already ended — any scripted
   * continuation still scheduled for one (a pending setTimeout chunk, or a
   * permission wait) must no-op instead of re-emitting events for a run that's
   * already terminated (PRD 9.3: a terminated run must never look alive). */
  private cancelledRuns = new Set<string>()

  private activeRunId(sessionId: string): string {
    return this.activeRuns.get(sessionId) ?? nextId('run')
  }

  private startTurn(session: Session, turnId: string, text: string): void {
    session.status = 'running'
    const runId = nextId('run')
    this.activeRuns.set(session.id, runId)
    this.steps.set(runId, [])

    const userMessage: Message = {
      id: nextId('msg'),
      session_id: session.id,
      turn_id: turnId,
      role: 'user',
      content: { kind: 'text', text },
      seq: (this.messages.get(session.id)?.length ?? 0) + 1
    }
    this.pushMessage(session.id, userMessage)
    this.emit('turn.started', { session_id: session.id, turn_id: turnId, run_id: runId })

    const [command, ...rest] = text.trim().split(/\s+/)
    const reason = rest.join(' ') || '(未提供原因)'

    if (command === '/error') {
      this.schedule(() => this.finishWithTermination(session, runId, 'error', reason))
      return
    }
    if (command === '/budget') {
      this.schedule(() => this.finishWithTermination(session, runId, 'budget', reason))
      return
    }
    if (command === '/permission') {
      this.schedule(() => this.requestPermissionThenContinue(session, runId, turnId, reason))
      return
    }

    this.runScriptedReply(session, runId, turnId)
  }

  private pushMessage(sessionId: string, message: Message): void {
    const list = this.messages.get(sessionId) ?? []
    list.push(message)
    this.messages.set(sessionId, list)
  }

  private finishWithTermination(session: Session, runId: string, kind: 'error' | 'budget', reason: string): void {
    session.status = 'idle'
    this.activeRuns.delete(session.id)
    const card: TerminationCard =
      kind === 'error'
        ? { run_id: runId, kind, reason, card: { error_message: reason } }
        : { run_id: runId, kind, reason, card: { budget: { name: 'session_tokens', used: 100000, limit: 100000, unit: 'tokens' } } }
    this.emit('run.terminated', card)
  }

  private requestPermissionThenContinue(session: Session, runId: string, turnId: string, reason: string): void {
    const requestId = nextId('perm')
    const request: PermissionRequest = {
      request_id: requestId,
      session_id: session.id,
      gate: 'user',
      risk: 'high',
      reasons: [reason],
      tool_call: { title: reason, kind: 'execute' }
    }
    this.permissions.set(requestId, request)
    this.emit('permission.requested', request)
    this.permissionWaiters.set(requestId, (decision) => {
      if (this.cancelledRuns.has(runId)) return // the run was stopped while the approval was pending
      if (decision === 'allow') {
        this.runScriptedReply(session, runId, turnId)
      } else {
        this.finishWithTermination(session, runId, 'error', '用户在审批面板拒绝了该动作')
      }
    })
  }

  private readonly replyChunks = ['好的，', '我先看一下相关文件，', '再告诉你结论。']

  private runScriptedReply(session: Session, runId: string, turnId: string): void {
    const messageId = nextId('msg')
    const assistantMessage: Message = {
      id: messageId,
      session_id: session.id,
      turn_id: turnId,
      role: 'assistant',
      content: { kind: 'text', text: '' },
      seq: (this.messages.get(session.id)?.length ?? 0) + 1,
      streaming: true
    }
    this.pushMessage(session.id, assistantMessage)

    const step: Step = {
      id: nextId('step'),
      run_id: runId,
      seq: 1,
      tool: 'read_file',
      args_summary: 'path=README.md',
      result_summary: null,
      duration_ms: null,
      status: 'running'
    }
    this.steps.get(runId)!.push(step)
    this.emit('step.started', step)

    let i = 0
    const emitNextChunk = (): void => {
      if (this.cancelledRuns.has(runId)) {
        this.cancelledRuns.delete(runId) // consumed: this continuation is the one being cancelled
        return
      }
      if (i === 0) {
        const completedStep: Step = { ...step, status: 'completed', result_summary: '12 行', duration_ms: 40 }
        this.steps.set(
          runId,
          (this.steps.get(runId) ?? []).map((s) => (s.id === step.id ? completedStep : s))
        )
        this.emit('step.completed', completedStep)
      }
      const chunk = this.replyChunks[i]
      if (chunk === undefined) {
        assistantMessage.streaming = false
        this.emit('message.completed', { ...assistantMessage })
        session.status = 'idle'
        this.activeRuns.delete(session.id)
        this.maybeSendNextQueued(session)
        return
      }
      assistantMessage.content = { ...assistantMessage.content, text: assistantMessage.content.text + chunk }
      this.emit('message.delta', { session_id: session.id, turn_id: turnId, message_id: messageId, delta: chunk })
      i += 1
      this.schedule(emitNextChunk)
    }
    this.schedule(emitNextChunk)
  }

  private maybeSendNextQueued(session: Session): void {
    const items = this.queue.get(session.id) ?? []
    const next = items[0]
    if (!next) return
    const remaining = items.slice(1).map((item, idx) => ({ ...item, position: idx }))
    this.queue.set(session.id, remaining)
    this.emit('queue.changed', { session_id: session.id, items: remaining })
    const turnId = nextId('turn')
    this.startTurn(session, turnId, next.text)
  }
}
