import { useCallback, useEffect, useState } from 'react'
import { CircleAlert, CircleCheck, Loader2, RefreshCw } from 'lucide-react'
import { BorderBeam } from './ui/border-beam'
import { cn } from '../lib/utils'
import type { RpcTransport } from '../rpc/transport'

interface PingResult {
  version: string
  pid: number
  uptime_s: number
}

type Status = { kind: 'loading' } | { kind: 'ok'; data: PingResult } | { kind: 'error'; message: string }

const STATUS_STYLE: Record<Status['kind'], { pill: string; icon: string }> = {
  loading: { pill: 'border-zinc-400/25 bg-zinc-400/5', icon: 'text-zinc-400' },
  ok: { pill: 'border-emerald-500/25 bg-emerald-500/5', icon: 'text-emerald-500' },
  error: { pill: 'border-red-500/30 bg-red-500/10', icon: 'text-red-500' }
}

/** Small card calling `daemon.ping` — no polling (idle CPU must stay ~0, PRD 11.2):
 * it pings once on mount and again only when the user asks. Goes through the
 * injected `RpcTransport` (not `window.jones` directly) so it also works under
 * `MockTransport` — vitest and `pnpm dev:mock` (01-w2-interfaces.md §5). */
export function DaemonStatusCard({ transport }: { transport: RpcTransport }): JSX.Element {
  // Initial state is already 'loading', so the mount-time fetch below never needs to
  // set state synchronously from within the effect body (react-hooks/set-state-in-effect).
  const [status, setStatus] = useState<Status>({ kind: 'loading' })

  const runPing = useCallback(() => {
    transport
      .call<PingResult>('daemon.ping')
      .then((res) => {
        if (res.ok && res.result) {
          setStatus({ kind: 'ok', data: res.result })
        } else {
          setStatus({ kind: 'error', message: res.message ?? 'unknown error' })
        }
      })
      .catch((err: unknown) => {
        setStatus({ kind: 'error', message: err instanceof Error ? err.message : String(err) })
      })
  }, [transport])

  const refresh = useCallback(() => {
    setStatus({ kind: 'loading' })
    runPing()
  }, [runPing])

  useEffect(() => {
    runPing()
  }, [runPing])

  // main pushes `daemon.error` after it gives up retrying (00-foundation.md
  // §4.2, PRD 11.3: "永不静默") — this card only otherwise pings on mount/
  // refresh, so without this subscription that push has no listener and the
  // card can sit on a stale "ok" or mid-retry state forever (§8 review).
  useEffect(() => {
    return transport.on('daemon.error', (raw) => {
      const payload = raw as { code?: string; message?: string }
      setStatus({ kind: 'error', message: payload.message ?? payload.code ?? 'daemon 报告了一个错误' })
    })
  }, [transport])

  const { pill, icon } = STATUS_STYLE[status.kind]

  return (
    <div
      title="守护进程状态"
      className={cn(
        'relative inline-flex items-center gap-1.5 overflow-hidden rounded-full border px-2.5 py-1 text-xs transition-colors',
        pill
      )}
    >
      {/* BorderBeam only for the error state — a moving accent that draws the eye
       * exactly when something needs it, not a permanent idle-CPU cost. */}
      {status.kind === 'error' && <BorderBeam size={40} duration={4} colorFrom="#ef4444" colorTo="#f87171" />}

      {status.kind === 'loading' && <Loader2 className={cn('size-3.5 shrink-0 animate-spin', icon)} />}
      {status.kind === 'ok' && <CircleCheck className={cn('size-3.5 shrink-0', icon)} />}
      {status.kind === 'error' && <CircleAlert className={cn('size-3.5 shrink-0', icon)} />}

      {status.kind === 'loading' && <span className="opacity-70">检测中…</span>}
      {status.kind === 'ok' && (
        <span className="opacity-80">
          v{status.data.version} · pid {status.data.pid} · {status.data.uptime_s.toFixed(1)}s
        </span>
      )}
      {status.kind === 'error' && <span className="max-w-56 truncate text-red-500">{status.message}</span>}

      <button
        onClick={refresh}
        disabled={status.kind === 'loading'}
        aria-label="刷新守护进程状态"
        className="ml-0.5 inline-flex size-4 shrink-0 items-center justify-center rounded-full opacity-50 transition hover:opacity-100 disabled:cursor-not-allowed disabled:opacity-30"
      >
        <RefreshCw className={cn('size-3', status.kind === 'loading' && 'animate-spin')} />
      </button>
    </div>
  )
}
