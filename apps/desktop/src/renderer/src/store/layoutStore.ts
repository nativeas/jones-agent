import { create } from 'zustand'
import { persist } from 'zustand/middleware'

/** Three-pane shell layout: left (sessions) and right (inspector) panes can be
 * collapsed independently; the collapsed state survives a reload via localStorage
 * (per-viewer preference, not daemon state — see docs/design/00-foundation.md §1). */
interface LayoutState {
  leftPaneOpen: boolean
  rightPaneOpen: boolean
  toggleLeftPane: () => void
  toggleRightPane: () => void
}

export const useLayoutStore = create<LayoutState>()(
  persist(
    (set) => ({
      leftPaneOpen: true,
      rightPaneOpen: true,
      toggleLeftPane: () => set((state) => ({ leftPaneOpen: !state.leftPaneOpen })),
      toggleRightPane: () => set((state) => ({ rightPaneOpen: !state.rightPaneOpen }))
    }),
    { name: 'jones.layout' }
  )
)
