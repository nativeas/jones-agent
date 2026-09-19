import type { PermissionRequest } from '../../domain/types'

const RISK_LABEL: Record<PermissionRequest['risk'], string> = { low: '低', medium: '中', high: '高' }
const GATE_LABEL: Record<PermissionRequest['gate'], string> = { rule: '规则闸', review: '审查闸', user: '用户闸' }

interface PermissionPanelProps {
  requests: PermissionRequest[]
  onDecide: (id: string, decision: 'allow' | 'deny', remember?: 'session' | 'project') => void
}

/** 审批面板：`permission.requested` 卡片，允许 / 拒绝 / 记住本会话（PRD FR05）。 */
export function PermissionPanel({ requests, onDecide }: PermissionPanelProps): JSX.Element | null {
  if (requests.length === 0) return null
  return (
    <div className="permission-panel">
      <div className="permission-panel__title">待审批（{requests.length}）</div>
      {requests.map((request) => (
        <div key={request.id} className={`permission-card permission-card--${request.risk}`}>
          <div className="permission-card__meta">
            {GATE_LABEL[request.gate]} · 风险 {RISK_LABEL[request.risk]}
          </div>
          <div className="permission-card__desc">{request.action_description}</div>
          <div className="permission-card__tool">
            {request.tool} · {request.args_summary}
          </div>
          <div className="permission-card__actions">
            <button className="permission-card__allow" onClick={() => onDecide(request.id, 'allow')}>
              允许
            </button>
            <button className="permission-card__deny" onClick={() => onDecide(request.id, 'deny')}>
              拒绝
            </button>
            <button onClick={() => onDecide(request.id, 'allow', 'session')}>允许并记住本会话</button>
          </div>
        </div>
      ))}
    </div>
  )
}
