import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import net from 'node:net'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { RpcClient, RpcError } from '../rpcClient'

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
    client.connect()
    await new Promise((resolve) => setTimeout(resolve, 50))
  })

  afterEach(() => {
    client.stop()
    server.close()
    fs.rmSync(dir, { recursive: true, force: true })
  })

  it('resolves a call when the response arrives as one write', async () => {
    const callPromise = client.call('daemon.ping')
    await new Promise((resolve) => setTimeout(resolve, 20))
    serverSocket!.write(
      JSON.stringify({ jsonrpc: '2.0', id: '1', result: { version: '0.1.0' } }) + '\n'
    )
    await expect(callPromise).resolves.toEqual({ version: '0.1.0' })
  })

  it('reassembles a response split across multiple chunks (no premature parse)', async () => {
    const callPromise = client.call('daemon.ping')
    await new Promise((resolve) => setTimeout(resolve, 20))
    const line = JSON.stringify({ jsonrpc: '2.0', id: '1', result: { pid: 42 } }) + '\n'
    // Drip-feed a few bytes at a time across separate socket writes (including the
    // trailing newline, which a `.`-based regex split would otherwise swallow).
    for (let i = 0; i < line.length; i += 3) {
      serverSocket!.write(line.slice(i, i + 3))
    }
    await expect(callPromise).resolves.toEqual({ pid: 42 })
  })

  it('handles two NDJSON messages arriving in a single chunk', async () => {
    const first = client.call('a')
    await new Promise((resolve) => setTimeout(resolve, 20))
    const second = client.call('b')
    await new Promise((resolve) => setTimeout(resolve, 20))
    const combined =
      JSON.stringify({ jsonrpc: '2.0', id: '1', result: 'first' }) +
      '\n' +
      JSON.stringify({ jsonrpc: '2.0', id: '2', result: 'second' }) +
      '\n'
    serverSocket!.write(combined)
    await expect(first).resolves.toBe('first')
    await expect(second).resolves.toBe('second')
  })

  it('rejects with an RpcError carrying the daemon error code/message/data', async () => {
    const callPromise = client.call('session.get', { id: 'missing' })
    await new Promise((resolve) => setTimeout(resolve, 20))
    serverSocket!.write(
      JSON.stringify({
        jsonrpc: '2.0',
        id: '1',
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
    const received: unknown[] = []
    client.onNotification('daemon.error', (params) => received.push(params))
    serverSocket!.write(
      JSON.stringify({ jsonrpc: '2.0', method: 'daemon.error', params: { code: 1006 } }) + '\n'
    )
    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(received).toEqual([{ code: 1006 }])
  })

  it('ignores a malformed line instead of crashing the client', async () => {
    serverSocket!.write('not json at all\n')
    const callPromise = client.call('daemon.ping')
    await new Promise((resolve) => setTimeout(resolve, 20))
    serverSocket!.write(JSON.stringify({ jsonrpc: '2.0', id: '1', result: 'ok' }) + '\n')
    await expect(callPromise).resolves.toBe('ok')
  })

  it('sends request ids as strings, per design §4', async () => {
    client.call('daemon.ping').catch(() => {
      // never resolved by this test (no response is sent); rejected by afterEach's
      // client.stop() during cleanup, which is expected and fine to ignore here.
    })
    await new Promise((resolve) => setTimeout(resolve, 20))
    const received: Buffer[] = []
    await new Promise<void>((resolve) => {
      serverSocket!.once('data', (chunk) => {
        received.push(chunk)
        resolve()
      })
    })
    const sent = JSON.parse(Buffer.concat(received).toString('utf8').trim())
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
})
