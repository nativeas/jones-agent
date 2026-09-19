import type { Project, Session } from './types'

export interface SessionNode {
  session: Session
  children: SessionNode[]
}

export interface ProjectGroup {
  project: Project
  /** Main session first (there is at most one, globally — 00-foundation.md §5),
   * then root-level (non-main, no parent) sessions in list order, each with its
   * subtree attached. */
  roots: SessionNode[]
}

/**
 * Pure grouping/tree-building for the left pane (01-w2-interfaces.md §5:
 * "左栏：Project 分组 → Session 树"). Kept side-effect-free and separate from
 * the store so it's cheap to unit test without any transport.
 */
export function buildProjectGroups(projects: Project[], sessions: Session[]): ProjectGroup[] {
  const byProject = new Map<string, Session[]>()
  for (const session of sessions) {
    const list = byProject.get(session.project_id) ?? []
    list.push(session)
    byProject.set(session.project_id, list)
  }

  return projects.map((project) => {
    const projectSessions = byProject.get(project.id) ?? []
    const childrenByParent = new Map<string, Session[]>()
    for (const session of projectSessions) {
      if (session.parent_id === null) continue
      const list = childrenByParent.get(session.parent_id) ?? []
      list.push(session)
      childrenByParent.set(session.parent_id, list)
    }

    const buildNode = (session: Session): SessionNode => ({
      session,
      children: (childrenByParent.get(session.id) ?? []).map(buildNode)
    })

    const rootSessions = projectSessions.filter((s) => s.parent_id === null)
    // Main session pinned first regardless of list order (issue #5: "主会话固定顶部").
    rootSessions.sort((a, b) => Number(b.is_main) - Number(a.is_main))

    return { project, roots: rootSessions.map(buildNode) }
  })
}
