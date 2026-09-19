import { useEffect } from 'react'
import type { RpcTransport } from '../../rpc/transport'
import { useSettingsStore } from '../../store/settingsStore'
import { useNavigationStore, type SettingsTab } from '../../store/navigationStore'
import { ProviderSettings } from './ProviderSettings'
import { AgentSettings } from './AgentSettings'
import { ProjectSettings } from './ProjectSettings'

const TABS: Array<{ id: SettingsTab; label: string }> = [
  { id: 'provider', label: 'Provider' },
  { id: 'agent', label: 'Agent' },
  { id: 'project', label: 'Project' }
]

/** 设置页：Provider / Agent / Project 三个子页（01-w2-interfaces.md §5）。
 * 当前 tab 存在 navigationStore 里（而不是本地 state），这样错误终止卡片的
 * "换模型" 按钮才能从中栏直接跳到 Agent 子页（见 CenterPane / TerminationCard）。 */
export function SettingsPage({ transport }: { transport: RpcTransport }): JSX.Element {
  const tab = useNavigationStore((s) => s.settingsTab)
  const setTab = useNavigationStore((s) => s.setSettingsTab)
  const init = useSettingsStore((s) => s.init)
  const error = useSettingsStore((s) => s.error)

  useEffect(() => {
    void init(transport)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <div className="settings-page">
      <nav className="settings-page__tabs">
        {TABS.map((t) => (
          <button
            key={t.id}
            className={`settings-page__tab${tab === t.id ? ' settings-page__tab--active' : ''}`}
            onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </nav>
      {error && <div className="settings-page__error">{error}</div>}
      <div className="settings-page__body">
        {tab === 'provider' && <ProviderSettings />}
        {tab === 'agent' && <AgentSettings />}
        {tab === 'project' && <ProjectSettings />}
      </div>
    </div>
  )
}
