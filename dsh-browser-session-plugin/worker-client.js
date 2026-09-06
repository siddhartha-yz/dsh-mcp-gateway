import { createConnection } from 'node:net'

const MAX_RESPONSE_BYTES = 256 * 1024

function abortReason(signal) {
  return signal?.reason instanceof Error ? signal.reason : new Error(String(signal?.reason ?? 'operation aborted'))
}

function waitWithSignal(promise, signal) {
  if (!signal) return promise
  signal.throwIfAborted?.()
  return new Promise((resolve, reject) => {
    const onAbort = () => reject(abortReason(signal))
    signal.addEventListener('abort', onAbort, { once: true })
    Promise.resolve(promise).then(
      value => { signal.removeEventListener('abort', onAbort); resolve(value) },
      error => { signal.removeEventListener('abort', onAbort); reject(error) },
    )
  })
}

class WorkerHandle {
  constructor(socket) {
    this.socket = socket
    this.pid = null
    this.exited = false
    this.exitInfo = null
    this._terminateSent = false
    this.done = new Promise(resolve => { this._resolveDone = resolve })
  }

  markExit(info) {
    if (this.exited) return
    this.exited = true
    this.exitInfo = info
    this._resolveDone(info)
  }

  terminate() {
    if (this._terminateSent || this.socket.destroyed) return
    this._terminateSent = true
    this.socket.write(`${JSON.stringify({ op: 'terminate' })}\n`)
  }

  async waitForExit(signal) {
    await waitWithSignal(this.done, signal)
    return true
  }
}

export async function launchBrowserViaWorker({ socketPath, argv, cwd, id, startupTimeoutMs, signal }) {
  if (typeof socketPath !== 'string' || !socketPath.startsWith('/')) throw new Error('browser worker socketPath must be absolute')
  const socket = createConnection({ path: socketPath })
  const handle = new WorkerHandle(socket)
  let buffer = ''
  let settled = false

  const startup = new Promise((resolve, reject) => {
    const fail = error => {
      if (settled) return
      settled = true
      reject(error)
    }

    socket.once('connect', () => {
      socket.write(`${JSON.stringify({ op: 'spawn', argv, cwd, id, startupTimeoutMs })}\n`)
    })
    socket.once('error', error => {
      fail(new Error(`browser worker connection failed: ${error.message}`))
      handle.markExit({ error: String(error) })
    })
    socket.on('close', () => {
      if (!settled) fail(new Error('browser worker connection closed before Chromium became ready'))
      handle.markExit(handle.exitInfo ?? { connectionClosed: true })
    })
    socket.on('data', chunk => {
      buffer += chunk.toString('utf8')
      if (Buffer.byteLength(buffer, 'utf8') > MAX_RESPONSE_BYTES) {
        socket.destroy()
        fail(new Error('browser worker response exceeded limit'))
        return
      }
      for (;;) {
        const newline = buffer.indexOf('\n')
        if (newline < 0) break
        const line = buffer.slice(0, newline)
        buffer = buffer.slice(newline + 1)
        if (!line.trim()) continue
        let message
        try { message = JSON.parse(line) } catch {
          socket.destroy()
          fail(new Error('browser worker returned invalid JSON'))
          return
        }
        if (message.event === 'exit') {
          handle.markExit(message)
          continue
        }
        if (!settled) {
          if (message.ok === true && typeof message.endpoint === 'string') {
            settled = true
            handle.pid = Number.isInteger(message.pid) ? message.pid : null
            resolve({ handle, endpoint: message.endpoint })
            continue
          }
          const detail = message.stderrTail ? `; stderr tail: ${String(message.stderrTail).slice(-4000)}` : ''
          settled = true
          reject(new Error(`browser worker failed to start Chromium: ${message.message ?? message.error ?? 'unknown error'}${detail}`))
          socket.end()
        }
      }
    })
  })

  try {
    return await waitWithSignal(startup, signal)
  } catch (error) {
    try { handle.terminate() } catch {}
    socket.destroy()
    throw error
  }
}

export async function pingBrowserWorker(socketPath, { timeoutMs = 3000 } = {}) {
  const socket = createConnection({ path: socketPath })
  let timer
  try {
    return await new Promise((resolve, reject) => {
      let buffer = ''
      timer = setTimeout(() => { socket.destroy(); reject(new Error('browser worker ping timed out')) }, timeoutMs)
      socket.once('error', reject)
      socket.once('connect', () => socket.write(`${JSON.stringify({ op: 'ping' })}\n`))
      socket.on('data', chunk => {
        buffer += chunk.toString('utf8')
        const newline = buffer.indexOf('\n')
        if (newline < 0) return
        try { resolve(JSON.parse(buffer.slice(0, newline))) } catch (error) { reject(error) }
        socket.end()
      })
    })
  } finally {
    if (timer) clearTimeout(timer)
    socket.destroy()
  }
}
