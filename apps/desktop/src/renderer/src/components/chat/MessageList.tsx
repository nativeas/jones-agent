import { useState } from 'react'
import type { TimelineEntry } from '../../store/chatStore'
import type { CardAction, Message, Model, Provider } from '../../domain/types'
import { cn } from '../../lib/utils'
import { BlurFade } from '../ui/blur-fade'
import { VirtualList } from '../common/VirtualList'
import { StepCard } from './StepCard'
import { TerminationCard } from './TerminationCard'
import { Bot, Brain, ChevronRight, Loader2, User } from 'lucide-react'

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
  /** Round-2 review #6: turn_ids currently awaiting a `session.retry` round-trip. */
  pendingTerminations?: Set<string>
  /** Round-2 review #2/#6: turn_id → which action already completed for it
   * (round-N2 review #2: plus `cleared_queue_items` for "放弃", see
   * `TerminationCard`'s `handled` prop doc comment). */
  handledTerminations?: Map<string, { action: CardAction; clearedQueueItems?: number }>
}

/** Collapsed-by-default "推理过程" block for `content.kind === 'thinking'`
 * messages — mirrors AI Elements' reasoning affordance: expands itself while
 * the thinking text is still streaming in, folds back down once it settles
 * (unless the viewer already toggled it by hand). */
function ReasoningBlock({ text, streaming }: { text: string; streaming?: boolean }): JSX.Element {
  const [open, setOpen] = useState(!!streaming)
  const [touched, setTouched] = useState(false)
  // Fold back up once streaming ends, unless the viewer already toggled it by
  // hand — adjusted during render (React's documented pattern for reacting to
  // a prop change) rather than in an effect, so this doesn't cost an extra
  // commit.
  const [prevStreaming, setPrevStreaming] = useState(streaming)
  if (streaming !== prevStreaming) {
    setPrevStreaming(streaming)
    if (!streaming && !touched) setOpen(false)
  }

  return (
    <div className="w-full rounded-xl border border-dashed border-neutral-300 bg-neutral-50/60 dark:border-neutral-700 dark:bg-neutral-900/40">
      <button
        type="button"
        onClick={() => {
          setTouched(true)
          setOpen((o) => !o)
        }}
        className="flex w-full items-center gap-1.5 px-3 py-1.5 text-left text-xs text-neutral-500 dark:text-neutral-400"
      >
        <ChevronRight className={cn('h-3.5 w-3.5 shrink-0 transition-transform', open && 'rotate-90')} />
        {streaming ? <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin" /> : <Brain className="h-3.5 w-3.5 shrink-0" />}
        <span>{streaming ? '正在思考…' : '推理过程'}</span>
      </button>
      {open && (
        <div className="whitespace-pre-wrap border-t border-dashed border-neutral-300 px-3 py-2 text-xs leading-relaxed text-neutral-500 dark:border-neutral-700 dark:text-neutral-400">
          {text}
        </div>
      )}
    </div>
  )
}

function MessageBubble({ message }: { message: Message }): JSX.Element {
  const isUser = message.role === 'user'
  const isAssistant = message.role === 'assistant'

  if (!isUser && !isAssistant) {
    return (
      <div className="flex justify-center px-2 py-1">
        <span className="rounded-full bg-neutral-100 px-3 py-1 text-xs text-neutral-500 dark:bg-neutral-800 dark:text-neutral-400">
          {ROLE_LABEL[message.role] ?? message.role}：{message.content.text}
        </span>
      </div>
    )
  }

  return (
    <div className={cn('flex gap-2 px-2 py-1.5', isUser && 'flex-row-reverse')}>
      <div
        aria-hidden
        className={cn(
          'flex h-7 w-7 shrink-0 items-center justify-center rounded-full',
          isUser
            ? 'bg-blue-600 text-white'
            : 'bg-neutral-200 text-neutral-600 dark:bg-neutral-700 dark:text-neutral-300'
        )}
      >
        {isUser ? <User className="h-4 w-4" /> : <Bot className="h-4 w-4" />}
      </div>
      <div className={cn('flex max-w-[78%] flex-col gap-1', isUser ? 'items-end' : 'items-start')}>
        <span className="px-0.5 text-[11px] text-neutral-400 dark:text-neutral-500">
          {ROLE_LABEL[message.role] ?? message.role}
        </span>
        {message.content.kind === 'thinking' ? (
          <ReasoningBlock text={message.content.text} streaming={message.streaming} />
        ) : (
          <div
            className={cn(
              'whitespace-pre-wrap rounded-2xl px-3 py-2 text-sm leading-relaxed',
              isUser
                ? 'rounded-tr-sm bg-blue-600 text-white'
                : 'rounded-tl-sm bg-neutral-100 text-neutral-900 dark:bg-neutral-800 dark:text-neutral-100'
            )}
          >
            {message.content.text}
            {message.streaming && (
              <span className="ml-0.5 inline-block animate-pulse" aria-hidden>
                ▍
              </span>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

function renderEntry(entry: TimelineEntry, props: MessageListProps): JSX.Element {
  if (entry.kind === 'message') {
    return <MessageBubble message={entry.message} />
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
      pending={props.pendingTerminations?.has(turnId)}
      handled={props.handledTerminations?.get(turnId)}
    />
  )
}

/** 中栏消息流：虚拟列表 + Step 折叠卡片 + 终止卡片，按到达顺序统一排列
 * （01-w2-interfaces.md §5）。每行套一层 BlurFade，新行挂载时轻微上浮淡入。 */
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
      renderItem={(entry) => (
        <BlurFade duration={0.25} offset={8} direction="up" className="pb-1">
          {renderEntry(entry, props)}
        </BlurFade>
      )}
      emptyState={
        <div className="flex h-full items-center justify-center px-6 text-center text-sm text-neutral-400 dark:text-neutral-500">
          还没有消息，说点什么开始吧。
        </div>
      }
    />
  )
}
