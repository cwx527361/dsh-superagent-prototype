// Minimal job hub for the dsh runner (M1 POC).
//
//   POST /runner/register      {accessToken, deviceId, devicePublicKey, os, arch, version}
//   POST /runner/oauth/start   (501 in the POC: the company SSO wiring is next)
//   POST /runner/oauth/exchange
//   GET  /health
//   POST /jobs                 {ticket, repo:{dir,gitUrl}, base, branch, prompt, model, timeoutMs, deviceId?}
//   GET  /jobs                 list
//   GET  /jobs/:id             state + events + result
//   POST /jobs/:id/cancel      {reason}
//   WSS  /runner/connect       Bearer <deviceToken>; envelope frames
//
// Env: PORT (4096), HUB_TOKEN (dev-token), STATE_DIR (~/.dsh-runner-hub), PUBLIC_URL.
import http from 'node:http'
import path from 'node:path'
import fs from 'node:fs'
import { randomBytes, randomUUID } from 'node:crypto'
import { WebSocketServer } from 'ws'

const PORT = Number(process.env.PORT ?? 4096)
const HUB_TOKEN = process.env.HUB_TOKEN ?? 'dev-token'
const STATE_DIR = process.env.STATE_DIR ?? path.join(process.env.HOME ?? '.', '.dsh-runner-hub')
const PUBLIC_URL = process.env.PUBLIC_URL ?? `http://127.0.0.1:${PORT}`
const PROTOCOL = 1

fs.mkdirSync(STATE_DIR, { recursive: true })
const file = (name) => path.join(STATE_DIR, name)
const load = (name, fallback) => {
  try { return JSON.parse(fs.readFileSync(file(name), 'utf8')) } catch { return fallback }
}
const save = (name, value) => {
  const tmp = file(`${name}.tmp`)
  fs.writeFileSync(tmp, JSON.stringify(value, null, 2))
  fs.renameSync(tmp, file(name))
}

/** deviceId -> { id, token, publicKey, os, arch, version, maxConcurrency, lastSeen, ws? } */
const devices = new Map()
/** jobId -> { id, spec, deviceId, status, createdAt, acceptedAt, finishedAt, events, result, failure } */
const jobs = new Map()

for (const [id, d] of Object.entries(load('devices.json', {}))) {
  devices.set(id, { ...d, maxConcurrency: d.maxConcurrency ?? 1, ws: null })
}
for (const [id, j] of Object.entries(load('jobs.json', {}))) {
  jobs.set(id, { ...j, spec: j.spec ?? {} })
}
const persistDevices = () => save('devices.json', Object.fromEntries(
  [...devices].map(([id, d]) => [id, { id, token: d.token, publicKey: d.publicKey, os: d.os, arch: d.arch, version: d.version, maxConcurrency: d.maxConcurrency, lastSeen: d.lastSeen }]),
))
const persistJobs = () => save('jobs.json', Object.fromEntries(
  [...jobs].map(([id, j]) => [id, { ...j, spec: j.spec, events: j.events }]),
))

const log = (...args) => console.log(new Date().toISOString(), ...args)

// --- HTTP helpers ----------------------------------------------------------

const json = (res, code, body) => {
  const raw = JSON.stringify(body)
  res.writeHead(code, { 'content-type': 'application/json', 'content-length': Buffer.byteLength(raw) })
  res.end(raw)
}

async function readJSON(req, limit = 1 << 20) {
  const chunks = []
  let size = 0
  for await (const chunk of req) {
    size += chunk.length
    if (size > limit) throw new Error('body too large')
    chunks.push(chunk)
  }
  if (size === 0) return {}
  return JSON.parse(Buffer.concat(chunks).toString('utf8'))
}

const frame = (type, payload) => JSON.stringify({ v: PROTOCOL, id: randomUUID(), type, ts: new Date().toISOString(), payload })
const sendWS = (ws, type, payload) => { try { ws?.send(frame(type, payload)) } catch {} }

function onlineDevices() {
  return [...devices.values()].filter((d) => d.ws && d.ws.readyState === 1)
}
function runningOn(deviceId) {
  return [...jobs.values()].filter((j) => j.deviceId === deviceId && (j.status === 'offered' || j.status === 'running')).length
}
function pickDevice(preferred) {
  if (preferred) {
    const d = devices.get(preferred)
    if (d?.ws) return d
  }
  const candidates = onlineDevices().filter((d) => runningOn(d.id) < (d.maxConcurrency ?? 1))
  candidates.sort((a, b) => runningOn(a.id) - runningOn(b.id))
  return candidates[0] ?? null
}

// --- HTTP server -----------------------------------------------------------

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, PUBLIC_URL)
  const route = `${req.method} ${url.pathname}`
  try {
    if (route === 'GET /health') {
      return json(res, 200, {
        ok: true,
        protocol: PROTOCOL,
        runners: onlineDevices().map((d) => ({ id: d.id, os: d.os, arch: d.arch, version: d.version, running: runningOn(d.id) })),
        jobs: {
          queued: [...jobs.values()].filter((j) => j.status === 'offered').length,
          running: [...jobs.values()].filter((j) => j.status === 'running').length,
          done: [...jobs.values()].filter((j) => j.status === 'succeeded' || j.status === 'failed').length,
        },
      })
    }

    if (route === 'POST /runner/register') {
      const body = await readJSON(req)
      if (body.accessToken !== HUB_TOKEN) return json(res, 401, { error: 'invalid access token' })
      const deviceId = body.deviceId || `dev_${randomBytes(6).toString('hex')}`
      const token = randomBytes(24).toString('base64url')
      devices.set(deviceId, {
        id: deviceId, token, publicKey: body.devicePublicKey ?? '', os: body.os ?? '', arch: body.arch ?? '',
        version: body.version ?? '', maxConcurrency: Number(body.maxConcurrency) || 1, lastSeen: new Date().toISOString(), ws: null,
      })
      persistDevices()
      log(`registered ${deviceId} (${body.os}/${body.arch} v${body.version})`)
      return json(res, 200, { deviceId, deviceToken: token, server: PUBLIC_URL })
    }

    if (route === 'POST /runner/oauth/start' || route === 'POST /runner/oauth/exchange') {
      return json(res, 501, { error: 'OAuth wiring is not part of the M1 POC; use POST /runner/register with an access token' })
    }

    if (route === 'POST /jobs') {
      const body = await readJSON(req)
      const device = pickDevice(body.deviceId)
      if (!device) return json(res, 503, { error: 'no runner online' })
      const jobId = `job_${randomBytes(6).toString('hex')}`
      const ticket = body.ticket ?? jobId
      const spec = {
        jobId,
        ticket,
        repo: { dir: body.repo?.dir ?? '', gitUrl: body.repo?.gitUrl ?? '' },
        base: body.base ?? 'HEAD',
        branch: body.branch ?? `feat/${ticket}`,
        prompt: body.prompt ?? '',
        model: body.model ?? 'deepseek/deepseek-flash',
        timeoutMs: Number(body.timeoutMs) || 30 * 60 * 1000,
        worktree: { dir: body.worktree?.dir ?? '' },
      }
      if (!spec.prompt) return json(res, 400, { error: 'prompt is required' })
      const job = { id: jobId, spec, deviceId: device.id, status: 'offered', createdAt: new Date().toISOString(), acceptedAt: null, finishedAt: null, events: [], inputs: [], result: null, failure: null }
      jobs.set(jobId, job)
      persistJobs()
      sendWS(device.ws, 'job.offer', spec)
      log(`job ${jobId} offered to ${device.id} (${ticket})`)
      return json(res, 200, { jobId, deviceId: device.id, status: job.status })
    }

    if (route === 'GET /jobs') {
      const list = [...jobs.values()].map(({ id, spec, deviceId, status, createdAt, finishedAt }) => ({ id, ticket: spec.ticket, deviceId, status, createdAt, finishedAt }))
      return json(res, 200, { jobs: list.reverse() })
    }

    const jobMatch = url.pathname.match(/^\/jobs\/([^/]+)(\/(cancel|input))?$/)
    if (jobMatch) {
      const job = jobs.get(jobMatch[1])
      if (!job) return json(res, 404, { error: 'unknown job' })
      if (req.method === 'GET' && !jobMatch[2]) return json(res, 200, job)
      if (req.method === 'POST' && jobMatch[3] === 'input') {
        const body = await readJSON(req)
        if (!body.text) return json(res, 400, { error: 'text is required' })
        const live = job.status === 'offered' || job.status === 'running'
        job.inputs.push({ at: new Date().toISOString(), text: body.text })
        sendWS(devices.get(job.deviceId)?.ws, 'job.input', { jobId: job.id, text: body.text })
        persistJobs()
        log(`job ${job.id} input (${body.text.length} chars)`)
        return json(res, 200, { jobId: job.id, accepted: live, status: job.status })
      }
      if (req.method === 'POST' && jobMatch[3] === 'cancel') {
        const body = await readJSON(req)
        job.status = 'cancelled'
        job.finishedAt = new Date().toISOString()
        job.failure = { cause: 'cancelled', detail: body.reason ?? '' }
        sendWS(devices.get(job.deviceId)?.ws, 'job.cancel', { jobId: job.id, reason: body.reason ?? 'cancelled' })
        persistJobs()
        return json(res, 200, { jobId: job.id, status: job.status })
      }
      return json(res, 405, { error: 'method not allowed' })
    }

    return json(res, 404, { error: 'not found' })
  } catch (err) {
    log('error', err?.stack ?? err)
    return json(res, 500, { error: String(err?.message ?? err) })
  }
})

// --- WebSocket: runner control channel -------------------------------------

const wss = new WebSocketServer({ noServer: true })
server.on('upgrade', (req, socket, head) => {
  const url = new URL(req.url, PUBLIC_URL)
  if (url.pathname !== '/runner/connect') return socket.destroy()
  const header = req.headers.authorization ?? ''
  const token = header.startsWith('Bearer ') ? header.slice(7) : url.searchParams.get('token')
  const device = [...devices.values()].find((d) => d.token === token)
  if (!device) {
    socket.write('HTTP/1.1 401 Unauthorized\r\n\r\n')
    return socket.destroy()
  }
  wss.handleUpgrade(req, socket, head, (ws) => wss.emit('connection', ws, device))
})

wss.on('connection', (ws, device) => {
  device.ws = ws
  device.lastSeen = new Date().toISOString()
  log(`runner ${device.id} connected`)
  sendWS(ws, 'welcome', {
    deviceId: device.id, protocol: PROTOCOL, leaseSeconds: 60,
    running: [...jobs.values()].filter((j) => j.deviceId === device.id && j.status === 'running').map((j) => j.id),
  })

  ws.on('message', (raw) => {
    let env
    try { env = JSON.parse(raw.toString()) } catch { return }
    const payload = env.payload ?? {}
    device.lastSeen = new Date().toISOString()
    switch (env.type) {
      case 'hello':
        device.os = payload.os ?? device.os
        device.arch = payload.arch ?? device.arch
        device.version = payload.version ?? device.version
        device.maxConcurrency = payload.maxConcurrency ?? device.maxConcurrency
        persistDevices()
        break
      case 'heartbeat':
        break
      case 'job.accept': {
        const job = jobs.get(payload.jobId)
        if (job) { job.status = 'running'; job.acceptedAt = new Date().toISOString(); persistJobs() }
        break
      }
      case 'job.event': {
        const job = jobs.get(payload.jobId)
        if (job) { job.events.push({ at: new Date().toISOString(), kind: payload.kind, data: payload.data }); persistJobs() }
        break
      }
      case 'job.result': {
        const job = jobs.get(payload.jobId)
        if (job) {
          job.status = 'succeeded'; job.finishedAt = new Date().toISOString()
          job.result = { branch: payload.branch, commit: payload.commit, diffstat: payload.diffstat, summary: payload.summary }
          persistJobs()
          log(`job ${job.id} succeeded (${payload.branch} ${payload.commit})`)
        }
        break
      }
      case 'job.failed': {
        const job = jobs.get(payload.jobId)
        if (job) {
          job.status = 'failed'; job.finishedAt = new Date().toISOString()
          job.failure = { cause: payload.cause, detail: payload.detail, retryable: payload.retryable }
          persistJobs()
          log(`job ${job.id} failed: ${payload.cause} ${payload.detail ?? ''}`)
        }
        break
      }
      case 'pong':
        break
      default:
        log(`unknown frame ${env.type}`)
    }
  })

  ws.on('close', () => {
    if (device.ws === ws) device.ws = null
    log(`runner ${device.id} disconnected`)
  })
})

server.listen(PORT, '0.0.0.0', () => log(`hub listening on :${PORT} (state ${STATE_DIR})`))
