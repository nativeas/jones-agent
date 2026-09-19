import { useState } from 'react'
import { useSettingsStore } from '../../store/settingsStore'
import { KNOWN_PROVIDERS } from '../../domain/types'

const PROVIDER_LABEL: Record<string, string> = {
  anthropic: 'Anthropic',
  openai: 'OpenAI',
  deepseek: 'DeepSeek',
  qwen: 'Qwen',
  gemini: 'Gemini',
  ollama: 'Ollama'
}

/** Provider 设置：六家厂商，配 Key，只显示末 4 位（PRD FR04：Key 永不完整回显）。 */
export function ProviderSettings(): JSX.Element {
  const providers = useSettingsStore((s) => s.providers)
  const setProviderKey = useSettingsStore((s) => s.setProviderKey)
  const deleteProviderKey = useSettingsStore((s) => s.deleteProviderKey)
  const [drafts, setDrafts] = useState<Record<string, string>>({})

  return (
    <div className="settings-section">
      <h3>Provider</h3>
      {KNOWN_PROVIDERS.map((name) => {
        const entry = providers.find((p) => p.provider === name)
        return (
          <div key={name} className="provider-row">
            <span className="provider-row__name">{PROVIDER_LABEL[name] ?? name}</span>
            {entry?.has_key ? (
              <>
                <span className="provider-row__hint">已配置（…{entry.key_hint}）</span>
                <button onClick={() => void deleteProviderKey(name)}>删除 Key</button>
              </>
            ) : (
              <>
                <input
                  type="password"
                  placeholder="API Key"
                  value={drafts[name] ?? ''}
                  onChange={(e) => setDrafts((d) => ({ ...d, [name]: e.target.value }))}
                />
                <button
                  disabled={!drafts[name]}
                  onClick={() => {
                    void setProviderKey(name, drafts[name] as string)
                    setDrafts((d) => ({ ...d, [name]: '' }))
                  }}
                >
                  保存
                </button>
              </>
            )}
          </div>
        )
      })}
    </div>
  )
}
