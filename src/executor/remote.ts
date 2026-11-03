/** Remote executor: runs each ticket's assignment on a developer's machine through the runner hub. */
import { latestAssignment, liveTicket, runningAssignment } from '../core/index.ts'
import type { Assignment, AssessmentOutcome, EngineForExecutor, Executor, Ticket } from '../core/index.ts'
import type { Machine } from '../deployment.ts'
import type { HealthEntry } from '../infra/health.ts'
import type { Store } from '../app/store.ts'
import type { Settings } from '../policy.ts'
import { ticketExecution } from './dsh.ts'

/** Where the hub lives and how jobs reach it; see `runner/hub/server.mjs`. */
export interface HubOptions {
  /** Base URL of the hub, such as `http://host.docker.internal:4096`. */
  url: string
  /** Token the hub accepts on `/jobs` (`HUB_TOKEN`). */
  token?: string
  /** Repo directory on the runner machine, or empty to let the job clone `repoUrl`. */
  repoDir?: string
  /** Clone URL used when `repoDir` is empty. */
  repoUrl?: string
  /** Base revision the runner branches from. */
  base?: string
  /** opencode model, such as `deepseek/deepseek-flash`. */
  model?: string
  /** Poll interval while a job runs. */
  pollMs?: number
}
/** What `RemoteExecutor` needs from its host, mirroring the local executor's own dependencies. */
export interface RemoteExecutorHost {
  engine: Pick<EngineForExecutor, 'assignmentStarted' | 'inputsDelivered' | 'inputsRead' | 'turnEnded' | 'deliveryFailed' | 'report'
    | 'assignmentInterrupted'>
  store: Pick<Store, 'tickets' | 'triggers' | 'settings'>
  machine: Pick<Machine, 'stateDir'>
  health: HealthEntry
  lifetime: AbortSignal
  hub: HubOptions
  paused(): boolean
  closing(): boolean
}
/** A hub job as the hub reports it. */
export interface HubJob {
  id: string
  ticket: string
  deviceId: string
  status: 'offered' | 'running' | 'succeeded' | 'failed' | 'cancelled'
  events?: { at: string; kind: string; data?: unknown }[]
  result?: { branch?: string; commit?: string; diffstat?: string; summary?: string } | null
  failure?: { cause?: string; detail?: string; retryable?: boolean } | null
}
/** HTTP client for the runner hub: submit, read, steer and cancel jobs. */
export class HubClient {
  constructor(private readonly options: HubOptions) {}

  /** POSTs a job and returns its id; throws when the hub rejects it (such as when no runner is online). */
  async submit(body: { ticket: string; prompt: string; branch?: string; timeoutMs?: number; agent?: string }): Promise<string> {
    const response = await this.call('/jobs', 'POST', {
      ticket: body.ticket,
      prompt: body.prompt,
      ...body.branch ? { branch: body.branch } : {},
      ...body.timeoutMs ? { timeoutMs: body.timeoutMs } : {},
      ...body.agent ? { agent: body.agent } : {},
      repo: { dir: this.options.repoDir ?? '', gitUrl: this.options.repoUrl ?? '' },
      ...this.options.base ? { base: this.options.base } : {},
      ...this.options.model ? { model: this.options.model } : {},
    })
    const jobId = (response as { jobId?: string }).jobId
    if (!jobId) throw new Error('the hub did not return a job id')
    return jobId
  }
  /** Reads the hub's health: protocol, connected runners and job counters. */
  async health(): Promise<{ ok?: boolean; protocol?: number; runners?: RemoteStatus['runners']; jobs?: { queued?: number; running?: number; done?: number } }> {
    return await this.call('/health', 'GET') as { ok?: boolean; runners?: RemoteStatus['runners'] }
  }
  /** Lists the hub's jobs, newest first. */
  async jobs(): Promise<{ id: string; ticket: string; status: string; deviceId: string }[]> {
    const response = await this.call('/jobs', 'GET') as { jobs?: JobSummary[] } | JobSummary[]
    return Array.isArray(response) ? response : response.jobs ?? []
  }
  /** Reads a job's current state. */
  async job(jobId: string): Promise<HubJob> { return await this.call(`/jobs/${encodeURIComponent(jobId)}`, 'GET') as HubJob }
  /** Steers a running job with a person's message. */
  async input(jobId: string, text: string): Promise<void> {
    await this.call(`/jobs/${encodeURIComponent(jobId)}/input`, 'POST', { text })
  }
  /** Cancels a job; failures are ignored because the job may have finished already. */
  async cancel(jobId: string, reason: string): Promise<void> {
    await this.call(`/jobs/${encodeURIComponent(jobId)}/cancel`, 'POST', { reason }).catch(() => undefined)
  }
  private async call(path: string, method: string, body?: unknown): Promise<unknown> {
    const headers: Record<string, string> = { 'x-orchestrator-request': '1' }
    if (body !== undefined) headers['content-type'] = 'application/json'
    if (this.options.token) headers.authorization = `Bearer ${this.options.token}`
    const response = await fetch(new URL(path, this.options.url), { method, headers, body: body === undefined ? undefined : JSON.stringify(body) })
    const text = await response.text()
    if (!response.ok) throw new Error(`hub ${method} ${path} failed (${response.status}): ${text.slice(0, 300)}`)
    return text ? JSON.parse(text) : {}
  }
}
/** What the panel shows about one ticket's remote job; kept after the job ends so the result stays visible. */
export interface RemoteJobView {
  jobId: string
  state: string
  startedAt: string
  branch?: string
  commit?: string
  diffstat?: string
  cause?: string
  /** The last lines the runner streamed, oldest first. */
  lines: string[]
}
/** The panel's overview of the runner side: the hub, the devices connected to it, and the jobs it holds. */
export interface RemoteStatus {
  hub: string
  healthy: boolean
  detail: string
  runners: { id: string; os: string; arch: string; version: string; running: number; agents: RunnerAgent[] }[]
  jobs: { id: string; ticket: string; status: string; deviceId: string }[]
}
/** One agent CLI a runner reported: detected always, drivable only when it names a driver. */
export interface RunnerAgent { id: string; name: string; path: string; version: string; driver: string | null }
/** One hub job as the hub lists it. */
interface JobSummary { id: string; ticket: string; status: string; deviceId: string }
/** One ticket's remote job. */
interface RunningJob {
  jobId?: string
  abort: AbortController
  /** Resolves once the runner accepted the job, so inputs can be steered into its session. */
  ready: Promise<void>
  markReady: () => void
}
/**
 * Executes assignments on registered developer machines: it submits each ticket's prompt to the runner hub, polls the job,
 * forwards people's follow-up messages as inputs, and reports the result back to the Engine. Device selection and the
 * runner's own concurrency are the hub's concern; this class only caps how many jobs it holds in flight.
 */
export class RemoteExecutor implements Executor {
  private readonly jobs = new Map<string, RunningJob>()
  private readonly views = new Map<string, RemoteJobView>()
  private readonly queue: { ticket: Ticket; assignment: Assignment }[] = []
  private readonly client: HubClient
  private stopping = false
  private healthTimer?: ReturnType<typeof setInterval>
  constructor(private readonly host: RemoteExecutorHost) {
    this.client = new HubClient(host.hub)
  }
  /** Populates a newly accepted ticket's `execution` attributes, as the local executor does. */
  prepare(ticket: Ticket, outcome: Extract<AssessmentOutcome, { kind: 'accept' }>): Ticket {
    return { ...ticket, execution: ticketExecution(ticket, outcome, this.host.store.settings(), this.host.machine.stateDir) }
  }
  /** Reads the hub's health and jobs for the panel's execution view; failures become an unhealthy status, not an error. */
  async status(): Promise<RemoteStatus> {
    const base = this.host.hub.url.replace(/\/+$/, '')
    try {
      const [health, jobs] = await Promise.all([this.client.health(), this.client.jobs()])
      return { hub: base, healthy: Boolean(health.ok), detail: health.ok ? `${health.runners?.length ?? 0} runner(s), ${health.jobs?.running ?? 0} running` : 'hub reports unhealthy',
        runners: health.runners ?? [], jobs: (jobs ?? []).filter(job => job.status === 'offered' || job.status === 'running') }
    } catch (error: unknown) {
      return { hub: base, healthy: false, detail: String(error).slice(0, 200), runners: [], jobs: [] }
    }
  }
  /** The panel's view of a ticket's remote job, if it has one. */
  jobView(ticketId: string): RemoteJobView | undefined { return this.views.get(ticketId) }
  start(ticket: Ticket, assignment: Assignment): void {
    if (this.stopping || this.host.closing()) return
    this.queue.push({ ticket, assignment })
    this.pump()
  }
  /** Steers a person's follow-up into the ticket's running remote session. */
  deliver(ticket: Ticket, assignment: Assignment): void {
    const text = this.undelivered(ticket).map(input => input.text).join('\n\n').trim()
    if (!text) return
    const running = this.jobs.get(ticket.id)
    if (!running?.jobId) {
      this.host.engine.deliveryFailed(ticket.id, assignment.id, 'the ticket has no running remote job')
      return
    }
    const jobId = running.jobId
    void (async () => {
      await running.ready
      try {
        await this.client.input(jobId, text)
        const ids = this.undelivered(ticket).map(input => input.id)
        this.host.engine.inputsDelivered(ticket.id, assignment.id, ids)
        this.host.engine.inputsRead(ticket.id, ids)
      } catch (error: unknown) {
        this.host.engine.deliveryFailed(ticket.id, assignment.id, String(error))
      }
    })()
  }
  /** Cancels the ticket's remote job. */
  stop(ticket: Ticket, _assignment: Assignment, _reason: string): void {
    const running = this.jobs.get(ticket.id)
    if (!running) return
    running.abort.abort()
    this.jobs.delete(ticket.id)
    if (running.jobId) void this.client.cancel(running.jobId, 'ticket stopped')
  }
  /** Throws when the hub is unreachable, so the host can report worker health. */
  async ping(): Promise<void> {
    const response = await fetch(new URL('/health', this.host.hub.url))
    if (!response.ok) throw new Error(`hub health check failed (${response.status})`)
  }
  /** Starts the hub reachability check that feeds the panel's worker health entry. */
  install(): void {
    const check = (): void => {
      void this.ping().then(
        () => { this.host.health.set({ state: 'ready', detail: `runner hub ${this.host.hub.url}`, at: new Date().toISOString() }) },
        (error: unknown) => { this.host.health.set({ state: 'error', detail: String(error), at: new Date().toISOString() }) })
    }
    check()
    this.healthTimer = setInterval(check, 30_000)
    this.healthTimer.unref?.()
  }
  /** Reconciles open tickets after a host restart: interrupted assignments are re-queued, pending ones launched. */
  resume(): void {
    for (const ticket of this.host.store.tickets.open().sort((a, b) => a.createdAt.localeCompare(b.createdAt))) {
      const running = runningAssignment(ticket)
      if (running) this.host.engine.assignmentInterrupted(ticket.id, running.id)
      const latest = latestAssignment(ticket)
      if (latest?.state === 'pending') this.start(ticket, latest)
    }
  }
  /** Stops accepting work and cancels every in-flight job. */
  async shutdown(): Promise<void> {
    this.stopping = true
    for (const [ticketId, running] of this.jobs) {
      running.abort.abort()
      if (running.jobId) await this.client.cancel(running.jobId, 'host shutting down')
      this.jobs.delete(ticketId)
    }
  }
  /** Stops accepting work; in-flight jobs were cancelled by `shutdown`. */
  dispose(): void {
    this.stopping = true
    if (this.healthTimer) clearInterval(this.healthTimer)
  }
  /** How many jobs this executor has in flight, for the panel's load line. */
  activeCount(): number { return this.jobs.size }
  /** Starts queued jobs while worker slots are free. */
  private pump(): void {
    if (this.stopping || this.host.closing()) return
    const limit = this.host.store.settings().executor.maxWorkers
    while (this.jobs.size < limit && this.queue.length) {
      const next = this.queue.shift()!
      if (!liveTicket(next.ticket)) continue
      void this.run(next.ticket, next.assignment)
    }
  }
  /** Submits one assignment, polls it to a terminal state, and reports back to the Engine. */
  private async run(ticket: Ticket, assignment: Assignment): Promise<void> {
    if (this.host.paused()) { this.queue.unshift({ ticket, assignment }); return }
    const abort = new AbortController()
    const onAbort = (): void => abort.abort()
    this.host.lifetime.addEventListener('abort', onAbort, { once: true })
    let markReady: () => void = () => {}
    const ready = new Promise<void>(resolve => { markReady = resolve })
    this.jobs.set(ticket.id, { abort, ready, markReady })
    const job = this.jobs.get(ticket.id)!
    try {
      this.host.engine.assignmentStarted(ticket.id, assignment.id)
      const agent = this.host.store.settings().executor.agent
      job.jobId = await this.client.submit({ ticket: ticket.id, prompt: this.prompt(ticket), branch: `feat/${ticket.id}`,
        ...agent ? { agent } : {} })
      this.views.set(ticket.id, { jobId: job.jobId, state: 'offered', startedAt: new Date().toISOString(), lines: [] })
      job.markReady()
      const terminal = await this.wait(job.jobId, abort.signal, ticket.id)
      if (abort.signal.aborted) return
      if (terminal.status === 'succeeded') {
        const result = terminal.result ?? {}
        const summary = [result.branch ? `branch ${result.branch}` : '', result.commit ? `commit ${result.commit}` : '',
          result.diffstat ?? '', result.summary ?? ''].filter(Boolean).join('\n')
        this.host.engine.report(ticket.id, { state: 'ready', summary: summary || 'the remote job succeeded' })
      }
      else if (terminal.status === 'failed') {
        this.host.engine.report(ticket.id, { state: 'failed', summary: `${terminal.failure?.cause ?? 'job failed'}: ${terminal.failure?.detail ?? ''}` })
      }
      this.observe(ticket.id, terminal)
      this.host.engine.turnEnded(ticket.id, assignment.id)
    } catch (error: unknown) {
      if (!abort.signal.aborted) this.host.engine.report(ticket.id, { state: 'failed', summary: `remote executor: ${String(error)}` })
    } finally {
      this.jobs.delete(ticket.id)
      this.host.lifetime.removeEventListener('abort', onAbort)
      this.pump()
    }
  }
  /** Merges a polled job into its view: the state, its result, and the new lines the runner streamed. */
  private observe(ticketId: string, job: HubJob): void {
    const view = this.views.get(ticketId)
    if (!view) return
    view.state = job.status
    if (job.result?.branch) view.branch = job.result.branch
    if (job.result?.commit) view.commit = job.result.commit
    if (job.result?.diffstat) view.diffstat = job.result.diffstat
    if (job.failure?.cause) view.cause = job.failure.cause
    const seen = new Set(view.lines)
    for (const event of job.events ?? []) {
      const text = typeof event.data === 'string' ? event.data : (event.data as { text?: string } | undefined)?.text
      if (event.kind !== 'log' || !text || seen.has(text)) continue
      seen.add(text)
      view.lines.push(text.trim().split('\n').slice(0, 3).join(' ').slice(0, 200))
    }
    view.lines = view.lines.slice(-40)
  }
  /** Polls a job until it is terminal or `signal` aborts. */
  private async wait(jobId: string, signal: AbortSignal, ticketId: string): Promise<HubJob> {
    const interval = this.host.hub.pollMs ?? 2000
    for (;;) {
      if (signal.aborted) throw new Error('cancelled')
      const job = await this.client.job(jobId)
      this.observe(ticketId, job)
      if (job.status === 'succeeded' || job.status === 'failed' || job.status === 'cancelled') return job
      await new Promise<void>(resolve => {
        const timer = setTimeout(resolve, interval)
        signal.addEventListener('abort', () => { clearTimeout(timer); resolve() }, { once: true })
      })
    }
  }
  /** The prompt handed to the runner: the assignment text, then the request that started the ticket. */
  private prompt(ticket: Ticket): string {
    const parts = [ticket.execution?.assignment?.trim() ?? '']
    const origin = this.host.store.triggers.get(ticket.origin.triggerId)
    if (origin?.text) parts.push(`Request:\n${origin.text}`)
    return parts.filter(Boolean).join('\n\n') || `Work on ticket ${ticket.id} (${ticket.title}).`
  }
  /** The ticket's undelivered human inputs, with the text of their Triggers. */
  private undelivered(ticket: Ticket): { id: string; text: string }[] {
    return ticket.inputs.flatMap(input => {
      if (input.kind !== 'trigger' || !ticket.undelivered.includes(input.id)) return []
      const trigger = this.host.store.triggers.get(input.triggerId)
      return trigger?.text ? [{ id: input.id, text: trigger.text }] : []
    })
  }
}
