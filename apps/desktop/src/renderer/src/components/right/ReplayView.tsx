import { useEffect } from 'react'
import type { RpcTransport } from '../../rpc/transport'
import { useReplayStore, type ReplayStep } from '../../store/replayStore'

const TERMINATED_KIND_LABEL: Record<string, string> = {
  user: '用户终止',
  error: '错误终止',
  budget: '预算终止'
}

function formatArgs(args: unknown): string {
  if (args == null) return '—'
  try {
    return JSON.stringify(args, null, 2)
  } catch {
    return String(args)
  }
}

function StepPayload({ step }: { step: ReplayStep }): JSX.Element | null {
  const payloadCache = useReplayStore((s) => s.payloadCache)
  const loadStepPayload = useReplayStore((s) => s.loadStepPayload)
  if (!step.payload_ref) return null
  const text = payloadCache[step.payload_ref]
  return (
    <div className="replay-view__payload">
      {text === undefined ? (
        <button onClick={() => void loadStepPayload(step.payload_ref as string)}>
          加载完整输出
        </button>
      ) : (
        <pre className="replay-view__pre">{text}</pre>
      )}
    </div>
  )
}

function PromptSnapshotPanel(): JSX.Element | null {
  const run = useReplayStore((s) => s.run)
  const promptSnapshot = useReplayStore((s) => s.promptSnapshot)
  const loadPromptSnapshot = useReplayStore((s) => s.loadPromptSnapshot)
  if (!run?.prompt_snapshot_ref) return null
  return (
    <div className="replay-view__prompt-snapshot">
      <div className="replay-view__section-title">发送给模型的 Prompt</div>
      {promptSnapshot === null ? (
        <button onClick={() => void loadPromptSnapshot()}>加载</button>
      ) : (
        <pre className="replay-view__pre">{promptSnapshot}</pre>
      )}
    </div>
  )
}

/**
 * 右栏「回放」视图 (PRD FR06 / docs/design/02-w3-interfaces.md §2): 选一个 Run →
 * Step 时间线可前进/后退，展示参数/结果/耗时/审批结果/发送给模型的 prompt。
 *
 * 纯读：每次交互只调用 `run.list`/`run.get`/`run.steps`/`run.payload`
 * （`replayStore.ts`），没有一次会改变服务端状态——回放不触发任何真实动作
 * 这条 PRD 硬约束，落到这个组件上就是"这个文件里不出现任何写类 RPC 调用"。
 */
export function ReplayView({
  transport,
  sessionId
}: {
  transport: RpcTransport
  sessionId: string | null
}): JSX.Element {
  const bindSession = useReplayStore((s) => s.bindSession)
  const refreshRuns = useReplayStore((s) => s.refreshRuns)
  const selectRun = useReplayStore((s) => s.selectRun)
  const stepForward = useReplayStore((s) => s.stepForward)
  const stepBack = useReplayStore((s) => s.stepBack)
  const runs = useReplayStore((s) => s.runs)
  const selectedRunId = useReplayStore((s) => s.selectedRunId)
  const run = useReplayStore((s) => s.run)
  const steps = useReplayStore((s) => s.steps)
  const cursor = useReplayStore((s) => s.cursor)
  const loading = useReplayStore((s) => s.loading)
  const error = useReplayStore((s) => s.error)

  useEffect(() => {
    if (sessionId) void bindSession(transport, sessionId)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId])

  if (!sessionId) {
    return <p className="shell__placeholder">左栏选择一个会话开始</p>
  }

  const step = steps[cursor]

  return (
    <div className="replay-view">
      <div className="replay-view__toolbar">
        <select
          value={selectedRunId ?? ''}
          onChange={(e) => e.target.value && void selectRun(e.target.value)}
        >
          <option value="" disabled>
            选择一个 Run…
          </option>
          {runs.map((r) => (
            <option key={r.id} value={r.id}>
              {(r.started_at ?? r.created_at).slice(0, 19).replace('T', ' ')} · {r.status}
              {r.terminated_kind ? ` · ${TERMINATED_KIND_LABEL[r.terminated_kind] ?? r.terminated_kind}` : ''}
            </option>
          ))}
        </select>
        <button onClick={() => void refreshRuns()} disabled={loading}>
          刷新
        </button>
      </div>

      {error && <div className="replay-view__error">{error}</div>}
      {!error && runs.length === 0 && !loading && (
        <p className="shell__placeholder">这个会话还没有可回放的 Run</p>
      )}

      {run && (
        <div className="replay-view__run-meta">
          <div>
            状态：{run.status}
            {run.terminated_kind && ` · ${TERMINATED_KIND_LABEL[run.terminated_kind] ?? run.terminated_kind}`}
          </div>
          {run.terminated_reason && <div className="replay-view__reason">{run.terminated_reason}</div>}
          {run.terminated_step_seq != null && (
            <div>终止于第 {run.terminated_step_seq} 步</div>
          )}
        </div>
      )}

      <PromptSnapshotPanel />

      {steps.length > 0 && (
        <div className="replay-view__timeline">
          <div className="replay-view__nav">
            <button onClick={stepBack} disabled={cursor <= 0}>
              ◀ 上一步
            </button>
            <span className="replay-view__position">
              第 {cursor + 1} / {steps.length} 步（seq {step?.seq}）
            </span>
            <button onClick={stepForward} disabled={cursor >= steps.length - 1}>
              下一步 ▶
            </button>
          </div>

          {step && (
            <div className={`replay-view__step replay-view__step--${step.status}`}>
              <div className="replay-view__step-header">
                <span className="step-card__tool">{step.tool}</span>
                <span className="step-card__status">{step.status}</span>
                {step.duration_ms != null && (
                  <span className="step-card__duration">{step.duration_ms}ms</span>
                )}
              </div>
              <div>
                <b>参数：</b>
                <pre className="replay-view__pre">{formatArgs(step.args)}</pre>
              </div>
              {step.result_summary && (
                <div>
                  <b>结果摘要：</b>
                  <pre className="replay-view__pre">{step.result_summary}</pre>
                </div>
              )}
              <StepPayload step={step} />
              <div className="replay-view__permission">
                审批：{step.permission_id ? step.permission_id : '未触发权限闸'}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
