/**
 * Batches `message.delta` notifications so a burst of tokens arriving within
 * one animation frame produces a single React state update instead of one per
 * token (01-w2-interfaces.md §5: "delta 合并批量 flush（≤ 16ms 一帧）"; DEV.md
 * 工程原则 #3: 渲染路径不做多余工作).
 *
 * The scheduler is injectable so tests can drive flushes deterministically
 * (call the captured callback themselves) instead of racing real rAF/timers.
 */
export type DeltaFlush = (updates: Array<{ messageId: string; text: string }>) => void
export type Scheduler = (run: () => void) => void

function defaultScheduler(run: () => void): void {
  if (typeof requestAnimationFrame === 'function') {
    requestAnimationFrame(run)
  } else {
    setTimeout(run, 16)
  }
}

export interface DeltaBatcher {
  /** Buffer one delta for `messageId`; schedules a flush if none is pending. */
  push(messageId: string, delta: string): void
  /** Flush immediately if a batch is pending (e.g. on unmount) — a no-op otherwise. */
  flushNow(): void
  /** Discard any buffered-but-not-yet-flushed deltas without emitting them (e.g.
   * switching the bound session out from under a still-scheduled flush). */
  cancel(): void
}

export function createDeltaBatcher(onFlush: DeltaFlush, schedule: Scheduler = defaultScheduler): DeltaBatcher {
  let buffer = new Map<string, string>()
  let scheduled = false

  function runFlush(): void {
    scheduled = false
    if (buffer.size === 0) return
    const updates = Array.from(buffer.entries()).map(([messageId, text]) => ({ messageId, text }))
    buffer = new Map()
    onFlush(updates)
  }

  return {
    push(messageId, delta) {
      buffer.set(messageId, (buffer.get(messageId) ?? '') + delta)
      if (!scheduled) {
        scheduled = true
        schedule(runFlush)
      }
    },
    flushNow() {
      if (scheduled) runFlush()
    },
    cancel() {
      scheduled = false
      buffer = new Map()
    }
  }
}
