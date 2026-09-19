import { afterEach, describe, expect, it } from 'vitest'
import { act } from 'react-dom/test-utils'
import { createRoot, type Root } from 'react-dom/client'
import { CenterPane } from '../CenterPane'
import { MockTransport } from '../../rpc/mockTransport'
import { useSessionsStore } from '../../store/sessionsStore'
import { useChatStore } from '../../store/chatStore'
import { useSettingsStore } from '../../store/settingsStore'
import type { Session } from '../../domain/types'

/** Round-1 review fix (#1/#5, apps/desktop/.../CenterPane.tsx:43): the old
 * selector was `useSettingsStore((s) => s.providers.filter((p) => p.has_key))`
 * — zustand v5's `useStore` feeds the selector result straight into
 * `useSyncExternalStore` with no memoization (v4's
 * `useSyncExternalStoreWithSelector` cache is gone in v5), so `.filter()`
 * inside the selector returned a new array reference on every call. React's
 * `checkIfSnapshotChanged` then saw a "changed" snapshot every render,
 * forever — `Maximum update depth exceeded` the instant CenterPane mounted
 * with a session selected (reviewer confirmed this with a scratch vitest
 * case; reproduces even with an empty `providers` array). None of the
 * existing 113 tests mounted CenterPane itself (only its children, e.g.
 * MessageList.test.tsx / TerminationCard.test.tsx), so this regression class
 * had zero coverage. This test mounts the real component the way the app
 * does, so the same class of bug can't recur silently. */
describe('CenterPane', () => {
  afterEach(() => {
    useSessionsStore.setState({ sessions: [], selectedSessionId: null })
    useChatStore.getState().unbindSession()
    useSettingsStore.setState({ providers: [], models: [], agents: [] })
  })

  it('mounts with a session selected without an infinite render loop', async () => {
    const session: Session = {
      id: 'session_main',
      project_id: 'proj_default',
      agent_id: 'agent_default',
      parent_id: null,
      is_main: true,
      mode: 'chat',
      title: '主会话',
      status: 'idle'
    }
    useSessionsStore.setState({ sessions: [session], selectedSessionId: session.id })

    const transport = new MockTransport({ schedule: (fn) => fn() })
    const container = document.createElement('div')
    document.body.appendChild(container)
    let root: Root | null = null
    try {
      await act(async () => {
        root = createRoot(container)
        root.render(<CenterPane transport={transport} />)
      })
      // Getting this far without React throwing "Maximum update depth
      // exceeded" (or the mount effects never settling) is the actual
      // assertion; this confirms the mounted DOM is the real center pane,
      // not the "no session selected" placeholder.
      expect(container.querySelector('.center-pane')).not.toBeNull()
    } finally {
      act(() => root?.unmount())
      container.remove()
    }
  })
})
