import { describe, expect, it, vi } from 'vitest'
import { act } from 'react-dom/test-utils'
import { createRoot, type Root } from 'react-dom/client'
import { ErrorBoundary } from '../ErrorBoundary'

function Bomb(): JSX.Element {
  throw new Error('boom: renderer crashed')
}

/** jones-agent#34 / PRD G08 N16: a render error anywhere below this boundary
 * must show a recoverable fallback, not take the whole app down (a white
 * screen). Component-mount test, same DaemonStatusCard.test.tsx precedent as
 * MessageList.test.tsx — the behavior under test (catching a thrown render
 * error) only exists at the DOM render layer. */
describe('ErrorBoundary', () => {
  it('catches a child render error and shows a recoverable fallback instead of propagating', () => {
    // React logs the caught error to console.error even when a boundary
    // handles it — expected noise for this test, silenced so it doesn't
    // pollute the real reporter output.
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})

    const container = document.createElement('div')
    document.body.appendChild(container)
    let root: Root | null = null
    try {
      expect(() => {
        act(() => {
          root = createRoot(container)
          root.render(
            <ErrorBoundary>
              <Bomb />
            </ErrorBoundary>
          )
        })
      }).not.toThrow()

      expect(container.textContent).toContain('界面出错了')
      expect(container.textContent).toContain('boom: renderer crashed')
    } finally {
      act(() => {
        root?.unmount()
      })
      container.remove()
      consoleError.mockRestore()
    }
  })
})
