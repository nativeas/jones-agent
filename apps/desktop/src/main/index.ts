import { existsSync, mkdirSync, openSync } from 'node:fs'
import { join } from 'node:path'
import os from 'node:os'
import { spawn } from 'node:child_process'
import { app, BrowserWindow, dialog, ipcMain } from 'electron'
import { RpcClient, RpcError } from './rpcClient'
import { ensureDaemonRunning as runDaemonLifecycle, startHeartbeat } from './daemonLifecycle'
import { RPC_V0_METHODS } from '../shared/rpcMethods'

// Same override the daemon's paths.py honors, so `JONES_HOME=... electron-vite dev`
// points both processes at the same sandbox during development/tests.
function daemonSocketPath(): string {
  const home = process.env.JONES_HOME || join(os.homedir(), '.jones')
  return join(home, 'runtime', 'daemon.sock')
}

/** An append fd under `<JONES_HOME>/logs/` for a daemon WE spawn, so its death
 * reason survives. Previously both spawn branches used `stdio: 'ignore'`: when a
 * spawned daemon died on startup the user (and CI) got a bare "daemon connection
 * closed" with no way to find out why — PRD 5.5 失败诚实 says an error must be
 * presented, never swallowed, and "nowhere at all" is the worst kind of swallow.
 * Returns `'ignore'` if the log can't be opened, so spawning still proceeds. */
function daemonSpawnLogStdio(): 'ignore' | ['ignore', number, number] {
  try {
    const home = process.env.JONES_HOME || join(os.homedir(), '.jones')
    const logs = join(home, 'logs')
    mkdirSync(logs, { recursive: true })
    const fd = openSync(join(logs, 'daemon-spawn.log'), 'a')
    return ['ignore', fd, fd]
  } catch {
    return 'ignore'
  }
}

const rpcClient = new RpcClient(daemonSocketPath())

// contextBridge only forwards `method`/`params` strings from the renderer (see
// preload/index.ts) — without a whitelist here, that renderer (the process most
// exposed to untrusted content: model output, tool results, rendered markdown)
// could invoke *any* daemon method, including provider.set_key, permission.decide
// and session.send, bypassing the permission gate entirely (design §1: "main ...
// contextBridge 白名单"). 02-w3-interfaces.md §2 集成收口 #3: this is now the
// complete RPC v0 surface (`../shared/rpcMethods.ts`, kept in sync with
// 00-foundation.md §4.1 by `src/shared/__tests__/rpcMethods.test.ts`) rather than
// the two-method placeholder from before that issue — a method not yet
// implemented server-side just answers `method_not_found`, the same as any
// other unregistered method, so listing the whole contract here up front means
// later issues implementing e.g. `cron.*` don't need to touch this file again.
const ALLOWED_RPC_METHODS: ReadonlySet<string> = new Set(RPC_V0_METHODS)

// Matches `service.DEFAULT_LABEL` in daemon/src/jones_daemon/service.py — the label
// `python -m jones_daemon service install` registers with launchd (design §6).
// Before that install has ever run, kickstart is a harmless no-op; spawnDevDaemon()
// below is what actually recovers the common local-dev case (daemon not started).
const DAEMON_LAUNCHD_LABEL = 'ai.jones.daemon'

function createWindow(): void {
  const win = new BrowserWindow({
    width: 1200,
    height: 800,
    show: false,
    webPreferences: {
      preload: join(__dirname, '../preload/index.cjs'),
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true
    }
  })

  win.once('ready-to-show', () => win.show())

  if (process.env.ELECTRON_RENDERER_URL) {
    win.loadURL(process.env.ELECTRON_RENDERER_URL)
  } else {
    win.loadFile(join(__dirname, '../renderer/index.html'))
  }
}

function findDaemonProjectDir(): string | null {
  // Walk up from this file looking for `daemon/pyproject.toml`: works whether
  // main is running from src (electron-vite dev) or out/main (build output),
  // since both sit at a fixed depth under the monorepo root.
  let dir = __dirname
  for (let i = 0; i < 6; i++) {
    if (existsSync(join(dir, 'daemon', 'pyproject.toml'))) return join(dir, 'daemon')
    const parent = join(dir, '..')
    if (parent === dir) break
    dir = parent
  }
  return null
}

// Packaged-mode daemon executable path (design §3.2, §6; Issue #25):
// electron-builder's `extraResources` (scripts/release/build-mac.sh,
// packaging/standalone) places the python-build-standalone daemon bundle under
// `Contents/Resources/daemon/`, with a self-contained entry script at
// `daemon/bin/jones-daemon` — the same script `service install --program`
// (daemon/src/jones_daemon/service.py) points a LaunchAgent's
// `ProgramArguments` at. `process.resourcesPath` is Electron's own accessor
// for that directory in a packaged build (it resolves somewhere under
// node_modules/electron in dev, which is why this is gated on
// `app.isPackaged`, not just `existsSync`).
function findPackagedDaemonExecutable(): string | null {
  if (!app.isPackaged) return null
  const exe = join(process.resourcesPath, 'daemon', 'bin', 'jones-daemon')
  return existsSync(exe) ? exe : null
}

function attemptLaunchdKickstart(): Promise<void> {
  if (process.platform !== 'darwin') return Promise.resolve()
  // Async, not spawnSync: this runs on Electron's main process thread, which
  // also owns window/menu/IPC event handling — blocking it synchronously for
  // up to 3s on every unreachable-daemon retry would freeze the whole UI for
  // that long (DEV.md 工程原则 #3: 性能是需求).
  return new Promise((resolve) => {
    const child = spawn(
      'launchctl',
      ['kickstart', '-k', `gui/${process.getuid?.() ?? 0}/${DAEMON_LAUNCHD_LABEL}`],
      { stdio: 'ignore' }
    )
    const timer = setTimeout(() => {
      child.kill()
      resolve()
    }, 3000)
    const done = (): void => {
      clearTimeout(timer)
      resolve()
    }
    // Result intentionally ignored either way: success means the daemon is (re)starting,
    // failure just means the service isn't installed yet (pre-packaging) or launchctl
    // isn't available — the ping retry below is what actually decides success.
    child.on('error', done)
    child.on('exit', done)
  })
}

function spawnDevDaemon(): void {
  if (app.isPackaged) {
    // 01-w2-interfaces.md §6 (E's own contract for this exact retry step):
    // "再失败且处于 dev 模式则直接 spawn `uv run python -m jones_daemon`
    // （打包模式 spawn `process.resourcesPath/daemon/...`）" — production still
    // relies on launchd as the primary supervisor (design §3), but this
    // specific fallback (3rd-attempt-of-3 recovery, after a kickstart against
    // a LaunchAgent that may never have been installed) is a direct spawn of
    // the bundled executable, the same shape as the dev-mode branch below,
    // not a `service install` call.
    const exe = findPackagedDaemonExecutable()
    if (!exe) return
    const child = spawn(exe, [], {
      env: process.env,
      stdio: daemonSpawnLogStdio(),
      detached: true
    })
    child.on('error', () => {
      // Best-effort, same reasoning as the dev-mode branch below.
    })
    child.unref()
    return
  }
  const daemonDir = findDaemonProjectDir()
  if (!daemonDir) return
  const child = spawn('uv', ['run', 'python', '-m', 'jones_daemon'], {
    cwd: daemonDir,
    env: process.env,
    stdio: daemonSpawnLogStdio(),
    detached: true
  })
  child.on('error', () => {
    // e.g. `uv` not on PATH — best-effort only; the retry loop's ping keeps
    // failing and eventually reports rather than crashing the main process on
    // an unhandled 'error' event.
  })
  child.unref()
}

async function pingOnce(timeoutMs: number): Promise<boolean> {
  try {
    await rpcClient.call('daemon.ping', undefined, timeoutMs)
    return true
  } catch (err) {
    // An RpcError means the daemon received the request and answered it (e.g.
    // `too_many_requests` when this connection's in-flight cap is hit) — that
    // still proves it's alive, the opposite of what daemonLifecycle's recovery
    // path (kickstart -k first) should react to. Only a connection-level
    // failure or timeout (no response at all) counts as unreachable.
    return err instanceof RpcError
  }
}

function reportDaemonUnreachable(): void {
  const payload = {
    code: -32000,
    message: 'daemon unreachable after retries',
    detail: { attempts: 3 }
  }
  const windows = BrowserWindow.getAllWindows()
  if (windows.length === 0) {
    // PRD 5.5 永不静默 / design §7 "worker/子进程失败必须变成 run.terminated 或
    // daemon.error 通知" — no window to deliver to (e.g. the last window was
    // closed on macOS) must not mean the failure vanishes silently.
    console.error('[daemon] unreachable after retries, no window to notify', payload)
    return
  }
  for (const win of windows) {
    win.webContents.send('rpc:notify', 'daemon.error', payload)
  }
}

// Real dependencies for daemonLifecycle.ts's injectable sequencing (design §3, §6):
// connect/ping this rpcClient, kickstart/spawn the real subprocess, push a real
// `daemon.error` notification. See daemonLifecycle.test.ts for the fake-dependency
// version of this same sequencing.
const daemonLifecycleDeps = {
  connect: () => rpcClient.connect(),
  ping: (timeoutMs: number) => pingOnce(timeoutMs),
  kickstart: attemptLaunchdKickstart,
  spawnDev: spawnDevDaemon,
  onUnreachable: reportDaemonUnreachable
}

/**
 * design §3: "Electron main 启动：先连 socket；连不上则尝试 launchctl kickstart
 * （已安装）或直接 spawn daemon（开发模式），最多重试 3 次后向 renderer 报错
 * （PRD 11.3）". Without this, a daemon that isn't running yet left the UI with
 * no recovery path and no error (PRD 5.5 永不静默) — rpcClient's own timeout fix
 * keeps a single ping from hanging forever, but this is what actually gets the
 * daemon running in the common dev case and reports it when it can't.
 */
function ensureDaemonRunning(): Promise<boolean> {
  return runDaemonLifecycle(daemonLifecycleDeps)
}

/** True when a failed `rpcClient.call` never reached the daemon (socket closed,
 * connect refused, timed out waiting for a connection) rather than the daemon
 * answering with an application error. An `RpcError` means the daemon DID answer
 * — replaying that would just repeat a real, already-reported failure. */
function isConnectionLevelFailure(err: unknown): boolean {
  return !(err instanceof RpcError)
}

rpcClient.onAnyNotification((method, params) => {
  for (const win of BrowserWindow.getAllWindows()) {
    win.webContents.send('rpc:notify', method, params)
  }
})

ipcMain.handle('rpc:call', async (_event, method: string, params?: Record<string, unknown>) => {
  if (!ALLOWED_RPC_METHODS.has(method)) {
    return { ok: false as const, message: `rpc method not allowed: ${method}` }
  }
  try {
    return { ok: true as const, result: await rpcClient.call(method, params) }
  } catch (err) {
    // A connection-level failure here is very often just "the daemon hasn't
    // finished starting yet": on a cold first launch main spawns it and needs a
    // few seconds, while the renderer's first `project.list`/`session.list` fire
    // as soon as the window loads. Those calls used to reject with a bare
    // "daemon connection closed" and no caller retried — the left pane then
    // stayed empty for the rest of the session (reproduced on a cold CI runner;
    // invisible on a warm dev machine where the daemon is already up).
    //
    // Daemon lifecycle knowledge belongs here, not in every renderer store, so
    // recover once and replay the call. `ensureDaemonRunning()` de-duplicates
    // concurrent recoveries internally (daemonLifecycle.ts's `inFlightRecovery`),
    // so a burst of first-paint calls triggers exactly one.
    if (!isConnectionLevelFailure(err)) {
      return { ok: false as const, message: err instanceof Error ? err.message : String(err) }
    }
    try {
      if (!(await ensureDaemonRunning())) {
        return { ok: false as const, message: err instanceof Error ? err.message : String(err) }
      }
      return { ok: true as const, result: await rpcClient.call(method, params) }
    } catch (retryErr) {
      return {
        ok: false as const,
        message: retryErr instanceof Error ? retryErr.message : String(retryErr)
      }
    }
  }
})

// 02-w3-interfaces.md §2 集成收口 #5: the Project 设置页 needs a real folder
// picker (FR02 "选目录即建 Project") — a native OS dialog is main-process-only
// (Node/Electron API, not something contextBridge can expose directly), so this
// is its own narrow IPC handler rather than being routed through `rpc:call`
// (which is specifically the daemon RPC bridge, not a general main-process
// capability channel — conflating the two would make the ALLOWED_RPC_METHODS
// whitelist above lie about what it actually bounds).
ipcMain.handle('dialog:pickDirectory', async (event) => {
  const win = BrowserWindow.fromWebContents(event.sender) ?? undefined
  const result = win
    ? await dialog.showOpenDialog(win, { properties: ['openDirectory', 'createDirectory'] })
    : await dialog.showOpenDialog({ properties: ['openDirectory', 'createDirectory'] })
  if (result.canceled || result.filePaths.length === 0) return { canceled: true as const }
  return { canceled: false as const, path: result.filePaths[0] }
})

let stopHeartbeat: (() => void) | null = null

// design §6: "健康检测：daemon.ping 心跳 5s，断线走 RpcClient 状态机重连" — started
// once per "app has a window" period (see window-all-closed / activate below),
// independent of ensureDaemonRunning's own outcome, since a daemon that answers
// now can still wedge later. Idempotent: a no-op if the heartbeat is already
// running, so activate can call it unconditionally.
function startDaemonHeartbeat(): void {
  if (stopHeartbeat) return
  stopHeartbeat = startHeartbeat(daemonLifecycleDeps)
}

app.whenReady().then(() => {
  createWindow()
  void ensureDaemonRunning()
  startDaemonHeartbeat()

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow()
    // macOS: window-all-closed below stops the client without quitting the app,
    // so reopening from the Dock needs to reconnect it (connect() is a no-op if
    // already connected/connecting) — otherwise every rpc call after "⌘W then
    // click the Dock icon" would hang on a socket nobody ever reopened.
    rpcClient.connect()
    // ...and the heartbeat that window-all-closed stopped alongside it (below)
    // needs restarting too, or a wedged-but-connected daemon would go undetected
    // until the next full quit/relaunch.
    startDaemonHeartbeat()
  })
})

app.on('window-all-closed', () => {
  // The daemon outlives the Electron shell by design (design §3); only stop the
  // client's own socket, never signal the daemon to exit here. Stop the
  // heartbeat in the same breath: left running, its next tick would find
  // rpcClient disconnected, treat that as an outage, and run the full recovery
  // sequence (reconnect + kickstart) — reviving the socket `stop()` just closed
  // within one heartbeat interval and making `stop()` a no-op in practice.
  rpcClient.stop()
  stopHeartbeat?.()
  stopHeartbeat = null
  if (process.platform !== 'darwin') app.quit()
})

app.on('will-quit', () => {
  stopHeartbeat?.()
  stopHeartbeat = null
})
