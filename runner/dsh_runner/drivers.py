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

# Filled from ~/.dsh-runner/config.json (`drivers`): one entry per tool the
# person added themselves, same shape as the built-ins below:
#   {"my-agent": {"command": "my-agent", "args": ["--cwd", "{dir}", "{prompt}"],
#                 "modelArgs": ["--model", "{model}"], "versionArgs": ["--version"]}}
CUSTOM: dict[str, dict[str, Any]] = {}

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


def _normalize(agent: str, entry: dict[str, Any]) -> dict[str, Any]:
    command = entry.get("command")
    if isinstance(command, str):
        command = [command]
    if not isinstance(command, list) or not command:
        raise ValueError(f"driver {agent}: command must be a string or a non-empty list")
    args = entry.get("args")
    if not isinstance(args, list):
        raise ValueError(f"driver {agent}: args must be a list")
    model = entry.get("modelArgs") or []
    if not isinstance(model, list):
        raise ValueError(f"driver {agent}: modelArgs must be a list")
    models = entry.get("models") or []
    if not isinstance(models, list):
        raise ValueError(f"driver {agent}: models must be a list")
    version = entry.get("versionArgs")
    return {"mode": "cli", "argv": [str(part) for part in command], "args": [str(part) for part in args],
            "model": [str(part) for part in model],
            "version": [str(part) for part in version] if isinstance(version, list) else ["--version"],
            "models": [str(model) for model in models],
            "custom": True}


def configure(custom: dict[str, Any] | None) -> list[str]:
    """Install the person's own drivers; returns one complaint per bad entry."""
    CUSTOM.clear()
    problems: list[str] = []
    for agent, entry in (custom or {}).items():
        try:
            if not isinstance(entry, dict):
                raise ValueError("entry must be an object")
            CUSTOM[agent] = _normalize(agent, entry)
        except ValueError as err:
            problems.append(str(err))
    return problems


def driver_for(agent: str) -> dict[str, Any] | None:
    """The driver entry for an agent id, or None when this runner cannot drive it."""
    return CUSTOM.get(agent) or DRIVERS.get(agent)


def all_drivers() -> dict[str, dict[str, Any]]:
    """Built-in + custom entries, for `dsh-runner drivers` and the doctors."""
    return {**DRIVERS, **CUSTOM}


def command(agent: str, path: str, directory: str, prompt: str, model: str = "") -> list[str]:
    """Build the argv for one CLI job: `path` wins over the bare command name."""
    entry = driver_for(agent)
    assert entry is not None
    template = [*entry["argv"], *entry.get("args", [])]
    argv = [path or template[0], *template[1:]]
    if model and entry.get("model"):
        argv += [part.replace("{model}", model) for part in entry["model"]]
    return [part.replace("{dir}", directory).replace("{prompt}", prompt) for part in argv]


def describe(agent: str) -> str:
    entry = driver_for(agent)
    if not entry:
        return "no driver"
    return entry["mode"] if entry["mode"] == "http" else " ".join(shlex.quote(part) for part in entry["argv"])
