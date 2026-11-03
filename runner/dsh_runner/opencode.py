"""Thin client for a local ``opencode serve`` HTTP API (v2.x).

Only the five calls the runner needs: info, create session, prompt, poll a
session, read its transcript (and a best-effort abort).
"""

from __future__ import annotations

import asyncio
import base64
import json
import urllib.error
import urllib.request
from typing import Any


class OpencodeError(RuntimeError):
    pass


class Opencode:
    def __init__(self, base: str, password: str = "", timeout: float = 30.0) -> None:
        self.base = base.rstrip("/")
        self.password = password
        self.timeout = timeout

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("content-type", "application/json")
        if self.password:
            token = base64.b64encode(f"opencode:{self.password}".encode()).decode()
            req.add_header("authorization", "Basic " + token)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as res:
                raw = res.read()
        except urllib.error.HTTPError as err:
            detail = err.read().decode(errors="replace")
            raise OpencodeError(f"{method} {path} -> {err.code}: {detail.strip()}") from err
        except urllib.error.URLError as err:
            raise OpencodeError(f"{method} {path} -> {err.reason}") from err
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw.decode(errors="replace")

    async def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        return await asyncio.to_thread(self._request, method, path, body)

    async def info(self) -> dict[str, Any]:
        return await self._call("GET", "/api/info") or {}

    async def create_session(self, directory: str, model: str) -> dict[str, Any]:
        body: dict[str, Any] = {"location": {"directory": directory}}
        if model and "/" in model:
            provider, model_id = model.split("/", 1)
            if provider and model_id:
                body["model"] = {"providerID": provider, "id": model_id}
        out = await self._call("POST", "/api/session", body) or {}
        return out.get("data", out)

    async def prompt(self, session_id: str, text: str) -> None:
        await self._call("POST", f"/api/session/{session_id}/prompt", {"text": text})

    async def session(self, session_id: str) -> dict[str, Any]:
        out = await self._call("GET", f"/api/session/{session_id}") or {}
        return out.get("data", out)

    async def messages(self, session_id: str) -> list[dict[str, Any]]:
        out = await self._call("GET", f"/api/session/{session_id}/message") or {}
        return out.get("data", []) if isinstance(out, dict) else out

    async def abort(self, session_id: str) -> None:
        try:
            await self._call("POST", f"/api/session/{session_id}/abort", {})
        except OpencodeError:
            pass
