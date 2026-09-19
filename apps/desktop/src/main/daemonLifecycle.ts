/**
 * Daemon connect/recover/health-check state machine (design §3, §6), extracted from
 * `index.ts` so it can be unit-tested with injected fake connect/ping/spawn (design
 * §6: "main 的 ensureDaemonRunning 用 vitest 注入 fake connect/spawn") instead of only
 * through a real Electron process and a real `uv run` subprocess.
 *
 * `index.ts` supplies the real implementations of every `DaemonLifecycleDeps` field
 * (RpcClient, `launchctl kickstart`, `uv run python -m jones_daemon`, pushing
 * `daemon.error` to renderer windows); this module only owns the *sequencing*.
 */

export const DAEMON_PING_TIMEOUT_MS = 2000
export const DAEMON_RETRY_WAIT_MS = 1000
export const MAX_DAEMON_START_ATTEMPTS = 3
export const HEARTBEAT_INTERVAL_MS = 5000
// A single missed heartbeat ping can be a transient blip (e.g. the connection's
// MAX_INFLIGHT_PER_CONNECTION request cap, or one slow-but-alive request) rather
// than a dead daemon — see `ping`'s doc below. Requiring two consecutive misses
// before running the (destructive, `kickstart -k`-first) recovery sequence trades
// ~intervalMs of extra detection latency for not killing a daemon that is merely
// busy or momentarily slow.
export const HEARTBEAT_FAILURE_THRESHOLD = 2

export interface DaemonLifecycleDeps {
  /** Open (or re-open) the RpcClient's socket connection. */
  connect: () => void
  /**
   * `daemon.ping` with a timeout. Resolves `true` whenever the daemon actually
   * answered — including a JSON-RPC *error* response (e.g. `too_many_requests`
   * when a connection's in-flight cap is hit): an error response still proves
   * the daemon is alive and processing requests, which is the opposite of what
   * the recovery path below should react to. Resolves `false` only when no
   * response arrives at all (connection failure or timeout) — see
   * `index.ts::pingOnce` for that classification.
   */
  ping: (timeoutMs: number) => Promise<boolean>
  /** `launchctl kickstart -k gui/<uid>/<label>` (no-op / resolves on any platform where it isn't applicable or fails — see index.ts). */
  kickstart: () => Promise<void>
  /** Dev-mode fallback: spawn the daemon directly (no-op in packaged builds). */
  spawnDev: () => void
  /** Called once, only after all `MAX_DAEMON_START_ATTEMPTS` retries are exhausted. */
  onUnreachable: () => void
}

/**
 * design §3: "Electron main 启动：先连 socket；连不上则尝试 launchctl kickstart
 * （已安装）或直接 spawn daemon（开发模式），最多重试 3 次后向 renderer 报错
 * （PRD 11.3）". Returns whether the daemon ended up reachable, so both the startup
 * call site and the heartbeat's recovery path can tell success from exhaustion
 * without each re-deciding what "reachable" means.
 */
// Shared across every caller (the startup call in index.ts and every heartbeat
// tick's recovery in startHeartbeat below): without this, a slow recovery
// triggered at startup and a heartbeat tick firing mid-recovery would each run
// their own `kickstart -k` sequence, with the second kickstart killing the
// daemon the first one just brought up. A caller that arrives while a recovery
// is already running simply awaits that same recovery's outcome instead of
// starting a second one.
let inFlightRecovery: Promise<boolean> | null = null

export async function ensureDaemonRunning(
  deps: DaemonLifecycleDeps,
  wait: (ms: number) => Promise<void> = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
): Promise<boolean> {
  if (inFlightRecovery) return inFlightRecovery
  inFlightRecovery = (async () => {
    deps.connect()
    if (await deps.ping(DAEMON_PING_TIMEOUT_MS)) return true

    for (let attempt = 1; attempt <= MAX_DAEMON_START_ATTEMPTS; attempt++) {
      await deps.kickstart()
      deps.spawnDev()
      deps.connect()
      await wait(DAEMON_RETRY_WAIT_MS)
      if (await deps.ping(DAEMON_PING_TIMEOUT_MS)) return true
    }

    deps.onUnreachable()
    return false
  })()
  try {
    return await inFlightRecovery
  } finally {
    inFlightRecovery = null
  }
}

/**
 * design §6: "健康检测：daemon.ping 心跳 5s，断线走 RpcClient 状态机重连." A plain
 * socket close (daemon process died, network hiccup) is already handled by
 * RpcClient's own reconnect backoff (see rpcClient.ts) without any help from this
 * heartbeat — what a passive reconnect backoff *cannot* catch is a daemon that is
 * still accepting the connection but has stopped answering (wedged event loop,
 * stuck request holding the dispatch semaphore). A missed `daemon.ping` is the
 * signal for that case, and the recovery is the same `ensureDaemonRunning` sequence
 * used at startup — not a second, parallel reconnect implementation.
 *
 * `inFlight` guards the *whole* tick (ping, and the recovery it may trigger)
 * against overlap: a heartbeat tick firing again before the previous tick's ping
 * (bounded by `DAEMON_PING_TIMEOUT_MS`) or recovery (which can itself take several
 * seconds across `MAX_DAEMON_START_ATTEMPTS` retries) has finished is simply
 * skipped, rather than starting a second concurrent ping or stacking a second
 * concurrent kickstart/spawn attempt.
 *
 * A single missed ping does not trigger recovery: `deps.ping` already resolves
 * `true` for a daemon that responded with an error (still alive), so a `false`
 * here means no response at all — but that can still be one slow request rather
 * than a dead daemon, and the recovery sequence's first move is a destructive
 * `kickstart -k`. `consecutiveFailures` requires `HEARTBEAT_FAILURE_THRESHOLD`
 * misses in a row (any success resets it to 0) before actually running recovery.
 */
export function startHeartbeat(
  deps: DaemonLifecycleDeps,
  intervalMs: number = HEARTBEAT_INTERVAL_MS,
  setIntervalFn: typeof setInterval = setInterval,
  clearIntervalFn: typeof clearInterval = clearInterval
): () => void {
  let inFlight = false
  let consecutiveFailures = 0
  const timer = setIntervalFn(() => {
    if (inFlight) return
    inFlight = true
    void (async () => {
      try {
        const ok = await deps.ping(DAEMON_PING_TIMEOUT_MS)
        if (ok) {
          consecutiveFailures = 0
          return
        }
        consecutiveFailures += 1
        if (consecutiveFailures >= HEARTBEAT_FAILURE_THRESHOLD) {
          consecutiveFailures = 0
          await ensureDaemonRunning(deps)
        }
      } finally {
        inFlight = false
      }
    })()
  }, intervalMs)
  return () => clearIntervalFn(timer)
}
