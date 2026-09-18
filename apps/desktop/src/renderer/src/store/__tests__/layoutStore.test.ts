import { beforeEach, describe, expect, it } from 'vitest'
import { useLayoutStore } from '../layoutStore'

describe('layoutStore', () => {
  beforeEach(() => {
    localStorage.clear()
    useLayoutStore.setState({ leftPaneOpen: true, rightPaneOpen: true })
  })

  it('defaults both panes open', () => {
    const state = useLayoutStore.getState()
    expect(state.leftPaneOpen).toBe(true)
    expect(state.rightPaneOpen).toBe(true)
  })

  it('toggles each pane independently', () => {
    useLayoutStore.getState().toggleLeftPane()
    expect(useLayoutStore.getState().leftPaneOpen).toBe(false)
    expect(useLayoutStore.getState().rightPaneOpen).toBe(true)

    useLayoutStore.getState().toggleRightPane()
    expect(useLayoutStore.getState().rightPaneOpen).toBe(false)
  })

  it('persists collapsed state to localStorage under the jones.layout key', () => {
    useLayoutStore.getState().toggleLeftPane()
    const raw = localStorage.getItem('jones.layout')
    expect(raw).not.toBeNull()
    const parsed = JSON.parse(raw as string)
    expect(parsed.state.leftPaneOpen).toBe(false)
  })
})
