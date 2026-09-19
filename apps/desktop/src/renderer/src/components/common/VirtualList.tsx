import { useRef, useState, type ReactNode, type UIEvent } from 'react'

export interface VisibleRange {
  startIndex: number
  endIndex: number // exclusive
  paddingTop: number
  paddingBottom: number
}

/**
 * Pure so it's directly unit-testable without mounting anything: given a
 * scroll position and a fixed row height, compute which rows are visible plus
 * the padding needed to keep the scrollbar the right total length.
 */
export function computeVisibleRange(
  scrollTop: number,
  containerHeight: number,
  itemCount: number,
  itemHeight: number,
  overscan = 4
): VisibleRange {
  if (itemCount === 0 || containerHeight <= 0) {
    return { startIndex: 0, endIndex: 0, paddingTop: 0, paddingBottom: 0 }
  }
  const firstVisible = Math.floor(scrollTop / itemHeight)
  const visibleCount = Math.ceil(containerHeight / itemHeight)
  const startIndex = Math.max(0, firstVisible - overscan)
  const endIndex = Math.min(itemCount, firstVisible + visibleCount + overscan)
  return {
    startIndex,
    endIndex,
    paddingTop: startIndex * itemHeight,
    paddingBottom: (itemCount - endIndex) * itemHeight
  }
}

interface VirtualListProps<T> {
  items: T[]
  /** Estimated row height in px — rows may render taller (step cards, long
   * messages); the estimate only drives which rows are mounted, not layout, so
   * an imprecise estimate costs a few extra off-screen rows, not incorrect
   * scrolling (see computeVisibleRange's overscan). */
  estimatedItemHeight: number
  renderItem: (item: T, index: number) => ReactNode
  getKey: (item: T, index: number) => string
  className?: string
  emptyState?: ReactNode
}

/**
 * Hand-rolled windowed list (01-w2-interfaces.md §5: "中栏：消息流（虚拟列表...)").
 * A message/step transcript in a long-running session can grow into the
 * thousands of rows; without windowing every delta re-render would touch that
 * whole DOM subtree (DEV.md 工程原则 #3: 渲染路径不做多余工作). No external
 * dependency — the visible-range math above is the entire algorithm and is
 * simple enough to keep in-repo and unit test directly.
 */
export function VirtualList<T>({
  items,
  estimatedItemHeight,
  renderItem,
  getKey,
  className,
  emptyState
}: VirtualListProps<T>): JSX.Element {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const [scrollTop, setScrollTop] = useState(0)
  const [containerHeight, setContainerHeight] = useState(0)

  const onScroll = (e: UIEvent<HTMLDivElement>): void => {
    setScrollTop(e.currentTarget.scrollTop)
  }

  // Measured on mount/resize via ResizeObserver where available (jsdom has
  // none — falls back to a generous default so tests/SSR don't crash).
  const measureRef = (el: HTMLDivElement | null): void => {
    containerRef.current = el
    if (!el) return
    if (typeof ResizeObserver === 'undefined') {
      setContainerHeight(el.clientHeight || 600)
      return
    }
    const observer = new ResizeObserver(() => setContainerHeight(el.clientHeight))
    observer.observe(el)
    setContainerHeight(el.clientHeight)
  }

  if (items.length === 0 && emptyState) {
    return (
      <div ref={measureRef} className={className}>
        {emptyState}
      </div>
    )
  }

  const range = computeVisibleRange(scrollTop, containerHeight || 600, items.length, estimatedItemHeight)

  return (
    <div ref={measureRef} className={className} onScroll={onScroll}>
      <div style={{ paddingTop: range.paddingTop, paddingBottom: range.paddingBottom }}>
        {items.slice(range.startIndex, range.endIndex).map((item, i) => (
          <div key={getKey(item, range.startIndex + i)}>{renderItem(item, range.startIndex + i)}</div>
        ))}
      </div>
    </div>
  )
}
