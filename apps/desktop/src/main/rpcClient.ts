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
  private connectedResolvers: Array<() => void> = []

  constructor(private readonly socketPath: string) {}

  connect(): void {
    this.stopped = false
    this.openSocket()
  }

  stop(): void {
    this.stopped = true
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
  }

  private openSocket(): void {
    const socket = net.createConnection(this.socketPath)
    this.socket = socket

    socket.on('connect', () => {
      this.reconnectAttempt = 0
      const resolvers = this.connectedResolvers
      this.connectedResolvers = []
      resolvers.forEach((resolve) => resolve())
    })
    socket.on('data', (chunk) => this.handleData(chunk))
    socket.on('error', () => {
      // 'close' fires right after; reconnect scheduling happens there so it only
      // happens once per disconnect.
    })
    socket.on('close', () => this.handleClose())
  }

  private handleClose(): void {
    this.leftover = Buffer.alloc(0)
    for (const [, call] of this.pending) {
      call.reject(new Error('daemon connection closed'))
    }
    this.pending.clear()
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
    if (this.socket && !this.socket.pending) return Promise.resolve()
    return new Promise((resolve) => this.connectedResolvers.push(resolve))
  }

  async call(method: string, params?: Record<string, unknown>, timeoutMs = 10_000): Promise<unknown> {
    await this.whenConnected()
    const id = this.nextId++
    const payload = JSON.stringify({ jsonrpc: '2.0', id, method, params: params ?? {} }) + '\n'

    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id)
        reject(new Error(`rpc call timed out: ${method}`))
      }, timeoutMs)

      this.pending.set(id, {
        resolve: (value) => {
          clearTimeout(timer)
          resolve(value)
        },
        reject: (err) => {
          clearTimeout(timer)
          reject(err)
        }
      })

      this.socket!.write(payload, (err) => {
        if (err) {
          clearTimeout(timer)
          this.pending.delete(id)
          reject(err)
        }
      })
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
