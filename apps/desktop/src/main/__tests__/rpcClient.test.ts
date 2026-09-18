import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import net from 'node:net'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { RpcClient, RpcError } from '../rpcClient'

/** Waits for the next 'data' event on `socket` instead of a fixed sleep — the
 * request bytes have actually arrived when this resolves, so a response can be
 * written back deterministically instead of hoping a blind delay was long
 * enough (DEV.md 工程原则 #5: 测试必须真的断言, not race a wall-clock guess). */
function nextData(socket: net.Socket): Promise<Buffer> {
  return new Promise((resolve) => socket.once('data', resolve))
}

describe('RpcClient NDJSON framing', () => {
  let dir: string
  let sockPath: string
  let server: net.Server
  let serverSocket: net.Socket | null = null
  let client: RpcClient

  beforeEach(async () => {
    dir = fs.mkdtempSync(path.join(os.tmpdir(), 'jn-'))
    sockPath = path.join(dir, 't.sock')
    server = net.createServer((socket) => {
      serverSocket = socket
    })
    await new Promise<void>((resolve) => server.listen(sockPath, resolve))
    client = new RpcClient(sockPath)
    const accepted = new Promise<void>((resolve) => server.once('connection', () => resolve()))
    client.connect()
    await accepted
  })

  afterEach(() => {
    client.stop()
    server.close()
    fs.rmSync(dir, { recursive: true, force: true })
  })

  it('resolves a call when the response arrives as one write', async () => {
    const callPromise = client.call('daemon.ping')
    await nextData(serverSocket!)
    serverSocket!.write(
      JSON.stringify({ jsonrpc: '2.0', id: 'c-1', result: { version: '0.1.0' } }) + '\n'
    )
    await expect(callPromise).resolves.toEqual({ version: '0.1.0' })
  })

  it('reassembles a response split across multiple chunks (no premature parse)', async () => {
    const callPromise = client.call('daemon.ping')
    await nextData(serverSocket!)
    const line = JSON.stringify({ jsonrpc: '2.0', id: 'c-1', result: { pid: 42 } }) + '\n'
    // Drip-feed a few bytes at a time across separate socket writes (including the
    // trailing newline, which a `.`-based regex split would otherwise swallow).
    for (let i = 0; i < line.length; i += 3) {
      serverSocket!.write(line.slice(i, i + 3))
    }
    await expect(callPromise).resolves.toEqual({ pid: 42 })
  })

  it('handles two NDJSON messages arriving in a single chunk', async () => {
    const first = client.call('a')
    await nextData(serverSocket!)
    const second = client.call('b')
    await nextData(serverSocket!)
    const combined =
      JSON.stringify({ jsonrpc: '2.0', id: 'c-1', result: 'first' }) +
      '\n' +
      JSON.stringify({ jsonrpc: '2.0', id: 'c-2', result: 'second' }) +
      '\n'
    serverSocket!.write(combined)
    await expect(first).resolves.toBe('first')
    await expect(second).resolves.toBe('second')
  })

  it('rejects with an RpcError carrying the daemon error code/message/data', async () => {
    const callPromise = client.call('session.get', { id: 'missing' })
    await nextData(serverSocket!)
    serverSocket!.write(
      JSON.stringify({
        jsonrpc: '2.0',
        id: 'c-1',
        error: { code: 1001, message: 'session not found', data: { id: 'missing' } }
      }) + '\n'
    )
    await expect(callPromise).rejects.toMatchObject({
      code: 1001,
      message: 'session not found',
      data: { id: 'missing' }
    })
    await expect(callPromise).rejects.toBeInstanceOf(RpcError)
  })

  it('dispatches notifications (no id) to registered handlers without touching pending calls', async () => {
    const received = await new Promise<unknown>((resolve) => {
      client.onNotification('daemon.error', (params) => resolve(params))
      serverSocket!.write(
        JSON.stringify({ jsonrpc: '2.0', method: 'daemon.error', params: { code: 1006 } }) + '\n'
      )
    })
    expect(received).toEqual({ code: 1006 })
  })

  it('ignores a malformed line instead of crashing the client', async () => {
    serverSocket!.write('not json at all\n')
    const callPromise = client.call('daemon.ping')
    await nextData(serverSocket!)
    serverSocket!.write(JSON.stringify({ jsonrpc: '2.0', id: 'c-1', result: 'ok' }) + '\n')
    await expect(callPromise).resolves.toBe('ok')
  })

  it('sends request ids as strings, per design §4', async () => {
    client.call('daemon.ping').catch(() => {
      // never resolved by this test (no response is sent); rejected by afterEach's
      // client.stop() during cleanup, which is expected and fine to ignore here.
    })
    const chunk = await nextData(serverSocket!)
    const sent = JSON.parse(chunk.toString('utf8').trim())
    expect(typeof sent.id).toBe('string')
  })

  it('times out a call made while the daemon is unreachable instead of hanging forever', async () => {
    // Regression test: whenConnected() used to have no deadline of its own, and
    // the per-call timer only started *after* it resolved — so a call made
    // before the first successful connect (the common "daemon not running yet"
    // case) would await forever. Point a fresh client at a socket nothing is
    // listening on and confirm call() rejects within its timeout instead.
    const deadSockPath = path.join(dir, 'nobody-listening.sock')
    const deadClient = new RpcClient(deadSockPath)
    deadClient.connect()
    try {
      await expect(deadClient.call('daemon.ping', undefined, 300)).rejects.toThrow()
    } finally {
      deadClient.stop()
    }
  })

  it('removes a timed-out call from the pending map instead of leaking it', async () => {
    // Regression test: the timeout handler used to only reject the caller's
    // promise, never delete the `pending` entry it had registered — since
    // nothing else ever cleans up an entry for an id the daemon was never
    // going to answer, a client that keeps timing out (wedged daemon, a
    // method that never replies) leaked one Map entry per call forever.
    await expect(client.call('session.stop', undefined, 30)).rejects.toThrow(/timed out/)
    expect((client as unknown as { pending: Map<string, unknown> }).pending.size).toBe(0)
  })

  it('does not multiply socket connections when the daemon keeps crashing', async () => {
    // Regression test for the state-machine bug: connect() used to treat
    // `socket.destroyed` as "is a connection already in flight", but that flag
    // is already `true` the instant a disconnect's 'close' event fires — the
    // same event that arms the internal reconnect backoff timer. A caller
    // that also calls connect() around then (exactly what ensureDaemonRunning's
    // retry loop and the macOS `activate` handler both do) used to open a
    // *second* socket on top of the one the timer was about to open too, and
    // every one of those, once it failed again, armed its own timer — sockets
    // multiplying with every daemon crash instead of staying at one.
    let connectionsAccepted = 0
    const crasher = net.createServer((socket) => {
      connectionsAccepted += 1
      socket.destroy() // simulates the daemon accepting then immediately dying
    })
    const crashSockPath = path.join(dir, 'crashy.sock')
    await new Promise<void>((resolve) => crasher.listen(crashSockPath, resolve))
    const crashyClient = new RpcClient(crashSockPath)
    try {
      // Simulate an external caller (ensureDaemonRunning-style) retrying
      // connect() on its own cadence, independent of the client's own backoff.
      crashyClient.connect()
      for (let i = 0; i < 5; i++) {
        await new Promise((resolve) => setTimeout(resolve, 30))
        crashyClient.connect()
      }
      await new Promise((resolve) => setTimeout(resolve, 200))
      // Upper bound generous enough to absorb real scheduling jitter, but far
      // below what unbounded multiplication would produce (which blows past
      // this within the same ~350ms window in practice).
      expect(connectionsAccepted).toBeLessThanOrEqual(10)
      expect(connectionsAccepted).toBeGreaterThan(0)
    } finally {
      crashyClient.stop()
      crasher.close()
    }
  })
})
