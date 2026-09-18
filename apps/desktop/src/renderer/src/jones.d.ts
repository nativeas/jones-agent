export {}

interface RpcCallResult {
  ok: boolean
  result?: unknown
  message?: string
}

declare global {
  interface Window {
    jones: {
      rpc: {
        call: (method: string, params?: Record<string, unknown>) => Promise<RpcCallResult>
        on: (method: string, callback: (params: unknown) => void) => () => void
      }
    }
  }
}
