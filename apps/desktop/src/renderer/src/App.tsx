import { DaemonStatusCard } from './components/DaemonStatusCard'
import { useLayoutStore } from './store/layoutStore'

export function App(): JSX.Element {
  const leftPaneOpen = useLayoutStore((s) => s.leftPaneOpen)
  const rightPaneOpen = useLayoutStore((s) => s.rightPaneOpen)
  const toggleLeftPane = useLayoutStore((s) => s.toggleLeftPane)
  const toggleRightPane = useLayoutStore((s) => s.toggleRightPane)

  return (
    <div className="shell">
      <header className="shell__topbar">
        <button onClick={toggleLeftPane}>{leftPaneOpen ? '◀' : '▶'} 会话</button>
        <span className="shell__title">Jones</span>
        <button onClick={toggleRightPane}>{rightPaneOpen ? '▶' : '◀'} 详情</button>
      </header>
      <div className="shell__body">
        {leftPaneOpen && (
          <aside className="shell__pane shell__pane--left">
            <p className="shell__placeholder">会话列表（后续 Issue）</p>
          </aside>
        )}
        <main className="shell__pane shell__pane--center">
          <DaemonStatusCard />
          <p className="shell__placeholder">对话区域（后续 Issue）</p>
        </main>
        {rightPaneOpen && (
          <aside className="shell__pane shell__pane--right">
            <p className="shell__placeholder">回放 / 详情（后续 Issue）</p>
          </aside>
        )}
      </div>
    </div>
  )
}
