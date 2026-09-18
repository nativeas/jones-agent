import { existsSync } from 'node:fs'
import { join } from 'node:path'
import os from 'node:os'
import { spawn } from 'node:child_process'
import { app, BrowserWindow, ipcMain } from 'electron'
import { RpcClient } from './rpcClient'
import { ensureDaemonRunning as runDaemonLifecycle, startHeartbeat } from './daemonLifecycle'

// Same override the daemon's paths.py honors, so `JONES_HOME=... electron-vite dev`
// points both processes at the same sandbox during development/tests.
function daemonSocketPath(): string {
  const home = process.env.JONES_HOME || join(os.homedir(), '.jones')
  return join(home, 'runtime', 'daemon.sock')
}

const rpcClient = new RpcClient(daemonSocketPath())

// contextBridge only forwards `method`/`params` strings from the renderer (see
// preload/index.ts) — without a whitelist here, that renderer (the process most
// exposed to untrusted content: model output, tool results, rendered markdown)
// could invoke *any* daemon method, including provider.set_key, permission.decide
// and session.send, bypassing the permission gate entirely (design §1: "main ...
// contextBridge 白名单"). The daemon itself currently only implements this
// subset (rpc/methods.py `register_builtin_methods`) — extend both together as
// later issues add methods from design §4.1.
const ALLOWED_RPC_METHODS: ReadonlySet<string> = new Set(['daemon.ping', 'daemon.status'])

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
  if (app.isPackaged) return // production installs rely on launchd, not a raw spawn
  const daemonDir = findDaemonProjectDir()
  if (!daemonDir) return
  const child = spawn('uv', ['run', 'python', '-m', 'jones_daemon'], {
    cwd: daemonDir,
    env: process.env,
    stdio: 'ignore',
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
  } catch {
    return false
  }
}

function reportDaemonUnreachable(): void {
  const payload = {
    code: -32000,
    message: 'daemon unreachable after retries',
    detail: { attempts: 3 }
  }
  for (const win of BrowserWindow.getAllWindows()) {
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
    return { ok: false as const, message: err instanceof Error ? err.message : String(err) }
  }
})

let stopHeartbeat: (() => void) | null = null

app.whenReady().then(() => {
  createWindow()
  void ensureDaemonRunning()
  // design §6: "健康检测：daemon.ping 心跳 5s，断线走 RpcClient 状态机重连" — started
  // once at app startup (not per-window), independent of ensureDaemonRunning's own
  // outcome, since a daemon that answers now can still wedge later.
  stopHeartbeat = startHeartbeat(daemonLifecycleDeps)

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow()
    // macOS: window-all-closed below stops the client without quitting the app,
    // so reopening from the Dock needs to reconnect it (connect() is a no-op if
    // already connected/connecting) — otherwise every rpc call after "⌘W then
    // click the Dock icon" would hang on a socket nobody ever reopened.
    rpcClient.connect()
  })
})

app.on('window-all-closed', () => {
  // The daemon outlives the Electron shell by design (design §3); only stop the
  // client's own socket, never signal the daemon to exit here.
  rpcClient.stop()
  if (process.platform !== 'darwin') app.quit()
})

app.on('will-quit', () => {
  stopHeartbeat?.()
})
