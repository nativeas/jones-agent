import { useState } from 'react'
import type { Step } from '../../domain/types'

const STATUS_LABEL: Record<Step['status'], string> = {
  running: '执行中',
  completed: '已完成',
  failed: '失败'
}

/** Collapsed-by-default tool-call card (01-w2-interfaces.md §5: "step.started/
 * completed 折叠卡片"). Expands to show the args/result summary. */
export function StepCard({ step }: { step: Step }): JSX.Element {
  const [open, setOpen] = useState(false)
  return (
    <div className={`step-card step-card--${step.status}`}>
      <button className="step-card__header" onClick={() => setOpen((o) => !o)}>
        <span className="step-card__toggle">{open ? '▾' : '▸'}</span>
        <span className="step-card__tool">{step.tool}</span>
        <span className="step-card__status">{STATUS_LABEL[step.status]}</span>
        {step.duration_ms != null && <span className="step-card__duration">{step.duration_ms}ms</span>}
      </button>
      {open && (
        <div className="step-card__body">
          <div>
            <b>参数：</b>
            {step.args_summary}
          </div>
          {step.result_summary && (
            <div>
              <b>结果：</b>
              {step.result_summary}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
