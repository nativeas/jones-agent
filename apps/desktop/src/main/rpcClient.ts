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

export class RpcClient {
  private socket: net.Socket | null = null
  private leftover: Buffer = Buffer.alloc(0)
  private nextId = 1
  private pending = new Map<string | number, PendingCall>()
  private notificationHandlers = new Map<string, Set<NotificationHandler>>()
  private anyNotificationHandlers = new Set<(method: string, params: unknown) => void>()
  private reconnectAttempt = 0
  private reconnectTimer: NodeJS.Timeout | null = null
  private stopped = false
  private connected = false
  private connectedWaiters: Array<{ resolve: () => void; reject: (err: Error) => void }> = []

  constructor(private readonly socketPath: string) {}

  connect(): void {
    this.stopped = false
    // Idempotent: a second call while a socket is already open or mid-connect
    // (e.g. macOS `activate` firing connect() again after window-all-closed
    // already stopped and reopened one) must not leak a duplicate socket.
    if (this.socket && !this.socket.destroyed) return
    this.openSocket()
  }

  stop(): void {
    this.stopped = true
    this.connected = false
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }
    this.socket?.destroy()
    this.socket = null
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
      this.connected = true
      this.reconnectAttempt = 0
      const waiters = this.connectedWaiters
      this.connectedWaiters = []
      waiters.forEach(({ resolve }) => resolve())
    })
    socket.on('data', (chunk) => this.handleData(chunk))
    socket.on('error', () => {
      // 'close' fires right after; reconnect scheduling happens there so it only
      // happens once per disconnect.
    })
    socket.on('close', () => this.handleClose())
  }

  private handleClose(): void {
    this.connected = false
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
      if (!this.stopped) this.openSocket()
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
    const call = this.pending.get(response.id)
    if (!call) return
    this.pending.delete(response.id)
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
    if (this.connected) return Promise.resolve()
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
      const timer = setTimeout(() => {
        settled = true
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
          // design §4: RPC ids are strings.
          const id = String(this.nextId++)
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
