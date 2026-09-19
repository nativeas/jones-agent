import { useChatStore } from '../../store/chatStore'
import { StepCard } from '../chat/StepCard'
import { PermissionPanel } from './PermissionPanel'

/** 右栏：动作流（Step 时间线）+ 审批面板（01-w2-interfaces.md §5）。 */
export function RightPane(): JSX.Element {
  const timeline = useChatStore((s) => s.timeline)
  const pendingPermissions = useChatStore((s) => s.pendingPermissions)
  const decidePermission = useChatStore((s) => s.decidePermission)

  const steps = timeline.filter((e) => e.kind === 'step').map((e) => e.step)

  return (
    <div className="right-pane">
      <PermissionPanel requests={pendingPermissions} onDecide={decidePermission} />
      <div className="action-stream">
        <div className="action-stream__title">动作流</div>
        {steps.length === 0 && <p className="shell__placeholder">本会话暂无动作</p>}
        {steps.map((step) => (
          <StepCard key={step.id} step={step} />
        ))}
      </div>
    </div>
  )
}
