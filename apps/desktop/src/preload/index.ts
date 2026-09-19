import { contextBridge, ipcRenderer } from 'electron'

/**
 * The only surface exposed to the renderer: `rpc.call` / `rpc.on` (design §3: renderer
 * has no Node access, only contextBridge IPC). No other Electron or Node API is bridged.
 */
export interface RpcCallResult {
  ok: boolean
  result?: unknown
  message?: string
}

export interface PickDirectoryResult {
  canceled: boolean
  path?: string
}

const jonesApi = {
  rpc: {
    call: (method: string, params?: Record<string, unknown>): Promise<RpcCallResult> =>
      ipcRenderer.invoke('rpc:call', method, params),
    on: (method: string, callback: (params: unknown) => void): (() => void) => {
      const listener = (_event: unknown, notifiedMethod: string, params: unknown): void => {
        if (notifiedMethod === method) callback(params)
      }
      ipcRenderer.on('rpc:notify', listener)
      return () => ipcRenderer.removeListener('rpc:notify', listener)
    }
  },
  // 02-w3-interfaces.md §2 集成收口 #5: a native folder picker for the Project
  // 设置页 (FR02) — its own narrow channel, not routed through `rpc.call`
  // (that bridge is specifically the daemon RPC transport; this is a Node/
  // Electron-only capability the daemon has no part in).
  dialog: {
    pickDirectory: (): Promise<PickDirectoryResult> => ipcRenderer.invoke('dialog:pickDirectory')
  }
}

export type JonesApi = typeof jonesApi

contextBridge.exposeInMainWorld('jones', jonesApi)
