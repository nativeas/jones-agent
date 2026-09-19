import type { PermissionRequest } from '../../domain/types'

const RISK_LABEL: Record<PermissionRequest['risk'], string> = {
  low: '低',
  medium: '中',
  high: '高',
  unclassified: '未分级'
}
const GATE_LABEL: Record<PermissionRequest['gate'], string> = { rule: '规则闸', review: '审查闸', user: '用户闸' }

// `daemon/src/jones_daemon/kernel/plugin/jones_gate/_review_payload.py::MARKER`
// — the review-gate escalation path (`jones_gate/__init__.py`'s "approve"
// verdict) hands Hermes an encoded `JONES_REVIEW_V1:{"tool":...,"args_json":...}`
// string as its `message`, which the installed hermes-agent then threads through
// unmodified as `description` (`tools/approval.py`) and finally
// `title=f"{description}: {command}"` (`acp_adapter/permissions.py`) — so a
// review-gate card's real `tool_call.title` is this marker string, not a
// human sentence. Kept as a literal (not imported) because renderer can't
// import daemon Python; this is the one string both sides must agree on.
const REVIEW_PAYLOAD_MARKER = 'JONES_REVIEW_V1:'

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
 * reasons 为止；缺口写在了这条分支的报告里，不是 renderer 能补的。
 *
 * 评审第 1 轮修复：审查闸升级上来的卡片，`tool_call.title` 实际是未解码的
 * `JONES_REVIEW_V1:{...}` 编码串（见上面 `REVIEW_PAYLOAD_MARKER` 注释），
 * 不是人话——直接渲染既不是占位也不可读。§5 的要求是"daemon 不含解码后的
 * 工具名/参数时只在 UI 做占位"，所以这里检测这个前缀，命中就回落到占位 +
 * reasons，而不是原样展示编码串。 */
export function PermissionPanel({ requests, onDecide }: PermissionPanelProps): JSX.Element | null {
  if (requests.length === 0) return null
  return (
    <div className="permission-panel">
      <div className="permission-panel__title">待审批（{requests.length}）</div>
      {requests.map((request) => {
        const title = request.tool_call.title
        const readableTitle = title && !title.startsWith(REVIEW_PAYLOAD_MARKER) ? title : undefined
        return (
          <div key={request.request_id} className={`permission-card permission-card--${request.risk}`}>
            <div className="permission-card__meta">
              {GATE_LABEL[request.gate]} · 风险 {RISK_LABEL[request.risk] ?? request.risk}
            </div>
            <div className="permission-card__desc">{readableTitle ?? '（daemon 未提供可读的动作描述）'}</div>
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
        )
      })}
    </div>
  )
}
