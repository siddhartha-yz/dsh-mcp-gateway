#!/usr/bin/env node

import { spawn, spawnSync } from 'node:child_process'
import { createRequire } from 'node:module'
import { createServer } from 'node:net'
import { readFile, rm, chmod, mkdir } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'

const MAX_REQUEST_BYTES = 128 * 1024
const MAX_ARGV = 160
const MAX_ARG_CHARS = 16_384
const MAX_STDOUT = 64 * 1024
const MAX_STDERR = 128 * 1024
const DEFAULT_STARTUP_TIMEOUT_MS = 15_000
const MAX_STARTUP_TIMEOUT_MS = 60_000
const TERMINATE_GRACE_MS = 3_000
const SESSION_ID_RE = /^browser-[0-9a-f-]{36}$/
const SOL_SOCKET = 1
const SO_PEERCRED = 17

function runtimeRootFromProcess() {
  if (process.env.DSH_RUNTIME_ROOT) return resolve(process.env.DSH_RUNTIME_ROOT)
  return resolve(dirname(process.execPath), '..', '..')
}

function playwrightChromiumExecutable(runtimeRoot = runtimeRootFromProcess()) {
  const require = createRequire(`${runtimeRoot}/package.json`)
  const { chromium } = require('playwright-core')
  return resolve(chromium.executablePath())
}

function boundedAppend(current, chunk, maxBytes) {
  const next = current + chunk.toString('utf8')
  if (Buffer.byteLength(next, 'utf8') <= maxBytes) return next
  return Buffer.from(next, 'utf8').subarray(-maxBytes).toString('utf8')
}

function parseProfileDir(argv, expectedExecutable, profileRoot, expectedBwrap, cwd) {
  const executableIndex = argv.indexOf(expectedExecutable)
  if (executableIndex < 0) throw new Error('argv does not contain the pinned Chromium executable')
  if (argv.indexOf(expectedExecutable, executableIndex + 1) >= 0) throw new Error('argv contains Chromium executable more than once')

  if (executableIndex !== 0) {
    const expectedPrefix = [
      resolve(expectedBwrap),
      '--ro-bind', '/', '/',
      '--dev', '/dev',
      '--unshare-pid',
      '--proc', '/proc',
      '--die-with-parent',
      '--tmpfs', '/tmp',
      '--bind', resolve(cwd), resolve(cwd),
      '--',
    ]
    if (executableIndex !== expectedPrefix.length) throw new Error('wrapped browser argv has unexpected bwrap profile length')
    for (let index = 0; index < expectedPrefix.length; index += 1) {
      const actual = index === 0 ? resolve(argv[index]) : argv[index]
      if (actual !== expectedPrefix[index]) throw new Error(`wrapped browser argv does not match pinned DSH bwrap profile at index ${index}`)
    }
  }

  const browserArgs = argv.slice(executableIndex + 1)
  if (!browserArgs.includes('--remote-debugging-port=0')) throw new Error('Chromium must use an ephemeral remote debugging port')
  if (browserArgs.some(arg => arg === '--no-sandbox' || arg.startsWith('--no-sandbox='))) throw new Error('--no-sandbox is forbidden')
  const profileArgs = browserArgs.filter(arg => arg.startsWith('--user-data-dir='))
  if (profileArgs.length !== 1) throw new Error('Chromium must have exactly one user-data-dir')
  const profileDir = resolve(profileArgs[0].slice('--user-data-dir='.length))
  const root = resolve(profileRoot)
  if (profileDir === root || !profileDir.startsWith(`${root}/`)) throw new Error('Chromium user-data-dir must be inside the configured worker profile root')
  return profileDir
}

export function validateSpawnRequest(request, {
  expectedExecutable = playwrightChromiumExecutable(),
  profileRoot = process.env.DSH_BROWSER_PROFILE_ROOT ?? '/tmp/dsh-browser-worker',
  expectedBwrap = '/usr/bin/bwrap',
} = {}) {
  if (!request || typeof request !== 'object' || Array.isArray(request)) throw new Error('request must be an object')
  const keys = Object.keys(request).sort()
  const allowed = new Set(['op', 'argv', 'cwd', 'id', 'startupTimeoutMs'])
  for (const key of keys) if (!allowed.has(key)) throw new Error(`unexpected request field: ${key}`)
  if (request.op !== 'spawn') throw new Error('first request must use op=spawn')
  if (typeof request.id !== 'string' || !SESSION_ID_RE.test(request.id)) throw new Error('invalid browser session id')
  if (typeof request.cwd !== 'string' || !request.cwd.startsWith('/') || request.cwd.length > 4096) throw new Error('cwd must be an absolute path')
  if (!Array.isArray(request.argv) || request.argv.length < 2 || request.argv.length > MAX_ARGV) throw new Error('argv has invalid length')
  for (const arg of request.argv) {
    if (typeof arg !== 'string' || arg.length === 0 || arg.length > MAX_ARG_CHARS || arg.includes('\0')) throw new Error('argv contains an invalid argument')
  }
  const startupTimeoutMs = request.startupTimeoutMs ?? DEFAULT_STARTUP_TIMEOUT_MS
  if (!Number.isInteger(startupTimeoutMs) || startupTimeoutMs < 1 || startupTimeoutMs > MAX_STARTUP_TIMEOUT_MS) throw new Error('startupTimeoutMs is out of range')
  const executable = resolve(expectedExecutable)
  const profileDir = parseProfileDir(request.argv.map(arg => arg === expectedExecutable ? executable : arg), executable, profileRoot, expectedBwrap, request.cwd)
  return { ...request, argv: request.argv.map(arg => arg === expectedExecutable ? executable : arg), startupTimeoutMs, profileDir, expectedExecutable: executable }
}

function send(socket, value) {
  if (!socket.destroyed) socket.write(`${JSON.stringify(value)}\n`)
}

function killGroup(child, signal) {
  if (!child?.pid) return
  try { process.kill(-child.pid, signal) } catch (error) {
    if (error?.code !== 'ESRCH') throw error
  }
}

async function terminateChild(child) {
  if (!child || child.exitCode !== null || child.signalCode !== null) return
  try { killGroup(child, 'SIGTERM') } catch {}
  const exited = new Promise(resolvePromise => child.once('exit', resolvePromise))
  const timer = new Promise(resolvePromise => setTimeout(resolvePromise, TERMINATE_GRACE_MS, 'timeout'))
  if (await Promise.race([exited, timer]) === 'timeout') {
    try { killGroup(child, 'SIGKILL') } catch {}
  }
}

async function readSecurityState() {
  const status = await readFile('/proc/self/status', 'utf8')
  const noNewPrivileges = /^NoNewPrivs:\s*1$/m.test(status)
  let apparmor = ''
  try { apparmor = (await readFile('/proc/self/attr/current', 'utf8')).trim() } catch {}
  return { noNewPrivileges, apparmor }
}

export async function assertWorkerSecurity() {
  const state = await readSecurityState()
  if (!state.noNewPrivileges) throw new Error('browser worker refuses to run without NoNewPrivs=1')
  if (!state.apparmor.startsWith('dsh-browser-worker')) throw new Error(`browser worker requires dsh-browser-worker AppArmor profile; got ${JSON.stringify(state.apparmor)}`)
  return state
}

export function createPeerCredentialReader(runtimeRoot = runtimeRootFromProcess()) {
  const require = createRequire(`${runtimeRoot}/package.json`)
  const koffi = require('koffi')
  const libc = koffi.load(null)
  const getsockopt = libc.func('int getsockopt(int,int,int,void*,void*)')
  return socket => {
    const fd = socket?._handle?.fd
    if (!Number.isInteger(fd) || fd < 0) throw new Error('browser worker cannot inspect Unix peer credentials')
    const cred = Buffer.alloc(12)
    const length = Buffer.alloc(4)
    length.writeUInt32LE(cred.length)
    const rc = getsockopt(fd, SOL_SOCKET, SO_PEERCRED, cred, length)
    if (rc !== 0 || length.readUInt32LE(0) !== cred.length) throw new Error('browser worker SO_PEERCRED lookup failed')
    return {
      pid: cred.readInt32LE(0),
      uid: cred.readUInt32LE(4),
      gid: cred.readUInt32LE(8),
    }
  }
}

export function dshHostMainPid() {
  const result = spawnSync('/usr/bin/systemctl', ['show', 'dsh-web-host.service', '-p', 'MainPID', '--value'], {
    encoding: 'utf8',
    timeout: 2000,
    stdio: ['ignore', 'pipe', 'pipe'],
  })
  if (result.error) throw new Error(`cannot query dsh-web-host MainPID: ${result.error.message}`)
  if (result.status !== 0) throw new Error(`cannot query dsh-web-host MainPID: ${String(result.stderr ?? '').trim() || `systemctl exited ${result.status}`}`)
  const pid = Number.parseInt(String(result.stdout ?? '').trim(), 10)
  if (!Number.isInteger(pid) || pid <= 1) throw new Error('dsh-web-host MainPID is unavailable')
  return pid
}

export function assertAuthorizedPeer(socket, {
  peerCredentials,
  hostMainPid = dshHostMainPid,
} = {}) {
  if (typeof peerCredentials !== 'function') throw new Error('browser worker peer credential reader is unavailable')
  const peer = peerCredentials(socket)
  const expectedPid = hostMainPid()
  const expectedUid = process.getuid?.()
  if (!Number.isInteger(peer?.pid) || peer.pid !== expectedPid) throw new Error(`browser worker rejects peer pid ${peer?.pid ?? 'unknown'}; expected DSH Host MainPID ${expectedPid}`)
  if (Number.isInteger(expectedUid) && peer.uid !== expectedUid) throw new Error(`browser worker rejects peer uid ${peer?.uid ?? 'unknown'}`)
  return peer
}

async function handleConnection(socket, config) {
  socket.setNoDelay(true)
  let buffer = ''
  let child = null
  let stdoutTail = ''
  let stderrTail = ''
  let spawned = false
  let terminating = false
  let profileDir = null

  const cleanup = async () => {
    if (terminating) return
    terminating = true
    await terminateChild(child).catch(() => {})
    if (profileDir) await rm(profileDir, { recursive: true, force: true }).catch(() => {})
  }

  socket.__cleanupBrowser = cleanup
  socket.on('close', () => { void cleanup() })
  socket.on('error', () => {})

  socket.on('data', chunk => {
    buffer += chunk.toString('utf8')
    if (Buffer.byteLength(buffer, 'utf8') > MAX_REQUEST_BYTES) {
      send(socket, { ok: false, error: 'request_too_large' })
      socket.destroy()
      return
    }
    for (;;) {
      const newline = buffer.indexOf('\n')
      if (newline < 0) break
      const line = buffer.slice(0, newline)
      buffer = buffer.slice(newline + 1)
      if (!line.trim()) continue
      void (async () => {
        let request
        try { request = JSON.parse(line) } catch {
          send(socket, { ok: false, error: 'invalid_json' })
          socket.end()
          return
        }
        if (!spawned) {
          if (request?.op === 'ping') {
            const security = await readSecurityState()
            send(socket, { ok: true, security })
            socket.end()
            return
          }
          let spec
          try {
            assertAuthorizedPeer(socket, config)
            spec = validateSpawnRequest(request, config)
          } catch (error) {
            send(socket, { ok: false, error: 'invalid_spawn', message: String(error?.message ?? error) })
            socket.end()
            return
          }
          spawned = true
          profileDir = spec.profileDir
          try {
            await mkdir(dirname(profileDir), { recursive: true, mode: 0o700 })
            await mkdir(profileDir, { mode: 0o700 })
          } catch (error) {
            send(socket, { ok: false, error: 'profile_create_failed', message: String(error?.message ?? error) })
            socket.end()
            return
          }
          const env = {
            ...process.env,
            HOME: profileDir,
            XDG_CACHE_HOME: `${profileDir}/.cache`,
            XDG_CONFIG_HOME: `${profileDir}/.config`,
            DSH_BROWSER_SESSION_ID: spec.id,
          }
          child = spawn(spec.argv[0], spec.argv.slice(1), {
            cwd: spec.cwd,
            env,
            detached: true,
            stdio: ['ignore', 'pipe', 'pipe'],
          })
          child.stdout.on('data', data => {
            stdoutTail = boundedAppend(stdoutTail, data, MAX_STDOUT)
            const match = `${stderrTail}\n${stdoutTail}`.match(/DevTools listening on (ws:\/\/[^\s]+)/)
            if (match && !socket.__readySent) {
              socket.__readySent = true
              send(socket, { ok: true, endpoint: match[1], pid: child.pid })
            }
          })
          child.stderr.on('data', data => {
            stderrTail = boundedAppend(stderrTail, data, MAX_STDERR)
            const match = `${stderrTail}\n${stdoutTail}`.match(/DevTools listening on (ws:\/\/[^\s]+)/)
            if (match && !socket.__readySent) {
              socket.__readySent = true
              send(socket, { ok: true, endpoint: match[1], pid: child.pid })
            }
          })
          child.once('error', error => {
            if (!socket.__readySent) send(socket, { ok: false, error: 'spawn_failed', message: String(error), stderrTail })
            socket.end()
          })
          child.once('exit', (exitCode, signal) => {
            if (!socket.__readySent) send(socket, { ok: false, error: 'browser_exited', exitCode, signal, stderrTail: stderrTail.slice(-4000) })
            else send(socket, { event: 'exit', exitCode, signal, stderrTail: stderrTail.slice(-4000) })
            socket.end()
          })
          setTimeout(() => {
            if (!socket.__readySent && !socket.destroyed) {
              send(socket, { ok: false, error: 'startup_timeout', stderrTail: stderrTail.slice(-4000) })
              socket.end()
            }
          }, spec.startupTimeoutMs).unref()
          return
        }

        if (request?.op === 'terminate') {
          await cleanup()
          send(socket, { ok: true, terminated: true })
          socket.end()
          return
        }
        send(socket, { ok: false, error: 'unsupported_operation' })
      })().catch(() => socket.destroy())
    }
  })
}

export async function main() {
  const socketPath = process.env.DSH_BROWSER_WORKER_SOCKET ?? '/run/dsh-browser-worker/browser.sock'
  const profileRoot = process.env.DSH_BROWSER_PROFILE_ROOT ?? '/tmp/dsh-browser-worker'
  const runtimeRoot = runtimeRootFromProcess()
  const expectedExecutable = playwrightChromiumExecutable(runtimeRoot)
  const expectedBwrap = '/usr/bin/bwrap'
  const peerCredentials = createPeerCredentialReader(runtimeRoot)
  const security = await assertWorkerSecurity()
  await rm(socketPath, { force: true })
  const sockets = new Set()
  const server = createServer(socket => {
    sockets.add(socket)
    socket.once('close', () => sockets.delete(socket))
    void handleConnection(socket, { expectedExecutable, expectedBwrap, profileRoot, peerCredentials })
  })
  server.maxConnections = 8
  await new Promise((resolvePromise, reject) => {
    server.once('error', reject)
    server.listen(socketPath, resolvePromise)
  })
  await chmod(socketPath, 0o600)
  process.stdout.write(`dsh-browser-worker ready socket=${socketPath} apparmor=${security.apparmor} nnp=1\n`)

  let shuttingDown = false
  const shutdown = async () => {
    if (shuttingDown) return
    shuttingDown = true
    const closed = new Promise(resolvePromise => server.close(resolvePromise))
    const active = [...sockets]
    await Promise.allSettled(active.map(socket => socket.__cleanupBrowser?.() ?? Promise.resolve()))
    for (const socket of active) socket.destroy()
    await closed
    await rm(socketPath, { force: true }).catch(() => {})
    process.exitCode = 0
  }
  process.once('SIGTERM', () => { void shutdown() })
  process.once('SIGINT', () => { void shutdown() })
}

const isMain = process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href
if (isMain) {
  main().catch(error => {
    console.error(error?.stack ?? String(error))
    process.exit(1)
  })
}
