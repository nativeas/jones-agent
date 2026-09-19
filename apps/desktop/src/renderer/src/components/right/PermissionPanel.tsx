import type { PermissionRequest } from '../../domain/types'

const RISK_LABEL: Record<PermissionRequest['risk'], string> = {
  low: '低',
  medium: '中',
  high: '高',
  unclassified: '未分级'
}
const GATE_LABEL: Record<PermissionRequest['gate'], string> = { rule: '规则闸', review: '审查闸', user: '用户闸' }

interface PermissionPanelProps {
  requests: PermissionRequest[]
  onDecide: (id: string, decision: 'allow' | 'deny', remember?: 'session' | 'project') => void
}

/** 审批面板：`permission.requested` 卡片，允许 / 拒绝 / 记住本会话（PRD FR05）。
 *
 * 按 03-w4-interfaces.md §5 的要求，这里只渲染 daemon 载荷里已经有的字段
 * （`tool_call.title`、`reasons`）——不在这里猜测解析 `tool_call.rawInput`
 * 的两种编码形状（见 domain/types.ts 的 `ToolCallInfo` 注释）。daemon 目前的
 * 真实 payload 不含解码后的工具名/参数，所以这里能给的可读性也就到 title +
 * reasons 为止；缺口写在了这条分支的报告里，不是 renderer 能补的。 */
export function PermissionPanel({ requests, onDecide }: PermissionPanelProps): JSX.Element | null {
  if (requests.length === 0) return null
  return (
    <div className="permission-panel">
      <div className="permission-panel__title">待审批（{requests.length}）</div>
      {requests.map((request) => (
        <div key={request.request_id} className={`permission-card permission-card--${request.risk}`}>
          <div className="permission-card__meta">
            {GATE_LABEL[request.gate]} · 风险 {RISK_LABEL[request.risk] ?? request.risk}
          </div>
          <div className="permission-card__desc">{request.tool_call.title ?? '（daemon 未提供动作描述）'}</div>
          {request.reasons && request.reasons.length > 0 && (
            <ul className="permission-card__reasons">
              {request.reasons.map((reason, i) => (
                <li key={i}>{reason}</li>
              ))}
            </ul>
          )}
          <div className="permission-card__actions">
            <button className="permission-card__allow" onClick={() => onDecide(request.request_id, 'allow')}>
              允许
            </button>
            <button className="permission-card__deny" onClick={() => onDecide(request.request_id, 'deny')}>
              拒绝
            </button>
            <button onClick={() => onDecide(request.request_id, 'allow', 'session')}>允许并记住本会话</button>
          </div>
        </div>
      ))}
    </div>
  )
}
