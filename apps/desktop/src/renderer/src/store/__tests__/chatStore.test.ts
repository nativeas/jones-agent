import { beforeEach, describe, expect, it } from 'vitest'
import { MockTransport } from '../../rpc/mockTransport'
import type { RpcCallResult, RpcTransport } from '../../rpc/transport'
import { useChatStore } from '../chatStore'
import { createDeltaBatcher } from '../deltaBatcher'

const MAIN_SESSION_ID = 'session_main'

function resetStore(): void {
  useChatStore.setState({
    transport: null,
    activeSessionId: null,
    timeline: [],
    queue: [],
    pendingPermissions: [],
    running: false,
    error: null,
    runToSession: new Map(),
    unsubscribers: [],
    bindGeneration: 0,
    // fresh batcher per test so a leftover scheduled flush from a previous
    // test's real rAF/timeout can never leak state into the next one. Only
    // used to drain buffered deltas between tests — the store's own flush
    // logic (message-not-yet-in-timeline case included) is exercised by
    // send()'s end-to-end assertions below, so this stub only needs to avoid
    // throwing if a stray flush lands mid-test.
    batcher: createDeltaBatcher(() => {})
  })
}

describe('chatStore', () => {
  beforeEach(resetStore)

  it('bindSession loads history/queue/pending permissions for that session', async () => {
    const transport = new MockTransport({ schedule: (fn) => fn() })
    await useChatStore.getState().bindSession(transport, MAIN_SESSION_ID)
    expect(useChatStore.getState().activeSessionId).toBe(MAIN_SESSION_ID)
    expect(useChatStore.getState().timeline).toEqual([])
  })

  it('send() runs a full turn to completion: running toggles, assistant message + step land in the timeline', async () => {
    const transport = new MockTransport({ schedule: (fn) => fn() })
    await useChatStore.getState().bindSession(transport, MAIN_SESSION_ID)

    await useChatStore.getState().send('你好')

    const state = useChatStore.getState()
    expect(state.running).toBe(false) // the scripted turn completed synchronously (sync schedule)
    const userMsg = state.timeline.find((e) => e.kind === 'message' && e.message.role === 'user')
    expect(userMsg).toBeDefined()
    const assistantMsg = state.timeline.find((e) => e.kind === 'message' && e.message.role === 'assistant')
    expect(assistantMsg).toBeDefined()
    if (assistantMsg?.kind === 'message') {
      expect(assistantMsg.message.content.length).toBeGreaterThan(0)
      expect(assistantMsg.message.streaming).toBe(false)
    }
    const step = state.timeline.find((e) => e.kind === 'step')
    expect(step).toBeDefined()
    if (step?.kind === 'step') expect(step.step.status).toBe('completed')
  })

  it('send() while running queues instead of starting a second turn', async () => {
    const transport = new MockTransport() // real scheduler: first turn stays in-flight
    await useChatStore.getState().bindSession(transport, MAIN_SESSION_ID)

    const first = await useChatStore.getState().send('第一条')
    expect(first?.queued).toBe(false)
    expect(useChatStore.getState().running).toBe(true)

    const second = await useChatStore.getState().send('第二条')
    expect(second?.queued).toBe(true)
    expect(useChatStore.getState().queue).toHaveLength(1)
    expect(useChatStore.getState().queue[0]?.text).toBe('第二条')
  })

  it('a /error send produces a termination timeline entry and clears `running`', async () => {
    const transport = new MockTransport({ schedule: (fn) => fn() })
    await useChatStore.getState().bindSession(transport, MAIN_SESSION_ID)

    await useChatStore.getState().send('/error 网络中断')

    const state = useChatStore.getState()
    expect(state.running).toBe(false)
    const termination = state.timeline.find((e) => e.kind === 'termination')
    expect(termination).toBeDefined()
    if (termination?.kind === 'termination') {
      expect(termination.card.kind).toBe('error')
      expect(termination.card.reason).toBe('网络中断')
    }
  })

  it('stop() ends the run with a user-kind termination', async () => {
    const transport = new MockTransport() // must still be running when stop() is called
    await useChatStore.getState().bindSession(transport, MAIN_SESSION_ID)
    await useChatStore.getState().send('你好')
    expect(useChatStore.getState().running).toBe(true)

    await useChatStore.getState().stop()

    const state = useChatStore.getState()
    expect(state.running).toBe(false)
    const termination = state.timeline.find((e) => e.kind === 'termination')
    expect(termination?.kind === 'termination' && termination.card.kind).toBe('user')
  })

  it('a /permission send surfaces a pending approval, and deciding it resolves the turn', async () => {
    const transport = new MockTransport({ schedule: (fn) => fn() })
    await useChatStore.getState().bindSession(transport, MAIN_SESSION_ID)

    await useChatStore.getState().send('/permission 危险动作')
    expect(useChatStore.getState().pendingPermissions).toHaveLength(1)
    const requestId = useChatStore.getState().pendingPermissions[0]!.id

    await useChatStore.getState().decidePermission(requestId, 'allow')

    expect(useChatStore.getState().pendingPermissions).toHaveLength(0)
    const assistantMsg = useChatStore.getState().timeline.find((e) => e.kind === 'message' && e.message.role === 'assistant')
    expect(assistantMsg).toBeDefined()
  })

  it('retryLastMessage() resends the most recent user message (PRD 9.3 错误终止卡片 "重试")', async () => {
    const transport = new MockTransport({ schedule: (fn) => fn() })
    await useChatStore.getState().bindSession(transport, MAIN_SESSION_ID)
    await useChatStore.getState().send('/error 网络中断')
    expect(useChatStore.getState().running).toBe(false)

    const result = await useChatStore.getState().retryLastMessage()

    expect(result).not.toBeNull()
    const state = useChatStore.getState()
    const userMessages = state.timeline.filter((e) => e.kind === 'message' && e.message.role === 'user')
    expect(userMessages).toHaveLength(2)
    if (userMessages[1]?.kind === 'message') expect(userMessages[1].message.content).toBe('/error 网络中断')
  })

  it('retryLastMessage() is a no-op when the timeline has no user message yet', async () => {
    const transport = new MockTransport({ schedule: (fn) => fn() })
    await useChatStore.getState().bindSession(transport, MAIN_SESSION_ID)

    const result = await useChatStore.getState().retryLastMessage()

    expect(result).toBeNull()
  })

  it('dismissTermination() removes only the named card (PRD 9.3 错误终止卡片 "放弃" — no server action, just hide it)', async () => {
    const transport = new MockTransport({ schedule: (fn) => fn() })
    await useChatStore.getState().bindSession(transport, MAIN_SESSION_ID)
    await useChatStore.getState().send('/error 第一次出错')
    const firstCard = useChatStore.getState().timeline.find((e) => e.kind === 'termination')
    const firstRunId = firstCard?.kind === 'termination' ? firstCard.card.run_id : undefined
    expect(firstRunId).toBeDefined()

    useChatStore.getState().dismissTermination(firstRunId!)

    expect(useChatStore.getState().timeline.some((e) => e.kind === 'termination')).toBe(false)
  })

  it('removeQueueItem drops the item from the queue panel', async () => {
    const transport = new MockTransport()
    await useChatStore.getState().bindSession(transport, MAIN_SESSION_ID)
    await useChatStore.getState().send('第一条')
    await useChatStore.getState().send('第二条')
    const itemId = useChatStore.getState().queue[0]!.id

    await useChatStore.getState().removeQueueItem(itemId)

    expect(useChatStore.getState().queue).toHaveLength(0)
  })

  it('rebinding a session whose run is still in flight restores `running`, backfills the step timeline via run.steps, and keeps attributing that run\'s later notifications instead of dropping them', async () => {
    // A controllable scheduler: nothing the mock schedules runs until we pop
    // it ourselves, so we can inspect state with a step already started but
    // the turn not yet finished — exactly "switched away mid-run".
    const scheduled: Array<() => void> = []
    const transport = new MockTransport({ schedule: (fn) => scheduled.push(fn) })

    await useChatStore.getState().bindSession(transport, MAIN_SESSION_ID)
    await useChatStore.getState().send('你好')
    expect(useChatStore.getState().running).toBe(true)
    expect(useChatStore.getState().timeline.some((e) => e.kind === 'step')).toBe(true)

    // Simulate switching to another session and back: unbind drops
    // runToSession/timeline, then bindSession() on the same still-running
    // session should reconstruct both from session.get/run.steps rather than
    // coming back looking idle.
    useChatStore.getState().unbindSession()
    await useChatStore.getState().bindSession(transport, MAIN_SESSION_ID)

    const afterRebind = useChatStore.getState()
    expect(afterRebind.running).toBe(true)
    const stepAfterRebind = afterRebind.timeline.find((e) => e.kind === 'step')
    expect(stepAfterRebind).toBeDefined()
    if (stepAfterRebind?.kind === 'step') expect(stepAfterRebind.step.status).toBe('running')

    // Let the run actually finish now that we're rebound. Before the fix,
    // runToSession was empty after rebind, so step.completed/message events
    // for this run_id were silently dropped as "belongs to no known session"
    // and the step would stay stuck at status "running" forever.
    while (scheduled.length > 0) {
      const fn = scheduled.shift()!
      fn()
    }

    const final = useChatStore.getState()
    expect(final.running).toBe(false)
    const finalStep = final.timeline.find((e) => e.kind === 'step')
    expect(finalStep?.kind === 'step' && finalStep.step.status).toBe('completed')
    const assistantMsg = final.timeline.find((e) => e.kind === 'message' && e.message.role === 'assistant')
    expect(assistantMsg).toBeDefined()
  })

  it('bindSession superseded mid-subscribe unsubscribes itself instead of leaking listeners or clobbering the newer bind', async () => {
    const pendingSubscribes = new Map<string, () => void>()
    const unsubscribeCalls: string[] = []
    const listenerCounts = new Map<string, number>()
    const transport: RpcTransport = {
      call: async <T,>(method: string, params?: Record<string, unknown>): Promise<RpcCallResult<T>> => {
        if (method === 'session.subscribe') {
          const id = (params as { id: string }).id
          await new Promise<void>((resolve) => {
            pendingSubscribes.set(id, resolve)
          })
          return { ok: true }
        }
        if (method === 'session.unsubscribe') {
          unsubscribeCalls.push((params as { id: string }).id)
          return { ok: true }
        }
        return { ok: true, result: [] as T }
      },
      on: (method: string) => {
        listenerCounts.set(method, (listenerCounts.get(method) ?? 0) + 1)
        return () => {
          listenerCounts.set(method, (listenerCounts.get(method) ?? 0) - 1)
        }
      }
    }

    // Both calls run synchronously up to their own `await session.subscribe`
    // before either promise settles, so bindGeneration is already 2 (from the
    // second call) by the time either subscribe resolves below — this is
    // exactly the "left pane double-click" race from the review.
    const firstBind = useChatStore.getState().bindSession(transport, 'session_a')
    const secondBind = useChatStore.getState().bindSession(transport, 'session_b')
    pendingSubscribes.get('session_a')!()
    pendingSubscribes.get('session_b')!()
    await Promise.all([firstBind, secondBind])

    // The stale first bind must have unsubscribed the session it subscribed to.
    expect(unsubscribeCalls).toContain('session_a')
    // Only the second (current) bind's listeners should still be registered —
    // each of the 9 notification types should have exactly one live listener,
    // not two (one leaked from the superseded first bind).
    for (const count of listenerCounts.values()) {
      expect(count).toBeLessThanOrEqual(1)
    }
    expect(useChatStore.getState().activeSessionId).toBe('session_b')
  })
})
