#!/usr/bin/env node
/**
 * POC: a container (the "cloud" side) dispatching a job to the opencode server
 * running on the developer's machine (the "local" side).
 *
 * It uses only opencode's HTTP API:
 *   GET  /api/info                      -> health / version
 *   POST /api/session                   -> {location:{directory}, model}
 *   POST /api/session/:id/prompt        -> {text}
 *   GET  /api/session/:id               -> outcome / tokens / cost
 *   GET  /api/session/:id/message       -> transcript
 *
 * Env:
 *   OPENCODE_URL        default http://host.docker.internal:4097
 *   OPENCODE_PASSWORD   the password printed by `opencode serve`
 *   OPENCODE_DIRECTORY  host path of the repo opencode should work in
 *   OPENCODE_MODEL      provider/model, default deepseek/deepseek-flash
 *   PROMPT              what to ask for
 *   TIMEOUT_MS          default 300000
 */

const env = process.env
const url = (env.OPENCODE_URL ?? 'http://host.docker.internal:4097').replace(/\/+$/, '')
const password = env.OPENCODE_PASSWORD ?? ''
const directory = env.OPENCODE_DIRECTORY ?? ''
const [providerID, modelID] = (env.OPENCODE_MODEL ?? 'deepseek/deepseek-flash').split('/')
const prompt = env.PROMPT ?? 'Create POC2.md containing exactly: hello from cloud. Then commit it.'
const timeoutMs = Number(env.TIMEOUT_MS ?? 300_000)

if (!password) throw new Error('OPENCODE_PASSWORD is required')
if (!directory) throw new Error('OPENCODE_DIRECTORY is required (host path of the repo)')

const auth = 'Basic ' + Buffer.from(`opencode:${password}`).toString('base64')

const call = async (path, init = {}) => {
  const res = await fetch(url + path, {
    ...init,
    headers: { authorization: auth, 'content-type': 'application/json', ...(init.headers ?? {}) },
    signal: AbortSignal.timeout(20_000),
  })
  const text = await res.text()
  if (!res.ok) throw new Error(`${init.method ?? 'GET'} ${path} -> ${res.status}: ${text.slice(0, 400)}`)
  return text ? JSON.parse(text) : {}
}

const main = async () => {
  const info = await call('/api/info')
  console.log(`[hop] container -> ${url} ok (opencode ${info.version}, pid ${info.pid})`)

  const created = await call('/api/session', {
    method: 'POST',
    body: JSON.stringify({ location: { directory }, model: { providerID, id: modelID } }),
  })
  const session = created.data
  console.log(`[session] ${session.id} in ${session.location?.directory ?? directory}`)

  await call(`/api/session/${session.id}/prompt`, {
    method: 'POST',
    body: JSON.stringify({ text: prompt }),
  })
  console.log('[prompt] sent, waiting for the local agent…')

  const started = Date.now()
  let state = {}
  for (;;) {
    if (Date.now() - started > timeoutMs) throw new Error(`timed out after ${timeoutMs}ms`)
    await new Promise(resolve => setTimeout(resolve, 3000))
    state = (await call(`/api/session/${session.id}`)).data
    if (state.outcome) break
    process.stdout.write('.')
  }
  process.stdout.write('\n')

  const messages = (await call(`/api/session/${session.id}/message`)).data ?? []
  for (const message of messages) {
    if (message.type === 'assistant' && message.content) {
      for (const part of message.content) {
        if (part.type === 'text' && part.text) console.log(`[assistant] ${part.text.trim().slice(0, 1200)}`)
      }
      if (message.finish === 'error' && message.error) console.error(`[error] ${JSON.stringify(message.error).slice(0, 600)}`)
    }
    if (message.type === 'idle' && message.error) console.error(`[error] ${JSON.stringify(message.error).slice(0, 600)}`)
  }

  console.log(JSON.stringify({
    session: session.id,
    outcome: state.outcome,
    model: state.model,
    tokens: state.tokens,
    cost: state.cost,
    elapsedMs: Date.now() - started,
  }))
  if (state.outcome !== 'succeeded') process.exitCode = 1
}

main().catch(error => {
  console.error(`[fail] ${error.message}`)
  process.exitCode = 1
})
