"""One offered job: worktree -> opencode session -> events -> result."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import drivers, expert
from .opencode import Opencode, OpencodeError

Emit = Callable[[str, dict[str, Any]], Awaitable[None]]
Log = Callable[[str], None]

JOB_TIMEOUT = 30 * 60.0
POLL_SECONDS = 2.0


class JobError(RuntimeError):
    def __init__(self, cause: str, detail: str, retryable: bool = True) -> None:
        super().__init__(detail)
        self.cause = cause
        self.detail = detail
        self.retryable = retryable


async def git(*args: str, cwd: str | None = None) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=cwd or None, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode(errors="replace").strip()


def expand(path: str) -> str:
    return os.path.expanduser(path) if path else ""


async def prepare_worktree(spec: dict[str, Any]) -> tuple[str, str]:
    repo = expand((spec.get("repo") or {}).get("dir", ""))
    if not repo:
        raise JobError("workspace", "job has no repo.dir")
    if not Path(repo, ".git").exists():
        git_url = (spec.get("repo") or {}).get("gitUrl", "")
        if not git_url:
            raise JobError("workspace", f"{repo} is not a git repository and no gitUrl was given")
        rc, out = await git("clone", git_url, repo)
        if rc != 0:
            raise JobError("workspace", f"git clone: {out}")
    rc, out = await git("fetch", "--all", "--prune", cwd=repo)
    if rc != 0:
        print(f"[warn] git fetch: {out}", file=sys.stderr)

    worktree = (spec.get("worktree") or {}).get("dir") or str(Path(repo, ".dsh", "worktrees", spec["jobId"]))
    Path(worktree).parent.mkdir(parents=True, exist_ok=True)
    branch = spec.get("branch") or f"feat/{spec.get('ticket') or spec['jobId']}"
    base = spec.get("base") or "HEAD"

    rc, out = await git("rev-parse", base, cwd=repo)
    if rc != 0:
        raise JobError("workspace", f"git rev-parse {base}: {out}")
    base_sha = out

    rc, out = await git("worktree", "add", "-B", branch, worktree, base_sha, cwd=repo)
    if rc != 0:
        raise JobError("workspace", f"git worktree add: {out}")
    if spec.get("gitUser"):
        await git("config", "user.name", spec["gitUser"], cwd=worktree)
    if spec.get("gitEmail"):
        await git("config", "user.email", spec["gitEmail"], cwd=worktree)
    return worktree, base_sha


async def cleanup_worktree(spec: dict[str, Any], worktree: str, keep: bool, log: Log) -> None:
    if keep:
        log(f"[job {spec['jobId']}] kept worktree for inspection: {worktree}")
        return
    repo = expand((spec.get("repo") or {}).get("dir", ""))
    rc, out = await git("worktree", "remove", "--force", worktree, cwd=repo)
    if rc != 0:
        log(f"[warn] worktree remove: {out}")


def _expert_id(spec: dict[str, Any]) -> str | None:
    profile = spec.get("expert")
    return profile.get("id") if isinstance(profile, dict) else None


async def git_result(worktree: str, base_sha: str) -> tuple[str, str, str]:
    _, branch = await git("rev-parse", "--abbrev-ref", "HEAD", cwd=worktree)
    _, commit = await git("rev-parse", "HEAD", cwd=worktree)
    _, diffstat = await git("diff", "--stat", f"{base_sha}...HEAD", cwd=worktree)
    return branch, commit, diffstat


async def _transcript_text(oc: Opencode, session_id: str, seen: int, emit: Emit) -> tuple[int, bool]:
    """Forward new assistant text parts; returns (messages_seen, emitted_text)."""
    try:
        messages = await oc.messages(session_id)
    except OpencodeError:
        return seen, False
    emitted = False
    for message in messages[seen:]:
        for part in message.get("content") or []:
            if part.get("type") == "text" and (part.get("text") or "").strip():
                await emit("log", {"role": message.get("type"), "text": part["text"].strip()})
                emitted = True
    return len(messages), emitted


async def run_job(
    spec: dict[str, Any],
    *,
    opencode_url: str,
    opencode_password: str,
    emit: Emit,
    log: Log,
    on_session: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Execute one job; raises JobError on failure (caller frames job.failed)."""
    job_id = spec["jobId"]
    agent = spec.get("agent") or "opencode"
    driver = drivers.driver_for(agent)
    if driver is None:
        raise JobError("agent_unsupported", f"this runner cannot drive {agent} yet", retryable=False)
    if driver["mode"] == "cli":
        return await cli_job(spec, agent, emit, log)
    timeout = min(float(spec.get("timeoutMs") or JOB_TIMEOUT) / 1000.0, JOB_TIMEOUT)
    oc = Opencode(opencode_url, opencode_password)
    session_id = ""

    try:
        async with asyncio.timeout(timeout):
            await emit("status", {"phase": "workspace"})
            worktree, base_sha = await prepare_worktree(spec)
            carrier = expert.materialize(worktree, spec)
            log(f"[job {job_id}] worktree {worktree}" + (f" (expert {carrier})" if carrier else ""))
            await emit("status", {"phase": "running", "worktree": worktree})

            try:
                await oc.info()
            except OpencodeError as err:
                raise JobError("opencode_unreachable", str(err)) from err
            try:
                session = await oc.create_session(worktree, spec.get("model") or "")
                session_id = session["id"]
                if on_session:
                    on_session(session_id)
                await oc.prompt(session_id, spec.get("prompt") or "")
            except (OpencodeError, KeyError) as err:
                raise JobError("opencode_session", str(err)) from err
            await emit("status", {"phase": "prompt", "session": session_id})

            seen = 0
            emitted_text = False
            state: dict[str, Any] = {}
            while True:
                await asyncio.sleep(POLL_SECONDS)
                seen, got = await _transcript_text(oc, session_id, seen, emit)
                emitted_text = emitted_text or got
                try:
                    state = await oc.session(session_id)
                except OpencodeError as err:
                    raise JobError("opencode_session_lost", str(err)) from err
                if state.get("outcome"):
                    break

            if state.get("outcome") != "succeeded":
                detail = state.get("outcome") or "failed"
                try:
                    messages = await oc.messages(session_id)
                except OpencodeError:
                    messages = []
                for message in reversed(messages):
                    error = message.get("error")
                    if error:
                        detail = f"{error.get('type')}: {error.get('message')}"
                        break
                raise JobError("agent_failed", detail)

            if not emitted_text:
                await _transcript_text(oc, session_id, 0, emit)
            branch, commit, diffstat = await git_result(worktree, base_sha)
            await cleanup_worktree(spec, worktree, keep=False, log=log)
            log(f"[job {job_id}] done: {branch} {commit}")
            await emit("status", {"phase": "done", "commit": commit})
            return {
                "expert": _expert_id(spec),
                "branch": branch,
                "commit": commit,
                "diffstat": diffstat,
                "summary": state.get("title") or "",
            }
    except TimeoutError as err:
        await oc.abort(session_id)
        raise JobError("timeout", f"job exceeded {timeout:.0f}s") from err
    except asyncio.CancelledError:
        await oc.abort(session_id)
        raise JobError("cancelled", "job cancelled", retryable=False) from None
    except JobError:
        raise
    except Exception as err:  # noqa: BLE001 - one bad job must not kill the runner
        raise JobError("internal", str(err)) from err


async def _commit(worktree: str, message: str) -> None:
    """Commit whatever a CLI agent left behind, so every agent yields a commit."""
    _code, out = await git("status", "--porcelain", cwd=worktree)
    if not out.strip():
        return
    await git("add", "-A", cwd=worktree)
    await git("-c", "user.name=dsh-runner", "-c", "user.email=runner@dsh.local", "commit", "-q", "-m", message, cwd=worktree)


async def cli_job(spec: dict[str, Any], agent: str, emit: Emit, log: Log) -> dict[str, Any]:
    """Run a one-shot CLI agent in the worktree and turn its output into events."""
    job_id = spec["jobId"]
    timeout = min(float(spec.get("timeoutMs") or JOB_TIMEOUT) / 1000.0, JOB_TIMEOUT)
    try:
        async with asyncio.timeout(timeout):
            await emit("status", {"phase": "workspace"})
            worktree, base_sha = await prepare_worktree(spec)
            expert.materialize(worktree, spec)
            log(f"[job {job_id}] worktree {worktree} via {agent}")
            await emit("status", {"phase": "running", "worktree": worktree, "agent": agent})

            argv = drivers.command(agent, spec.get("agentPath") or "", worktree,
                                   spec.get("prompt") or "", spec.get("model") or "")
            log(f"[job {job_id}] {' '.join(argv[:2])}")
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=worktree, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            tail: list[str] = []

            async def pump(stream: Any, is_error: bool) -> None:
                while True:
                    raw = await stream.readline()
                    if not raw:
                        return
                    line = raw.decode(errors="replace").rstrip()
                    if not line:
                        continue
                    tail.append(line)
                    del tail[:-40]
                    if not is_error:
                        await emit("log", {"role": "assistant", "text": line})

            await asyncio.gather(pump(proc.stdout, False), pump(proc.stderr, True))
            code = await proc.wait()
            if code != 0:
                raise JobError("agent_failed", f"{agent} exited {code}: " + " | ".join(tail[-3:]))

            await _commit(worktree, f"feat({spec.get('ticket') or job_id}): work by {agent}")
            branch, commit, diffstat = await git_result(worktree, base_sha)
            await cleanup_worktree(spec, worktree, keep=False, log=log)
            log(f"[job {job_id}] done: {branch} {commit}")
            await emit("status", {"phase": "done", "commit": commit})
            return {"expert": _expert_id(spec), "branch": branch, "commit": commit, "diffstat": diffstat,
                    "summary": tail[-1] if tail else f"{agent} finished"}
    except TimeoutError as err:
        raise JobError("timeout", f"job exceeded {timeout:.0f}s") from err
    except asyncio.CancelledError:
        raise JobError("cancelled", "job cancelled", retryable=False) from None
    except JobError:
        raise
    except Exception as err:  # noqa: BLE001 - one bad job must not kill the runner
        raise JobError("internal", str(err)) from err
