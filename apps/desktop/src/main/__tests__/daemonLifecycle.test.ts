import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  DAEMON_RETRY_WAIT_MS,
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

  it('runs the full recovery sequence when a heartbeat ping fails', async () => {
    let pingCount = 0
    const { deps, calls } = makeDeps({
      ping: async () => {
        pingCount += 1
        // first call is the heartbeat tick itself (fails); the recovery's own
        // initial ping (second call) succeeds so it doesn't cascade into retries.
        return pingCount !== 1
      }
    })
    const stop = startHeartbeat(deps, 1000)

    await vi.advanceTimersByTimeAsync(1000)
    // let the tick's own async ping/recovery chain settle
    await vi.advanceTimersByTimeAsync(0)

    expect(calls.connect).toBeGreaterThanOrEqual(1)
    stop()
  })

  it('does not stack a second recovery attempt while one is still in flight', async () => {
    const pending: { resolve: (ok: boolean) => void }[] = []
    let pingCalls = 0
    const { deps, calls } = makeDeps({
      ping: () =>
        new Promise<boolean>((resolve) => {
          pingCalls += 1
          if (pingCalls === 1) {
            pending.push({ resolve }) // first heartbeat tick: left pending deliberately
          } else {
            resolve(true)
          }
        })
    })
    const stop = startHeartbeat(deps, 1000)

    await vi.advanceTimersByTimeAsync(1000) // tick 1 fires, ping() left pending
    await vi.advanceTimersByTimeAsync(1000) // tick 2 fires while tick 1's ping is still unresolved
    expect(pingCalls).toBe(1) // tick 2 was skipped by the `inFlight` guard

    pending[0]?.resolve(false) // now let tick 1 resolve as unhealthy, entering recovery
    await vi.advanceTimersByTimeAsync(0)

    expect(calls.connect).toBeGreaterThanOrEqual(1)
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
