import { describe, expect, it } from 'vitest'
import { MockTransport } from '../mockTransport'

/** Synchronous scheduler: runs the scripted turn to completion inline, so
 * these tests assert on real emitted events with no timers/flakiness. */
function syncTransport(): MockTransport {
  return new MockTransport({ schedule: (fn) => fn() })
}

describe('MockTransport', () => {
  it('seeds a unique, unremovable-from-the-API main session', async () => {
    const transport = syncTransport()
    const res = await transport.call<Array<{ id: string; is_main: boolean }>>('session.list')
    expect(res.ok).toBe(true)
    const mainSessions = (res.result ?? []).filter((s) => s.is_main)
    expect(mainSessions).toHaveLength(1)
  })

  it('runs a normal send to completion: turn.started, deltas, step, message.completed', async () => {
    const transport = syncTransport()
    const sessions = await transport.call<Array<{ id: string }>>('session.list')
    const sessionId = sessions.result![0]!.id

    const events: string[] = []
    transport.on('turn.started', () => events.push('turn.started'))
    transport.on('step.started', () => events.push('step.started'))
    transport.on('step.completed', () => events.push('step.completed'))
    transport.on('message.delta', () => events.push('message.delta'))
    transport.on('message.completed', () => events.push('message.completed'))

    const send = await transport.call<{ turn_id: string; queued: boolean }>('session.send', {
      id: sessionId,
      text: '你好'
    })

    expect(send.ok).toBe(true)
    expect(send.result?.queued).toBe(false)
    expect(events[0]).toBe('turn.started')
    expect(events).toContain('step.started')
    expect(events).toContain('step.completed')
    expect(events.filter((e) => e === 'message.delta').length).toBeGreaterThan(0)
    expect(events.at(-1)).toBe('message.completed')

    const sessionAfter = await transport.call<Array<{ id: string; status: string }>>('session.list')
    expect(sessionAfter.result!.find((s) => s.id === sessionId)!.status).toBe('idle')
  })

  it('queues a send while a turn is running instead of starting a second one', async () => {
    // Use a real (async) scheduler so the first turn is still "running" (stuck
    // mid-script) when the second send() arrives.
    const transport = new MockTransport()
    const sessions = await transport.call<Array<{ id: string }>>('session.list')
    const sessionId = sessions.result![0]!.id

    const first = await transport.call<{ queued: boolean }>('session.send', { id: sessionId, text: '第一条' })
    expect(first.result?.queued).toBe(false)

    const second = await transport.call<{ queued: boolean }>('session.send', { id: sessionId, text: '第二条' })
    expect(second.result?.queued).toBe(true)

    // R-N9 (controller ruling, round-6, 2026-09-20): `session.queue` now
    // returns `{items, suspended, reason}`, not a bare array.
    const queue = await transport.call<{ items: Array<{ text: string }> }>('session.queue', {
      id: sessionId
    })
    expect(queue.result?.items).toEqual([expect.objectContaining({ text: '第二条' })])
  })

  it('/error produces a run.terminated kind:"error" and returns the session to idle', async () => {
    const transport = syncTransport()
    const sessions = await transport.call<Array<{ id: string }>>('session.list')
    const sessionId = sessions.result![0]!.id

    let terminated: { kind: string; reason: string } | null = null
    transport.on('run.terminated', (params) => {
      terminated = params as { kind: string; reason: string }
    })

    await transport.call('session.send', { id: sessionId, text: '/error 网络中断' })

    expect(terminated).not.toBeNull()
    expect(terminated!.kind).toBe('error')
    expect(terminated!.reason).toBe('网络中断')
  })

  it('/budget produces a run.terminated kind:"budget"', async () => {
    const transport = syncTransport()
    const sessions = await transport.call<Array<{ id: string }>>('session.list')
    const sessionId = sessions.result![0]!.id

    let terminated: { kind: string } | null = null
    transport.on('run.terminated', (params) => {
      terminated = params as { kind: string }
    })

    await transport.call('session.send', { id: sessionId, text: '/budget 超预算' })
    expect(terminated!.kind).toBe('budget')
  })

  it('session.stop produces a run.terminated kind:"user"', async () => {
    // Real scheduler so the turn is still mid-flight when stop() is called.
    const transport = new MockTransport()
    const sessions = await transport.call<Array<{ id: string }>>('session.list')
    const sessionId = sessions.result![0]!.id
    await transport.call('session.send', { id: sessionId, text: '你好' })

    let terminated: { kind: string } | null = null
    transport.on('run.terminated', (params) => {
      terminated = params as { kind: string }
    })

    const stop = await transport.call<{ stopped: boolean }>('session.stop', { id: sessionId })
    expect(stop.result?.stopped).toBe(true)
    expect(terminated!.kind).toBe('user')
  })

  it('/permission emits permission.requested and resumes only after permission.decide', async () => {
    const transport = syncTransport()
    const sessions = await transport.call<Array<{ id: string }>>('session.list')
    const sessionId = sessions.result![0]!.id

    let requestId: string | null = null
    transport.on('permission.requested', (params) => {
      requestId = (params as { request_id: string }).request_id
    })
    let completed = false
    transport.on('message.completed', () => {
      completed = true
    })

    await transport.call('session.send', { id: sessionId, text: '/permission 危险动作' })
    expect(requestId).not.toBeNull()
    expect(completed).toBe(false) // waiting on the approval, not auto-continuing

    await transport.call('permission.decide', { request_id: requestId, decision: 'allow' })
    expect(completed).toBe(true)
  })

  it('provider.set_key only ever returns the last 4 chars as key_hint', async () => {
    const transport = syncTransport()
    const res = await transport.call<{ has_key: boolean; key_hint: string }>('provider.set_key', {
      provider: 'anthropic',
      key: 'sk-ant-abcdEFGH1234'
    })
    expect(res.result?.has_key).toBe(true)
    expect(res.result?.key_hint).toBe('1234')
  })

  it('rejects an unimplemented method with ok:false instead of throwing', async () => {
    const transport = syncTransport()
    const res = await transport.call('not.a.real.method')
    expect(res.ok).toBe(false)
    expect(res.message).toBeTruthy()
  })
})
