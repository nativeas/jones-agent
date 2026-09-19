import type { TimelineEntry } from '../../store/chatStore'
import type { Model, Provider } from '../../domain/types'
import { VirtualList } from '../common/VirtualList'
import { StepCard } from './StepCard'
import { TerminationCard } from './TerminationCard'

const ROLE_LABEL: Record<string, string> = {
  user: '我',
  assistant: 'Jones',
  system: '系统',
  tool: '工具'
}

interface MessageListProps {
  timeline: TimelineEntry[]
  /** Providers with a configured Key, for the "换模型" inline picker
   * (Issue #22, 04-w5-interfaces.md §4). */
  providers: Provider[]
  models: Model[]
  onRetry?: (turnId: string) => void
  onSwitchModel?: (turnId: string, override: { provider: string; model: string }) => void
  onAbandon?: (turnId: string) => void
}

function renderEntry(entry: TimelineEntry, props: MessageListProps): JSX.Element {
  if (entry.kind === 'message') {
    const { message } = entry
    return (
      <div className={`message-bubble message-bubble--${message.role}`}>
        <div className="message-bubble__role">{ROLE_LABEL[message.role] ?? message.role}</div>
        <div className="message-bubble__content">
          {message.content.text}
          {message.streaming && <span className="message-bubble__cursor" aria-hidden>▍</span>}
        </div>
      </div>
    )
  }
  if (entry.kind === 'step') {
    return <StepCard step={entry.step} />
  }
  const { turn_id: turnId } = entry.card
  return (
    <TerminationCard
      card={entry.card}
      providers={props.providers}
      models={props.models}
      onRetry={props.onRetry ? () => props.onRetry!(turnId) : undefined}
      onSwitchModel={props.onSwitchModel ? (override) => props.onSwitchModel!(turnId, override) : undefined}
      onAbandon={props.onAbandon ? () => props.onAbandon!(turnId) : undefined}
    />
  )
}

/** 中栏消息流：虚拟列表 + Step 折叠卡片 + 终止卡片，按到达顺序统一排列
 * （01-w2-interfaces.md §5）。 */
export function MessageList(props: MessageListProps): JSX.Element {
  const { timeline } = props
  return (
    <VirtualList
      className="message-list"
      items={timeline}
      estimatedItemHeight={64}
      getKey={(entry) =>
        entry.kind === 'message' ? entry.message.id : entry.kind === 'step' ? entry.step.id : entry.card.run_id
      }
      renderItem={(entry) => renderEntry(entry, props)}
      emptyState={<p className="shell__placeholder">还没有消息，说点什么开始吧。</p>}
    />
  )
}
