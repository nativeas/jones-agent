import { describe, expect, it } from 'vitest'
import { act } from 'react-dom/test-utils'
import { createRoot, type Root } from 'react-dom/client'
import { StepCard } from '../chat/StepCard'
import type { Step } from '../../domain/types'

/** 评审第 3 轮 important: `Step.args_summary` never existed on the wire —
 * `steps.args_json` decodes (`sessions/queries.py::_d()`) under the key
 * `args`, a plain object. Guards against the same "renderer type vs. daemon
 * `_d()` reality" drift #34 was, this time for `Step` instead of `Message`. */
describe('StepCard', () => {
  it('renders the real args object once expanded, not an undefined args_summary', () => {
    const step: Step = {
      id: 'step_1',
      run_id: 'run_1',
      seq: 1,
      tool: 'read_file',
      args: { path: 'README.md' },
      result_summary: '12 行',
      duration_ms: 40,
      status: 'completed'
    }

    const container = document.createElement('div')
    document.body.appendChild(container)
    let root: Root | null = null
    try {
      act(() => {
        root = createRoot(container)
        root.render(<StepCard step={step} />)
      })
      act(() => {
        container.querySelector<HTMLButtonElement>('.step-card__header')!.click()
      })

      expect(container.textContent).toContain('README.md')
      expect(container.textContent).not.toContain('undefined')
    } finally {
      act(() => {
        root?.unmount()
      })
      container.remove()
    }
  })

  it('shows a placeholder instead of an empty string when args is empty', () => {
    const step: Step = {
      id: 'step_2',
      run_id: 'run_1',
      seq: 2,
      tool: 'noop',
      args: {},
      result_summary: null,
      duration_ms: null,
      status: 'running'
    }

    const container = document.createElement('div')
    document.body.appendChild(container)
    let root: Root | null = null
    try {
      act(() => {
        root = createRoot(container)
        root.render(<StepCard step={step} />)
      })
      act(() => {
        container.querySelector<HTMLButtonElement>('.step-card__header')!.click()
      })

      expect(container.textContent).toContain('（无参数）')
    } finally {
      act(() => {
        root?.unmount()
      })
      container.remove()
    }
  })
})
