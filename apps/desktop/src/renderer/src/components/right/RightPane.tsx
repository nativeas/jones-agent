import { useState } from 'react'
import type { RpcTransport } from '../../rpc/transport'
import { useChatStore } from '../../store/chatStore'
import { useSessionsStore } from '../../store/sessionsStore'
import { StepCard } from '../chat/StepCard'
import { PermissionPanel } from './PermissionPanel'
import { ReplayView } from './ReplayView'

type RightTab = 'live' | 'replay'

/** 右栏：动作流（Step 时间线，实时）+ 审批面板（01-w2-interfaces.md §5）+
 * 回放视图（PRD FR06 / 02-w3-interfaces.md §2，issue #12 新增，纯读）。 */
export function RightPane({ transport }: { transport: RpcTransport }): JSX.Element {
  const [tab, setTab] = useState<RightTab>('live')
  const timeline = useChatStore((s) => s.timeline)
  const pendingPermissions = useChatStore((s) => s.pendingPermissions)
  const decidePermission = useChatStore((s) => s.decidePermission)
  const selectedSessionId = useSessionsStore((s) => s.selectedSessionId)

  const steps = timeline.filter((e) => e.kind === 'step').map((e) => e.step)

  return (
    <div className="right-pane">
      <div className="right-pane__tabs">
        <button
          className={tab === 'live' ? 'right-pane__tab--active' : ''}
          onClick={() => setTab('live')}
        >
          动作流
        </button>
        <button
          className={tab === 'replay' ? 'right-pane__tab--active' : ''}
          onClick={() => setTab('replay')}
        >
          回放
        </button>
      </div>

      {tab === 'live' ? (
        <>
          <PermissionPanel requests={pendingPermissions} onDecide={decidePermission} />
          <div className="action-stream">
            <div className="action-stream__title">动作流</div>
            {steps.length === 0 && <p className="shell__placeholder">本会话暂无动作</p>}
            {steps.map((step) => (
              <StepCard key={step.id} step={step} />
            ))}
          </div>
        </>
      ) : (
        <ReplayView transport={transport} sessionId={selectedSessionId} />
      )}
    </div>
  )
}
