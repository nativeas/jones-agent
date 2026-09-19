import { useEffect, useState } from 'react'
import { getTransport } from './rpc'
import { useLayoutStore } from './store/layoutStore'
import { useSessionsStore } from './store/sessionsStore'
import { DaemonStatusCard } from './components/DaemonStatusCard'
import { LeftPane } from './components/left/LeftPane'
import { CenterPane } from './components/CenterPane'
import { RightPane } from './components/right/RightPane'
import { SettingsPage } from './components/settings/SettingsPage'

// One transport for the lifetime of this renderer — created once at module
// scope (not per-render) since it owns notification subscriptions.
const transport = getTransport()

type View = 'sessions' | 'settings'

export function App(): JSX.Element {
  const leftPaneOpen = useLayoutStore((s) => s.leftPaneOpen)
  const rightPaneOpen = useLayoutStore((s) => s.rightPaneOpen)
  const toggleLeftPane = useLayoutStore((s) => s.toggleLeftPane)
  const toggleRightPane = useLayoutStore((s) => s.toggleRightPane)

  const initSessions = useSessionsStore((s) => s.init)
  const [view, setView] = useState<View>('sessions')

  useEffect(() => {
    void initSessions(transport)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <div className="shell">
      <header className="shell__topbar">
        <button onClick={toggleLeftPane}>{leftPaneOpen ? '◀' : '▶'} 会话</button>
        <span className="shell__title">Jones</span>
        <nav className="shell__nav">
          <button
            className={view === 'sessions' ? 'shell__nav-btn--active' : ''}
            onClick={() => setView('sessions')}
          >
            会话
          </button>
          <button
            className={view === 'settings' ? 'shell__nav-btn--active' : ''}
            onClick={() => setView('settings')}
          >
            设置
          </button>
        </nav>
        <DaemonStatusCard transport={transport} />
        <button onClick={toggleRightPane}>{rightPaneOpen ? '▶' : '◀'} 详情</button>
      </header>
      {view === 'settings' ? (
        <SettingsPage transport={transport} />
      ) : (
        <div className="shell__body">
          {leftPaneOpen && (
            <aside className="shell__pane shell__pane--left">
              <LeftPane />
            </aside>
          )}
          <CenterPane transport={transport} />
          {rightPaneOpen && (
            <aside className="shell__pane shell__pane--right">
              <RightPane />
            </aside>
          )}
        </div>
      )}
    </div>
  )
}
