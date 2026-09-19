import { create } from 'zustand'
import type { RpcTransport } from '../rpc/transport'
import type {
  CardAction,
  Message,
  PermissionRequest,
  QueueItem,
  Session,
  Step,
  TerminationCard
} from '../domain/types'
import { createDeltaBatcher, type DeltaBatcher } from './deltaBatcher'

export type TimelineEntry =
  | { kind: 'message'; message: Message }
  | { kind: 'step'; step: Step }
  | { kind: 'termination'; card: TerminationCard }

function entryKey(entry: TimelineEntry): string {
  if (entry.kind === 'message') return `message:${entry.message.id}`
  if (entry.kind === 'step') return `step:${entry.step.id}`
  return `termination:${entry.card.run_id}`
}

interface ChatState {
  transport: RpcTransport | null
  activeSessionId: string | null
  timeline: TimelineEntry[]
  queue: QueueItem[]
  /** R-N4 (controller ruling, 2026-09-20; 04-w5-interfaces.md §4.3, PRD 9.3):
   * `null` when the queue is running normally; the terminated Run's outer
   * `kind` ("user"|"error"|"budget") once `queue.changed`'s `suspended:true`
   * arrives — the three termination kinds no longer auto-advance the queue,
   * so `QueuePanel` shows "已暂停" + a "继续" button instead of silently
   * looking like nothing is queued. Cleared by any `queue.changed` broadcast
   * that carries `suspended:false` (a normal advance, `queueResume()`, or
   * `abandonTermination()`'s own clear). */
  queueSuspendedReason: 'user' | 'error' | 'budget' | null
  pendingPermissions: PermissionRequest[]
  running: boolean
  error: string | null
  /** run_id → session_id, learned from `turn.started` — `run.terminated` and the
   * `step.*` notifications don't carry session_id themselves (00-foundation.md
   * §4.2/§5), so this is how the store knows a given run belongs to the session
   * currently bound. */
  runToSession: Map<string, string>
  batcher: DeltaBatcher
  unsubscribers: Array<() => void>
  /** Bumped on every bindSession() call and captured locally by that call —
   * lets a bind that's superseded (user switched sessions again before the
   * first `session.subscribe` round-trip returned) detect it's stale and
   * back out instead of overwriting a newer bind's state/listeners. */
  bindGeneration: number
  /** turn_id currently awaiting a `session.retry` round-trip (round-2 review
   * #6) — guards `retryTermination`/`switchModelTermination`/
   * `abandonTermination` against a second click firing a second real RPC (and
   * a second real Turn/model call) before the first resolves, and lets
   * `TerminationCard` disable its buttons while true. */
  pendingTerminations: Set<string>
  /** turn_id → which action already completed successfully (round-2 review
   * #2/#6) — once a termination card's action has been used, it renders inert
   * (no buttons, a status line instead) rather than staying clickable forever
   * (the bug that let one card spawn unlimited Turns). Round-N2 review #2:
   * also carries `session.retry(action:"abandon")`'s own `cleared_queue_
   * items` count, so the "放弃" status line can say how many (if any)
   * queued instructions actually got cleared instead of always claiming the
   * queue was cleared. */
  handledTerminations: Map<string, { action: CardAction; clearedQueueItems?: number }>

  bindSession(transport: RpcTransport, sessionId: string): Promise<void>
  unbindSession(): void
  send(text: string): Promise<{ queued: boolean } | null>
  /** Issue #22 (04-w5-interfaces.md §4) 错误卡片"重试" — `session.retry
   * {action:"retry"}`，daemon 用原 Turn 的用户消息发起一个新 Turn。 */
  retryTermination(turnId: string): Promise<void>
  /** 错误卡片"换模型" — `session.retry {action:"retry", model_override}`。 */
  switchModelTermination(turnId: string, override: { provider: string; model: string }): Promise<void>
  /** 错误卡片"放弃" — `session.retry {action:"abandon"}`：daemon 清掉该 Session
   * 排队中的后续指令并标记 Run/Turn（04-w5-interfaces.md §4），不只是本地隐藏。 */
  abandonTermination(turnId: string): Promise<void>
  stop(): Promise<void>
  removeQueueItem(itemId: string): Promise<void>
  reorderQueue(orderedIds: string[]): Promise<void>
  /** R-N4 队列面板"继续"按钮 — `session.queue_resume`。 */
  queueResume(): Promise<void>
  decidePermission(
    requestId: string,
    decision: 'allow' | 'deny',
    remember?: 'session' | 'project'
  ): Promise<void>
}

/** The original user message text for `turnId`, read back from the client's
 * own timeline — `session.retry`'s "用户消息复用" (Issue #22) reuses whatever
 * text the daemon already has for that Turn, and the renderer already has the
 * same text locally (it's how the failed Turn got there in the first place),
 * so this avoids a round trip just to echo it back optimistically. */
function findUserMessageText(timeline: TimelineEntry[], turnId: string): string | null {
  for (const entry of timeline) {
    if (entry.kind === 'message' && entry.message.turn_id === turnId && entry.message.role === 'user') {
      return entry.message.content.text
    }
  }
  return null
}

function upsertTimeline(timeline: TimelineEntry[], entry: TimelineEntry): TimelineEntry[] {
  const key = entryKey(entry)
  const idx = timeline.findIndex((e) => entryKey(e) === key)
  if (idx === -1) return [...timeline, entry]
  const next = timeline.slice()
  next[idx] = entry
  return next
}

/** Round-2 review #2: `session.retry`'s `RpcError` text (`turn ... is not in
 * a retryable state (status=...)`, `turn not found`, ...) is an internal
 * state-machine string that was leaking straight into `CenterPane`'s error
 * banner via `res.message` — never meant for an end user, and the main
 * process's `ipcMain.handle('rpc:call', ...)` catch (`apps/desktop/src/main/
 * index.ts`, outside this branch's touch list) drops the RpcError `code`
 * before it reaches this transport, so there is no structured field to switch
 * on here — only substring matching against the known daemon-side messages
 * (`sessions/service.py::retry`). Anything unrecognized falls back to one
 * generic line rather than ever showing the raw text again. */
function friendlyTerminationError(message: string | undefined): string {
  if (message && /not in a retryable state/.test(message)) {
    return '这条错误卡片已经处理过了，请刷新查看最新状态。'
  }
  if (message && /turn not found/.test(message)) {
    return '找不到这条消息了，可能已经被处理。'
  }
  if (message && /session not found/.test(message)) {
    return '会话不存在，请刷新页面。'
  }
  return '操作未成功，请稍后再试。'
}

export const useChatStore = create<ChatState>()((set, get) => ({
  transport: null,
  activeSessionId: null,
  timeline: [],
  queue: [],
  queueSuspendedReason: null,
  pendingPermissions: [],
  running: false,
  error: null,
  runToSession: new Map(),
  unsubscribers: [],
  bindGeneration: 0,
  pendingTerminations: new Set(),
  handledTerminations: new Map(),
  batcher: createDeltaBatcher((updates) => {
    set((state) => {
      let timeline = state.timeline
      for (const { messageId, text } of updates) {
        const idx = timeline.findIndex((e) => e.kind === 'message' && e.message.id === messageId)
        if (idx === -1) {
          const placeholder: Message = {
            id: messageId,
            session_id: state.activeSessionId ?? '',
            turn_id: '',
            role: 'assistant',
            content: { kind: 'text', text },
            seq: timeline.length,
            streaming: true
          }
          timeline = [...timeline, { kind: 'message', message: placeholder }]
        } else {
          const entry = timeline[idx] as { kind: 'message'; message: Message }
          const updated = {
            ...entry.message,
            content: { ...entry.message.content, text: entry.message.content.text + text }
          }
          timeline = timeline.slice()
          timeline[idx] = { kind: 'message', message: updated }
        }
      }
      return { timeline }
    })
  }),

  async bindSession(transport, sessionId) {
    get().unbindSession()
    const generation = get().bindGeneration + 1
    set({
      transport,
      activeSessionId: sessionId,
      timeline: [],
      queue: [],
      queueSuspendedReason: null,
      pendingPermissions: [],
      running: false,
      error: null,
      runToSession: new Map(),
      bindGeneration: generation,
      pendingTerminations: new Set(),
      handledTerminations: new Map()
    })

    const isActive = (): boolean => get().activeSessionId === sessionId && get().bindGeneration === generation

    const subscribeRes = await transport.call('session.subscribe', { id: sessionId })
    if (get().bindGeneration !== generation) {
      // A newer bindSession() call already started while this subscribe was
      // in flight (e.g. two quick clicks in the left pane) — back out rather
      // than register listeners or write state that belongs to that newer
      // call now; otherwise every superseded call here leaks a full set of
      // notification subscriptions forever (01-w2-interfaces.md §5 review).
      //
      // Only unsubscribe when the session that superseded us is a *different*
      // session. If the newer bind is for this same sessionId (React
      // StrictMode's mount→cleanup→mount double-invoke, or the user flipping
      // back to the session they just left before its first subscribe
      // round-trip returned), the newer bind already issued its own
      // subscribe(sessionId) — which, because it fires after ours but before
      // this stale continuation resumes, can complete *before* this
      // unsubscribe reaches the daemon. Sending it anyway would then cancel
      // the newer bind's brand-new subscription instead of the one we own,
      // leaving the pane looking bound while no notification ever arrives
      // again (01-w2-interfaces.md §5 review round 2 — same failure this
      // generation check exists to prevent, via a different path).
      if (get().activeSessionId !== sessionId) {
        transport.call('session.unsubscribe', { id: sessionId }).catch((err) => {
          console.warn('session.unsubscribe (stale bind) failed', err)
        })
      }
      return
    }
    if (!subscribeRes.ok) {
      // §5 review: a failed subscribe must not leave the pane looking bound
      // — no delta/step/termination notification will ever arrive for it.
      set({ error: subscribeRes.message ?? '订阅会话通知失败' })
      return
    }

    const sessionOf = (runId: string): string | undefined => get().runToSession.get(runId)

    const unsubscribers = [
      transport.on('turn.started', (raw) => {
        const params = raw as { session_id: string; turn_id: string; run_id: string }
        if (params.session_id !== sessionId) return
        set((state) => ({
          running: true,
          // R-N4: a Run just started for real — whatever suspended the
          // queue before (this new Turn is exactly the "send a new message"
          // implicit-resume path, or `queueResume()`'s own explicit one)
          // no longer applies; the eventual `queue.changed` broadcast this
          // Turn's own completion fires will re-confirm this, but the panel
          // shouldn't keep showing "已暂停" while something is visibly
          // running.
          queueSuspendedReason: null,
          runToSession: new Map(state.runToSession).set(params.run_id, params.session_id)
        }))
      }),
      transport.on('message.delta', (raw) => {
        const params = raw as { session_id: string; message_id: string; delta: string }
        if (params.session_id !== sessionId || !isActive()) return
        get().batcher.push(params.message_id, params.delta)
      }),
      transport.on('message.completed', (raw) => {
        const message = raw as Message
        if (message.session_id !== sessionId || !isActive()) return
        get().batcher.flushNow()
        set((state) => ({
          timeline: upsertTimeline(state.timeline, { kind: 'message', message: { ...message, streaming: false } }),
          // RPC v0 §4.2 has no separate "turn ended normally" notification — a
          // completed *assistant* message is that signal (the three kinds that
          // do get an explicit notification, user/error/budget, all arrive via
          // run.terminated instead, per PRD 9.3's own "三类终止" — a normal
          // finish isn't one of them).
          running: message.role === 'assistant' ? false : state.running
        }))
      }),
      transport.on('step.started', (raw) => {
        const step = raw as Step
        if (sessionOf(step.run_id) !== sessionId) return
        set((state) => ({ timeline: upsertTimeline(state.timeline, { kind: 'step', step }) }))
      }),
      transport.on('step.completed', (raw) => {
        const step = raw as Step
        if (sessionOf(step.run_id) !== sessionId) return
        set((state) => ({ timeline: upsertTimeline(state.timeline, { kind: 'step', step }) }))
      }),
      transport.on('run.terminated', (raw) => {
        const card = raw as TerminationCard
        if (sessionOf(card.run_id) !== sessionId) return
        get().batcher.flushNow()
        set((state) => ({
          running: false,
          timeline: upsertTimeline(state.timeline, { kind: 'termination', card })
        }))
      }),
      transport.on('queue.changed', (raw) => {
        // R-N4 (04-w5-interfaces.md §4.3): `suspended`/`reason` are only
        // populated by `_advance_queue`/`session.queue_resume`/`abandon`'s
        // own broadcasts (00-foundation.md §4.2's `queue.changed` row) — the
        // other, untouched broadcast points (`session.queue`/`_remove`/
        // `_reorder`, `send()`'s enqueue branch) omit them entirely, so a
        // missing `suspended` here means "no new information", not "no
        // longer suspended" — only an explicit `false` clears it.
        const params = raw as {
          session_id: string
          items: QueueItem[]
          suspended?: boolean
          reason?: 'user' | 'error' | 'budget' | null
        }
        if (params.session_id !== sessionId) return
        set((state) => ({
          queue: params.items,
          queueSuspendedReason:
            params.suspended === undefined
              ? state.queueSuspendedReason
              : params.suspended
                ? (params.reason ?? null)
                : null
        }))
      }),
      // permission.requested carries session_id directly — confirmed against
      // `sessions/service.py`'s real broadcast (domain/types.ts's
      // `PermissionRequest` doc comment has the full shape audit).
      transport.on('permission.requested', (raw) => {
        const request = raw as PermissionRequest
        if (request.session_id !== sessionId) return
        set((state) => ({ pendingPermissions: [...state.pendingPermissions, request] }))
      }),
      transport.on('permission.decided', (raw) => {
        const decision = raw as { request_id: string }
        set((state) => ({
          pendingPermissions: state.pendingPermissions.filter((p) => p.request_id !== decision.request_id)
        }))
      })
    ]
    set({ unsubscribers })

    // Restore in-flight run state. 00-foundation.md §4.1 has `session.get`
    // return the Session + its most recent Turn (which carries run_id), and
    // `run.get`/`run.steps` are explicitly the replay data source — this is
    // what lets rebinding a session whose run kept going while this pane was
    // pointed elsewhere come back looking "running" instead of idle, and lets
    // the run's remaining step/termination notifications (which do arrive —
    // we just resubscribed above) resolve via runToSession instead of being
    // silently dropped as belonging to no known session.
    const sessionRes = await transport.call<Session & { turn?: { run_id: string | null } | null }>('session.get', {
      id: sessionId
    })
    if (isActive() && sessionRes.ok && sessionRes.result) {
      const runId = sessionRes.result.turn?.run_id ?? null
      if (runId) {
        const running = sessionRes.result.status === 'running'
        set((state) => ({ running, runToSession: new Map(state.runToSession).set(runId, sessionId) }))
        const stepsRes = await transport.call<Step[]>('run.steps', { run_id: runId })
        if (isActive() && stepsRes.ok) {
          set((state) => ({
            timeline: (stepsRes.result ?? []).reduce<TimelineEntry[]>(
              (acc, step) => upsertTimeline(acc, { kind: 'step', step }),
              state.timeline
            )
          }))
        }
      }
    }

    const [messagesRes, queueRes, pendingRes] = await Promise.all([
      transport.call<Message[]>('turn.messages', { session_id: sessionId, limit: 200 }),
      transport.call<QueueItem[]>('session.queue', { id: sessionId }),
      transport.call<PermissionRequest[]>('permission.pending', { session_id: sessionId })
    ])
    if (!isActive()) return
    if (!messagesRes.ok) {
      set({ error: messagesRes.message ?? '加载消息失败' })
    } else {
      set((state) => ({
        timeline: (messagesRes.result ?? []).reduce<TimelineEntry[]>(
          (acc, message) => upsertTimeline(acc, { kind: 'message', message }),
          state.timeline
        )
      }))
    }
    if (queueRes.ok) set({ queue: queueRes.result ?? [] })
    if (pendingRes.ok) set({ pendingPermissions: pendingRes.result ?? [] })
  },

  unbindSession() {
    const { unsubscribers, transport, activeSessionId, batcher } = get()
    unsubscribers.forEach((fn) => fn())
    batcher.cancel()
    if (transport && activeSessionId) {
      // Fire-and-forget cleanup — the pane is already torn down, so there's
      // no state left to render an error into — but a rejected call (real
      // windowTransport on a dead/reject-ing IPC round-trip) must still be
      // caught or it becomes an unhandled promise rejection (§7 review).
      transport.call('session.unsubscribe', { id: activeSessionId }).catch((err) => {
        console.warn('session.unsubscribe failed', err)
      })
    }
    set({ unsubscribers: [] })
  },

  async send(text) {
    const { transport, activeSessionId } = get()
    if (!transport || !activeSessionId) return null
    const trimmed = text.trim()
    if (!trimmed) return null
    const res = await transport.call<{ turn_id: string; queued: boolean }>('session.send', {
      id: activeSessionId,
      text: trimmed
    })
    if (!res.ok || !res.result) {
      set({ error: res.message ?? '发送失败' })
      return null
    }
    if (!res.result.queued) {
      // Optimistic local echo: RPC v0 has no "message created" notification for
      // the user's own turn (§4.2 only lists delta/completed for the *assistant*
      // side) — the daemon accepting the call (queued:false) is the only signal
      // that it was actually sent, so we render it immediately rather than wait.
      const message: Message = {
        id: `local_${res.result.turn_id}`,
        session_id: activeSessionId,
        turn_id: res.result.turn_id,
        role: 'user',
        content: { kind: 'text', text: trimmed },
        seq: get().timeline.length
      }
      set((state) => ({ timeline: upsertTimeline(state.timeline, { kind: 'message', message }) }))
    }
    return res.result
  },

  async retryTermination(turnId) {
    const { transport, activeSessionId, timeline, pendingTerminations } = get()
    if (!transport || !activeSessionId) return
    // Round-2 review #6: a second click before the first round-trip resolves
    // (or a second click after `handled` should already have hidden the
    // button, in case some caller ignores that) must not fire a second real
    // Turn — no-op instead of re-entering.
    if (pendingTerminations.has(turnId)) return
    set((state) => ({ pendingTerminations: new Set(state.pendingTerminations).add(turnId) }))
    try {
      const originalText = findUserMessageText(timeline, turnId)
      const res = await transport.call<{ turn_id: string; queued: boolean }>('session.retry', {
        id: activeSessionId,
        turn_id: turnId,
        action: 'retry'
      })
      if (!res.ok || !res.result) {
        set({ error: friendlyTerminationError(res.message) })
        return
      }
      // Same optimistic local echo as send() (see its own comment) — the daemon
      // has no "message created" notification for the user's own turn even when
      // that turn was started by session.retry rather than session.send.
      if (!res.result.queued && originalText) {
        const message: Message = {
          id: `local_${res.result.turn_id}`,
          session_id: activeSessionId,
          turn_id: res.result.turn_id,
          role: 'user',
          content: { kind: 'text', text: originalText },
          seq: get().timeline.length
        }
        set((state) => ({ timeline: upsertTimeline(state.timeline, { kind: 'message', message }) }))
      }
      // Round-2 review #2/#6: mark the ORIGINAL failed turn's card inert —
      // it already spawned a new Turn, retrying it again would spawn another.
      set((state) => ({
        handledTerminations: new Map(state.handledTerminations).set(turnId, { action: 'retry' })
      }))
    } finally {
      set((state) => {
        const next = new Set(state.pendingTerminations)
        next.delete(turnId)
        return { pendingTerminations: next }
      })
    }
  },

  async switchModelTermination(turnId, override) {
    const { transport, activeSessionId, timeline, pendingTerminations } = get()
    if (!transport || !activeSessionId) return
    if (pendingTerminations.has(turnId)) return
    set((state) => ({ pendingTerminations: new Set(state.pendingTerminations).add(turnId) }))
    try {
      const originalText = findUserMessageText(timeline, turnId)
      const res = await transport.call<{ turn_id: string; queued: boolean }>('session.retry', {
        id: activeSessionId,
        turn_id: turnId,
        action: 'retry',
        model_override: override
      })
      if (!res.ok || !res.result) {
        set({ error: friendlyTerminationError(res.message) })
        return
      }
      if (!res.result.queued && originalText) {
        const message: Message = {
          id: `local_${res.result.turn_id}`,
          session_id: activeSessionId,
          turn_id: res.result.turn_id,
          role: 'user',
          content: { kind: 'text', text: originalText },
          seq: get().timeline.length
        }
        set((state) => ({ timeline: upsertTimeline(state.timeline, { kind: 'message', message }) }))
      }
      set((state) => ({
        handledTerminations: new Map(state.handledTerminations).set(turnId, { action: 'switch_model' })
      }))
    } finally {
      set((state) => {
        const next = new Set(state.pendingTerminations)
        next.delete(turnId)
        return { pendingTerminations: next }
      })
    }
  },

  async abandonTermination(turnId) {
    const { transport, activeSessionId, pendingTerminations } = get()
    if (!transport || !activeSessionId) return
    if (pendingTerminations.has(turnId)) return
    set((state) => ({ pendingTerminations: new Set(state.pendingTerminations).add(turnId) }))
    try {
      // Round-N2 review #2: `session.retry(action:"abandon")` returns
      // `cleared_queue_items` (sessions/service.py::retry) — carry it through
      // instead of discarding it, so the status line below can say how many
      // (if any) queued instructions actually got cleared.
      const res = await transport.call<{
        turn_id: string
        action: string
        cleared_queue_items: number
      }>('session.retry', {
        id: activeSessionId,
        turn_id: turnId,
        action: 'abandon'
      })
      if (!res.ok) {
        set({ error: friendlyTerminationError(res.message) })
        return
      }
      // Round-2 review #2: this used to leave the card exactly as it was —
      // with an empty queue (the common case) that's zero visible change, so
      // a user had no way to tell "放弃" actually did anything. Mark it
      // handled so the card swaps its actions for a "已放弃" status line.
      set((state) => ({
        handledTerminations: new Map(state.handledTerminations).set(turnId, {
          action: 'abandon',
          clearedQueueItems: res.result?.cleared_queue_items
        })
      }))
    } finally {
      set((state) => {
        const next = new Set(state.pendingTerminations)
        next.delete(turnId)
        return { pendingTerminations: next }
      })
    }
  },

  async stop() {
    const { transport, activeSessionId } = get()
    if (!transport || !activeSessionId) return
    const res = await transport.call<{ stopped: boolean }>('session.stop', { id: activeSessionId })
    if (!res.ok) set({ error: res.message ?? '停止失败' })
  },

  async removeQueueItem(itemId) {
    const { transport, activeSessionId } = get()
    if (!transport || !activeSessionId) return
    const res = await transport.call<QueueItem[]>('session.queue_remove', {
      id: activeSessionId,
      item_id: itemId
    })
    if (res.ok) set({ queue: res.result ?? [] })
    else set({ error: res.message ?? '撤回失败' })
  },

  async reorderQueue(orderedIds) {
    const { transport, activeSessionId } = get()
    if (!transport || !activeSessionId) return
    const res = await transport.call<QueueItem[]>('session.queue_reorder', {
      id: activeSessionId,
      order: orderedIds
    })
    if (res.ok) set({ queue: res.result ?? [] })
    else set({ error: res.message ?? '调序失败' })
  },

  async queueResume() {
    const { transport, activeSessionId } = get()
    if (!transport || !activeSessionId) return
    const res = await transport.call<{ resumed: boolean; items: QueueItem[] }>(
      'session.queue_resume',
      { id: activeSessionId }
    )
    if (res.ok && res.result) {
      set({ queue: res.result.items, queueSuspendedReason: null })
    } else {
      set({ error: res.message ?? '继续失败' })
    }
  },

  async decidePermission(requestId, decision, remember) {
    const { transport } = get()
    if (!transport) return
    const res = await transport.call('permission.decide', { request_id: requestId, decision, remember })
    if (!res.ok) {
      set({ error: res.message ?? '审批提交失败' })
      return
    }
    set((state) => ({
      pendingPermissions: state.pendingPermissions.filter((p) => p.request_id !== requestId)
    }))
  }
}))
