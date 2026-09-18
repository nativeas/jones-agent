import { useCallback, useEffect, useState } from 'react'

interface PingResult {
  version: string
  pid: number
  uptime_s: number
}

type Status = { kind: 'loading' } | { kind: 'ok'; data: PingResult } | { kind: 'error'; message: string }

/** Small card calling `daemon.ping` — no polling (idle CPU must stay ~0, PRD 11.2):
 * it pings once on mount and again only when the user asks. */
export function DaemonStatusCard(): JSX.Element {
  // Initial state is already 'loading', so the mount-time fetch below never needs to
  // set state synchronously from within the effect body (react-hooks/set-state-in-effect).
  const [status, setStatus] = useState<Status>({ kind: 'loading' })

  const runPing = useCallback(() => {
    window.jones.rpc
      .call('daemon.ping')
      .then((res) => {
        if (res.ok) {
          setStatus({ kind: 'ok', data: res.result as PingResult })
        } else {
          setStatus({ kind: 'error', message: res.message ?? 'unknown error' })
        }
      })
      .catch((err: unknown) => {
        setStatus({ kind: 'error', message: err instanceof Error ? err.message : String(err) })
      })
  }, [])

  const refresh = useCallback(() => {
    setStatus({ kind: 'loading' })
    runPing()
  }, [runPing])

  useEffect(() => {
    runPing()
  }, [runPing])

  return (
    <div className="daemon-status-card">
      <div className="daemon-status-card__header">
        <span>守护进程</span>
        <button onClick={refresh} disabled={status.kind === 'loading'}>
          刷新
        </button>
      </div>
      {status.kind === 'loading' && <p>检测中…</p>}
      {status.kind === 'ok' && (
        <p>
          v{status.data.version} · pid {status.data.pid} · 运行 {status.data.uptime_s.toFixed(1)}s
        </p>
      )}
      {status.kind === 'error' && <p className="daemon-status-card__error">{status.message}</p>}
    </div>
  )
}
