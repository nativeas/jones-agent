import { afterEach, describe, expect, it, vi } from 'vitest'
import { createWindowTransport } from '../windowTransport'

// jones.d.ts declares `window.jones` as always-present (that's true in a real
// Electron renderer, where contextBridge runs before any page script) — these
// tests need to simulate it being absent, so they go through an untyped view
// of `window` rather than fighting that global declaration.
type LooseWindow = Omit<typeof window, 'jones'> & { jones?: Window['jones'] }
const win = window as LooseWindow

describe('createWindowTransport', () => {
  afterEach(() => {
    delete win.jones
  })

  it('forwards call() to window.jones.rpc.call with the same method/params', async () => {
    const call = vi.fn().mockResolvedValue({ ok: true, result: { pid: 1 } })
    win.jones = { rpc: { call, on: vi.fn() } }

    const transport = createWindowTransport()
    const result = await transport.call('daemon.ping', { a: 1 })

    expect(call).toHaveBeenCalledWith('daemon.ping', { a: 1 })
    expect(result).toEqual({ ok: true, result: { pid: 1 } })
  })

  it('forwards on() subscriptions and their unsubscribe function', () => {
    const unsubscribe = vi.fn()
    const on = vi.fn().mockReturnValue(unsubscribe)
    win.jones = { rpc: { call: vi.fn(), on } }

    const transport = createWindowTransport()
    const cb = vi.fn()
    const stop = transport.on('daemon.error', cb)

    expect(on).toHaveBeenCalledWith('daemon.error', cb)
    stop()
    expect(unsubscribe).toHaveBeenCalled()
  })

  it('surfaces a clear rejection instead of hanging when window.jones is missing', async () => {
    // Regression target for 01-w2-interfaces.md §5's "known issue": a first
    // call landing before the bridge exists must fail explicitly, not silently.
    delete win.jones
    const transport = createWindowTransport()
    const result = await transport.call('daemon.ping')
    expect(result.ok).toBe(false)
    expect(result.message).toMatch(/未就绪/)
  })

  it('on() is a harmless no-op when window.jones is missing', () => {
    delete win.jones
    const transport = createWindowTransport()
    expect(() => transport.on('daemon.error', () => {})()).not.toThrow()
  })
})
