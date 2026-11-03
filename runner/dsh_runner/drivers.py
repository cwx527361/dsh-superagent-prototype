"""How this runner drives each agent tool.

A driver is data, not code: picking a tool in the panel needs no new branch in
the runner — the entry below says how to start the tool non-interactively and
how to feed it a prompt. Adding a tool means adding an entry here (and a probe
in `agents.py`); the job pipeline is shared.

Two modes are supported:

* ``http``  — an already-running local server (opencode today): create a
  session, send the prompt, poll for output.
* ``cli``   — a one-shot command in the worktree; its stdout is streamed as
  job events. Changes it leaves behind are committed by the runner, so every
  agent produces the same result shape (branch + commit + diffstat).
"""

from __future__ import annotations

import shlex
from typing import Any

DRIVERS: dict[str, dict[str, Any]] = {
    "opencode": {"mode": "http"},
    "codex": {
        "mode": "cli",
        "argv": ["codex", "exec", "--cd", "{dir}", "--skip-git-repo-check", "{prompt}"],
        # codex takes model overrides as `-c model="..."`.
        "model": ["-c", 'model="{model}"'],
        "version": ["--version"],
    },
    "claude": {
        "mode": "cli",
        "argv": ["claude", "-p", "{prompt}"],
        "model": ["--model", "{model}"],
        "version": ["--version"],
    },
    "gemini": {
        "mode": "cli",
        "argv": ["gemini", "-p", "{prompt}"],
        "model": ["-m", "{model}"],
        "version": ["--version"],
    },
    "dsh": {
        "mode": "cli",
        "argv": ["dsh", "-p", "{prompt}"],
        "version": ["--version"],
    },
}


def driver_for(agent: str) -> dict[str, Any] | None:
    """The driver entry for an agent id, or None when this runner cannot drive it."""
    return DRIVERS.get(agent)


def command(agent: str, path: str, directory: str, prompt: str, model: str = "") -> list[str]:
    """Build the argv for one CLI job: `path` wins over the bare command name."""
    entry = DRIVERS[agent]
    argv = [path or entry["argv"][0], *entry["argv"][1:]]
    if model and entry.get("model"):
        argv += [part.replace("{model}", model) for part in entry["model"]]
    return [part.replace("{dir}", directory).replace("{prompt}", prompt) for part in argv]


def describe(agent: str) -> str:
    entry = DRIVERS.get(agent)
    if not entry:
        return "no driver"
    return entry["mode"] if entry["mode"] == "http" else " ".join(shlex.quote(part) for part in entry["argv"])
