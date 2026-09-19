import { useState } from 'react'
import type { CardAction, ErrorKind, Model, Provider, TerminationCard as TerminationCardData } from '../../domain/types'

interface TerminationCardProps {
  card: TerminationCardData
  /** Only providers with a configured key — the "换模型" picker has nothing
   * useful to offer for one without a Key anyway. */
  providers: Provider[]
  models: Model[]
  onRetry?: () => void
  onSwitchModel?: (override: { provider: string; model: string }) => void
  onAbandon?: () => void
  /** Round-2 review #6: awaiting this card's `session.retry` round-trip —
   * disable every action button so a second click can't fire a second real
   * Turn/model call before the first resolves. */
  pending?: boolean
  /** Round-2 review #2/#6: which action already completed successfully for
   * this card, if any — renders a status line instead of live buttons so a
   * used-up card can never be actioned again, and so "放弃" has a visible
   * effect even when the queue it cleared was already empty.
   *
   * Round-N2 review #2: `clearedQueueItems` is `session.retry(action:
   * "abandon")`'s own `cleared_queue_items` count, threaded through from the
   * RPC result — the "放弃" status line used to be a single unconditional
   * sentence claiming the queue was cleared regardless of whether it actually
   * had anything in it (or anything left in it, see 04-w5-interfaces.md
   * §4.2's open item on `_advance_queue`), which is untrue in the common
   * (empty-queue) case. Left `undefined` for `retry`/`switch_model`, which
   * don't touch the queue. */
  handled?: { action: CardAction; clearedQueueItems?: number }
}

/** Issue #22 (FR14, 04-w5-interfaces.md §4) — daemon classifies, this only
 * renders: one style per `ErrorKind`, folded down to 7 distinct visual
 * buckets (`errors/classify.py::VISUALLY_DISTINCT_KINDS`) — `provider_error`/
 * `internal` share the generic fallback rather than getting their own
 * color/icon, matching that module's own comment on why. */
const KIND_STYLE: Record<ErrorKind, { label: string; className: string; icon: string }> = {
  network: { label: '网络', className: 'termination-card--network', icon: '⚠' },
  provider_quota: { label: '配额', className: 'termination-card--quota', icon: '⛔' },
  provider_auth: { label: '认证', className: 'termination-card--auth', icon: '🔑' },
  tool_exception: { label: '工具', className: 'termination-card--tool', icon: '🛠' },
  worker_crash: { label: '崩溃', className: 'termination-card--crash', icon: '💥' },
  approval_timeout: { label: '超时', className: 'termination-card--timeout', icon: '⏱' },
  budget: { label: '预算', className: 'termination-card--budget-kind', icon: '📊' },
  provider_error: { label: '出错', className: 'termination-card--generic', icon: '❗' },
  internal: { label: '出错', className: 'termination-card--generic', icon: '❗' }
}

const ACTION_LABEL: Record<CardAction, string> = {
  retry: '重试',
  switch_model: '换模型',
  abandon: '放弃'
}

/** Round-2 review #2/#6: what a used-up card says instead of its buttons. */
const HANDLED_LABEL: Record<'retry' | 'switch_model', string> = {
  retry: '已重试，新的对话已经开始。',
  switch_model: '已换模型重试，新的对话已经开始。'
}

/** Round-N2 review #2: "放弃" splits on the RPC's actual `cleared_queue_items`
 * count instead of always claiming the queue was cleared — see the `handled`
 * prop's doc comment above for why. */
function abandonLabel(clearedQueueItems: number | undefined): string {
  if (!clearedQueueItems) return '已放弃这条消息。'
  return `已放弃，同时清空了排队中的 ${clearedQueueItems} 条后续指令。`
}

function ModelPicker({
  providers,
  models,
  onConfirm,
  onCancel
}: {
  providers: Provider[]
  models: Model[]
  onConfirm: (override: { provider: string; model: string }) => void
  onCancel: () => void
}): JSX.Element {
  const [provider, setProvider] = useState(providers[0]?.provider ?? '')
  const modelsForProvider = models.filter((m) => m.provider === provider)
  const [model, setModel] = useState(modelsForProvider[0]?.id ?? '')

  if (providers.length === 0) {
    return (
      <div className="termination-card__model-picker">
        <span>还没有配置好 Key 的模型厂商 — 先去设置页配置一个。</span>
        <button onClick={onCancel}>取消</button>
      </div>
    )
  }

  return (
    <div className="termination-card__model-picker">
      <select
        value={provider}
        onChange={(e) => {
          const next = e.target.value
          setProvider(next)
          setModel(models.find((m) => m.provider === next)?.id ?? '')
        }}
      >
        {providers.map((p) => (
          <option key={p.provider} value={p.provider}>
            {p.provider}
          </option>
        ))}
      </select>
      <select value={model} onChange={(e) => setModel(e.target.value)}>
        {models
          .filter((m) => m.provider === provider)
          .map((m) => (
            <option key={m.id} value={m.id}>
              {m.label || m.id}
            </option>
          ))}
      </select>
      <button disabled={!provider || !model} onClick={() => onConfirm({ provider, model })}>
        确认重试
      </button>
      <button onClick={onCancel}>取消</button>
    </div>
  )
}

/**
 * The three termination renderings PRD 9.3 / 04-w5-interfaces.md §4 require —
 * `kind==="user"` (nothing to act on), and `kind==="error"|"budget"` sharing
 * one `ErrorCard`-driven rendering (the outer `kind` only decides the top-line
 * framing text; the 7-bucket style and the available actions come from
 * `card.card` — see `KIND_STYLE`/`ACTION_LABEL` above). Every field is read
 * defensively (`card.card` may be an older/partial shape from a daemon this
 * exact renderer build hasn't seen) so a missing field renders less detail
 * instead of a white screen (N16).
 */
export function TerminationCard({
  card,
  providers,
  models,
  onRetry,
  onSwitchModel,
  onAbandon,
  pending,
  handled
}: TerminationCardProps): JSX.Element {
  const [picking, setPicking] = useState(false)
  const [expanded, setExpanded] = useState(false)
  const inner = card.card

  if (card.kind === 'user' || !inner || inner.actions?.length === 0) {
    return (
      <div className="termination-card termination-card--user">
        <div className="termination-card__title">{inner?.title ?? '已停止'}</div>
        <div className="termination-card__body">
          {inner?.message || card.reason || '用户手动停止，正在执行的动作已收尾。'}
        </div>
      </div>
    )
  }

  const style = KIND_STYLE[inner.kind as ErrorKind] ?? KIND_STYLE.internal
  const actions = inner.actions ?? []
  const rawExcerpt = inner.raw_excerpt ?? ''

  return (
    <div className={`termination-card ${style.className}`}>
      <div className="termination-card__title">
        <span aria-hidden className="termination-card__icon">
          {style.icon}
        </span>
        {inner.title || style.label}
      </div>
      {inner.step_seq != null && (
        <div className="termination-card__detail">发生在第 {inner.step_seq} 步</div>
      )}
      <div className="termination-card__body">{inner.message || card.reason}</div>
      {/* Round-2 review #4: PRD 9.3 "显式卡片说明是哪个预算、用了多少、上限多少" —
       * only rendered when the daemon actually sent structured numbers
       * (`errors/classify.py::ErrorCard.budget`'s docstring: nothing does yet,
       * this is the structural slot for when something does). */}
      {inner.budget && (
        <div className="termination-card__detail">
          {inner.budget.name ?? '预算'}：{inner.budget.used ?? '?'} / {inner.budget.limit ?? '?'}{' '}
          {inner.budget.unit ?? ''}
        </div>
      )}
      {rawExcerpt && (
        <details className="termination-card__raw" open={expanded} onToggle={(e) => setExpanded(e.currentTarget.open)}>
          <summary>查看原始错误</summary>
          <pre>{rawExcerpt}</pre>
        </details>
      )}
      {handled ? (
        <div className="termination-card__handled">
          {handled.action === 'abandon' ? abandonLabel(handled.clearedQueueItems) : HANDLED_LABEL[handled.action]}
        </div>
      ) : picking ? (
        <ModelPicker
          providers={providers}
          models={models}
          onCancel={() => setPicking(false)}
          onConfirm={(override) => {
            setPicking(false)
            onSwitchModel?.(override)
          }}
        />
      ) : (
        <div className="termination-card__actions">
          {actions.includes('retry') && (
            <button disabled={pending} onClick={onRetry}>
              {ACTION_LABEL.retry}
            </button>
          )}
          {actions.includes('switch_model') && (
            <button disabled={pending} onClick={() => setPicking(true)}>
              {ACTION_LABEL.switch_model}
            </button>
          )}
          {actions.includes('abandon') && (
            <button disabled={pending} onClick={onAbandon}>
              {ACTION_LABEL.abandon}
            </button>
          )}
        </div>
      )}
    </div>
  )
}
