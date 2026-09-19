import { useState } from 'react'
import { useSettingsStore } from '../../store/settingsStore'
import { KNOWN_PROVIDERS, type Agent } from '../../domain/types'

const EMPTY_DRAFT = {
  name: '',
  persona: '',
  tone: '',
  principles: '',
  tool_allowlist: '',
  skills: '',
  model_provider: '',
  model_id: ''
}

/** Agent 设置：六项（人设/语气/原则/白名单/Skill/模型偏好）可编辑（PRD FR03）。 */
export function AgentSettings(): JSX.Element {
  const agents = useSettingsStore((s) => s.agents)
  const models = useSettingsStore((s) => s.models)
  const upsertAgent = useSettingsStore((s) => s.upsertAgent)
  const deleteAgent = useSettingsStore((s) => s.deleteAgent)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [draft, setDraft] = useState(EMPTY_DRAFT)

  const startEdit = (agent?: Agent): void => {
    setEditingId(agent?.id ?? 'new')
    setDraft(
      agent
        ? {
            name: agent.name,
            persona: agent.persona,
            tone: agent.tone,
            principles: agent.principles,
            tool_allowlist: agent.tool_allowlist.join(', '),
            skills: agent.skills.join(', '),
            model_provider: agent.model_pref?.provider ?? '',
            model_id: agent.model_pref?.model ?? ''
          }
        : EMPTY_DRAFT
    )
  }

  const modelsForProvider = (provider: string): typeof models =>
    models.filter((m) => m.provider === provider)

  const save = async (): Promise<void> => {
    if (!draft.name.trim()) return
    await upsertAgent({
      id: editingId !== 'new' ? (editingId ?? undefined) : undefined,
      name: draft.name,
      persona: draft.persona,
      tone: draft.tone,
      principles: draft.principles,
      tool_allowlist: draft.tool_allowlist
        .split(',')
        .map((s) => s.trim())
        .filter(Boolean),
      skills: draft.skills
        .split(',')
        .map((s) => s.trim())
        .filter(Boolean),
      // 未选择厂商 = 用户默认（Protocol: model_pref: dict | None，01-w2-interfaces.md §3）。
      model_pref: draft.model_provider ? { provider: draft.model_provider, model: draft.model_id || undefined } : null
    })
    setEditingId(null)
  }

  return (
    <div className="settings-section">
      <h3>Agent</h3>
      <ul className="agent-list">
        {agents.map((agent) => (
          <li key={agent.id} className="agent-list__item">
            <span>{agent.name}</span>
            <span className="agent-list__actions">
              <button onClick={() => startEdit(agent)}>编辑</button>
              <button onClick={() => void deleteAgent(agent.id)}>删除</button>
            </span>
          </li>
        ))}
      </ul>
      {editingId ? (
        <div className="agent-editor">
          <label>
            名称
            <input value={draft.name} onChange={(e) => setDraft((d) => ({ ...d, name: e.target.value }))} />
          </label>
          <label>
            人设
            <textarea value={draft.persona} onChange={(e) => setDraft((d) => ({ ...d, persona: e.target.value }))} />
          </label>
          <label>
            语气
            <input value={draft.tone} onChange={(e) => setDraft((d) => ({ ...d, tone: e.target.value }))} />
          </label>
          <label>
            原则
            <textarea
              value={draft.principles}
              onChange={(e) => setDraft((d) => ({ ...d, principles: e.target.value }))}
            />
          </label>
          <label>
            工具白名单（逗号分隔）
            <input
              value={draft.tool_allowlist}
              onChange={(e) => setDraft((d) => ({ ...d, tool_allowlist: e.target.value }))}
            />
          </label>
          <label>
            Skill（逗号分隔）
            <input value={draft.skills} onChange={(e) => setDraft((d) => ({ ...d, skills: e.target.value }))} />
          </label>
          <label>
            模型偏好
            <select
              value={draft.model_provider}
              onChange={(e) => setDraft((d) => ({ ...d, model_provider: e.target.value, model_id: '' }))}
            >
              <option value="">跟随用户默认</option>
              {KNOWN_PROVIDERS.map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </select>
            {draft.model_provider && (
              <select
                value={draft.model_id}
                onChange={(e) => setDraft((d) => ({ ...d, model_id: e.target.value }))}
              >
                <option value="">该厂商默认模型</option>
                {modelsForProvider(draft.model_provider).map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.label}
                  </option>
                ))}
              </select>
            )}
          </label>
          <div className="agent-editor__actions">
            <button onClick={() => void save()}>保存</button>
            <button onClick={() => setEditingId(null)}>取消</button>
          </div>
        </div>
      ) : (
        <button onClick={() => startEdit()}>+ 新建 Agent</button>
      )}
    </div>
  )
}
