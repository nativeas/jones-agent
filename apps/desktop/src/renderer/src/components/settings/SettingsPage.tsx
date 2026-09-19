import { useEffect, useState } from 'react'
import type { RpcTransport } from '../../rpc/transport'
import { useSettingsStore } from '../../store/settingsStore'
import { ProviderSettings } from './ProviderSettings'
import { AgentSettings } from './AgentSettings'
import { ProjectSettings } from './ProjectSettings'

type Tab = 'provider' | 'agent' | 'project'
const TABS: Array<{ id: Tab; label: string }> = [
  { id: 'provider', label: 'Provider' },
  { id: 'agent', label: 'Agent' },
  { id: 'project', label: 'Project' }
]

/** 设置页：Provider / Agent / Project 三个子页（01-w2-interfaces.md §5）。 */
export function SettingsPage({ transport }: { transport: RpcTransport }): JSX.Element {
  const [tab, setTab] = useState<Tab>('provider')
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
