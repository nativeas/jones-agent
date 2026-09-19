export {}

interface RpcCallResult {
  ok: boolean
  result?: unknown
  message?: string
}

interface PickDirectoryResult {
  canceled: boolean
  path?: string
}

declare global {
  interface Window {
    jones: {
      rpc: {
        call: (method: string, params?: Record<string, unknown>) => Promise<RpcCallResult>
        on: (method: string, callback: (params: unknown) => void) => () => void
      }
      // 02-w3-interfaces.md §2 集成收口 #5: native folder picker for the
      // Project 设置页 (FR02) — see preload/index.ts.
      dialog: {
        pickDirectory: () => Promise<PickDirectoryResult>
      }
    }
  }
}
