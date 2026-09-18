import { join } from 'node:path'
import os from 'node:os'
import { app, BrowserWindow, ipcMain } from 'electron'
import { RpcClient } from './rpcClient'

// Same override the daemon's paths.py honors, so `JONES_HOME=... electron-vite dev`
// points both processes at the same sandbox during development/tests.
function daemonSocketPath(): string {
  const home = process.env.JONES_HOME || join(os.homedir(), '.jones')
  return join(home, 'runtime', 'daemon.sock')
}

const rpcClient = new RpcClient(daemonSocketPath())

function createWindow(): void {
  const win = new BrowserWindow({
    width: 1200,
    height: 800,
    show: false,
    webPreferences: {
      preload: join(__dirname, '../preload/index.mjs'),
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: false
    }
  })

  win.once('ready-to-show', () => win.show())

  if (process.env.ELECTRON_RENDERER_URL) {
    win.loadURL(process.env.ELECTRON_RENDERER_URL)
  } else {
    win.loadFile(join(__dirname, '../renderer/index.html'))
  }
}

rpcClient.onAnyNotification((method, params) => {
  for (const win of BrowserWindow.getAllWindows()) {
    win.webContents.send('rpc:notify', method, params)
  }
})

ipcMain.handle('rpc:call', async (_event, method: string, params?: Record<string, unknown>) => {
  try {
    return { ok: true as const, result: await rpcClient.call(method, params) }
  } catch (err) {
    return { ok: false as const, message: err instanceof Error ? err.message : String(err) }
  }
})

app.whenReady().then(() => {
  rpcClient.connect()
  createWindow()

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow()
  })
})

app.on('window-all-closed', () => {
  // The daemon outlives the Electron shell by design (design §3); only stop the
  // client's own socket, never signal the daemon to exit here.
  rpcClient.stop()
  if (process.platform !== 'darwin') app.quit()
})
