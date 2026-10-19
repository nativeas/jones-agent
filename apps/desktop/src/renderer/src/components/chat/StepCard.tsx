import { useState } from 'react'
import { cn } from '../../lib/utils'
import type { Step } from '../../domain/types'

const STATUS_CONFIG: Record<Step['status'], { label: string; dot: string; text: string }> = {
  running: { label: '执行中', dot: 'bg-sky-500', text: 'text-sky-600 dark:text-sky-400' },
  completed: { label: '已完成', dot: 'bg-emerald-500', text: 'text-emerald-600 dark:text-emerald-400' },
  failed: { label: '失败', dot: 'bg-red-500', text: 'text-red-600 dark:text-red-400' }
}

/** Collapsed-by-default tool-call card (01-w2-interfaces.md §5: "step.started/
 * completed 折叠卡片"). Expands to show the args/result summary. */
export function StepCard({ step }: { step: Step }): JSX.Element {
  const [open, setOpen] = useState(false)
  const status = STATUS_CONFIG[step.status]
  const hasArgs = Object.keys(step.args).length > 0

  return (
    <div className="my-1.5 overflow-hidden rounded-lg border border-black/10 bg-black/[0.02] dark:border-white/10 dark:bg-white/[0.04]">
      <button
        type="button"
        data-testid="step-card-header"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-[13px]"
      >
        <svg
          viewBox="0 0 24 24"
          aria-hidden
          className={cn(
            'size-3 shrink-0 fill-none stroke-current stroke-[3] text-black/40 transition-transform duration-150 dark:text-white/40',
            open && 'rotate-90'
          )}
        >
          <path d="M9 6l6 6-6 6" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
        {step.status === 'running' ? (
          <span
            aria-hidden
            className="size-2.5 shrink-0 animate-spin rounded-full border-2 border-sky-500 border-t-transparent"
          />
        ) : (
          <span aria-hidden className={cn('size-2 shrink-0 rounded-full', status.dot)} />
        )}
        <span className="flex-1 truncate font-mono text-[12.5px] font-semibold">{step.tool}</span>
        <span className={cn('shrink-0 text-[11px] font-medium', status.text)}>{status.label}</span>
        {step.duration_ms != null && (
          <span className="shrink-0 text-[11px] text-black/40 dark:text-white/40">{step.duration_ms}ms</span>
        )}
      </button>
      <div
        className={cn(
          'grid transition-[grid-template-rows] duration-200 ease-out',
          open ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'
        )}
      >
        <div className="overflow-hidden">
          <div className="space-y-1 border-t border-black/10 px-3 py-2 font-mono text-[12px] leading-relaxed text-black/70 dark:border-white/10 dark:text-white/70">
            <div>
              <span className="text-black/40 dark:text-white/40">参数 </span>
              {hasArgs ? JSON.stringify(step.args) : '（无参数）'}
            </div>
            {step.result_summary && (
              <div>
                <span className="text-black/40 dark:text-white/40">结果 </span>
                {step.result_summary}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
