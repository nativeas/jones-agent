import { useState } from 'react'
import { buildProjectGroups, type SessionNode } from '../../domain/tree'
import { useSessionsStore, mainSession } from '../../store/sessionsStore'

function SessionRow({
  node,
  depth,
  selectedId,
  onSelect
}: {
  node: SessionNode
  depth: number
  selectedId: string | null
  onSelect: (id: string) => void
}): JSX.Element {
  const { session, children } = node
  return (
    <div>
      <button
        className={`session-row${session.id === selectedId ? ' session-row--selected' : ''}${
          session.is_main ? ' session-row--main' : ''
        }`}
        style={{ paddingLeft: 8 + depth * 16 }}
        onClick={() => onSelect(session.id)}
      >
        <span className="session-row__title">{session.title}</span>
        {session.is_main && <span className="session-row__badge">主会话</span>}
        <span className={`session-row__status session-row__status--${session.status}`}>
          {session.status === 'running' ? '●' : ''}
        </span>
      </button>
      {children.map((child) => (
        <SessionRow key={child.session.id} node={child} depth={depth + 1} selectedId={selectedId} onSelect={onSelect} />
      ))}
    </div>
  )
}

/** 左栏：Project 分组 → Session 树（01-w2-interfaces.md §5）。 */
export function LeftPane(): JSX.Element {
  const projects = useSessionsStore((s) => s.projects)
  const sessions = useSessionsStore((s) => s.sessions)
  const selectedSessionId = useSessionsStore((s) => s.selectedSessionId)
  const selectSession = useSessionsStore((s) => s.selectSession)
  const createSession = useSessionsStore((s) => s.createSession)
  const error = useSessionsStore((s) => s.error)

  const [creatingFor, setCreatingFor] = useState<string | null>(null)

  const groups = buildProjectGroups(projects, sessions)

  const newSubSession = async (projectId: string, parentId: string, agentId: string): Promise<void> => {
    setCreatingFor(parentId)
    try {
      await createSession({ project_id: projectId, agent_id: agentId, parent_id: parentId, title: '新子会话' })
    } finally {
      setCreatingFor(null)
    }
  }

  // No Agent picker in this issue's scope (settings page owns Agent CRUD) —
  // default to that project's main session's agent, or the daemon's seeded
  // placeholder id (01-w2-interfaces.md §2: `agent_default`) for a project
  // that has no sessions yet.
  const defaultAgentIdFor = (projectId: string): string =>
    mainSession(sessions.filter((s) => s.project_id === projectId))?.agent_id ?? 'agent_default'

  const newRootSession = async (projectId: string): Promise<void> => {
    setCreatingFor(projectId)
    try {
      await createSession({ project_id: projectId, agent_id: defaultAgentIdFor(projectId), title: '新会话' })
    } finally {
      setCreatingFor(null)
    }
  }

  return (
    <div className="left-pane">
      {error && <div className="left-pane__error">{error}</div>}
      {groups.map((group) => (
        <div key={group.project.id} className="project-group">
          <div className="project-group__header">
            <span className="project-group__title">{group.project.name}</span>
            <button
              className="project-group__new-session"
              disabled={creatingFor === group.project.id}
              onClick={() => void newRootSession(group.project.id)}
            >
              + 新建会话
            </button>
          </div>
          {group.roots.map((node) => (
            <div key={node.session.id}>
              <SessionRow node={node} depth={0} selectedId={selectedSessionId} onSelect={selectSession} />
              <button
                className="session-row__new-child"
                style={{ paddingLeft: 8 + 16 }}
                disabled={creatingFor === node.session.id}
                onClick={() => void newSubSession(group.project.id, node.session.id, node.session.agent_id)}
              >
                + 派生子会话
              </button>
            </div>
          ))}
        </div>
      ))}
    </div>
  )
}
