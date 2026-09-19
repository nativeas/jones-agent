import { describe, expect, it } from 'vitest'
import { act } from 'react-dom/test-utils'
import { createRoot, type Root } from 'react-dom/client'
import { DaemonStatusCard } from '../DaemonStatusCard'
import type { RpcCallResult, RpcTransport } from '../../rpc/transport'

/**
 * The only component-level test in this branch — deliberately: every other
 * test here targets pure functions/store logic per the established
 * convention (no @testing-library/react). This one exists because the bug
 * it guards (§8 review: main's pushed `daemon.error` had no renderer
 * listener at all) can only be demonstrated by observing that a mounted
 * card actually reacts to a pushed notification, not by unit-testing a
 * payload-formatting helper in isolation. Uses only react-dom (already a
 * dependency) — no new test-rendering library added.
 */
describe('DaemonStatusCard', () => {
  it('renders the daemon.error notification main pushes after giving up retrying, not just the mount-time ping result', async () => {
    let errorHandler: ((params: unknown) => void) | null = null
    const transport: RpcTransport = {
      call: async <T,>(): Promise<RpcCallResult<T>> => ({
        ok: true,
        result: { version: '1.0.0', pid: 1, uptime_s: 0 } as T
      }),
      on: (method, cb) => {
        if (method === 'daemon.error') errorHandler = cb
        return () => {
          errorHandler = null
        }
      }
    }

    const container = document.createElement('div')
    document.body.appendChild(container)
    let root: Root | null = null
    try {
      await act(async () => {
        root = createRoot(container)
        root.render(<DaemonStatusCard transport={transport} />)
      })

      // The mount-time ping resolved ok — the card should not be showing an error yet.
      expect(container.textContent).not.toContain('daemon 报告了一个错误')
      expect(errorHandler).not.toBeNull()

      act(() => {
        errorHandler!({ code: 'daemon_unreachable', message: '重试 3 次后仍无法连接 daemon' })
      })

      expect(container.textContent).toContain('重试 3 次后仍无法连接 daemon')
    } finally {
      act(() => {
        root?.unmount()
      })
      container.remove()
    }
  })
})
