# dsh-runner

A small local agent that lets the containerized superagent platform run coding
jobs on a developer's own machine — without opening a single inbound port.

M1 scope: login, an outbound WebSocket to the hub, worktree per job, a local
`opencode` session as the execution engine, streamed events, and a final result
(branch + commit + diffstat). This is the first slice of
[`platform-design/local-runner-cli.md`](../platform-design/local-runner-cli.md).

```
┌──────────────────────────────┐         WSS (outbound only)        ┌─────────────────────────┐
│ superagent container         │◀───────────────────────────────────│ dsh-runner (this desk)  │
│  hub :4096  ── job.offer ────┼───────────────────────────────────▶│  worktree per job       │
│  events / result ◀───────────┼────────────────────────────────────│  opencode serve :4097   │
└──────────────────────────────┘                                    └─────────────────────────┘
```

## Quick start (POC)

```sh
# 1. Start the hub (it ships in the same image as the panel)
cd ~/path/to/dsh-superagent-prototype
docker compose -f docker/docker-compose.yml up -d hub
curl -s http://127.0.0.1:4096/health   # {"ok":true,...}

# 2. Install the CLI (pure Python, no .exe/.dmg)
cd runner && uv sync && uv run dsh-runner --help
# or from a published channel:
#   sh install/install.sh https://<bucket>/channels/stable.json

# 3. Log the runner in (dev token; OAuth wiring is the next milestone)
export DSH_RUNNER_HOME="$PWD/../.poc/runner-home"
uv run dsh-runner login --server http://127.0.0.1:4096 --token dev-token

# 4. Point it at the local opencode server and run it
export OPENCODE_PASSWORD="$(grep -o 'server password .*' ../.poc/logs/opencode-serve.log | tail -1 | cut -d' ' -f3)"
uv run dsh-runner doctor
uv run dsh-runner start

# 5. Dispatch a job from another shell
curl -s -X POST http://127.0.0.1:4096/jobs -H 'content-type: application/json' -d '{
  "ticket": "demo-1",
  "repo": {"dir": "/absolute/path/to/a/repo"},
  "base": "main",
  "prompt": "Add NOTES.md with two bullets about this repo and commit it.",
  "model": "deepseek/deepseek-flash"
}'
curl -s http://127.0.0.1:4096/jobs/<jobId> | jq '.status, .result'
```

The runner must be able to reach `opencode serve` (default
`http://127.0.0.1:4097`).

## Install (CLI only)

The runner ships as a Python 3.12 wheel and is installed with
[`uv`](https://docs.astral.sh/uv/):

```sh
uv tool install dsh-runner                    # once published to PyPI
sh runner/install/install.sh <channel.json>   # from our S3 channel (sha256 checked)
```

No `.exe`, `.dmg`, or PyInstaller bundle: it is a normal command-line tool.
`runner/release/release.sh` builds the wheel, its `.sha256`, an optional ed25519
`.sig`, and `channels/stable.json`. `docker/poc/README.md` explains how to start it with
an isolated `XDG_DATA_HOME` and a printed password.

## Commands

| command | purpose |
| --- | --- |
| `login --server URL --token T` | dev/CI registration (`POST /runner/register`) |
| `login --server URL --oauth` | Authorization Code + PKCE via `127.0.0.1` callback |
| `start` | connect, heartbeat, accept offers until interrupted |
| `status` | print config and hit `/health` |
| `doctor` | check git, opencode, the opencode API and the device token |
| `keys generate --out FILE` | ed25519 key pair for release signing |
| `verify --pub K --file F --sig S` | verify an artifact signature |

Env overrides: `DSH_RUNNER_HOME`, `DSH_OPENCODE_URL`,
`DSH_OPENCODE_PASSWORD`, `DSH_RUNNER_WORKSPACE`,
`DSH_RUNNER_MAX_CONCURRENCY`.

## Agents on this machine

On every connect the runner probes a fixed list of agent CLIs
(`dsh_runner/agents.py`: opencode, claude, codex, gemini, aider, cursor-agent),
reads `<binary> --version`, and sends them in `hello.agents`:

```json
{"id": "opencode", "name": "opencode", "path": "/opt/homebrew/bin/opencode",
 "version": "v2.1.2", "driver": "opencode",
 "models": [{"id": "deepseek/deepseek-flash", "name": "DeepSeek V4.1 Flash 1017", "provider": "deepseek"}]}
```

`models` is read from the agent's own configuration (for opencode,
`GET /api/model` on the local server), so the list is exactly what that machine
can already run — no API keys travel to the panel. `executor.agent` and
`executor.agentModel` pick one; an empty model means "the agent's own default".

`driver` names the adapter the runner implements for that tool; `null` means
"installed, but this runner cannot drive it yet". The hub stores the list, its
`/health` returns it per device, and the panel shows those tags on the Execution
tab. `executor.agent` in the panel's settings picks which one a job uses: the
hub only offers the job to a runner that reported that agent with a driver, and
answers `503 no runner online can drive <agent>` otherwise. Today only
`opencode` has a driver; a job asking for anything else fails with
`agent_unsupported` rather than silently running something else.

## Job lifecycle

1. Hub sends `job.offer` with a `JobSpec` (repo dir or clone URL, base, ticket,
   prompt, model, timeout).
2. Runner replies `job.accept`, resolves the base commit, and cuts
   `<repo>/.dsh/worktrees/<jobId>` on `feat/<ticket>` so the user's checkout is
   never touched.
3. Runner drives the requested `spec.agent` (opencode today) in that worktree,
   sends the prompt, and polls every 2s, forwarding assistant text as
   `job.event`.
4. On success it sends `job.result` (branch, commit, `git diff --stat
   <base>...HEAD`) and removes the worktree. On failure the worktree is kept for
   inspection and `job.failed` carries the cause (`workspace`, `timeout`,
   `cancelled`, `agent_failed`, `agent_unsupported`, `opencode_*`, `runner_too_old`).

## Hub API (POC)

| endpoint | notes |
| --- | --- |
| `POST /runner/register` | access token -> long-lived device token |
| `WS /runner/connect` | `Authorization: Bearer <deviceToken>` |
| `POST /jobs` | pick an idle runner, return `{jobId}` |
| `GET /jobs`, `GET /jobs/:id` | status, events, result |
| `POST /jobs/:id/input` | steers a person's follow-up into the running session |
| `POST /jobs/:id/cancel` | sends `job.cancel` |
| `GET /health` | runners + queue counters |

Frames are `{v, id, type, ts, payload}` (see `dsh_runner/protocol.py`). `HUB_TOKEN`
defaults to `dev-token`; set it in the environment for anything shared.

## Release

```sh
VERSION=0.1.0-$(git rev-parse --short HEAD) ./runner/release/release.sh
```

Writes `dist/releases/<version>/dsh_runner-<version>-py3-none-any.whl` and
`dist/channels/stable.json` (layout in the design doc §2). With
`RELEASE_KEY=<ed25519 PEM>` the wheel also gets a `.sig`. `install.sh` resolves
the channel, checks sha256 and runs `uv tool install`.

## Known limits (deliberately deferred)

- No TLS/device pinning yet: the POC talks plain `ws://` on loopback.
- OAuth endpoints return `501`; only the dev token path is implemented.
- `install.sh` checks sha256; the detached ed25519 signature is produced but
  the self-updater (M2) is what will enforce it end to end.
- One job at a time by default; a second offer waits in the hub.
- A steered input (`POST /jobs/:id/input`) is delivered to the opencode session
  on a best-effort basis: a job that already finished answers `accepted: false`.
- No remote cancellation of opencode processes beyond an abort request.

## Remote executor (panel -> runner)

The superagent panel can dispatch a ticket's assignment here instead of running
a worker Session inside the container. Set these on the **superagent** service
and restart it:

| variable | meaning |
| --- | --- |
| `DSH_REMOTE_HUB` | hub base URL, e.g. `http://hub:4096` (switches the executor) |
| `DSH_REMOTE_TOKEN` | `HUB_TOKEN` value |
| `DSH_REMOTE_REPO_DIR` | checkout on the developer machine (or leave empty and set the next) |
| `DSH_REMOTE_REPO_URL` | clone URL the runner clones when no directory exists |
| `DSH_REMOTE_BASE` | base revision (default `main`) |
| `DSH_REMOTE_MODEL` | opencode model (default `deepseek/deepseek-flash`) |

`src/executor/remote.ts` implements the Engine's `Executor`: it submits each
ticket's assignment to the hub, polls the job, forwards people's follow-ups as
inputs, reports the branch/commit/diffstat back, and marks the hub's health.
Concurrency is `settings.executor.maxWorkers`; device choice is the hub's.
