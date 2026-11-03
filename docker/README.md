# Docker deployment

Run dsh-superagent as a container: one `dsh --profile superagent` process,
supervised by Docker instead of tmux on the host. The image ships every host
prerequisite from `docs/006-deployment.md` — Node.js 24, pnpm, git, tmux,
python3, the GitHub CLI and `@deepseek-ai/dsh` — plus this bundle built and
linked into a `superagent` profile.

```
docker/
├── Dockerfile                  image: prerequisites + this bundle + seeded DSH_HOME
├── entrypoint.sh               seeds the DSH_HOME volume, starts the port bridge
├── healthcheck.sh              probes the management API on container loopback
├── profile.cordis.patch.yml    machine config: webserver 127.0.0.1:3082, stateDir
├── docker-compose.yml          the local deployment
└── README.md
```

## Why the extra port hop

`dsh-superagent` refuses a non-loopback Web server (its management API trusts
`Host: 127.0.0.1:3082` / `localhost:3082` only), and the harness's own webserver
schema rejects wildcard binds. So the container keeps the harness on
`127.0.0.1:3082` and the entrypoint runs `socat` on `0.0.0.0:3080` forwarding to
it; Compose publishes host `127.0.0.1:3082` to that bridge. The browser's `Host`
header still says `127.0.0.1:3082`, so the same-origin check passes, and the
panel stays reachable only from the host's loopback interface.

## Build and start

From the repository root:

```sh
docker compose -f docker/docker-compose.yml build
docker compose -f docker/docker-compose.yml up -d
docker compose -f docker/docker-compose.yml logs -f
```

The harness prints its entry URL on startup, including the access token:

```sh
docker compose -f docker/docker-compose.yml logs superagent | grep 'dsh web: http'
```

Open that URL (`http://127.0.0.1:3082/?token=...`) on the host. The management
API keeps its Host-header guard; API calls need `x-orchestrator-request: 1`:

```sh
curl -H 'x-orchestrator-request: 1' http://127.0.0.1:3082/orchestrator/api/state
```

First boot copies the seeded profile out of the image into the `dsh-home` volume
(`/home/dsh/.dsh`), so later restarts and image upgrades keep sessions, settings
and credentials.

`docker compose exec` runs as root by default; use `exec -u dsh` whenever a
command writes into the volume (gh logins, git config), or the entrypoint's
ownership repair will lag one restart behind.

The entry URL's token rotates when the harness restarts (including the restart
triggered by saving a Source). Re-read it from the logs after such a restart; a
browser that already opened the URL keeps working through plain restarts, but a
token-less page shows "dsh web authentication required" after a runtime
restart — reopen the printed URL.

## First-boot configuration

Everything that needs a secret is configured through the panel, never baked into
the image:

1. **Model provider** — the panel asks for a provider on first use; sign in with
   a DeepSeek account, or add an API key in Settings → Models. The default model
   is `deepseek-flash`.
2. **GitHub Source** — dsh-superagent wants a dedicated agent GitHub account
   (the agent posts reviews and PRs under that identity). Authenticate the
   in-container `gh` wrapper once, as `dsh`:

   ```sh
   docker compose -f docker/docker-compose.yml exec -u dsh superagent \
     gh-agent auth login --hostname github.com --git-protocol https
   docker compose -f docker/docker-compose.yml exec -u dsh superagent \
     gh-agent auth setup-git --hostname github.com
   ```

   Or reuse a token from a `gh` you already trust:

   ```sh
   gh auth token | docker compose -f docker/docker-compose.yml exec -T -u dsh superagent \
     sh -c 'GH_CONFIG_DIR=/home/dsh/.dsh/gh-agent gh auth login --hostname github.com --git-protocol https --with-token'
   ```

   Credentials land in the `dsh-home` volume under `gh-agent/`, so they survive
   restarts. Then set the repository and the agent CLI
   (`/home/dsh/.local/bin/gh-agent`) under Settings → Sources.
3. **Sources and settings** — the rest (repositories, review policy, model
   settings) is edited in the panel and persisted in the volume.

## Calling a local opencode (POC)

The container can call an opencode server running on the host, which is how the
platform is meant to reach a developer's own machine:

```bash
# host: a writable data dir (the file sandbox denies ~/.local/share/opencode)
export XDG_DATA_HOME=$PWD/.poc/xdg/data XDG_CACHE_HOME=$PWD/.poc/xdg/cache XDG_STATE_HOME=$PWD/.poc/xdg/state
opencode serve --hostname 0.0.0.0 --port 4097 --print-logs   # prints "server password <PW>"

export OPENCODE_PASSWORD=<PW>
docker compose -f docker/docker-compose.yml up -d --build

docker compose -f docker/docker-compose.yml exec -T \
  -e OPENCODE_DIRECTORY=/absolute/host/path/to/repo \
  -e PROMPT='Create POC2.md with one line: hello from cloud. Then commit it.' \
  superagent node /opt/poc/dispatch.mjs
```

Details, API shape, and results: `docker/poc/README.md`. POC only: the server
listens on every interface with one shared password; production needs a
per-runner token over loopback/tunnel.

## Runner hub (M1 POC)

The same image also ships `runner/hub`, the control plane a local
`dsh-runner` connects to. Start it alongside the panel:

```bash
docker compose -f docker/docker-compose.yml up -d hub
curl -s http://127.0.0.1:4096/health
```

The hub listens on `127.0.0.1:4096` and keeps its state under
`/home/dsh/.dsh/hub` in the shared `dsh-home` volume. The runner itself runs on
the developer's machine (`runner/README.md`); it dials out, so no host port is
opened. `HUB_TOKEN` defaults to `dev-token` — set it before sharing the hub.

To make the **panel** dispatch tickets to that runner instead of running worker
Sessions in this container, set `DSH_REMOTE_HUB` on the `superagent` service
and restart it:

```yaml
environment:
  DSH_REMOTE_HUB: http://hub:4096
  DSH_REMOTE_TOKEN: dev-token
  DSH_REMOTE_REPO_DIR: /Users/me/corp-work/superagent/dsh-superagent-prototype
  DSH_REMOTE_BASE: main
  DSH_REMOTE_MODEL: deepseek/deepseek-flash
```

`src/executor/remote.ts` then submits each ticket's assignment to the hub,
polls it, forwards follow-ups as inputs, and reports branch/commit/diffstat
back to the Engine. `runner/README.md` lists every variable and the
`POST /jobs/:id/input` call.

Each runner reports the agent CLIs it found (`opencode`, `claude`, ...) with
their versions; the Execution tab shows them as tags, and 设置 → 执行 → 本地智能体
picks which one a remote job runs. The hub only offers a job to a machine that
can drive the requested agent, so a mis-set choice fails loudly instead of
running a different tool.

## Volumes and lifecycle

| Volume     | Mount point       | Holds                                    |
| ---------- | ----------------- | ---------------------------------------- |
| `dsh-home` | `/home/dsh/.dsh`  | profile, settings, state, gh credentials |
| `dsh-work` | `/home/dsh/work`  | checkouts and other agent work           |

The container runs the harness as the unprivileged user `dsh` (uid/gid 1000);
only the entrypoint starts as root (volume ownership + port bridge). `tini`
reaps children; `restart: unless-stopped` plus the healthcheck restarts the
process if it dies.

```sh
docker compose -f docker/docker-compose.yml ps
docker compose -f docker/docker-compose.yml restart
docker compose -f docker/docker-compose.yml down        # keeps volumes
docker compose -f docker/docker-compose.yml down -v     # deletes profile + credentials
```

## Upgrading

```sh
git pull
docker compose -f docker/docker-compose.yml build --pull
docker compose -f docker/docker-compose.yml up -d
```

The volume is not re-seeded when it already holds a profile, so a bundle upgrade
only needs the rebuilt image. Override `DSH_VERSION` to upgrade the harness
itself:

```sh
docker compose -f docker/docker-compose.yml build --build-arg DSH_VERSION=0.2.1-rc.3
```

## Troubleshooting

- **Panel unreachable** — check the bridge: `docker compose exec superagent
  curl -fsS http://127.0.0.1:3082/ >/dev/null` and `docker compose ps` for the
  published `127.0.0.1:3082->3080/tcp` mapping.
- **`dsh` exits immediately** — the same status codes as `scripts/dsh-superagent`
  apply (1 unconfigured, 2 bad config, 3 not running). Inspect the resolved
  config with `docker compose run --rm superagent dsh --profile superagent
  --dump-config`; startup diagnostics land in the volume under `logs/`.
- **Profile bundle fails to resolve** — the profile links the bundle by absolute
  path; the entrypoint repairs `profiles/superagent/node_modules/dsh-superagent`
  on every boot. If you copied a profile by hand, re-run the boot or recreate
  the volume.
- **TLS / CA problems in agent runs** — install certificates into the base image
  or mount them; nothing beyond Debian's CA bundle is trusted.
- **Apple Silicon / colima** — the image builds for the host architecture; give
  the `colima` VM enough memory (16 GiB recommended) for the harness and the
  agent's TypeScript builds.
