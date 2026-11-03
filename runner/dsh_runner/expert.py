"""Materialize an expert profile inside the job's worktree.

The platform decides who does a task (an expert with a role, skills and tools); the
worker only has to apply that definition. Every agent CLI we drive reads AGENTS.md
from the working directory, so writing it per worktree is the portable carrier: no
global config, no cross-project leakage, and the profile stays visible in the diff.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

# Files that already carry project instructions; the expert definition goes next to
# them instead of overwriting what the repository wrote.
RESERVED = ("AGENTS.md",)


def _bullets(items: Any) -> list[str]:
    if not isinstance(items, list):
        return []
    return [f"- {item}" for item in items if isinstance(item, str) and item.strip()]


def render(profile: dict[str, Any]) -> str:
    """The expert definition as Markdown instructions for the worker's agent."""
    name = profile.get("name") or profile.get("id") or "expert"
    role = profile.get("role") or ""
    lines = [f"# 专家：{name}" + (f"（{role}）" if role else ""), ""]
    instructions = profile.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        lines += [instructions.strip(), ""]
    skills = _bullets(profile.get("skills"))
    if skills:
        lines += ["## 技能（按此顺序使用）", *skills, ""]
    tools = _bullets(profile.get("tools"))
    if tools:
        lines += ["## 可用工具", *tools, ""]
    guardrails = _bullets(profile.get("guardrails"))
    if guardrails:
        lines += ["## 约束", *guardrails, ""]
    lines += [
        "## 交付要求",
        "- 改动只发生在当前工作区内，提交前运行项目自带的检查命令。",
        "- 完成后用一段话说明改了什么、为什么、如何验证。",
        "",
    ]
    return "\n".join(lines)


def materialize(worktree: str, spec: dict[str, Any]) -> str | None:
    """Write the profile into the worktree; return the file written, if any."""
    profile = spec.get("expert")
    if not isinstance(profile, dict) or not profile:
        return None
    root = Path(worktree)
    text = render(profile)
    # Never clobber instructions the repository already ships; keep ours alongside.
    target = "AGENTS.md"
    if (root / target).exists():
        target = "AGENTS.dsh-expert.md"
        text = ("> 本项目已有 AGENTS.md；下面这位专家的定义需要一并遵守。\n\n" + text)
    (root / target).write_text(text, encoding="utf-8")
    # A machine-readable copy lets the platform audit exactly which profile version ran.
    meta = {
        "id": profile.get("id"),
        "name": profile.get("name"),
        "role": profile.get("role"),
        "version": profile.get("version"),
        "skills": profile.get("skills"),
        "tools": profile.get("tools"),
    }
    state = root / ".dsh"
    state.mkdir(exist_ok=True)
    (state / "expert.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    _exclude(root, (target, ".dsh/"))
    return target


def _exclude(root: Path, entries: tuple[str, ...]) -> None:
    """Keep the carrier out of the diff: it is context for the agent, not a change."""
    try:
        found = subprocess.run(["git", "-C", str(root), "rev-parse", "--git-path", "info/exclude"],
                               capture_output=True, text=True, timeout=10)
        if found.returncode != 0:
            return
        path = Path(found.stdout.strip())
        if not path.is_absolute():
            path = root / path
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        missing = [entry for entry in entries if entry not in existing.splitlines()]
        if missing:
            path.write_text(existing + ("" if existing.endswith("\n") or not existing else "\n") + "\n".join(missing) + "\n", encoding="utf-8")
    except Exception:
        return
