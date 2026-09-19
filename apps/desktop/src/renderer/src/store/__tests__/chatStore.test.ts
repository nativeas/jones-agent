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
      expect(assistantMsg.message.content.text.length).toBeGreaterThan(0)
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
    const requestId = useChatStore.getState().pendingPermissions[0]!.request_id

    await useChatStore.getState().decidePermission(requestId, 'allow')

    expect(useChatStore.getState().pendingPermissions).toHaveLength(0)
    const assistantMsg = useChatStore.getState().timeline.find((e) => e.kind === 'message' && e.message.role === 'assistant')
    expect(assistantMsg).toBeDefined()
  })

  it('retryTermination() resends the original message via session.retry (Issue #22 FR14 "重试")', async () => {
    const transport = new MockTransport({ schedule: (fn) => fn() })
    await useChatStore.getState().bindSession(transport, MAIN_SESSION_ID)
    await useChatStore.getState().send('/error 网络中断')
    expect(useChatStore.getState().running).toBe(false)

    const card = useChatStore.getState().timeline.find((e) => e.kind === 'termination')
    const turnId = card?.kind === 'termination' ? card.card.turn_id : undefined
    expect(turnId).toBeDefined()

    await useChatStore.getState().retryTermination(turnId!)

    expect(useChatStore.getState().error).toBeNull()
    const userMessages = useChatStore
      .getState()
      .timeline.filter((e) => e.kind === 'message' && e.message.role === 'user')
    expect(userMessages).toHaveLength(2)
    if (userMessages[1]?.kind === 'message') expect(userMessages[1].message.content.text).toBe('/error 网络中断')
  })

  it('retryTermination() surfaces an error when the daemon rejects a stale/unknown turn_id', async () => {
    const transport = new MockTransport({ schedule: (fn) => fn() })
    await useChatStore.getState().bindSession(transport, MAIN_SESSION_ID)

    await useChatStore.getState().retryTermination('turn_does_not_exist')

    expect(useChatStore.getState().error).toBeTruthy()
  })

  it('abandonTermination() clears the pending queue via session.retry (Issue #22 FR14 "放弃")', async () => {
    // A controllable scheduler (same pattern as the "rebinding" test below) —
    // the first Turn's scripted termination must stay pending long enough for
    // a second send() to actually land in the queue behind it.
    const scheduled: Array<() => void> = []
    const transport = new MockTransport({ schedule: (fn) => scheduled.push(fn) })
    await useChatStore.getState().bindSession(transport, MAIN_SESSION_ID)

    await useChatStore.getState().send('/error 第一次出错')
    expect(useChatStore.getState().running).toBe(true) // termination not fired yet

    await useChatStore.getState().send('排在后面的第二条')
    expect(useChatStore.getState().queue).toHaveLength(1)

    scheduled.shift()!() // fire the scripted termination now

    const card = useChatStore.getState().timeline.find((e) => e.kind === 'termination')
    const turnId = card?.kind === 'termination' ? card.card.turn_id : undefined
    expect(turnId).toBeDefined()

    await useChatStore.getState().abandonTermination(turnId!)

    expect(useChatStore.getState().error).toBeNull()
    expect(useChatStore.getState().queue).toHaveLength(0)
    // The card itself stays in the timeline as history — 04-w5-interfaces.md
    // §4 / PRD FR06: 错误卡片本身进入 Session 记录，可回放. Abandoning clears
    // the queue and the server-side Turn status, it doesn't hide the card.
    expect(useChatStore.getState().timeline.some((e) => e.kind === 'termination')).toBe(true)
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

  it('bindSession superseded by a re-bind of the SAME session must not unsubscribe it (01-w2-interfaces.md §5 review round 2)', async () => {
    // React.StrictMode's mount→cleanup→mount (or a fast A→B→A flip in the
    // left pane back to where it started) re-binds the *same* sessionId
    // while the first subscribe is still in flight. `unbindSession()` at the
    // top of the second call already sends one legitimate unsubscribe (it's
    // tearing down whatever the store currently claims to be bound to,
    // same-session or not — that part isn't new). The bug was the *stale*
    // first bind then sending a second, redundant unsubscribe for this same
    // session after backing out — which can land after the second bind's own
    // subscribe and cancel it, leaving the pane "bound" but deaf.
    const pendingSubscribes: Array<() => void> = []
    const unsubscribeCalls: string[] = []
    const listenerCounts = new Map<string, number>()
    const transport: RpcTransport = {
      call: async <T,>(method: string, params?: Record<string, unknown>): Promise<RpcCallResult<T>> => {
        if (method === 'session.subscribe') {
          await new Promise<void>((resolve) => {
            pendingSubscribes.push(resolve)
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
    // before either settles — the second bind's `unbindSession()` cleanup
    // (which fires its one legitimate unsubscribe) and its own subscribe
    // call both happen before the first bind's subscribe resolves.
    const firstBind = useChatStore.getState().bindSession(transport, MAIN_SESSION_ID)
    const secondBind = useChatStore.getState().bindSession(transport, MAIN_SESSION_ID)
    pendingSubscribes[0]!()
    pendingSubscribes[1]!()
    await Promise.all([firstBind, secondBind])

    // Exactly one unsubscribe(MAIN_SESSION_ID) — the second bind's own
    // teardown-on-rebind. The stale first bind must NOT add a second one.
    expect(unsubscribeCalls.filter((id) => id === MAIN_SESSION_ID)).toHaveLength(1)
    expect(useChatStore.getState().activeSessionId).toBe(MAIN_SESSION_ID)
    // Only the winning (second) bind's listeners should be live.
    for (const count of listenerCounts.values()) {
      expect(count).toBeLessThanOrEqual(1)
    }
  })
})
