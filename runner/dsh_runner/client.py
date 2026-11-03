"""Outbound WebSocket client: one connection to the hub, reconnect forever."""

from __future__ import annotations

import asyncio
import json
import platform
import sys
from typing import Any, Callable

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from . import agents, protocol
from .config import Config
from .job import JobError, run_job
from .opencode import Opencode

Log = Callable[[str], None]


class Runner:
    def __init__(self, cfg: Config, log: Log) -> None:
        self.cfg = cfg
        self.log = log
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.sessions: dict[str, str] = {}
        # Live chat sessions: hub sessionId -> {agentSession, oc, pump}
        self.live: dict[str, dict[str, Any]] = {}
        self.version = "0.1.0"

    @property
    def endpoint(self) -> str:
        base = self.cfg.server.rstrip("/")
        if base.startswith("https://"):
            base = "wss://" + base[len("https://") :]
        elif base.startswith("http://"):
            base = "ws://" + base[len("http://") :]
        return base + "/runner/connect"

    async def serve(self, stop: asyncio.Event) -> None:
        backoff = 1.0
        while not stop.is_set():
            try:
                await self._connect_once(stop)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - reconnect on any failure
                self.log(f"[warn] connection ended: {err}; reconnecting in {backoff:.0f}s")
            if stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    async def _connect_once(self, stop: asyncio.Event) -> None:
        headers = {"Authorization": f"Bearer {self.cfg.device_token}"}
        async with connect(self.endpoint, additional_headers=headers, max_size=16 << 20, open_timeout=15) as ws:
            self.log(f"[connect] {self.endpoint} ({platform.system().lower()}/{platform.machine()}) as {self.cfg.device_id}")
            await ws.send(
                protocol.hello(
                    self.cfg.device_id,
                    self.version,
                    platform.system().lower(),
                    self._arch(),
                    self.cfg.max_concurrency,
                    await agents.detect_async(self.cfg.opencode_url, self.cfg.opencode_password),
                )
            )
            heartbeat = asyncio.create_task(self._heartbeat(ws))
            try:
                async for raw in ws:
                    await self._handle(ws, raw)
            except ConnectionClosed:
                return
            finally:
                heartbeat.cancel()
                for task in list(self.tasks.values()):
                    task.cancel()
                self.tasks.clear()

    def _arch(self) -> str:
        machine = platform.machine().lower()
        return {"x86_64": "amd64", "amd64": "amd64", "arm64": "arm64", "aarch64": "arm64"}.get(machine, machine)

    async def _heartbeat(self, ws: Any) -> None:
        try:
            while True:
                await asyncio.sleep(15)
                await ws.send(protocol.heartbeat(len(self.tasks), self.version))
        except (asyncio.CancelledError, ConnectionClosed):
            return

    async def _handle(self, ws: Any, raw: str | bytes) -> None:
        try:
            env = json.loads(raw)
        except json.JSONDecodeError:
            return
        kind = env.get("type")
        payload = env.get("payload") or {}
        if kind == "welcome":
            self.log(
                f"[welcome] protocol={payload.get('protocol')} lease={payload.get('leaseSeconds')}s"
            )
        elif kind == "job.offer":
            job_id = payload.get("jobId") or ""
            task = asyncio.create_task(self._execute(ws, payload))
            self.tasks[job_id] = task
            task.add_done_callback(lambda _t, j=job_id: self.tasks.pop(j, None))
        elif kind == "job.input":
            job_id = payload.get("jobId") or ""
            text = payload.get("text") or ""
            session_id = self.sessions.get(job_id)
            if session_id and text:
                asyncio.create_task(self._steer(ws, job_id, session_id, text))
        elif kind == "session.open":
            asyncio.create_task(self._session_open(ws, payload))
        elif kind == "session.prompt":
            asyncio.create_task(self._session_prompt(ws, payload))
        elif kind == "session.close":
            asyncio.create_task(self._session_close(payload))
        elif kind == "job.cancel":
            task = self.tasks.get(payload.get("jobId") or "")
            if task:
                self.log(f"[cancel] job {payload.get('jobId')}: {payload.get('reason', '')}")
                task.cancel()
        elif kind == "ping":
            await ws.send(protocol.frame("pong", {}))

    async def _steer(self, ws: Any, job_id: str, session_id: str, text: str) -> None:
        """Forward a person's follow-up into the job's opencode session."""
        oc = Opencode(self.cfg.opencode_url, self.cfg.opencode_password)
        try:
            await oc.prompt(session_id, text)
            await self._send(ws, protocol.job_event(job_id, "input", {"session": session_id}, 0))
            self.log(f"[job {job_id}] input delivered")
        except Exception as err:  # noqa: BLE001 - the hub records the failure as an event
            await self._send(ws, protocol.job_event(job_id, "input_failed", {"error": str(err)}, 0))

    async def _send(self, ws: Any, text: str) -> None:
        try:
            await ws.send(text)
        except ConnectionClosed:
            pass

    async def _execute(self, ws: Any, spec: dict[str, Any]) -> None:
        job_id = spec["jobId"]
        await self._send(ws, protocol.job_accept(job_id))
        self.log(f"[job {job_id}] accepted: {spec.get('ticket')}")

        async def emit(kind: str, data: dict[str, Any]) -> None:
            await self._send(ws, protocol.job_event(job_id, kind, data, 0))

        try:
            result = await run_job(
                spec,
                opencode_url=self.cfg.opencode_url,
                opencode_password=self.cfg.opencode_password,
                on_session=lambda session_id: self.sessions.__setitem__(job_id, session_id),
                emit=emit,
                log=self.log,
            )
        except JobError as err:
            self.log(f"[job {job_id}] failed ({err.cause}): {err.detail}")
            await self._send(ws, protocol.job_failed(job_id, err.cause, err.detail, err.retryable))
            return
        except asyncio.CancelledError:
            self.sessions.pop(job_id, None)
            raise
        self.sessions.pop(job_id, None)
        await self._send(
            ws,
            protocol.job_result(job_id, result["branch"], result["commit"], result["diffstat"], result["summary"]),
        )


    # --- live chat sessions ------------------------------------------------
    #
    # A session is not a job: it stays open, the person sends turn after turn,
    # and every event opencode produces for it travels to the hub. That is what
    # lets the panel show the whole conversation, including tool calls and their
    # output, instead of only a final summary.

    async def _session_open(self, ws: Any, payload: dict[str, Any]) -> None:
        session_id = payload.get("sessionId") or ""
        directory = payload.get("directory") or ""
        model = payload.get("model") or ""
        oc = Opencode(self.cfg.opencode_url, self.cfg.opencode_password)
        try:
            agent_session = (await oc.create_session(directory, model))["id"]
        except Exception as err:  # noqa: BLE001
            await self._send(ws, protocol.frame("session.failed", {"sessionId": session_id, "error": str(err)}))
            return
        self.live[session_id] = {"agentSession": agent_session, "oc": oc, "pump": None}
        pump = asyncio.create_task(self._session_events(ws, session_id, agent_session, oc))
        self.live[session_id]["pump"] = pump
        self.log(f"[session] open {session_id} -> {agent_session} in {directory}")
        await self._send(ws, protocol.frame("session.opened", {
            "sessionId": session_id, "agentSession": agent_session, "directory": directory,
        }))

    async def _session_events(self, ws: Any, session_id: str, agent_session: str, oc: Opencode) -> None:
        try:
            async for event in oc.events():
                data = event.get("data") or {}
                if data.get("sessionID") != agent_session:
                    continue
                await self._send(ws, protocol.frame("session.event", {
                    "sessionId": session_id, "event": {
                        "type": event.get("type"), "data": data, "at": event.get("created"),
                    },
                }))
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - the pump must not kill the runner
            self.log(f"[session {session_id}] event stream ended: {err}")

    async def _session_prompt(self, ws: Any, payload: dict[str, Any]) -> None:
        session_id = payload.get("sessionId") or ""
        state = self.live.get(session_id)
        if state is None:
            await self._send(ws, protocol.frame("session.failed", {
                "sessionId": session_id, "error": "no such session on this runner",
            }))
            return
        try:
            await state["oc"].prompt(state["agentSession"], payload.get("text") or "")
        except Exception as err:  # noqa: BLE001
            await self._send(ws, protocol.frame("session.failed", {"sessionId": session_id, "error": str(err)}))
            return
        await self._send(ws, protocol.frame("session.prompted", {"sessionId": session_id}))

    async def _session_close(self, payload: dict[str, Any]) -> None:
        session_id = payload.get("sessionId") or ""
        state = self.live.pop(session_id, None)
        if state is None:
            return
        pump = state.get("pump")
        if pump:
            pump.cancel()
        try:
            await state["oc"].abort(state["agentSession"])
        except Exception:  # noqa: BLE001 - closing is best effort
            pass
        self.log(f"[session] close {session_id}")


def logger() -> Log:
    def log(message: str) -> None:
        print(message, file=sys.stderr, flush=True)

    return log


