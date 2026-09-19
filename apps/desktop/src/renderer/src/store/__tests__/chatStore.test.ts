import { beforeEach, describe, expect, it } from 'vitest'
import { MockTransport } from '../../rpc/mockTransport'
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

  it('removeQueueItem drops the item from the queue panel', async () => {
    const transport = new MockTransport()
    await useChatStore.getState().bindSession(transport, MAIN_SESSION_ID)
    await useChatStore.getState().send('第一条')
    await useChatStore.getState().send('第二条')
    const itemId = useChatStore.getState().queue[0]!.id

    await useChatStore.getState().removeQueueItem(itemId)

    expect(useChatStore.getState().queue).toHaveLength(0)
  })
})
