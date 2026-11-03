"""Detect the agent CLIs installed on this machine, so the panel can offer them.

A runner executes jobs through one of them. `driver` names the adapter this
runner implements for that agent; `None` means the tool is installed but this
runner cannot drive it yet — the panel still lists it so a person can see what
the machine has.
"""

from __future__ import annotations

import re
import shutil
import subprocess
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


def detect() -> list[dict[str, Any]]:
    """Every known agent binary on PATH, with its version and the driver that can run it."""
    found: list[dict[str, Any]] = []
    for agent_id, binary, driver in CANDIDATES:
        path = shutil.which(binary)
        if not path:
            continue
        found.append({"id": agent_id, "name": binary, "path": path, "version": version_of(path), "driver": driver})
    return found
