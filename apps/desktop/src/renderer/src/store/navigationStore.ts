import { create } from 'zustand'

export type AppView = 'sessions' | 'settings'
export type SettingsTab = 'provider' | 'agent' | 'project'

/** Top-level view/tab the shell is on. Deliberately separate from
 * `layoutStore` (pane open/closed is a persisted per-viewer preference;
 * which screen is showing is transient navigation state, reset on reload). */
interface NavigationState {
  view: AppView
  settingsTab: SettingsTab
  setView: (view: AppView) => void
  setSettingsTab: (tab: SettingsTab) => void
  /** Used by the error termination card's "换模型" action (01-w2-interfaces.md
   * §5 中栏验收) to jump straight to where the model preference lives. */
  goToAgentModelSettings: () => void
}

export const useNavigationStore = create<NavigationState>()((set) => ({
  view: 'sessions',
  settingsTab: 'provider',
  setView: (view) => set({ view }),
  setSettingsTab: (settingsTab) => set({ settingsTab }),
  goToAgentModelSettings: () => set({ view: 'settings', settingsTab: 'agent' })
}))
