/**
 * An `llm` adapter that answers from a live opencode session on a local runner.
 *
 * The harness keeps its own loop, session log, and rendering: this adapter is
 * only the model boundary. A turn forwards the newest user text to the hub,
 * follows the session's parts, and streams them back as ordinary reasoning and
 * text chunks, so the chat looks like any other session while the actual work
 * (tools, edits, permissions) happens on the person's laptop.
 */
import type { Context } from '@deepseek-ai/cordis'
import { LlmAdapter, type GenerateOptions, type LlmModelInfo, type LlmProviderInfo, type LlmResolvedModelInfo, type StreamChunk } from '@deepseek-ai/dsh-llm'

/** Provider route registered by this adapter; the chat's model selector reads its models. */
export const OPENCODE_PROVIDER = 'opencode'

export interface OpencodeHubOptions {
  readonly url: string
  readonly token?: string
  readonly agent?: string
  readonly directory?: string
  /** Polling interval for the live session, in milliseconds. */
  readonly pollMs?: number
}

interface Part {
  key: string
  kind: 'text' | 'reasoning' | 'tool' | 'step'
  text?: string
  name?: string
  status?: string
  input?: string
  output?: string
}

interface SessionView {
  sessionId: string
  status: 'opening' | 'ready' | 'failed' | 'closed'
  turn?: number
  parts?: Part[]
  error?: string | null
}

const sleep = (ms: number, signal?: AbortSignal): Promise<void> =>
  new Promise((resolve, reject) => {
    const timer = setTimeout(resolve, ms)
    signal?.addEventListener('abort', () => { clearTimeout(timer); reject(new Error('aborted')) }, { once: true })
  })

/**
 * Context the harness prepends to every turn (its own runtime facts, timing
 * samples). It is not something the person said, so it never becomes the prompt.
 */
const INJECTED = /^(Current runtime context\.|Time sampled while preparing turn|\[Compressed conversation section\])/

/** The newest user turn's text; earlier history already lives in the opencode session. */
function latestUserText(options: GenerateOptions): string {
  const messages = options.messages as readonly { role?: string; content?: unknown }[]
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index]
    if (message?.role !== 'user') continue
    const content = message.content
    if (typeof content === 'string') return content
    if (!Array.isArray(content)) continue
    const text = content
      .filter((block): block is { type: 'text'; text: string } => (block as { type?: string })?.type === 'text' && typeof (block as { text?: string }).text === 'string')
      .map(block => block.text)
      .join('\n')
    if (text && !INJECTED.test(text.trim())) return text
  }
  return ''
}

/** One model boundary backed by a hub session per harness session. */
export class OpencodeAdapter extends LlmAdapter {
  private readonly options: Required<Pick<OpencodeHubOptions, 'url' | 'agent' | 'pollMs'>> & OpencodeHubOptions
  private readonly sessions = new Map<string, string>()

  constructor(options: OpencodeHubOptions) {
    super()
    this.options = {
      ...options,
      url: options.url.replace(/\/+$/, ''),
      agent: options.agent ?? 'opencode',
      pollMs: options.pollMs ?? 250,
    }
  }

  providerInfo(provider: string): LlmProviderInfo {
    return { id: provider, name: '本地智能体 (runner)' }
  }

  async listModels(provider: string): Promise<readonly LlmModelInfo[]> {
    const agents = await this.agents()
    const models = agents.flatMap(agent => agent.models ?? [])
    if (models.length === 0) return [{ provider, id: 'default', name: '智能体默认模型' }]
    return models.map(model => ({ provider, id: model.id, name: model.name ?? model.id }))
  }

  async resolveModel(provider: string, model: string): Promise<LlmResolvedModelInfo> {
    const models = await this.listModels(provider).catch(() => [])
    const known = models.find(entry => entry.id === model)
    return known ?? { provider, id: model, name: model }
  }

  async *stream(options: GenerateOptions): AsyncGenerator<StreamChunk> {
    const text = latestUserText(options)
    if (!text) {
      yield { type: 'finish', reason: { kind: 'stop' } }
      return
    }
    const sessionId = await this.session(options.sessionId ? String(options.sessionId) : undefined, options.model)
    const before = await this.get(sessionId)
    let index = 0
    let open: { kind: 'text' | 'reasoning'; index: number; text: string } | undefined
    const seen = new Map<string, number>()
    const finishedTools = new Set<string>()

    const closeOpen = (): StreamChunk[] => {
      if (!open) return []
      const block = open.kind === 'text' ? { type: 'text' as const, text: open.text } : { type: 'reasoning' as const, text: open.text }
      const chunk: StreamChunk = { type: 'block-end', index: open.index, block }
      open = undefined
      return [chunk]
    }

    const push = (kind: 'text' | 'reasoning', delta: string): StreamChunk[] => {
      const chunks: StreamChunk[] = []
      if (open && open.kind !== kind) chunks.push(...closeOpen())
      if (!open) {
        open = { kind, index, text: '' }
        index += 1
        chunks.push({ type: 'block-start', index: open.index, blockType: kind })
      }
      open.text += delta
      chunks.push({ type: kind === 'text' ? 'text-delta' : 'reasoning-delta', index: open.index, text: delta })
      return chunks
    }

    try {
      await this.post(`/sessions/${sessionId}/messages`, { text })
      const started = before.turn ?? 0
      const deadline = Date.now() + 15 * 60_000
      for (;;) {
        if (options.signal?.aborted) {
          yield* closeOpen()
          yield { type: 'finish', reason: { kind: 'aborted', failure: { code: 'aborted', message: 'aborted' } } }
          return
        }
        if (Date.now() > deadline) {
          yield* closeOpen()
          yield { type: 'finish', reason: { kind: 'error', failure: { code: 'timeout', message: 'the local agent did not finish in time' } } }
          return
        }
        let view: SessionView
        try {
          view = await this.get(sessionId)
        } catch {
          await sleep(this.options.pollMs, options.signal)
          continue
        }
        for (const part of view.parts ?? []) {
          if (part.kind === 'text' || part.kind === 'reasoning') {
            const current = part.text ?? ''
            const previous = seen.get(part.key) ?? 0
            if (current.length > previous) {
              seen.set(part.key, current.length)
              for (const chunk of push(part.kind, current.slice(previous))) yield chunk
            }
            continue
          }
          if (part.kind === 'tool') {
            if (!part.status || part.status === 'pending' || finishedTools.has(part.key)) continue
            finishedTools.add(part.key)
            const output = (part.output ?? '').trim().split('\n').slice(0, 3).join(' ')
            const line = `\n▸ ${part.name ?? 'tool'} · ${part.status}${output ? ` — ${output.slice(0, 200)}` : ''}\n`
            for (const chunk of push('text', line)) yield chunk
          }
        }
        const done = view.status === 'ready' && (view.turn ?? 0) > started
        if (done || view.status === 'failed' || view.status === 'closed') {
          for (const chunk of closeOpen()) yield chunk
          if (view.status === 'failed') {
            yield { type: 'finish', reason: { kind: 'error', failure: { code: 'runner_failed', message: view.error ?? 'the local agent failed' } } }
          } else {
            yield { type: 'finish', reason: { kind: 'stop' } }
          }
          return
        }
        await sleep(this.options.pollMs, options.signal)
      }
    } catch (error) {
      for (const chunk of closeOpen()) yield chunk
      yield { type: 'finish', reason: { kind: 'error', failure: { code: 'runner_unreachable', message: error instanceof Error ? error.message : String(error) } } }
    }
  }

  private async agents(): Promise<{ id: string; models?: { id: string; name?: string }[] }[]> {
    const health = await this.request('/health') as { runners?: { agents?: { id: string; driver?: string | null; models?: { id: string; name?: string }[] }[] }[] }
    return (health.runners ?? []).flatMap(runner => (runner.agents ?? []).filter(agent => agent.driver))
  }

  private async session(key: string | undefined, model: string): Promise<string> {
    if (key) {
      const existing = this.sessions.get(key)
      if (existing) return existing
    }
    const created = await this.post('/sessions', {
      agent: this.options.agent,
      ...(model && model !== 'default' ? { model } : {}),
      ...(this.options.directory ? { directory: this.options.directory } : {}),
    }) as { sessionId: string }
    if (key) this.sessions.set(key, created.sessionId)
    return created.sessionId
  }

  private async get(sessionId: string): Promise<SessionView> {
    return await this.request(`/sessions/${sessionId}`) as SessionView
  }

  private async request(path: string): Promise<unknown> {
    const response = await fetch(`${this.options.url}${path}`, {
      headers: this.options.token ? { authorization: `Bearer ${this.options.token}` } : {},
    })
    if (!response.ok) throw new Error(`hub GET ${path} failed (${response.status})`)
    return await response.json()
  }

  private async post(path: string, body: unknown): Promise<unknown> {
    const response = await fetch(`${this.options.url}${path}`, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        ...(this.options.token ? { authorization: `Bearer ${this.options.token}` } : {}),
      },
      body: JSON.stringify(body),
    })
    if (!response.ok) throw new Error(`hub POST ${path} failed (${response.status}): ${(await response.text()).slice(0, 300)}`)
    return await response.json()
  }
}

/** Register the adapter with the harness when this deployment talks to a runner hub. */
export function registerOpencodeLlm(ctx: Context, options: OpencodeHubOptions): () => void {
  return ctx.llm.registerAdapter([OPENCODE_PROVIDER], new OpencodeAdapter(options))
}
