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


def logger() -> Log:
    def log(message: str) -> None:
        print(message, file=sys.stderr, flush=True)

    return log
