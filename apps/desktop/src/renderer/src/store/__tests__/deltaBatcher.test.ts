import { describe, expect, it, vi } from 'vitest'
import { createDeltaBatcher } from '../deltaBatcher'

/** A manually-driven scheduler: captures the callback instead of running it,
 * so tests control exactly when a "frame" happens — no fake timers, no rAF
 * polyfill, no flakiness (DEV.md 工程原则 #5: 测试必须真的断言). */
function manualScheduler(): { schedule: (run: () => void) => void; flip: () => void } {
  let pending: (() => void) | null = null
  return {
    schedule: (run) => {
      pending = run
    },
    flip: () => {
      const run = pending
      pending = null
      run?.()
    }
  }
}

describe('deltaBatcher', () => {
  it('merges multiple deltas for the same message into one flush call', () => {
    const onFlush = vi.fn()
    const { schedule, flip } = manualScheduler()
    const batcher = createDeltaBatcher(onFlush, schedule)

    batcher.push('m1', 'Hel')
    batcher.push('m1', 'lo')
    batcher.push('m1', ', world')
    expect(onFlush).not.toHaveBeenCalled() // nothing flushed until the "frame" fires

    flip()
    expect(onFlush).toHaveBeenCalledTimes(1)
    expect(onFlush).toHaveBeenCalledWith([{ messageId: 'm1', text: 'Hello, world' }])
  })

  it('batches deltas for different messages into one flush', () => {
    const onFlush = vi.fn()
    const { schedule, flip } = manualScheduler()
    const batcher = createDeltaBatcher(onFlush, schedule)

    batcher.push('m1', 'a')
    batcher.push('m2', 'b')
    batcher.push('m1', 'c')
    flip()

    expect(onFlush).toHaveBeenCalledTimes(1)
    const [updates] = onFlush.mock.calls[0] as [Array<{ messageId: string; text: string }>]
    expect(updates).toEqual(
      expect.arrayContaining([
        { messageId: 'm1', text: 'ac' },
        { messageId: 'm2', text: 'b' }
      ])
    )
    expect(updates).toHaveLength(2)
  })

  it('only schedules once per pending batch, not once per push', () => {
    let scheduleCalls = 0
    const batcher = createDeltaBatcher(vi.fn(), (run) => {
      scheduleCalls += 1
      setTimeout(run, 0)
    })
    batcher.push('m1', 'a')
    batcher.push('m1', 'b')
    batcher.push('m1', 'c')
    expect(scheduleCalls).toBe(1)
  })

  it('schedules a fresh flush after a previous one already ran', () => {
    const onFlush = vi.fn()
    const { schedule, flip } = manualScheduler()
    const batcher = createDeltaBatcher(onFlush, schedule)

    batcher.push('m1', 'first')
    flip()
    batcher.push('m1', 'second')
    flip()

    expect(onFlush).toHaveBeenNthCalledWith(1, [{ messageId: 'm1', text: 'first' }])
    expect(onFlush).toHaveBeenNthCalledWith(2, [{ messageId: 'm1', text: 'second' }])
  })

  it('flushNow flushes an already-scheduled batch immediately', () => {
    const onFlush = vi.fn()
    const batcher = createDeltaBatcher(onFlush, () => {
      // never actually runs the callback — flushNow must not depend on it firing
    })
    batcher.push('m1', 'x')
    expect(onFlush).not.toHaveBeenCalled()
    batcher.flushNow()
    expect(onFlush).toHaveBeenCalledWith([{ messageId: 'm1', text: 'x' }])
  })

  it('flushNow is a no-op when nothing is pending', () => {
    const onFlush = vi.fn()
    const batcher = createDeltaBatcher(onFlush, () => {})
    batcher.flushNow()
    expect(onFlush).not.toHaveBeenCalled()
  })

  it('cancel discards a pending batch without emitting it', () => {
    const onFlush = vi.fn()
    const { schedule, flip } = manualScheduler()
    const batcher = createDeltaBatcher(onFlush, schedule)

    batcher.push('m1', 'lost')
    batcher.cancel()
    flip() // even if the stale callback somehow still fired, nothing should emit
    expect(onFlush).not.toHaveBeenCalled()

    // a push after cancel schedules a brand new batch normally
    batcher.push('m1', 'kept')
    flip()
    expect(onFlush).toHaveBeenCalledWith([{ messageId: 'm1', text: 'kept' }])
  })
})
