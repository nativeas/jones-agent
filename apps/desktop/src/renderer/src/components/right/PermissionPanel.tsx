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

/** 评审第 3 轮 critical：`tool_call.rawInput.description` carries this same
 * marker string verbatim (it's what Hermes's `_build_permission_tool_call`
 * copies `message` into — see the constant above), and decoding it is NOT
 * "guessing" a shell command — it's a JSON format Jones itself defines and
 * documents (`_review_payload.py::encode`/`decode`, docs/design/
 * 02-w3-interfaces.md §1.2). Mirrors that Python `decode()` function
 * field-for-field so the one place this shape is parsed on the daemon side
 * and the one place it's parsed here stay in lockstep. Returns `null` for
 * anything that isn't this exact shape — callers must fall back to the
 * title-based placeholder, never guess. */
function decodeReviewPayload(
  rawInput: unknown
): { tool: string; args: Record<string, unknown>; argsTruncated: boolean } | null {
  if (!rawInput || typeof rawInput !== 'object') return null
  const description = (rawInput as { description?: unknown }).description
  if (typeof description !== 'string' || !description.startsWith(REVIEW_PAYLOAD_MARKER)) return null
  let payload: unknown
  try {
    payload = JSON.parse(description.slice(REVIEW_PAYLOAD_MARKER.length))
  } catch {
    return null
  }
  if (!payload || typeof payload !== 'object' || typeof (payload as { tool?: unknown }).tool !== 'string') {
    return null
  }
  const p = payload as { tool: string; args_json?: unknown; args_truncated?: unknown }
  let args: Record<string, unknown> = {}
  if (!p.args_truncated && typeof p.args_json === 'string') {
    try {
      const parsedArgs: unknown = JSON.parse(p.args_json)
      if (parsedArgs && typeof parsedArgs === 'object') args = parsedArgs as Record<string, unknown>
    } catch {
      // Same fail-open-to-empty-args behavior as the Python `decode()` —
      // a truncated/malformed args_json still leaves `tool` (and thus the
      // card) readable, it just can't show the args too.
    }
  }
  return { tool: p.tool, args, argsTruncated: Boolean(p.args_truncated) }
}

interface PermissionPanelProps {
  requests: PermissionRequest[]
  onDecide: (id: string, decision: 'allow' | 'deny', remember?: 'session' | 'project') => void
}

/** 审批面板：`permission.requested` 卡片，允许 / 拒绝 / 记住本会话（PRD FR05）。
 *
 * 按 03-w4-interfaces.md §5 的要求，这里只渲染 daemon 载荷里已经有的字段
 * （`tool_call.title`/`tool_call.rawInput`、`reasons`）——不猜测解析
 * `write_file`/`patch` 走的那种编辑审批 `rawInput` 形状（见 domain/types.ts 的
 * `ToolCallInfo` 注释）。
 *
 * 评审第 3 轮 critical：审查闸升级路径（终端命令等，02-w3-interfaces.md §1.4
 * R5 裁定"`tool_call` 本来就携带真实命令/参数，满足原样展示命令"）的
 * `rawInput.description` 携带的是 Jones 自己定义、自己解码的
 * `JONES_REVIEW_V1:{...}` 编码串（`decodeReviewPayload`，格式来源见
 * `_review_payload.py`），不是需要理解 shell 的"猜测解析"——解出来就能满足
 * R5，所以这里解码并渲染真实 `tool`/`args`，不再对这种情况回落到占位文案。
 * 只有解码失败（daemon 没送这个字段、或格式对不上）时才回落到 `title`/占位——
 * 这才是"daemon 不含可读字段时只做占位"该覆盖的范围。 */
export function PermissionPanel({ requests, onDecide }: PermissionPanelProps): JSX.Element | null {
  if (requests.length === 0) return null
  return (
    <div className="permission-panel">
      <div className="permission-panel__title">待审批（{requests.length}）</div>
      {requests.map((request) => {
        const title = request.tool_call.title
        const readableTitle = title && !title.startsWith(REVIEW_PAYLOAD_MARKER) ? title : undefined
        const decoded = decodeReviewPayload(request.tool_call.rawInput)
        const command = decoded && typeof decoded.args.command === 'string' ? decoded.args.command : null
        return (
          <div key={request.request_id} className={`permission-card permission-card--${request.risk}`}>
            <div className="permission-card__meta">
              {GATE_LABEL[request.gate]} · 风险 {RISK_LABEL[request.risk] ?? request.risk}
            </div>
            {decoded ? (
              <div className="permission-card__desc">
                <div>
                  工具：<code>{decoded.tool}</code>
                </div>
                <div>
                  命令/参数：<code>{command ?? JSON.stringify(decoded.args)}</code>
                  {decoded.argsTruncated && '（参数已截断）'}
                </div>
              </div>
            ) : (
              <div className="permission-card__desc">{readableTitle ?? '（daemon 未提供可读的动作描述）'}</div>
            )}
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
