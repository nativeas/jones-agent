import { useEffect } from 'react'
import { getTransport } from './rpc'
import { useLayoutStore } from './store/layoutStore'
import { useSessionsStore } from './store/sessionsStore'
import { useNavigationStore } from './store/navigationStore'
import { DaemonStatusCard } from './components/DaemonStatusCard'
import { ErrorBoundary } from './components/ErrorBoundary'
import { LeftPane } from './components/left/LeftPane'
import { CenterPane } from './components/CenterPane'
import { RightPane } from './components/right/RightPane'
import { SettingsPage } from './components/settings/SettingsPage'

// One transport for the lifetime of this renderer — created once at module
// scope (not per-render) since it owns notification subscriptions.
const transport = getTransport()

export function App(): JSX.Element {
  const leftPaneOpen = useLayoutStore((s) => s.leftPaneOpen)
  const rightPaneOpen = useLayoutStore((s) => s.rightPaneOpen)
  const toggleLeftPane = useLayoutStore((s) => s.toggleLeftPane)
  const toggleRightPane = useLayoutStore((s) => s.toggleRightPane)

  const initSessions = useSessionsStore((s) => s.init)
  const view = useNavigationStore((s) => s.view)
  const setView = useNavigationStore((s) => s.setView)

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
      {/* jones-agent#34 / PRD G08 / N16: one bad render below (a message
       * whose shape drifted from what the daemon actually sent, e.g.) must
       * not take the whole shell — including the topbar's session/settings
       * nav above — down with it.
       *
       * 评审第 1 轮 #4：`key={view}` — 没有它，boundary 的 `state.error` 和
       * `children` 无关，会话区崩溃后点"设置"切到 SettingsPage，React 认为
       * 还是同一个 ErrorBoundary 实例，继续渲染 fallback（导航"能点"但画面
       * 不动，对用户等同白屏）。换 `view` 时 key 变化 → React 卸载重挂这个
       * 子树 → boundary 的 state 随之重置，真正做到"崩溃后导航仍可用"。 */}
      <ErrorBoundary key={view}>
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
                <RightPane transport={transport} />
              </aside>
            )}
          </div>
        )}
      </ErrorBoundary>
    </div>
  )
}
