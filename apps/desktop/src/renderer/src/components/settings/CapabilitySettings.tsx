import { useEffect, useState } from 'react'
import type { RpcTransport } from '../../rpc/transport'
import { useCapabilitiesStore } from '../../store/capabilitiesStore'
import { useSessionsStore } from '../../store/sessionsStore'
import type { CapabilityHiddenReason, CapabilitySource, SkillTier } from '../../domain/types'

const SOURCE_LABEL: Record<CapabilitySource, string> = { builtin: 'Hermes 内置', mcp: 'MCP', skill: 'Skill' }
const HIDDEN_REASON_LABEL: Record<CapabilityHiddenReason, string> = {
  not_in_allowlist: '不在 Agent 白名单',
  denied_by_rule: '被规则闸禁止',
  mode_chat: '纯对话模式',
  mcp_server_down: 'MCP Server 未启动',
  unknown_tool: '未知工具（新出现，未映射分级）'
}
const TIER_LABEL: Record<SkillTier, string> = { project: '项目级', user: '用户级', builtin: '内置' }
// 评审第 1 轮 #5：项目级 Skill 来自用户 clone 的仓库内容（`<project>/.jones/skills/`），
// 可信度与第三方 MCP 工具同级，但 `worker_skill_dirs()` 目前把它直接交给 H
// 写进 worker 的 `skills.external_dirs`，没有任何用户确认门（03-w4-interfaces.md
// §2 / N15 对第三方 MCP 的要求是默认不进白名单，直到用户显式启用——这条分支
// 没法在这里补上那道门，写 config.yaml 是 H 独占的 `_prepare_hermes_home`；
// 能在 K 的范围内做的，是先在透明页上把它显式标出来，不让用户以为项目级
// Skill 经过了确认）。

/** 「能力透明」设置页（PRD FR16 / issue #19，G21）：按 Session 展示
 * `capability.list` 的期望∪实际对账结果，drift 非空时醒目警告；同时展示
 * `skill.list`（issue #18）三层 Skill 发现结果，因为 Skill 是 capability.list
 * `source: "skill"` 条目的来源之一。
 *
 * "切换 Agent / 启停 MCP / 增删 Skill 后即时刷新"（03-w4-interfaces.md §5）
 * 在这个分支里落地为：切换下面的 Session 选择器（等价于切换该 Session 绑定
 * 的 Agent 装配集合）立即重新拉取，外加一个手动刷新按钮——MCP 启停/Skill
 * 增删目前都不是这个分支能触发的 UI 动作（前者是 H/#17 的设置面板，后者是
 * 文件系统操作），所以没有专门的"变更事件"可订阅；这点在 PR 报告里写明，不
 * 在这里假装有一个不存在的通知。
 *
 * 评审第 1 轮：`init(transport)` 挪到这里的挂载 effect（之前在 SettingsPage
 * 挂载时无条件调用，见 SettingsPage.tsx 的注释）——只有用户点开这个 tab、
 * 这个组件真的挂载时才设置 transport 并触发下面 session effect 的首次
 * 拉取，不再每次打开设置页就白扫一次。 */
export function CapabilitySettings({ transport }: { transport: RpcTransport }): JSX.Element {
  const sessions = useSessionsStore((s) => s.sessions)
  const init = useCapabilitiesStore((s) => s.init)
  const skills = useCapabilitiesStore((s) => s.skills)
  const skillsLoading = useCapabilitiesStore((s) => s.skillsLoading)
  const skillsError = useCapabilitiesStore((s) => s.skillsError)
  const loadSkills = useCapabilitiesStore((s) => s.loadSkills)
  const capability = useCapabilitiesStore((s) => s.capability)
  const capabilityLoading = useCapabilitiesStore((s) => s.capabilityLoading)
  const capabilityError = useCapabilitiesStore((s) => s.capabilityError)
  const loadCapability = useCapabilitiesStore((s) => s.loadCapability)

  // Local state only tracks an explicit user choice — the actual selection
  // shown/used always falls back to the first session, computed at render
  // time (not written back with setState-in-effect, which the lint config
  // here forbids; see DaemonStatusCard.tsx's matching comment for the same
  // pattern elsewhere in this codebase).
  const [manualSessionId, setManualSessionId] = useState<string>('')
  const selectedSessionId = manualSessionId || sessions[0]?.id || ''

  useEffect(() => {
    void init(transport)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    if (!selectedSessionId) return
    void loadCapability(selectedSessionId)
    const session = sessions.find((s) => s.id === selectedSessionId)
    void loadSkills(session?.project_id ?? null)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedSessionId])

  const refresh = (): void => {
    if (selectedSessionId) void loadCapability(selectedSessionId)
    const session = sessions.find((s) => s.id === selectedSessionId)
    void loadSkills(session?.project_id ?? null)
  }

  return (
    <div className="settings-section capability-settings">
      <h3>能力透明</h3>

      <div className="capability-settings__toolbar">
        <label>
          Session：
          <select value={selectedSessionId} onChange={(e) => setManualSessionId(e.target.value)}>
            {sessions.length === 0 && <option value="">（还没有会话）</option>}
            {sessions.map((s) => (
              <option key={s.id} value={s.id}>
                {s.title || s.id}
              </option>
            ))}
          </select>
        </label>
        <button onClick={refresh} disabled={!selectedSessionId}>
          刷新
        </button>
      </div>

      {capability && capability.drift.length > 0 && (
        <div className="capability-settings__drift-warning" role="alert">
          <strong>装配对账不一致（G21）：</strong>
          <ul>
            {capability.drift.map((d, i) => (
              <li key={i}>{d}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="capability-settings__section">
        <h4>本 Session 装配的工具</h4>
        {capabilityLoading && <p>加载中…</p>}
        {capabilityError && <div className="settings-page__error">{capabilityError}</div>}
        {!capabilityLoading && !capabilityError && !selectedSessionId && <p>先创建一个会话。</p>}
        {capability && (
          <table className="capability-table">
            <thead>
              <tr>
                <th>工具</th>
                <th>来源</th>
                <th>启用</th>
                <th>隐藏原因</th>
                <th>实际装配</th>
              </tr>
            </thead>
            <tbody>
              {capability.tools.map((t) => (
                <tr key={t.name} className={t.enabled ? undefined : 'capability-table__row--hidden'}>
                  <td>{t.name}</td>
                  <td>{SOURCE_LABEL[t.source] ?? t.source}</td>
                  <td>{t.enabled ? '是' : '否'}</td>
                  <td>{t.hidden_reason ? (HIDDEN_REASON_LABEL[t.hidden_reason] ?? t.hidden_reason) : '—'}</td>
                  <td>{t.actually_loaded ? '是' : '否'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="capability-settings__section">
        <h4>Skill（用户级 / 项目级 / 内置三层，issue #18）</h4>
        {skillsLoading && <p>加载中…</p>}
        {skillsError && <div className="settings-page__error">{skillsError}</div>}
        {!skillsLoading && !skillsError && skills.length === 0 && (
          <p>还没有 Skill——把一个符合 SKILL.md 格式的目录放进 ~/.jones/skills/ 试试。</p>
        )}
        {skills.length > 0 && (
          <>
            {skills.some((s) => s.tier === 'project') && (
              <p className="capability-settings__project-skill-warning" role="alert">
                项目级 Skill 来自这个 Project 目录本身（例如 clone 下来的仓库），未经用户确认——谁能提交到这个目录，谁就能让 Agent 加载这个 Skill。
              </p>
            )}
            <table className="capability-table">
              <thead>
                <tr>
                  <th>名称</th>
                  <th>层级</th>
                  <th>描述</th>
                  <th>状态</th>
                </tr>
              </thead>
              <tbody>
                {skills.map((s) => (
                  <tr
                    key={s.source_path}
                    className={
                      !s.valid
                        ? 'capability-table__row--invalid'
                        : s.tier === 'project'
                          ? 'capability-table__row--project'
                          : undefined
                    }
                  >
                    <td>
                      {s.name}
                      {s.tier === 'project' && (
                        <span className="capability-table__badge" title="来自项目仓库，未经确认">
                          未确认
                        </span>
                      )}
                    </td>
                    <td>{TIER_LABEL[s.tier] ?? s.tier}</td>
                    <td>{s.description}</td>
                    <td>{s.valid ? '正常' : `格式错误：${s.error ?? '未知原因'}`}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}
      </div>
    </div>
  )
}
