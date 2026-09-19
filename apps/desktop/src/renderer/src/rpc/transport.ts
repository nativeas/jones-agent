/**
 * Renderer-side transport contract (01-w2-interfaces.md §5): "在 renderer 内实现
 * `RpcTransport` 接口 + `MockTransport`（vitest 与 `pnpm dev:mock` 用），真实实现走
 * `window.jones.rpc`". Every store talks to this interface, never to
 * `window.jones` directly — that's what lets the whole UI run under vitest and
 * under `pnpm dev:mock` without an Electron main process or a daemon socket.
 */

export interface RpcCallResult<T = unknown> {
  ok: boolean
  result?: T
  message?: string
}

export interface RpcTransport {
  call<T = unknown>(method: string, params?: Record<string, unknown>): Promise<RpcCallResult<T>>
  on(method: string, callback: (params: unknown) => void): () => void
}
