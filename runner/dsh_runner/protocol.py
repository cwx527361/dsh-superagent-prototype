"""Wire protocol shared with runner/hub/server.mjs.

Frames are ``{"v": 1, "id": <uuid>, "type": <kind>, "ts": <iso8601>, "payload": {...}}``.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

PROTOCOL_VERSION = 1


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def frame(kind: str, payload: dict[str, Any]) -> str:
    return json.dumps(
        {"v": PROTOCOL_VERSION, "id": str(uuid.uuid4()), "type": kind, "ts": now(), "payload": payload},
        separators=(",", ":"),
    )


def envelope(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"v": PROTOCOL_VERSION, "id": str(uuid.uuid4()), "type": kind, "ts": now(), "payload": payload}


def hello(device_id: str, version: str, os_name: str, arch: str, max_concurrency: int) -> str:
    return frame(
        "hello",
        {
            "deviceId": device_id,
            "version": version,
            "os": os_name,
            "arch": arch,
            "capabilities": ["worktree", "cancel", "eventStream"],
            "maxConcurrency": max_concurrency,
        },
    )


def heartbeat(running: int, version: str) -> str:
    return frame("heartbeat", {"runningJobs": running, "version": version})


def job_accept(job_id: str) -> str:
    return frame("job.accept", {"jobId": job_id})


def job_event(job_id: str, kind: str, data: dict[str, Any], seq: int) -> str:
    return frame("job.event", {"jobId": job_id, "seq": seq, "kind": kind, "data": data})


def job_result(job_id: str, branch: str, commit: str, diffstat: str, summary: str) -> str:
    return frame(
        "job.result",
        {"jobId": job_id, "branch": branch, "commit": commit, "diffstat": diffstat, "summary": summary, "exitCode": 0},
    )


def job_failed(job_id: str, cause: str, detail: str, retryable: bool) -> str:
    return frame("job.failed", {"jobId": job_id, "cause": cause, "detail": detail, "retryable": retryable})
