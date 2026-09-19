import type { RpcTransport } from './transport'
import { MockTransport } from './mockTransport'
import { createWindowTransport } from './windowTransport'

export type { RpcCallResult, RpcTransport } from './transport'
export { MockTransport } from './mockTransport'
export { createWindowTransport } from './windowTransport'

/**
 * Picks the transport for this renderer process:
 * - `pnpm dev:mock` sets `VITE_JONES_TRANSPORT=mock` (see package.json) so the
 *   UI runs against `MockTransport` with no Electron main process and no daemon.
 * - Otherwise, when `window.jones` is present (a real Electron renderer), use
 *   the real bridge.
 * - Falling back to a mock with a loud warning (instead of throwing) covers the
 *   case of previewing `index.html` in a plain browser tab during development.
 */
export function getTransport(): RpcTransport {
  if (import.meta.env.VITE_JONES_TRANSPORT === 'mock') {
    return new MockTransport()
  }
  if (typeof window !== 'undefined' && window.jones?.rpc) {
    return createWindowTransport()
  }
  console.warn('[jones] window.jones.rpc 不存在，回退到 MockTransport（非 Electron 环境）')
  return new MockTransport()
}
