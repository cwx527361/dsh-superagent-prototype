# POC：容器（云端）调用本机 opencode（本地执行）

**已验证**：容器里的调度脚本通过 HTTP 调用**宿主机的 opencode server**，本机 agent 在指定仓库里改文件并 commit，结果回传容器。
这条链路就是"控制面在云端、执行面在员工电脑"的最小可行验证。

```
┌──────────────── 容器 dsh-superagent ────────────────┐        ┌──────── 本机（macOS）────────┐
│ /opt/poc/dispatch.mjs                               │  HTTP  │ opencode serve :4097         │
│  POST /api/session {location, model}                │ ─────▶ │  └─ session → agent 执行      │
│  POST /api/session/:id/prompt {text}                │        │     在 ~/.../demo-repo 里     │
│  GET  /api/session/:id (+ /message)                 │ ◀───── │     改文件、git commit        │
└─────────────────────────────────────────────────────┘        └──────────────────────────────┘
        host.docker.internal:4097  ← compose extra_hosts: host-gateway
```

## 复现步骤

```bash
# 0) 宿主机：opencode 需要一个可写的数据目录（沙箱下 XDG 指到工作区）
cd /Users/chenlei/workspace/cpxx-superagent
export XDG_DATA_HOME=$PWD/.poc/xdg/data XDG_CACHE_HOME=$PWD/.poc/xdg/cache XDG_STATE_HOME=$PWD/.poc/xdg/state

# 1) 宿主机：起 opencode server（会打印 password）
opencode serve --hostname 0.0.0.0 --port 4097 --print-logs
#   记下 "server password <PW>"

# 2) 宿主机：一个测试仓库
mkdir -p .poc/demo-repo && cd .poc/demo-repo && git init -q
printf '# demo\n' > README.md && git add -A && git -c user.name=poc -c user.email=poc@local commit -qm init

# 3) 重建容器（带上 extra_hosts 与 OPENCODE_PASSWORD）
cd dsh-superagent-prototype
export DOCKER_CONFIG=/Users/chenlei/workspace/cpxx-superagent/.docker \
       DOCKER_HOST=unix:///Users/chenlei/.colima/default/docker.sock \
       OPENCODE_PASSWORD=<PW>
docker compose -f docker/docker-compose.yml up -d --build

# 4) 从容器发起一个任务（目录是本机路径，因为 agent 在本机执行）
docker compose -f docker/docker-compose.yml exec -T \
  -e OPENCODE_DIRECTORY=/Users/chenlei/workspace/cpxx-superagent/.poc/demo-repo \
  -e PROMPT='Create POC2.md with one line: hello from cloud. Then commit it.' \
  superagent node /opt/poc/dispatch.mjs

# 5) 本机验收
git -C /Users/chenlei/workspace/cpxx-superagent/.poc/demo-repo log --oneline -3
cat /Users/chenlei/workspace/cpxx-superagent/.poc/demo-repo/POC2.md
```

## 实测结果（2026-11-03）

| 项 | 结果 |
|---|---|
| 容器 → `host.docker.internal:4097/api/info` | 200，opencode 2.1.2 |
| 建 session（`location.directory` + `deepseek/deepseek-flash`） | 200，session 绑定到测试仓库 |
| 发 prompt → agent 执行 | `outcome: succeeded`，6 秒 |
| 产物 | `POC2.md` 写入 + commit `8c6ee5f`，工作区干净 |
| 回传 | 容器拿到 outcome / tokens(968 in, 335 out) / cost / 耗时 |

## opencode HTTP API（POC 用到的部分）

- 认证：`Authorization: Basic base64("opencode:<server password>")`
- `GET /api/info`、`POST /api/session {location:{directory}, model:{providerID,id}}`
- `POST /api/session/:id/prompt {text}`
- `GET /api/session/:id`（`outcome`）、`GET /api/session/:id/message`（transcript）
- 事件流：`GET /api/event`（SSE，后续接超时/进度用）

## 这个 POC 证明了什么 / 没证明什么

证明：

1. **网络可达**：容器经 `host.docker.internal` 访问本机服务，不需要公网、不需要反连。
2. **本机真执行**：opencode 在宿主机仓库里改文件并 commit，不是"云端假装执行"。
3. **结果回传**：outcome/token/成本/transcript 都能拉回容器侧。
4. **模型可用**：复用本机已有的 `deepseek/deepseek-flash` 凭据（opencode 自有 auth），无需在容器里再配一份。

没证明（下一步）：

1. **还没有接进 superagent 的 Executor**：现在是独立脚本模拟派工；要做的是一条 `Executor` 实现（`start/deliver/stop`）→ 调本机 runner。注意原型 `Executor` 与 DSH Session 耦合较深（`src/executor/dsh.ts`），需要先抽一个"远程 worker"抽象，不能直接套。
2. **没有工作区/租约语义**：POC 用固定目录，没有 per-job worktree、10 分钟短 token、心跳、重派。
3. **没有鉴权**：opencode 密码走 compose 环境变量；生产要一次性 runner token + 只监听 loopback/隧道，不能长期 `0.0.0.0`。
4. **没有队列与并发**：一次一个 job。

## 安全注意（POC 限定）

- `opencode serve --hostname 0.0.0.0` 会监听所有网卡，只在本机/受信网络做 POC。
- password 不要写进仓库或聊天记录；用 `export` 传。
- 结束后停掉：`pkill -f "opencode serve"`（或 `opencode service stop`）。
