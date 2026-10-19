import { AnimatePresence, motion } from 'motion/react'
import { cn } from '../../lib/utils'
import type { QueueItem } from '../../domain/types'

const SUSPENDED_REASON_LABEL: Record<'user' | 'error' | 'budget', string> = {
  user: '已停止',
  error: '出错终止',
  budget: '达到预算上限'
}

interface QueuePanelProps {
  items: QueueItem[]
  /** R-N4（04-w5-interfaces.md §4.3，PRD 9.3）：三类终止之一发生后，队列不再
   * 自动续跑时的终止原因；`null` 表示队列正常运行/没有被挂起。 */
  suspendedReason: 'user' | 'error' | 'budget' | null
  onRemove: (id: string) => void
  onReorder: (orderedIds: string[]) => void
  onResume: () => void
}

const controlButtonClass =
  'flex size-6 items-center justify-center rounded-md text-black/50 transition-colors hover:bg-black/5 hover:text-black/80 disabled:pointer-events-none disabled:opacity-30 dark:text-white/50 dark:hover:bg-white/10 dark:hover:text-white/80'

/** 队列面板：查看 / 撤回 / 调序待发送指令（PRD 9.2），挂起后显示「继续」（PRD 9.3 / R-N4）。
 * 列表项的增删/调序走 `motion` 的 layout 动画，让状态变化比纯 DOM 重排更看得见。 */
export function QueuePanel({
  items,
  suspendedReason,
  onRemove,
  onReorder,
  onResume
}: QueuePanelProps): JSX.Element | null {
  if (items.length === 0) return null

  const move = (index: number, direction: -1 | 1): void => {
    const target = index + direction
    if (target < 0 || target >= items.length) return
    const ids = items.map((i) => i.id)
    const [moved] = ids.splice(index, 1)
    ids.splice(target, 0, moved as string)
    onReorder(ids)
  }

  return (
    <div className="border-t border-black/10 px-3 py-2 text-[13px] dark:border-white/10">
      <div className="mb-1 flex flex-wrap items-center gap-2 font-semibold">
        队列（{items.length}）
        <AnimatePresence>
          {suspendedReason && (
            <motion.span
              initial={{ opacity: 0, scale: 0.9 }}
              animate={{ opacity: 1, scale: 1 }}
              exit={{ opacity: 0, scale: 0.9 }}
              className="inline-flex items-center gap-1.5 rounded-full bg-amber-100 px-2 py-0.5 font-normal text-amber-700 dark:bg-amber-500/15 dark:text-amber-400"
            >
              已暂停：{items.length} 条待发（{SUSPENDED_REASON_LABEL[suspendedReason]}）
              <button
                type="button"
                className="font-semibold underline decoration-dotted underline-offset-2"
                onClick={onResume}
              >
                继续
              </button>
            </motion.span>
          )}
        </AnimatePresence>
      </div>
      <ul className="m-0 flex list-none flex-col gap-1 p-0">
        <AnimatePresence initial={false}>
          {items.map((item, index) => (
            <motion.li
              key={item.id}
              layout
              initial={{ opacity: 0, y: -6 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, x: 12 }}
              transition={{ duration: 0.18, ease: 'easeOut' }}
              className={cn(
                'flex items-center gap-2 rounded-md border border-black/10 bg-black/[0.02] px-2 py-1 dark:border-white/10 dark:bg-white/[0.03]',
                item.state === 'sent' && 'opacity-50'
              )}
            >
              <span className="flex-1 overflow-hidden text-ellipsis whitespace-nowrap">{item.text}</span>
              <span className="flex items-center gap-0.5">
                <button
                  type="button"
                  className={controlButtonClass}
                  onClick={() => move(index, -1)}
                  disabled={index === 0}
                  aria-label="上移"
                >
                  ↑
                </button>
                <button
                  type="button"
                  className={controlButtonClass}
                  onClick={() => move(index, 1)}
                  disabled={index === items.length - 1}
                  aria-label="下移"
                >
                  ↓
                </button>
                <button
                  type="button"
                  className={controlButtonClass}
                  onClick={() => onRemove(item.id)}
                  aria-label="撤回"
                >
                  ✕
                </button>
              </span>
            </motion.li>
          ))}
        </AnimatePresence>
      </ul>
    </div>
  )
}
