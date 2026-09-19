import { describe, expect, it } from 'vitest'
import { buildOffsets, computeVisibleRange } from '../VirtualList'

const uniform = (itemCount: number, height: number): number[] => buildOffsets(itemCount, () => height)

describe('buildOffsets', () => {
  it('is the cumulative sum of each row height, one entry longer than the item count', () => {
    const offsets = buildOffsets(3, (i) => [10, 20, 30][i]!)
    expect(offsets).toEqual([0, 10, 30, 60])
  })

  it('is [0] for an empty list', () => {
    expect(buildOffsets(0, () => 64)).toEqual([0])
  })
})

describe('computeVisibleRange (uniform row height, via buildOffsets)', () => {
  it('renders nothing for an empty list', () => {
    const range = computeVisibleRange(0, 600, uniform(0, 64))
    expect(range).toEqual({ startIndex: 0, endIndex: 0, paddingTop: 0, paddingBottom: 0 })
  })

  it('renders nothing when the container has not been measured yet (height 0)', () => {
    const range = computeVisibleRange(0, 0, uniform(1000, 64))
    expect(range).toEqual({ startIndex: 0, endIndex: 0, paddingTop: 0, paddingBottom: 0 })
  })

  it('at scrollTop 0 starts at index 0 (no negative overscan) and pads only the bottom', () => {
    const range = computeVisibleRange(0, 600, uniform(1000, 64), 4)
    expect(range.startIndex).toBe(0)
    expect(range.paddingTop).toBe(0)
    expect(range.paddingBottom).toBeGreaterThan(0)
  })

  it('windows to a small slice in the middle of a long list, with overscan on both sides', () => {
    // 1000 rows of 64px; scrolled to row ~100 with a 600px viewport (~9.4 visible).
    const range = computeVisibleRange(100 * 64, 600, uniform(1000, 64), 4)
    expect(range.startIndex).toBe(100 - 4)
    expect(range.endIndex).toBeGreaterThan(range.startIndex)
    expect(range.endIndex).toBeLessThan(1000)
    // Padding must reproduce the true scroll height so the scrollbar doesn't jump.
    expect(range.paddingTop).toBe(range.startIndex * 64)
    expect(range.paddingBottom).toBe((1000 - range.endIndex) * 64)
  })

  it('clamps endIndex to itemCount when scrolled to the bottom', () => {
    const range = computeVisibleRange(999 * 64, 600, uniform(1000, 64), 4)
    expect(range.endIndex).toBe(1000)
    expect(range.paddingBottom).toBe(0)
  })
})

describe('computeVisibleRange (variable row height — the bug this fixes)', () => {
  it('windows around the row actually under scrollTop, not the row a uniform estimate would guess', () => {
    // Exactly the review's repro: 50 rows whose *real* height is 200px, but
    // the component only ever had a 64px estimate to go on. scrollTop=2000
    // lands inside row 10 (2000 / 200) — a fixed-64px calculation would have
    // guessed row 31 (2000 / 64) and windowed/padded around the wrong rows
    // entirely, with the scroll position visibly drifting from the content.
    const offsets = uniform(50, 200)
    const range = computeVisibleRange(2000, 600, offsets, 0)
    expect(range.startIndex).toBe(10)
    expect(range.paddingTop).toBe(2000)
  })

  it('mixed row heights: padding matches the true cumulative height, not startIndex * one row height', () => {
    // 3 short rows (40px) then a tall one (400px) then more short ones.
    const heights = [40, 40, 40, 400, 40, 40, 40, 40]
    const offsets = buildOffsets(heights.length, (i) => heights[i]!)
    const range = computeVisibleRange(0, 100, offsets, 0)
    // Viewport covers rows 0-2 (120px of content within 100px + the row straddling it).
    expect(range.startIndex).toBe(0)
    expect(range.paddingTop).toBe(0)
    const total = offsets[offsets.length - 1]!
    expect(range.paddingBottom).toBe(total - offsets[range.endIndex]!)
  })
})
