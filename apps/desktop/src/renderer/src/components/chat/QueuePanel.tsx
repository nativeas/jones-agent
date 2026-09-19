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

/** 队列面板：查看 / 撤回 / 调序待发送指令（PRD 9.2），挂起后显示「继续」（PRD 9.3 / R-N4）。 */
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
    <div className="queue-panel">
      <div className="queue-panel__title">
        队列（{items.length}）
        {suspendedReason && (
          <span className="queue-panel__suspended">
            已暂停：{items.length} 条待发（{SUSPENDED_REASON_LABEL[suspendedReason]}）
            <button className="queue-panel__resume" onClick={onResume}>
              继续
            </button>
          </span>
        )}
      </div>
      <ul className="queue-panel__list">
        {items.map((item, index) => (
          <li key={item.id} className="queue-panel__item">
            <span className="queue-panel__text">{item.text}</span>
            <span className="queue-panel__controls">
              <button onClick={() => move(index, -1)} disabled={index === 0} aria-label="上移">
                ↑
              </button>
              <button onClick={() => move(index, 1)} disabled={index === items.length - 1} aria-label="下移">
                ↓
              </button>
              <button onClick={() => onRemove(item.id)} aria-label="撤回">
                撤回
              </button>
            </span>
          </li>
        ))}
      </ul>
    </div>
  )
}
