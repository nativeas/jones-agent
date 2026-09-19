import type { TerminationCard as TerminationCardData } from '../../domain/types'

interface TerminationCardProps {
  card: TerminationCardData
  onRetry?: () => void
  onSwitchModel?: () => void
  onAbandon?: () => void
}

/**
 * The three termination renderings required by PRD 9.3 / 01-w2-interfaces.md §5
 * ("三种终止卡片"). Field names inside `card.card` are this branch's assumed
 * shape (A's Session/Worker module isn't built yet) — documented in the
 * handoff report; every field is read defensively so an eventual real shape
 * that's missing one just renders less detail instead of crashing.
 */
export function TerminationCard({ card, onRetry, onSwitchModel, onAbandon }: TerminationCardProps): JSX.Element {
  if (card.kind === 'user') {
    return (
      <div className="termination-card termination-card--user">
        <div className="termination-card__title">已停止</div>
        <div className="termination-card__body">{card.reason || '用户手动停止，正在执行的动作已收尾。'}</div>
      </div>
    )
  }

  if (card.kind === 'budget') {
    const budget = card.card.budget as { name?: string; used?: number; limit?: number; unit?: string } | undefined
    return (
      <div className="termination-card termination-card--budget">
        <div className="termination-card__title">预算已用完</div>
        <div className="termination-card__body">{card.reason}</div>
        {budget && (
          <div className="termination-card__detail">
            {budget.name ?? '预算'}：{budget.used ?? '?'} / {budget.limit ?? '?'} {budget.unit ?? ''}
          </div>
        )}
      </div>
    )
  }

  // error
  const errorMessage = (card.card.error_message as string | undefined) ?? card.reason
  const step = card.card.step as { tool?: string; seq?: number } | undefined
  return (
    <div className="termination-card termination-card--error">
      <div className="termination-card__title">出错了</div>
      {step && (
        <div className="termination-card__detail">
          发生在第 {step.seq ?? '?'} 步（{step.tool ?? '未知工具'}）
        </div>
      )}
      <div className="termination-card__body">{errorMessage}</div>
      <div className="termination-card__actions">
        <button onClick={onRetry}>重试</button>
        <button onClick={onSwitchModel}>换模型</button>
        <button onClick={onAbandon}>放弃</button>
      </div>
    </div>
  )
}
