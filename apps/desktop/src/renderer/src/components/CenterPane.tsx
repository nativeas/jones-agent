import { useEffect } from 'react'
import type { RpcTransport } from '../rpc/transport'
import type { SessionMode } from '../domain/types'
import { useChatStore } from '../store/chatStore'
import { useSessionsStore } from '../store/sessionsStore'
import { useNavigationStore } from '../store/navigationStore'
import { MessageList } from './chat/MessageList'
import { InputBar } from './chat/InputBar'
import { QueuePanel } from './chat/QueuePanel'

const MODE_LABEL: Record<SessionMode, string> = { chat: '纯对话', task: '任务', auto: '自动' }
const MODES: SessionMode[] = ['chat', 'task', 'auto']

interface CenterPaneProps {
  transport: RpcTransport
}

/** 中栏：消息流 + 输入框 + 队列面板 + 停止（01-w2-interfaces.md §5）。 */
export function CenterPane({ transport }: CenterPaneProps): JSX.Element {
  const selectedSessionId = useSessionsStore((s) => s.selectedSessionId)
  const sessions = useSessionsStore((s) => s.sessions)
  const setMode = useSessionsStore((s) => s.setMode)
  const session = sessions.find((s) => s.id === selectedSessionId)

  const bindSession = useChatStore((s) => s.bindSession)
  const unbindSession = useChatStore((s) => s.unbindSession)
  const timeline = useChatStore((s) => s.timeline)
  const running = useChatStore((s) => s.running)
  const queue = useChatStore((s) => s.queue)
  const send = useChatStore((s) => s.send)
  const stop = useChatStore((s) => s.stop)
  const retryLastMessage = useChatStore((s) => s.retryLastMessage)
  const dismissTermination = useChatStore((s) => s.dismissTermination)
  const removeQueueItem = useChatStore((s) => s.removeQueueItem)
  const reorderQueue = useChatStore((s) => s.reorderQueue)
  const error = useChatStore((s) => s.error)
  const goToAgentModelSettings = useNavigationStore((s) => s.goToAgentModelSettings)

  useEffect(() => {
    if (!selectedSessionId) return
    void bindSession(transport, selectedSessionId)
    return () => unbindSession()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedSessionId])

  if (!session) {
    return (
      <main className="shell__pane shell__pane--center">
        <p className="shell__placeholder">左栏选择一个会话开始</p>
      </main>
    )
  }

  return (
    <main className="shell__pane shell__pane--center center-pane">
      <div className="center-pane__header">
        <span className="center-pane__title">{session.title}</span>
        <div className="center-pane__modes">
          {MODES.map((mode) => (
            <button
              key={mode}
              className={`mode-pill${session.mode === mode ? ' mode-pill--active' : ''}`}
              onClick={() => void setMode(session.id, mode)}
            >
              {MODE_LABEL[mode]}
            </button>
          ))}
        </div>
      </div>
      {error && <div className="center-pane__error">{error}</div>}
      <div className="center-pane__body">
        <MessageList
          timeline={timeline}
          onRetry={() => void retryLastMessage()}
          onSwitchModel={goToAgentModelSettings}
          onAbandon={dismissTermination}
        />
      </div>
      <QueuePanel items={queue} onRemove={removeQueueItem} onReorder={reorderQueue} />
      <InputBar running={running} onSend={send} onStop={stop} />
    </main>
  )
}
