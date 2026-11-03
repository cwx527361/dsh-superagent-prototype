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
