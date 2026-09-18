/**
 * NDJSON JSON-RPC 2.0 client for the daemon's Unix socket (docs/design/00-foundation.md §4).
 *
 * Framing: only the still-incomplete tail of the stream is retained between reads
 * (`Buffer.subarray`, a view, not a copy) — a complete line is sliced off and handed to
 * the parser as soon as it arrives, so the client never accumulates the whole
 * connection's traffic into one growing string (DEV.md 工程原则 #3: 性能是需求).
 */
import net from 'node:net'

const NEWLINE = 0x0a

interface JsonRpcResponse {
  jsonrpc: '2.0'
  id: string | number | null
  result?: unknown
  error?: { code: number; message: string; data?: unknown }
}

interface JsonRpcNotification {
  jsonrpc: '2.0'
  method: string
  params?: unknown
}

type NotificationHandler = (params: unknown) => void

export class RpcError extends Error {
  code: number
  data?: unknown

  constructor(code: number, message: string, data?: unknown) {
    super(message)
    this.code = code
    this.data = data
  }
}

interface PendingCall {
  resolve: (value: unknown) => void
  reject: (err: Error) => void
}

const RECONNECT_DELAYS_MS = [200, 500, 1000, 2000, 5000]

/**
 * Explicit connection state machine. At any instant there is at most one live
 * socket and at most one pending timer (the reconnect backoff), because every
 * place that could open a socket or arm a timer goes through `beginConnecting()`
 * / `handleClose()`, which check and set this field instead of inferring
 * "are we connected" from `socket`/`socket.destroyed` (see `beginConnecting()`
 * for why that inference was the actual bug).
 */
type ConnState = 'disconnected' | 'connecting' | 'connected' | 'closing'

export class RpcClient {
  private socket: net.Socket | null = null
  private leftover: Buffer = Buffer.alloc(0)
  private nextId = 1
  private pending = new Map<string, PendingCall>()
  private notificationHandlers = new Map<string, Set<NotificationHandler>>()
  private anyNotificationHandlers = new Set<(method: string, params: unknown) => void>()
  private reconnectAttempt = 0
  private reconnectTimer: NodeJS.Timeout | null = null
  private stopped = false
  private state: ConnState = 'disconnected'
  private connectedWaiters: Array<{ resolve: () => void; reject: (err: Error) => void }> = []

  constructor(private readonly socketPath: string) {}

  connect(): void {
    this.stopped = false
    this.beginConnecting()
  }

  private beginConnecting(): void {
    // Idempotent by construction: a caller re-triggering connect() while a
    // socket is already open or mid-connect (macOS `activate` firing after
    // window-all-closed, or ensureDaemonRunning's own retry loop calling
    // connect() on its own cadence) must never open a *second* socket
    // alongside the one already in flight — that's exactly how a daemon that
    // keeps crashing used to multiply socket connections: each manual
    // connect() call tested `socket.destroyed`, which is already `true` the
    // instant `close` fires (handleClose() never nulled `this.socket`), so it
    // opened a new socket in addition to the reconnect timer handleClose()
    // had just armed for the same disconnect. Gating on `state` instead of
    // socket identity closes that gap: only 'disconnected' may proceed.
    if (this.state === 'connecting' || this.state === 'connected') return
    if (this.state === 'closing') return // stop() is mid-teardown; it settles to 'disconnected' synchronously before returning, so this should not be observable, but never race ahead of it regardless
    // A caller asking to connect *now* supersedes any pending backoff wait —
    // cancel it so we never end up with a timer *and* a socket in flight.
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }
    this.state = 'connecting'
    this.openSocket()
  }

  stop(): void {
    this.stopped = true
    this.state = 'closing'
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }
    const socket = this.socket
    this.socket = null
    socket?.destroy()
    // destroy() emits 'close' asynchronously (or not at all if the socket was
    // never actually opened at the OS level yet); settle the state machine
    // synchronously here rather than waiting for that event, so a connect()
    // call immediately after stop() is never blocked on it. handleClose(), if
    // it does still fire for `socket`, will no-op: it only acts when the
    // socket it was called for is still the one this client is tracking.
    this.state = 'disconnected'
    for (const [, call] of this.pending) {
      call.reject(new Error('rpc client stopped'))
    }
    this.pending.clear()
    const waiters = this.connectedWaiters
    this.connectedWaiters = []
    waiters.forEach(({ reject }) => reject(new Error('rpc client stopped')))
  }

  private openSocket(): void {
    const socket = net.createConnection(this.socketPath)
    this.socket = socket

    socket.on('connect', () => {
      if (this.socket !== socket) return // stale: superseded by a later stop()/connect()
      this.state = 'connected'
      this.reconnectAttempt = 0
      const waiters = this.connectedWaiters
      this.connectedWaiters = []
      waiters.forEach(({ resolve }) => resolve())
    })
    socket.on('data', (chunk) => {
      if (this.socket === socket) this.handleData(chunk)
    })
    socket.on('error', () => {
      // 'close' fires right after; reconnect scheduling happens there so it only
      // happens once per disconnect.
    })
    socket.on('close', () => this.handleClose(socket))
  }

  private handleClose(socket: net.Socket): void {
    // Ignore a close event from a socket that isn't the one we're currently
    // tracking — e.g. stop() already replaced/cleared `this.socket` before
    // this (async) event had a chance to fire. Acting on it here would null
    // out a *newer* socket's reference and/or arm a second reconnect timer
    // alongside whatever the newer socket's own lifecycle is doing.
    if (this.socket !== socket) return
    this.socket = null
    this.state = 'disconnected'
    this.leftover = Buffer.alloc(0)
    for (const [, call] of this.pending) {
      call.reject(new Error('daemon connection closed'))
    }
    this.pending.clear()
    // Anyone waiting in whenConnected() (a call() made while this attempt was
    // still connecting) must be rejected here too — otherwise a connection
    // failure (daemon not running is the common case: first launch, or any dev
    // session before the daemon has started) leaves those callers pending
    // forever instead of the timeout ever getting a chance to fire, since the
    // timeout is only armed once whenConnected() resolves.
    const waiters = this.connectedWaiters
    this.connectedWaiters = []
    waiters.forEach(({ reject }) => reject(new Error('daemon connection closed')))
    if (this.stopped) return
    const delay =
      RECONNECT_DELAYS_MS[Math.min(this.reconnectAttempt, RECONNECT_DELAYS_MS.length - 1)]
    this.reconnectAttempt += 1
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null
      if (!this.stopped) this.beginConnecting()
    }, delay)
  }

  private handleData(chunk: Buffer): void {
    this.leftover = this.leftover.length ? Buffer.concat([this.leftover, chunk]) : chunk
    let newlineIndex: number
    while ((newlineIndex = this.leftover.indexOf(NEWLINE)) !== -1) {
      const line = this.leftover.subarray(0, newlineIndex)
      this.leftover = this.leftover.subarray(newlineIndex + 1)
      if (line.length > 0) this.handleLine(line)
    }
  }

  private handleLine(line: Buffer): void {
    let message: JsonRpcResponse | JsonRpcNotification
    try {
      message = JSON.parse(line.toString('utf8'))
    } catch {
      return // malformed line from the daemon: drop it, never crash the client
    }

    if ('method' in message && message.method) {
      const handlers = this.notificationHandlers.get(message.method)
      handlers?.forEach((handler) => handler(message.params))
      this.anyNotificationHandlers.forEach((handler) => handler(message.method, message.params))
      return
    }

    const response = message as JsonRpcResponse
    if (response.id === null || response.id === undefined) return
    // `pending` is keyed by the string ids this client generates (see call());
    // coerce here so a well-behaved daemon echoing back exactly what we sent
    // (always a string) still matches, without widening the map's key type to
    // also accept a bare number for a case that should never happen.
    const id = String(response.id)
    const call = this.pending.get(id)
    if (!call) return
    this.pending.delete(id)
    if (response.error) {
      call.reject(new RpcError(response.error.code, response.error.message, response.error.data))
    } else {
      call.resolve(response.result)
    }
  }

  private whenConnected(): Promise<void> {
    // `socket.pending` is not "have we connected yet": it flips back to `true`
    // after `destroy()`, so during a disconnect/reconnect window a live-looking
    // socket reference would make this resolve immediately and hand back a
    // socket that's about to reject every write. Track connectedness explicitly.
    if (this.state === 'connected') return Promise.resolve()
    return new Promise((resolve, reject) => this.connectedWaiters.push({ resolve, reject }))
  }

  async call(method: string, params?: Record<string, unknown>, timeoutMs = 10_000): Promise<unknown> {
    // The timeout has to cover waiting-to-connect, not just waiting-for-response:
    // previously it was only armed *after* whenConnected() resolved, so when the
    // daemon isn't running (first launch, or any dev session before it's started)
    // a call would await whenConnected() forever with nothing to ever time it out
    // — the exact "stuck on 检测中…" bug. Wrapping the whole thing in one Promise
    // lets a single timer own both phases.
    return new Promise((resolve, reject) => {
      let settled = false
      // Set only once the request is actually registered in `pending` (i.e. once
      // it's been written, not just queued behind whenConnected()), so the
      // timeout handler knows whether there's an entry it needs to clean up.
      let sentId: string | null = null
      const timer = setTimeout(() => {
        settled = true
        // Without this, a timed-out call left its entry in `pending` forever —
        // nothing else ever deletes it (a real response can't arrive for an id
        // the daemon was never going to answer, and the map is otherwise only
        // cleared wholesale on stop()/disconnect), so a client that keeps
        // timing out (a wedged daemon, a method that never replies) leaks one
        // Map entry per call indefinitely.
        if (sentId !== null) this.pending.delete(sentId)
        reject(new Error(`rpc call timed out: ${method}`))
      }, timeoutMs)

      const finish = (fn: () => void): void => {
        if (settled) return
        settled = true
        clearTimeout(timer)
        fn()
      }

      this.whenConnected()
        .then(() => {
          if (settled) return // already timed out while still waiting to connect
          // design §4: RPC ids are strings. Prefixed (not a bare stringified
          // counter) so they read unambiguously as client-generated correlation
          // ids, distinct from the ULID ids design §5 uses for persisted domain
          // objects (session/agent/project rows) — the two are different id
          // spaces that happen to share the RPC envelope's `id` field name.
          const id = `c-${this.nextId++}`
          sentId = id
          const payload = JSON.stringify({ jsonrpc: '2.0', id, method, params: params ?? {} }) + '\n'

          this.pending.set(id, {
            resolve: (value) => finish(() => resolve(value)),
            reject: (err) => finish(() => reject(err))
          })

          this.socket!.write(payload, (err) => {
            if (err) {
              this.pending.delete(id)
              finish(() => reject(err))
            }
          })
        })
        .catch((err: Error) => finish(() => reject(err)))
    })
  }

  onNotification(method: string, handler: NotificationHandler): () => void {
    let handlers = this.notificationHandlers.get(method)
    if (!handlers) {
      handlers = new Set()
      this.notificationHandlers.set(method, handlers)
    }
    handlers.add(handler)
    return () => handlers!.delete(handler)
  }

  /** Fires for every notification regardless of method — used to relay to the renderer. */
  onAnyNotification(handler: (method: string, params: unknown) => void): () => void {
    this.anyNotificationHandlers.add(handler)
    return () => this.anyNotificationHandlers.delete(handler)
  }
}
