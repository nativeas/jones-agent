import type { QueueItem } from '../../domain/types'

interface QueuePanelProps {
  items: QueueItem[]
  onRemove: (id: string) => void
  onReorder: (orderedIds: string[]) => void
}

/** 队列面板：查看 / 撤回 / 调序待发送指令（PRD 9.2）。 */
export function QueuePanel({ items, onRemove, onReorder }: QueuePanelProps): JSX.Element | null {
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
      <div className="queue-panel__title">队列（{items.length}）</div>
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
