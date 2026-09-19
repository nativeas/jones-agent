import { useState } from 'react'
import { useSessionsStore } from '../../store/sessionsStore'

/** Project 设置：选目录即建 Project（PRD FR02）。桌面壳无 Node 权限，目录路径
 * 由用户手填 —— 真正的目录选择对话框需要 IPC 暴露一个新方法，超出本 Issue
 * 范围（见报告）。 */
export function ProjectSettings(): JSX.Element {
  const projects = useSessionsStore((s) => s.projects)
  const createProject = useSessionsStore((s) => s.createProject)
  const [path, setPath] = useState('')

  const add = async (): Promise<void> => {
    if (!path.trim()) return
    const created = await createProject(path.trim())
    if (created) setPath('')
  }

  return (
    <div className="settings-section">
      <h3>Project</h3>
      <ul className="project-list">
        {projects.map((project) => (
          <li key={project.id} className="project-list__item">
            <span>{project.name}</span>
            <span className="project-list__path">{project.path}</span>
          </li>
        ))}
      </ul>
      <div className="project-add">
        <input
          value={path}
          onChange={(e) => setPath(e.target.value)}
          placeholder="项目目录绝对路径"
        />
        <button onClick={() => void add()} disabled={!path.trim()}>
          新建 Project
        </button>
      </div>
    </div>
  )
}
