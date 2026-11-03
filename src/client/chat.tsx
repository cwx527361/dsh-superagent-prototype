/** Chat tab: talk to a local agent (opencode) through the runner hub and read its full transcript. */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { usePoll, type SessionPart, type SessionView } from './api.ts'
import { Button, Input } from './controls.tsx'
import { usePanel } from './provider.tsx'
import { Field } from './settings-kit.tsx'
import { When } from './widgets.tsx'
import type { T } from './text.ts'

interface Entry { at: string; kind: 'user' | SessionPart['kind']; text?: string; part?: SessionPart }

/** Merge the person's turns and the agent's parts into one reading-order timeline. */
export function timeline(session: SessionView): Entry[] {
  const entries: Entry[] = [
    ...(session.messages ?? []).filter(message => message.role === 'user')
      .map(message => ({ at: message.at, kind: 'user' as const, text: message.text })),
    ...(session.parts ?? []).map(part => ({ at: part.at ?? '', kind: part.kind, part, text: part.text })),
  ]
  return entries.sort((a, b) => (a.at < b.at ? -1 : a.at > b.at ? 1 : 0))
}

function ToolCard({ part, t }: { part: SessionPart; t: T }) {
  const input = part.input === undefined || part.input === null ? '' : JSON.stringify(part.input)
  return <div className="dt-tool" data-status={part.status ?? 'pending'}>
    <div className="dt-tool-head"><code className="dt-mono">{part.name ?? part.id}</code>
      <span className="dt-hint">{part.status}</span></div>
    {input && <pre className="dt-mono dt-tool-io">{input.slice(0, 2000)}</pre>}
    {part.output ? <pre className="dt-mono dt-tool-io">{part.output.slice(0, 4000)}</pre> : null}
  </div>
}

function EntryView({ entry, t }: { entry: Entry; t: T }) {
  if (entry.kind === 'user') return <div className="dt-msg"><p>{entry.text}</p></div>
  const part = entry.part as SessionPart
  if (part.kind === 'text') return <div className="dt-msg"><p>{part.text}</p></div>
  if (part.kind === 'reasoning') return <details className="dt-msg">
    <summary>{t('chatReasoning')}</summary><p>{part.text}</p>
  </details>
  if (part.kind === 'tool') return <ToolCard part={part} t={t} />
  return <p className="dt-hint">{part.finish} · {part.tokens?.input ?? 0}→{part.tokens?.output ?? 0} tokens</p>
}

export function ChatPage() {
  const { t, call, notify } = usePanel()
  const { data: remote } = usePoll(() => call('GET remote'), [])
  const [agent, setAgent] = useState('opencode')
  const [model, setModel] = useState('')
  const [directory, setDirectory] = useState('')
  const [sessionId, setSessionId] = useState<string | undefined>(() => localStorage.getItem('dsh-superagent/chat-session') ?? undefined)
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [problem, setProblem] = useState<string>()

  const agents = useMemo(() => (remote?.runners ?? []).flatMap(runner => runner.agents ?? [])
    .filter(item => item.driver), [remote])
  const models = useMemo(() => agents.find(item => item.id === agent)?.models ?? [], [agents, agent])

  useEffect(() => {
    if (sessionId) localStorage.setItem('dsh-superagent/chat-session', sessionId)
  }, [sessionId])

  const { data: session, reload } = usePoll(async (): Promise<SessionView | undefined> => {
    if (!sessionId) return undefined
    return call('GET sessions/:id', { id: sessionId })
  }, [sessionId, call], 1500)

  const create = useCallback(async (title: string) => {
    const created = await call('POST sessions', { body: { agent, model, directory, title } })
    setSessionId(created.sessionId)
    localStorage.setItem('dsh-superagent/chat-session', created.sessionId)
    return created.sessionId
  }, [agent, model, directory, call])

  const send = useCallback(async () => {
    if (!draft.trim()) return
    setBusy(true)
    try {
      const id = sessionId ?? await create(draft.slice(0, 60))
      await call('POST sessions/:id/messages', { id, body: { text: draft } })
      setDraft('')
      await reload()
    } catch (error) {
      const text = error instanceof Error ? error.message : String(error)
      setProblem(text)
      notify(text)
    } finally { setBusy(false) }
  }, [draft, sessionId, create, call, reload, notify])

  const entries = session ? timeline(session) : []
  return <div className="dt-column">
    <h2>{t('chatTitle')}</h2>
    <p className="dt-hint">{t('chatHint')}</p>
    <div className="dt-form dt-block">
      <Field label={t('localAgent')}>
        <select className="dt-select" value={agent} onChange={event => { setAgent(event.target.value); setModel('') }}>
          {(agents.length ? agents : [{ id: 'opencode', name: 'opencode' }]).map(item =>
            <option key={item.id} value={item.id}>{item.id}</option>)}
        </select>
      </Field>
      <Field label={t('localAgentModel')}>
        <select className="dt-select" value={model} onChange={event => setModel(event.target.value)}>
          <option value="">{t('localAgentModelEmpty')}</option>
          {models.map(item => <option key={item.id} value={item.id}>{item.name} · {item.id}</option>)}
        </select>
      </Field>
      <Field label={t('chatDirectory')}>
        <Input value={directory} onChange={event => setDirectory(event.target.value)} placeholder={t('chatDirectoryHint')} />
      </Field>
    </div>
    <div className="dt-row">
      <Button disabled={busy} onClick={() => { void create(t('chatNewTitle')).catch(error =>
        notify(error instanceof Error ? error.message : String(error))) }}>{t('chatNew')}</Button>
      {problem && <span className="dt-error">{problem}</span>}
      <span className="dt-hint">{session ? `${session.id} · ${session.status}${session.usage?.cost ? ` · $${session.usage.cost.toFixed(4)}` : ''}` : t('chatEmpty')}</span>
    </div>
    <div className="dt-transcript">
      {entries.map((entry, index) => <EntryView key={`${index}:${entry.at}`} entry={entry} t={t} />)}
    </div>
    <div className="dt-composer">
      <textarea className="dt-input dt-mono" rows={3} value={draft} placeholder={t('chatPlaceholder')}
        onChange={event => setDraft(event.target.value)} />
      <Button disabled={busy || !draft.trim()} onClick={() => void send()}>{t('chatSend')}</Button>
    </div>
    <When at={session?.createdAt} />
  </div>
}
