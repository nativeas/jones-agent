import { useEffect } from 'react'
import type { RpcTransport } from '../../rpc/transport'
import { useSettingsStore } from '../../store/settingsStore'
import { useNavigationStore, type SettingsTab } from '../../store/navigationStore'
import { ProviderSettings } from './ProviderSettings'
import { AgentSettings } from './AgentSettings'
import { ProjectSettings } from './ProjectSettings'
import { CapabilitySettings } from './CapabilitySettings'

const TABS: Array<{ id: SettingsTab; label: string }> = [
  { id: 'provider', label: 'Provider' },
  { id: 'agent', label: 'Agent' },
  { id: 'project', label: 'Project' },
  { id: 'capabilities', label: '能力透明' }
]

/** 设置页：Provider / Agent / Project / 能力透明 四个子页（01-w2-interfaces.md
 * §5，能力透明为 03-w4-interfaces.md §5 / issue #19 新增）。当前 tab 存在
 * navigationStore 里（而不是本地 state），这样错误终止卡片的 "换模型" 按钮
 * 才能从中栏直接跳到 Agent 子页（见 CenterPane / TerminationCard）。
 *
 * 评审第 1 轮修复：能力透明子页的 `skill.list` 扫描不再在这里预取——它只有
 * 在用户真的点开「能力透明」tab、`CapabilitySettings` 挂载时才触发（见该
 * 组件），不管打开设置页时停在哪个 tab 都跑一次全量扫描。 */
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
        {tab === 'capabilities' && <CapabilitySettings transport={transport} />}
      </div>
    </div>
  )
}
