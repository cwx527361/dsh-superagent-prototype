/** Thin client for the runner hub's live-session API: one opencode session, turn after turn. */
export interface SessionPart {
  key: string
  kind: 'text' | 'reasoning' | 'tool' | 'step'
  at?: string
  text?: string
  name?: string
  id?: string
  status?: string
  input?: unknown
  output?: string
  finish?: string
  cost?: number
  tokens?: { input?: number; output?: number; reasoning?: number }
}

export interface SessionMessage { at: string; role: 'user' | 'assistant'; text: string }
export interface SessionView {
  id: string
  agent: string
  deviceId: string
  title: string
  status: string
  directory: string
  createdAt: string
  messages: SessionMessage[]
  parts: SessionPart[]
  usage?: { cost?: number; tokens?: unknown }
  turn?: { state: string; at: string }
  error?: string
}
export interface SessionsView {
  hub: string
  sessions: { id: string; agent: string; deviceId: string; title: string; status: string; createdAt: string }[]
}

const base = (): string | undefined => process.env.DSH_REMOTE_HUB?.trim() || undefined

async function hub<T>(path: string, init: RequestInit = {}): Promise<T> {
  const url = base()
  if (!url) throw new Error('DSH_REMOTE_HUB is not configured')
  const headers: Record<string, string> = { 'x-orchestrator-request': '1', 'content-type': 'application/json' }
  const token = process.env.DSH_REMOTE_TOKEN?.trim()
  if (token) headers.authorization = `Bearer ${token}`
  const res = await fetch(`${url.replace(/\/$/, '')}${path}`, { ...init, headers })
  const text = await res.text()
  if (!res.ok) throw new Error(`hub ${init.method ?? 'GET'} ${path} failed (${res.status}): ${text.slice(0, 300)}`)
  return (text ? JSON.parse(text) : null) as T
}

/** Every session the hub knows, or null when this deployment has no remote hub. */
export const listSessions = async (): Promise<SessionsView | null> => {
  const url = base()
  if (!url) return null
  const answer = await hub<{ sessions?: SessionsView['sessions'] }>('/sessions')
  return { hub: url, sessions: answer?.sessions ?? [] }
}

export const openHubSession = (body: { agent?: string; directory?: string; model?: string; title?: string }) =>
  hub<{ sessionId: string; deviceId: string; status: string }>('/sessions', { method: 'POST', body: JSON.stringify(body) })

export const getHubSession = (id: string) => hub<SessionView>(`/sessions/${encodeURIComponent(id)}`)

export const sendHubMessage = (id: string, text: string) =>
  hub<{ sessionId: string; status: string }>(`/sessions/${encodeURIComponent(id)}/messages`,
    { method: 'POST', body: JSON.stringify({ text }) })

export const closeHubSession = (id: string) =>
  hub<{ sessionId: string; status: string }>(`/sessions/${encodeURIComponent(id)}`, { method: 'DELETE' })
