import { useState } from 'react'
import { useSessionsStore } from '../../store/sessionsStore'

/** Project 设置：选目录即建 Project（PRD FR02）。目录路径可手填，也可用
 * "浏览…" 弹出原生目录选择对话框（02-w3-interfaces.md §2 集成收口 #5,
 * main/index.ts 的 `dialog:pickDirectory` IPC）——`window.jones.dialog` 只在真实
 * Electron 渲染进程里存在（`pnpm dev:mock`/浏览器预览没有），所以那个按钮在
 * 没有它的环境里直接不渲染，而不是点了报错。 */
export function ProjectSettings(): JSX.Element {
  const projects = useSessionsStore((s) => s.projects)
  const createProject = useSessionsStore((s) => s.createProject)
  const [path, setPath] = useState('')
  const [pickError, setPickError] = useState<string | null>(null)

  const canPickDirectory =
    typeof window !== 'undefined' && typeof window.jones?.dialog?.pickDirectory === 'function'

  const pickDirectory = async (): Promise<void> => {
    setPickError(null)
    try {
      const result = await window.jones.dialog.pickDirectory()
      if (!result.canceled && result.path) setPath(result.path)
    } catch (err) {
      setPickError(err instanceof Error ? err.message : String(err))
    }
  }

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
      {pickError && <div className="settings-page__error">{pickError}</div>}
      <div className="project-add">
        <input
          value={path}
          onChange={(e) => setPath(e.target.value)}
          placeholder="项目目录绝对路径"
        />
        {canPickDirectory && <button onClick={() => void pickDirectory()}>浏览…</button>}
        <button onClick={() => void add()} disabled={!path.trim()}>
          新建 Project
        </button>
      </div>
    </div>
  )
}
