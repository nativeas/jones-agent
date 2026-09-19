import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
  type ReactNode,
  type UIEvent
} from 'react'

export interface VisibleRange {
  startIndex: number
  endIndex: number // exclusive
  paddingTop: number
  paddingBottom: number
}

/**
 * Cumulative row offsets: `offsets[i]` is the top of row `i`, `offsets[count]`
 * is the total scroll height. Building this once per render (O(itemCount), no
 * IPC, no DOM) and then binary-searching it is what lets rows have real
 * (measured or estimated) heights instead of one fixed height for every row.
 */
export function buildOffsets(itemCount: number, getHeight: (index: number) => number): number[] {
  const offsets = new Array<number>(itemCount + 1)
  offsets[0] = 0
  for (let i = 0; i < itemCount; i += 1) {
    offsets[i + 1] = offsets[i]! + getHeight(i)
  }
  return offsets
}

/**
 * Pure so it's directly unit-testable without mounting anything: given a
 * scroll position and each row's cumulative offset, compute which rows are
 * visible plus the padding needed to keep the scrollbar the right total
 * length. Works for uniform rows (`buildOffsets(n, () => h)`) and for
 * variable/measured ones alike — there's no separate "fixed height" path to
 * drift out of sync with the real layout.
 */
export function computeVisibleRange(
  scrollTop: number,
  containerHeight: number,
  offsets: number[],
  overscan = 4
): VisibleRange {
  const itemCount = offsets.length - 1
  if (itemCount <= 0 || containerHeight <= 0) {
    return { startIndex: 0, endIndex: 0, paddingTop: 0, paddingBottom: 0 }
  }
  const totalHeight = offsets[itemCount]!

  // Smallest index whose row spans past scrollTop, i.e. offsets[i] <= scrollTop < offsets[i+1].
  let lo = 0
  let hi = itemCount - 1
  while (lo < hi) {
    const mid = (lo + hi) >> 1
    if (offsets[mid + 1]! <= scrollTop) lo = mid + 1
    else hi = mid
  }
  const firstVisible = lo

  const viewportEnd = Math.min(scrollTop + containerHeight, totalHeight)
  let lastVisible = firstVisible
  while (lastVisible < itemCount - 1 && offsets[lastVisible + 1]! < viewportEnd) {
    lastVisible += 1
  }

  const startIndex = Math.max(0, firstVisible - overscan)
  const endIndex = Math.min(itemCount, lastVisible + 1 + overscan)

  return {
    startIndex,
    endIndex,
    paddingTop: offsets[startIndex]!,
    paddingBottom: totalHeight - offsets[endIndex]!
  }
}

const STICK_TO_BOTTOM_THRESHOLD_PX = 48

interface VirtualListProps<T> {
  items: T[]
  /** Fallback height for a row that hasn't been measured yet (first paint, or
   * one currently off-screen) — every mounted row is then measured for real
   * (see MeasuredRow below) so an imprecise estimate only costs a slightly
   * off scrollbar position until that row is measured, not permanently wrong
   * scroll math (message bubbles, step cards and termination cards are all
   * very different heights). */
  estimatedItemHeight: number
  renderItem: (item: T, index: number) => ReactNode
  getKey: (item: T, index: number) => string
  className?: string
  emptyState?: ReactNode
}

/** Measures its rendered child's real height after every commit (content can
 * grow mid-stream) and reports it up — no ResizeObserver needed per row since
 * a height change here only ever follows a React re-render we already get. */
function MeasuredRow({
  rowKey,
  onMeasure,
  children
}: {
  rowKey: string
  onMeasure: (key: string, height: number) => void
  children: ReactNode
}): JSX.Element {
  const ref = useRef<HTMLDivElement | null>(null)
  useLayoutEffect(() => {
    const height = ref.current?.getBoundingClientRect().height
    if (height) onMeasure(rowKey, height)
  })
  return <div ref={ref}>{children}</div>
}

/**
 * Hand-rolled windowed list (01-w2-interfaces.md §5: "中栏：消息流（虚拟列表...)").
 * A message/step transcript in a long-running session can grow into the
 * thousands of rows; without windowing every delta re-render would touch that
 * whole DOM subtree (DEV.md 工程原则 #3: 渲染路径不做多余工作). No external
 * dependency — the visible-range math above is the entire algorithm and is
 * simple enough to keep in-repo and unit test directly.
 *
 * Rows are message bubbles / step cards / termination cards of very different
 * real heights, so this measures each mounted row and uses those measurements
 * (falling back to `estimatedItemHeight` for anything not yet measured) to
 * build the scroll offsets — the scrollbar and the mounted window both track
 * actual layout instead of drifting against a single fixed row height.
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
  const heightsRef = useRef(new Map<string, number>())
  const [measureTick, bumpMeasureTick] = useReducer((n: number) => n + 1, 0)
  const stickToBottomRef = useRef(true)

  const onMeasure = useCallback((key: string, height: number) => {
    if (heightsRef.current.get(key) === height) return
    heightsRef.current.set(key, height)
    bumpMeasureTick()
  }, [])

  // Container height: measured once on mount via ResizeObserver, disconnected
  // on unmount. A previous version of this used an inline ref callback that
  // got a new identity every render — React then re-invoked it (null, then
  // the node) on every re-render, and the null branch never disconnected the
  // observer it had just created, so a new ResizeObserver piled up on every
  // re-render (a long streaming reply re-renders every frame) and none of
  // them were ever released.
  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    if (typeof ResizeObserver === 'undefined') {
      setContainerHeight(el.clientHeight || 600)
      return
    }
    const observer = new ResizeObserver(() => setContainerHeight(el.clientHeight))
    observer.observe(el)
    setContainerHeight(el.clientHeight)
    return () => observer.disconnect()
  }, [])

  const onScroll = (e: UIEvent<HTMLDivElement>): void => {
    const el = e.currentTarget
    setScrollTop(el.scrollTop)
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight
    stickToBottomRef.current = distanceFromBottom < STICK_TO_BOTTOM_THRESHOLD_PX
  }

  const offsets = useMemo(
    // Reading heightsRef.current here is deliberate: buildOffsets calls this
    // callback synchronously, within this same render — it's a plain pure
    // function, never stored for later — so there's no stale/torn read for
    // this rule to guard against.
    // eslint-disable-next-line react-hooks/refs
    () => buildOffsets(items.length, (i) => heightsRef.current.get(getKey(items[i] as T, i)) ?? estimatedItemHeight),
    // measureTick isn't read here, but it's the signal that a cached height
    // changed and offsets must be rebuilt.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [items, getKey, estimatedItemHeight, measureTick]
  )

  // Auto-follow new content (deltas append to the last message, steps/cards
  // append new rows) the way a chat UI is expected to, but only while the
  // viewer hasn't scrolled up to read earlier history — mirrors the sticky-
  // bottom behavior of every chat client, and fixes streaming output being
  // invisible by default (nothing here previously ever scrolled the list).
  useEffect(() => {
    const el = containerRef.current
    if (!el || !stickToBottomRef.current) return
    el.scrollTop = el.scrollHeight
  }, [items, offsets])

  if (items.length === 0 && emptyState) {
    return (
      <div ref={containerRef} className={className}>
        {emptyState}
      </div>
    )
  }

  const range = computeVisibleRange(scrollTop, containerHeight || 600, offsets)

  return (
    <div ref={containerRef} className={className} onScroll={onScroll}>
      <div style={{ paddingTop: range.paddingTop, paddingBottom: range.paddingBottom }}>
        {items.slice(range.startIndex, range.endIndex).map((item, i) => {
          const index = range.startIndex + i
          const key = getKey(item, index)
          return (
            <MeasuredRow key={key} rowKey={key} onMeasure={onMeasure}>
              {renderItem(item, index)}
            </MeasuredRow>
          )
        })}
      </div>
    </div>
  )
}
