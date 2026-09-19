import { create } from 'zustand'
import type { RpcTransport } from '../rpc/transport'
import type { Message, PermissionRequest, QueueItem, Step, TerminationCard } from '../domain/types'
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

  bindSession(transport: RpcTransport, sessionId: string): Promise<void>
  unbindSession(): void
  send(text: string): Promise<{ queued: boolean } | null>
  stop(): Promise<void>
  removeQueueItem(itemId: string): Promise<void>
  reorderQueue(orderedIds: string[]): Promise<void>
  decidePermission(
    requestId: string,
    decision: 'allow' | 'deny',
    remember?: 'session' | 'project'
  ): Promise<void>
}

function upsertTimeline(timeline: TimelineEntry[], entry: TimelineEntry): TimelineEntry[] {
  const key = entryKey(entry)
  const idx = timeline.findIndex((e) => entryKey(e) === key)
  if (idx === -1) return [...timeline, entry]
  const next = timeline.slice()
  next[idx] = entry
  return next
}

export const useChatStore = create<ChatState>()((set, get) => ({
  transport: null,
  activeSessionId: null,
  timeline: [],
  queue: [],
  pendingPermissions: [],
  running: false,
  error: null,
  runToSession: new Map(),
  unsubscribers: [],
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
            content: text,
            seq: timeline.length,
            streaming: true
          }
          timeline = [...timeline, { kind: 'message', message: placeholder }]
        } else {
          const entry = timeline[idx] as { kind: 'message'; message: Message }
          const updated = { ...entry.message, content: entry.message.content + text }
          timeline = timeline.slice()
          timeline[idx] = { kind: 'message', message: updated }
        }
      }
      return { timeline }
    })
  }),

  async bindSession(transport, sessionId) {
    get().unbindSession()
    set({
      transport,
      activeSessionId: sessionId,
      timeline: [],
      queue: [],
      pendingPermissions: [],
      running: false,
      error: null,
      runToSession: new Map()
    })

    await transport.call('session.subscribe', { id: sessionId })

    const isActive = (): boolean => get().activeSessionId === sessionId
    const sessionOf = (runId: string): string | undefined => get().runToSession.get(runId)

    const unsubscribers = [
      transport.on('turn.started', (raw) => {
        const params = raw as { session_id: string; turn_id: string; run_id: string }
        if (params.session_id !== sessionId) return
        set((state) => ({
          running: true,
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
        const params = raw as { session_id: string; items: QueueItem[] }
        if (params.session_id !== sessionId) return
        set({ queue: params.items })
      }),
      // permission.requested's PermissionRequest is assumed to carry session_id
      // directly (see chatStore module doc / report: real shape TBD by A's
      // Session/Worker module, not yet built as of this branch).
      transport.on('permission.requested', (raw) => {
        const request = raw as PermissionRequest
        if (request.session_id !== sessionId) return
        set((state) => ({ pendingPermissions: [...state.pendingPermissions, request] }))
      }),
      transport.on('permission.decided', (raw) => {
        const decision = raw as { request_id: string }
        set((state) => ({
          pendingPermissions: state.pendingPermissions.filter((p) => p.id !== decision.request_id)
        }))
      })
    ]
    set({ unsubscribers })

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
      void transport.call('session.unsubscribe', { id: activeSessionId })
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
        content: trimmed,
        seq: get().timeline.length
      }
      set((state) => ({ timeline: upsertTimeline(state.timeline, { kind: 'message', message }) }))
    }
    return res.result
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

  async decidePermission(requestId, decision, remember) {
    const { transport } = get()
    if (!transport) return
    const res = await transport.call('permission.decide', { request_id: requestId, decision, remember })
    if (!res.ok) {
      set({ error: res.message ?? '审批提交失败' })
      return
    }
    set((state) => ({ pendingPermissions: state.pendingPermissions.filter((p) => p.id !== requestId) }))
  }
}))
