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

export interface DaemonLifecycleDeps {
  /** Open (or re-open) the RpcClient's socket connection. */
  connect: () => void
  /** `daemon.ping` with a timeout; resolves false on any failure or timeout. */
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
export async function ensureDaemonRunning(
  deps: DaemonLifecycleDeps,
  wait: (ms: number) => Promise<void> = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
): Promise<boolean> {
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
 */
export function startHeartbeat(
  deps: DaemonLifecycleDeps,
  intervalMs: number = HEARTBEAT_INTERVAL_MS,
  setIntervalFn: typeof setInterval = setInterval,
  clearIntervalFn: typeof clearInterval = clearInterval
): () => void {
  let inFlight = false
  const timer = setIntervalFn(() => {
    if (inFlight) return
    inFlight = true
    void (async () => {
      try {
        const ok = await deps.ping(DAEMON_PING_TIMEOUT_MS)
        if (!ok) await ensureDaemonRunning(deps)
      } finally {
        inFlight = false
      }
    })()
  }, intervalMs)
  return () => clearIntervalFn(timer)
}
