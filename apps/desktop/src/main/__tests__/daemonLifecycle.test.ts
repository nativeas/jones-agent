import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  DAEMON_RETRY_WAIT_MS,
  HEARTBEAT_FAILURE_THRESHOLD,
  MAX_DAEMON_START_ATTEMPTS,
  ensureDaemonRunning,
  startHeartbeat,
  type DaemonLifecycleDeps
} from '../daemonLifecycle'

function makeDeps(overrides: Partial<DaemonLifecycleDeps> = {}): {
  deps: DaemonLifecycleDeps
  calls: { connect: number; kickstart: number; spawnDev: number; onUnreachable: number }
} {
  const calls = { connect: 0, kickstart: 0, spawnDev: 0, onUnreachable: 0 }
  const deps: DaemonLifecycleDeps = {
    connect: () => {
      calls.connect += 1
    },
    ping: async () => true,
    kickstart: async () => {
      calls.kickstart += 1
    },
    spawnDev: () => {
      calls.spawnDev += 1
    },
    onUnreachable: () => {
      calls.onUnreachable += 1
    },
    ...overrides
  }
  return { deps, calls }
}

const noWait = async (): Promise<void> => {
  // tests don't need the real 1s retry wait — a resolved promise keeps them fast
}

describe('ensureDaemonRunning', () => {
  it('connects once and returns true without retrying when the first ping succeeds', async () => {
    const { deps, calls } = makeDeps()
    const ok = await ensureDaemonRunning(deps, noWait)
    expect(ok).toBe(true)
    expect(calls.connect).toBe(1)
    expect(calls.kickstart).toBe(0)
    expect(calls.onUnreachable).toBe(0)
  })

  it('retries kickstart/spawnDev/connect and succeeds on a later attempt', async () => {
    let pingCount = 0
    const { deps, calls } = makeDeps({
      ping: async () => {
        pingCount += 1
        return pingCount === 3 // fails the initial ping + attempt 1, succeeds on attempt 2
      }
    })

    const ok = await ensureDaemonRunning(deps, noWait)

    expect(ok).toBe(true)
    expect(pingCount).toBe(3)
    expect(calls.kickstart).toBe(2)
    expect(calls.spawnDev).toBe(2)
    expect(calls.connect).toBe(3) // initial + 2 retries
    expect(calls.onUnreachable).toBe(0)
  })

  it('reports unreachable exactly once after exhausting all retries', async () => {
    const { deps, calls } = makeDeps({ ping: async () => false })

    const ok = await ensureDaemonRunning(deps, noWait)

    expect(ok).toBe(false)
    expect(calls.kickstart).toBe(MAX_DAEMON_START_ATTEMPTS)
    expect(calls.spawnDev).toBe(MAX_DAEMON_START_ATTEMPTS)
    expect(calls.onUnreachable).toBe(1)
  })

  it('waits DAEMON_RETRY_WAIT_MS between each retry attempt', async () => {
    const waited: number[] = []
    const { deps } = makeDeps({ ping: async () => false })

    await ensureDaemonRunning(deps, async (ms) => {
      waited.push(ms)
    })

    expect(waited).toEqual(Array(MAX_DAEMON_START_ATTEMPTS).fill(DAEMON_RETRY_WAIT_MS))
  })

  it('dedupes concurrent callers into a single in-flight recovery sequence', async () => {
    let pingCount = 0
    const { deps, calls } = makeDeps({
      ping: async () => {
        pingCount += 1
        return pingCount === 3 // fails the initial ping + attempt 1, succeeds on attempt 2 — same shape as the retry test above
      }
    })

    // Two callers racing (e.g. a startup ensureDaemonRunning() call and a
    // heartbeat tick's own recovery firing before it finishes) must share one
    // recovery instead of each running their own `kickstart -k` sequence.
    const [a, b] = await Promise.all([
      ensureDaemonRunning(deps, noWait),
      ensureDaemonRunning(deps, noWait)
    ])

    expect(a).toBe(true)
    expect(b).toBe(true)
    expect(pingCount).toBe(3)
    expect(calls.kickstart).toBe(2) // matches the single-caller sequence above, not double it
    expect(calls.spawnDev).toBe(2)
  })
})

describe('startHeartbeat', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('pings on the configured interval and does nothing while healthy', async () => {
    const { deps } = makeDeps()
    const pingSpy = vi.spyOn(deps, 'ping')
    const stop = startHeartbeat(deps, 1000)

    await vi.advanceTimersByTimeAsync(3500)

    expect(pingSpy).toHaveBeenCalledTimes(3)
    stop()
  })

  it('does not escalate to recovery on a single missed ping', async () => {
    const { deps, calls } = makeDeps({ ping: async () => false })
    const stop = startHeartbeat(deps, 1000)

    await vi.advanceTimersByTimeAsync(1000) // one tick, one miss — below HEARTBEAT_FAILURE_THRESHOLD
    await vi.advanceTimersByTimeAsync(0)

    expect(calls.connect).toBe(0) // ensureDaemonRunning never ran: no connect(), no kickstart
    expect(calls.kickstart).toBe(0)
    stop()
  })

  it('resets the miss streak on a successful ping in between', async () => {
    let tick = 0
    const { deps, calls } = makeDeps({
      ping: async () => {
        tick += 1
        return tick === 2 // miss, hit, miss — never two misses in a row
      }
    })
    const stop = startHeartbeat(deps, 1000)

    await vi.advanceTimersByTimeAsync(3000)
    await vi.advanceTimersByTimeAsync(0)

    expect(tick).toBe(3)
    expect(calls.kickstart).toBe(0) // streak reset by the hit at tick 2, so recovery never ran
    stop()
  })

  it('runs the full recovery sequence after HEARTBEAT_FAILURE_THRESHOLD consecutive misses', async () => {
    let pingCount = 0
    const { deps, calls } = makeDeps({
      ping: async () => {
        pingCount += 1
        // The first HEARTBEAT_FAILURE_THRESHOLD calls are the heartbeat ticks
        // themselves (all fail); the recovery's own initial ping (the next call)
        // succeeds so it doesn't cascade into retries.
        return pingCount > HEARTBEAT_FAILURE_THRESHOLD
      }
    })
    const stop = startHeartbeat(deps, 1000)

    await vi.advanceTimersByTimeAsync(HEARTBEAT_FAILURE_THRESHOLD * 1000)
    await vi.advanceTimersByTimeAsync(0)

    expect(pingCount).toBe(HEARTBEAT_FAILURE_THRESHOLD + 1)
    expect(calls.connect).toBeGreaterThanOrEqual(1)
    stop()
  })

  it('does not stack a second concurrent ping while one is still in flight', async () => {
    const pending: { resolve: (ok: boolean) => void }[] = []
    let pingCalls = 0
    const { deps } = makeDeps({
      ping: () =>
        new Promise<boolean>((resolve) => {
          pingCalls += 1
          pending.push({ resolve }) // left pending deliberately
        })
    })
    const stop = startHeartbeat(deps, 1000)

    await vi.advanceTimersByTimeAsync(1000) // tick 1 fires, ping() left pending
    await vi.advanceTimersByTimeAsync(1000) // tick 2 fires while tick 1's ping is still unresolved
    expect(pingCalls).toBe(1) // tick 2 was skipped by the `inFlight` guard

    pending[0]?.resolve(false) // let tick 1 resolve as a miss — one miss, below threshold
    await vi.advanceTimersByTimeAsync(0)
    expect(pingCalls).toBe(1) // no recovery (and so no second ping) triggered yet

    stop()
  })

  it('stop() clears the interval so no further pings fire', async () => {
    const { deps } = makeDeps()
    const pingSpy = vi.spyOn(deps, 'ping')
    const stop = startHeartbeat(deps, 1000)

    await vi.advanceTimersByTimeAsync(1000)
    expect(pingSpy).toHaveBeenCalledTimes(1)

    stop()
    await vi.advanceTimersByTimeAsync(5000)
    expect(pingSpy).toHaveBeenCalledTimes(1)
  })
})
