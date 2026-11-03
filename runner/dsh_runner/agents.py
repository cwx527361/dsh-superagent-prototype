"""Detect the agent CLIs installed on this machine, so the panel can offer them.

A runner executes jobs through one of them. `driver` names the adapter this
runner implements for that agent; `None` means the tool is installed but this
runner cannot drive it yet — the panel still lists it so a person can see what
the machine has.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import shutil
import subprocess
import urllib.request
from typing import Any

CANDIDATES: tuple[tuple[str, str, str | None], ...] = (
    ("opencode", "opencode", "opencode"),
    ("claude", "claude", None),
    ("codex", "codex", None),
    ("gemini", "gemini", None),
    ("aider", "aider", None),
    ("cursor-agent", "cursor-agent", None),
)


def version_of(path: str) -> str:
    """The first line of `<binary> --version`; empty when the probe fails."""
    try:
        done = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    lines = ((done.stdout or "") + "\n" + (done.stderr or "")).strip().splitlines()
    for line in lines:
        match = re.search(r"v?\d+\.\d+[\w.\-+]*", line)
        if match:
            return match.group(0)[:40]
    return ""


def _opencode_models(url: str, password: str) -> list[dict[str, str]]:
    """The models the local opencode server already has credentials for."""
    token = base64.b64encode(f"opencode:{password}".encode()).decode()
    request = urllib.request.Request(url.rstrip("/") + "/api/model", headers={"Authorization": f"Basic {token}"})
    with urllib.request.urlopen(request, timeout=5) as response:
        payload = json.load(response)
    entries = payload.get("data") if isinstance(payload, dict) else payload
    models: list[dict[str, str]] = []
    for entry in entries or []:
        provider, model = entry.get("providerID"), entry.get("modelID")
        if not provider or not model:
            continue
        models.append({"id": f"{provider}/{model}", "name": entry.get("name") or model, "provider": provider})
    return models


async def detect_async(opencode_url: str = "", opencode_password: str = "") -> list[dict[str, Any]]:
    """detect(), plus the models each drivable agent already has configured."""
    found = await asyncio.to_thread(detect)
    for entry in found:
        entry.setdefault("models", [])
        if entry.get("driver") != "opencode" or not opencode_url:
            continue
        try:
            entry["models"] = await asyncio.to_thread(_opencode_models, opencode_url, opencode_password)
        except (OSError, ValueError):
            entry["models"] = []
    return found


def detect() -> list[dict[str, Any]]:
    """Every known agent binary on PATH, with its version and the driver that can run it."""
    found: list[dict[str, Any]] = []
    for agent_id, binary, driver in CANDIDATES:
        path = shutil.which(binary)
        if not path:
            continue
        found.append({"id": agent_id, "name": binary, "path": path, "version": version_of(path), "driver": driver})
    return found
