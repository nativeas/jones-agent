import type { RpcCallResult, RpcTransport } from './transport'

/**
 * Adapts `window.jones.rpc` (exposed by the sandboxed preload, see
 * src/preload/index.ts) to the `RpcTransport` interface. The bridge is
 * installed by Electron *before* any renderer script runs (contextBridge runs
 * ahead of page scripts by construction), so `window.jones` itself is never
 * missing in a real Electron window — what *can* still fail is the first call:
 * main's `ipcMain.handle('rpc:call', ...)` rejects with `ok:false` for methods
 * outside its allowlist, and 01-w2-interfaces.md §5's "known issue" is exactly
 * about a first call landing before that handler exists at all. Either way this
 * adapter must surface the failure through the normal `RpcCallResult`/rejection
 * path rather than swallow it — callers (the stores) already render an explicit
 * error state for a rejected/`ok:false` call.
 */
export function createWindowTransport(): RpcTransport {
  return {
    call<T>(method: string, params?: Record<string, unknown>): Promise<RpcCallResult<T>> {
      if (typeof window === 'undefined' || !window.jones?.rpc) {
        // Should not happen in a real Electron window (see above), but guards
        // against ever silently hanging if this file is loaded somewhere the
        // bridge genuinely isn't present — an explicit rejection beats a promise
        // that never settles.
        return Promise.resolve({ ok: false, message: 'window.jones.rpc 未就绪' })
      }
      return window.jones.rpc.call(method, params) as Promise<RpcCallResult<T>>
    },
    on(method: string, callback: (params: unknown) => void): () => void {
      if (typeof window === 'undefined' || !window.jones?.rpc) return () => {}
      return window.jones.rpc.on(method, callback)
    }
  }
}
