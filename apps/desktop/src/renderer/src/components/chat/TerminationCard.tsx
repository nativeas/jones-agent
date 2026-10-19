import { useState } from 'react'
import { cn } from '../../lib/utils'
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
 * color/icon, matching that module's own comment on why. Buckets further
 * share one of 4 accent colors, same grouping the original design used. */
const RED = { card: 'border-red-300/70 bg-red-50 dark:border-red-500/30 dark:bg-red-950/20', iconColor: 'text-red-600 dark:text-red-400' }
const AMBER = { card: 'border-amber-300/70 bg-amber-50 dark:border-amber-500/30 dark:bg-amber-950/20', iconColor: 'text-amber-600 dark:text-amber-400' }
const PURPLE = { card: 'border-purple-300/70 bg-purple-50 dark:border-purple-500/30 dark:bg-purple-950/20', iconColor: 'text-purple-600 dark:text-purple-400' }
const SLATE = { card: 'border-slate-300/70 bg-slate-50 dark:border-slate-500/30 dark:bg-slate-900/40', iconColor: 'text-slate-600 dark:text-slate-400' }

const KIND_CONFIG: Record<ErrorKind, { label: string; icon: string; card: string; iconColor: string }> = {
  network: { label: '网络', icon: '⚠', ...RED },
  provider_quota: { label: '配额', icon: '⛔', ...AMBER },
  provider_auth: { label: '认证', icon: '🔑', ...AMBER },
  tool_exception: { label: '工具', icon: '🛠', ...RED },
  worker_crash: { label: '崩溃', icon: '💥', ...PURPLE },
  approval_timeout: { label: '超时', icon: '⏱', ...SLATE },
  budget: { label: '预算', icon: '📊', ...AMBER },
  provider_error: { label: '出错', icon: '❗', ...RED },
  internal: { label: '出错', icon: '❗', ...RED }
}

const ACTION_LABEL: Record<CardAction, string> = {
  retry: '重试',
  switch_model: '换模型',
  abandon: '放弃'
}

const actionButtonClass =
  'rounded-md border px-2.5 py-1 text-[12px] font-medium transition-colors disabled:cursor-default disabled:opacity-50'
const primaryButtonClass = cn(
  actionButtonClass,
  'border-transparent bg-black text-white hover:bg-black/80 dark:bg-white dark:text-black dark:hover:bg-white/80'
)
const secondaryButtonClass = cn(
  actionButtonClass,
  'border-black/15 bg-transparent hover:bg-black/5 dark:border-white/20 dark:hover:bg-white/10'
)

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

const selectClass = 'rounded-md border border-black/15 bg-transparent px-2 py-1 text-[12px] dark:border-white/20'

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
      <div data-testid="termination-model-picker" className="mt-2 flex flex-wrap items-center gap-2 text-[12px]">
        <span className="text-black/60 dark:text-white/60">还没有配置好 Key 的模型厂商 — 先去设置页配置一个。</span>
        <button type="button" className={secondaryButtonClass} onClick={onCancel}>
          取消
        </button>
      </div>
    )
  }

  return (
    <div data-testid="termination-model-picker" className="mt-2 flex flex-wrap items-center gap-2">
      <select
        className={selectClass}
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
      <select className={selectClass} value={model} onChange={(e) => setModel(e.target.value)}>
        {models
          .filter((m) => m.provider === provider)
          .map((m) => (
            <option key={m.id} value={m.id}>
              {m.label || m.id}
            </option>
          ))}
      </select>
      <button
        type="button"
        className={primaryButtonClass}
        disabled={!provider || !model}
        onClick={() => onConfirm({ provider, model })}
      >
        确认重试
      </button>
      <button type="button" className={secondaryButtonClass} onClick={onCancel}>
        取消
      </button>
    </div>
  )
}

/**
 * The three termination renderings PRD 9.3 / 04-w5-interfaces.md §4 require —
 * `kind==="user"` (nothing to act on), and `kind==="error"|"budget"` sharing
 * one `ErrorCard`-driven rendering (the outer `kind` only decides the top-line
 * framing text; the 7-bucket style and the available actions come from
 * `card.card` — see `KIND_CONFIG`/`ACTION_LABEL` above). Every field is read
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
      <div className="my-2.5 rounded-lg border border-black/10 bg-black/[0.02] px-3 py-2.5 text-[13px] dark:border-white/10 dark:bg-white/[0.03]">
        <div className="mb-0.5 font-semibold">{inner?.title ?? '已停止'}</div>
        <div className="text-black/60 dark:text-white/60">
          {inner?.message || card.reason || '用户手动停止，正在执行的动作已收尾。'}
        </div>
      </div>
    )
  }

  const style = KIND_CONFIG[inner.kind as ErrorKind] ?? KIND_CONFIG.internal
  const actions = inner.actions ?? []
  const rawExcerpt = inner.raw_excerpt ?? ''

  return (
    <div className={cn('my-2.5 rounded-lg border px-3 py-2.5 text-[13px]', style.card)}>
      <div className="flex items-center gap-1.5 font-semibold">
        <span aria-hidden className={style.iconColor}>
          {style.icon}
        </span>
        {inner.title || style.label}
      </div>
      {inner.step_seq != null && (
        <div className="mt-0.5 text-[12px] text-black/50 dark:text-white/50">发生在第 {inner.step_seq} 步</div>
      )}
      <div className="mt-1 text-black/80 dark:text-white/80">{inner.message || card.reason}</div>
      {/* Round-2 review #4: PRD 9.3 "显式卡片说明是哪个预算、用了多少、上限多少" —
       * only rendered when the daemon actually sent structured numbers
       * (`errors/classify.py::ErrorCard.budget`'s docstring: nothing does yet,
       * this is the structural slot for when something does). */}
      {inner.budget && (
        <div className="mt-1 font-mono text-[12px] text-black/50 dark:text-white/50">
          {inner.budget.name ?? '预算'}：{inner.budget.used ?? '?'} / {inner.budget.limit ?? '?'}{' '}
          {inner.budget.unit ?? ''}
        </div>
      )}
      {rawExcerpt && (
        <details
          className="mt-1.5 text-[12px]"
          open={expanded}
          onToggle={(e) => setExpanded(e.currentTarget.open)}
        >
          <summary className="cursor-pointer text-black/50 select-none hover:text-black/70 dark:text-white/50 dark:hover:text-white/70">
            查看原始错误
          </summary>
          <pre className="mt-1 max-h-48 overflow-auto rounded-md bg-black/5 px-2 py-1.5 font-mono text-[11px] whitespace-pre-wrap break-words dark:bg-white/10">
            {rawExcerpt}
          </pre>
        </details>
      )}
      {handled ? (
        <div className="mt-2 text-[12px] text-black/60 dark:text-white/60">
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
        <div data-testid="termination-actions" className="mt-2 flex gap-2">
          {actions.includes('retry') && (
            <button type="button" className={primaryButtonClass} disabled={pending} onClick={onRetry}>
              {ACTION_LABEL.retry}
            </button>
          )}
          {actions.includes('switch_model') && (
            <button type="button" className={secondaryButtonClass} disabled={pending} onClick={() => setPicking(true)}>
              {ACTION_LABEL.switch_model}
            </button>
          )}
          {actions.includes('abandon') && (
            <button type="button" className={secondaryButtonClass} disabled={pending} onClick={onAbandon}>
              {ACTION_LABEL.abandon}
            </button>
          )}
        </div>
      )}
    </div>
  )
}
