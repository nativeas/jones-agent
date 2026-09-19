import { useEffect } from 'react'
import type { RpcTransport } from '../rpc/transport'
import type { SessionMode } from '../domain/types'
import { useChatStore } from '../store/chatStore'
import { useSessionsStore } from '../store/sessionsStore'
import { useSettingsStore } from '../store/settingsStore'
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
  const queueSuspendedReason = useChatStore((s) => s.queueSuspendedReason)
  const queueResume = useChatStore((s) => s.queueResume)
  const send = useChatStore((s) => s.send)
  const stop = useChatStore((s) => s.stop)
  const retryTermination = useChatStore((s) => s.retryTermination)
  const switchModelTermination = useChatStore((s) => s.switchModelTermination)
  const abandonTermination = useChatStore((s) => s.abandonTermination)
  const removeQueueItem = useChatStore((s) => s.removeQueueItem)
  const reorderQueue = useChatStore((s) => s.reorderQueue)
  const error = useChatStore((s) => s.error)
  // Round-2 review #2/#6: per-card in-flight guard + "already used" marker,
  // threaded down to TerminationCard via MessageList.
  const pendingTerminations = useChatStore((s) => s.pendingTerminations)
  const handledTerminations = useChatStore((s) => s.handledTerminations)
  // Issue #22 (04-w5-interfaces.md §4): the "换模型" card action needs a real
  // provider/model list — only providers with a configured Key are offered
  // (picking one without a Key would just fail `session.retry` the same way
  // the original Turn did).
  // Round-1 review fix: the old selector was `(s) => s.providers.filter(...)`
  // — zustand v5's `useStore` feeds the selector result straight into
  // `useSyncExternalStore` with no memoization (v4's
  // `useSyncExternalStoreWithSelector` cache is gone in v5), so a `.filter()`
  // inside the selector returns a new array on every call. React's
  // `checkIfSnapshotChanged` then sees a changed snapshot on every render,
  // forever — `Maximum update depth exceeded` on mount, empty `providers` or
  // not. Select the stable `providers` reference and filter in the component
  // body instead.
  const settingsInit = useSettingsStore((s) => s.init)
  const providers = useSettingsStore((s) => s.providers)
  const models = useSettingsStore((s) => s.models)
  // Issue #22 (04-w5-interfaces.md §4): the "换模型" card action needs a real
  // provider/model list — only providers with a configured Key are offered
  // (picking one without a Key would just fail `session.retry` the same way
  // the original Turn did).
  const configuredProviders = providers.filter((p) => p.has_key)

  useEffect(() => {
    void settingsInit(transport)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [transport])

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
          providers={configuredProviders}
          models={models}
          onRetry={(turnId) => void retryTermination(turnId)}
          onSwitchModel={(turnId, override) => void switchModelTermination(turnId, override)}
          onAbandon={(turnId) => void abandonTermination(turnId)}
          pendingTerminations={pendingTerminations}
          handledTerminations={handledTerminations}
        />
      </div>
      <QueuePanel
        items={queue}
        suspendedReason={queueSuspendedReason}
        onRemove={removeQueueItem}
        onReorder={reorderQueue}
        onResume={queueResume}
      />
      <InputBar running={running} onSend={send} onStop={stop} />
    </main>
  )
}
