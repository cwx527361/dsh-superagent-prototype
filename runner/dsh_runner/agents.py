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
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
import urllib.request
from typing import Any

from . import drivers

# (id, binary, driver). driver=None means "detected, but this runner has no adapter yet".
CANDIDATES: tuple[tuple[str, str, str | None], ...] = (
    ("opencode", "opencode", "opencode"),
    ("claude", "claude", None),
    ("codex", "codex", None),
    ("gemini", "gemini", None),
    ("aider", "aider", None),
    ("cursor-agent", "cursor-agent", None),
    ("dsh", "dsh", None),
    ("workbuddy", "workbuddy", None),
    ("codebuddy", "codebuddy", None),
)

# Desktop apps that are agent tools but ship no CLI on PATH: report the bundle and
# its version so the panel can show "this machine has DeepSeek Harness / WorkBuddy".
APP_CANDIDATES: tuple[tuple[str, str, str], ...] = (
    ("deepseek-harness", "DeepSeek Harness", "DeepSeek Harness.app"),
    ("workbuddy", "WorkBuddy", "WorkBuddy.app"),
    ("codebuddy", "CodeBuddy", "CodeBuddy CN.app"),
)
APP_DIRS = ("/Applications", "/Applications/Setapp", "/System/Applications", "~/Applications")

# Extra CLI locations a person may have installed a tool into without it being on
# the runner's PATH (the panel's setting is the source of truth for extras).
EXTRA_DIRS = ("~/.local/bin", "~/bin", "/opt/homebrew/bin", "/usr/local/bin")


def _plist_version(app: Path) -> str:
    plist = app / "Contents/Info.plist"
    if not plist.exists():
        return ""
    try:
        done = subprocess.run(["/usr/bin/plutil", "-extract", "CFBundleShortVersionString", "raw", "-o", "-", str(plist)],
                              capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (done.stdout or "").strip()[:40]


def _locate(binary: str) -> str:
    found = shutil.which(binary)
    if found:
        return found
    for directory in EXTRA_DIRS:
        candidate = Path(directory).expanduser() / binary
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return ""


def _apps() -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if sys.platform != "darwin":
        return found
    for agent_id, name, bundle in APP_CANDIDATES:
        for directory in APP_DIRS:
            app = Path(directory).expanduser() / bundle
            if not app.exists():
                continue
            found.append({"id": agent_id, "name": name, "path": str(app), "kind": "app",
                          "version": _plist_version(app), "driver": None, "models": []})
            break
    return found


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
    for agent_id, binary, _declared in CANDIDATES:
        path = _locate(binary)
        if not path:
            continue
        # drivable iff the runner has a driver entry; detection alone is not enough.
        driver = agent_id if drivers.driver_for(agent_id) else None
        found.append({"id": agent_id, "name": binary, "path": path, "kind": "cli",
                      "version": version_of(path), "driver": driver, "models": []})
    # Custom drivers are explicit: probe their binary and report them as drivable.
    for agent_id, entry in drivers.CUSTOM.items():
        path = _locate(entry["argv"][0])
        if not path:
            continue
        models = [{"id": model, "name": model.split("/", 1)[-1], "provider": model.split("/", 1)[0]}
                  for model in entry.get("models") or []]
        found.append({"id": agent_id, "name": entry["argv"][0], "path": path, "kind": "cli",
                      "version": version_of(path), "driver": agent_id, "models": models})
    found.extend(_apps())
    return found
