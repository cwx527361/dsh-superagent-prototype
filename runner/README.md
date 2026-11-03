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

## Job lifecycle

1. Hub sends `job.offer` with a `JobSpec` (repo dir or clone URL, base, ticket,
   prompt, model, timeout).
2. Runner replies `job.accept`, resolves the base commit, and cuts
   `<repo>/.dsh/worktrees/<jobId>` on `feat/<ticket>` so the user's checkout is
   never touched.
3. Runner creates an opencode session pinned to that worktree + model, sends the
   prompt, and polls every 2s, forwarding assistant text as `job.event`.
4. On success it sends `job.result` (branch, commit, `git diff --stat
   <base>...HEAD`) and removes the worktree. On failure the worktree is kept for
   inspection and `job.failed` carries the cause (`workspace`, `timeout`,
   `cancelled`, `agent_failed`, `opencode_*`, `runner_too_old`).

## Hub API (POC)

| endpoint | notes |
| --- | --- |
| `POST /runner/register` | access token -> long-lived device token |
| `WS /runner/connect` | `Authorization: Bearer <deviceToken>` |
| `POST /jobs` | pick an idle runner, return `{jobId}` |
| `GET /jobs`, `GET /jobs/:id` | status, events, result |
| `POST /jobs/:id/cancel` | sends `job.cancel` |
| `GET /health` | runners + queue counters |

Frames are `{v, id, type, ts, payload}` (see `protocol.go`). `HUB_TOKEN`
defaults to `dev-token`; set it in the environment for anything shared.

## Release

```sh
VERSION=0.1.0-$(git rev-parse --short HEAD) ./runner/release/release.sh
```

Writes `dist/releases/<version>/...` and `dist/channels/stable.json` (layout in
the design doc §2). With `RELEASE_KEY=<ed25519 PEM>` each tarball also gets a
`.sig`. `install.sh` resolves the channel, checks sha256 and installs to
`~/.local/bin`.

## Known limits (deliberately deferred)

- No TLS/device pinning yet: the POC talks plain `ws://` on loopback.
- OAuth endpoints return `501`; only the dev token path is implemented.
- First install verifies sha256 but not yet the detached signature; the
  self-updater (M2) verifies `sha256 + ed25519` before swapping binaries.
- One job at a time by default; a second offer waits in the hub.
- No remote cancellation of opencode processes beyond an abort request.
