"""Runner state: ~/.dsh-runner/config.json (0600) plus the device key."""

from __future__ import annotations

import base64
import json
import os
from typing import Any
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat


def state_dir() -> Path:
    override = os.environ.get("DSH_RUNNER_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".dsh-runner"


def config_path() -> Path:
    return state_dir() / "config.json"


def _home_workdir() -> str:
    return str(Path.home() / "corp-work")


@dataclass
class Config:
    server: str = ""
    device_id: str = ""
    device_token: str = ""
    device_public_key: str = ""
    device_private_key: str = ""
    channel: str = "stable"
    workspace_root: str = field(default_factory=_home_workdir)
    opencode_url: str = "http://127.0.0.1:4097"
    opencode_password: str = ""
    # Custom drivers (id -> {command, args, modelArgs, versionArgs}); see drivers.py.
    drivers: dict[str, Any] = field(default_factory=dict)

    @property
    def max_concurrency(self) -> int:
        try:
            return max(1, int(os.environ.get("DSH_RUNNER_MAX_CONCURRENCY", "1")))
        except ValueError:
            return 1

    def ensure_key(self) -> None:
        """Create the ed25519 device identity once (release signing / future mTLS)."""
        if self.device_public_key and self.device_private_key:
            return
        key = Ed25519PrivateKey.generate()
        self.device_public_key = base64.b64encode(
            key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        ).decode()
        self.device_private_key = base64.b64encode(
            key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
        ).decode()

    def apply_env(self) -> None:
        if value := os.environ.get("DSH_OPENCODE_URL"):
            self.opencode_url = value
        if value := os.environ.get("DSH_OPENCODE_PASSWORD") or os.environ.get("OPENCODE_PASSWORD"):
            self.opencode_password = value
        if value := os.environ.get("DSH_RUNNER_WORKSPACE"):
            self.workspace_root = value


def load_config() -> Config:
    path = config_path()
    if not path.exists():
        cfg = Config()
        cfg.ensure_key()
        return cfg
    raw = json.loads(path.read_text())
    cfg = Config(
        server=raw.get("server", ""),
        device_id=raw.get("deviceId", ""),
        device_token=raw.get("deviceToken", ""),
        device_public_key=raw.get("devicePublicKey", ""),
        device_private_key=raw.get("devicePrivateKey", ""),
        channel=raw.get("channel", "stable"),
        workspace_root=raw.get("workspaceRoot") or _home_workdir(),
        opencode_url=raw.get("opencodeUrl") or "http://127.0.0.1:4097",
        opencode_password=raw.get("opencodePassword", ""),
        drivers=raw.get("drivers") or {},
    )
    cfg.ensure_key()
    return cfg


def save_config(cfg: Config) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    payload = {
        "server": cfg.server,
        "deviceId": cfg.device_id,
        "deviceToken": cfg.device_token,
        "devicePublicKey": cfg.device_public_key,
        "devicePrivateKey": cfg.device_private_key,
        "channel": cfg.channel,
        "workspaceRoot": cfg.workspace_root,
        "opencodeUrl": cfg.opencode_url,
        "opencodePassword": cfg.opencode_password,
        "drivers": cfg.drivers,
    }
    path.write_text(json.dumps(payload, indent=2) + "\n")
    os.chmod(path, 0o600)


def new_device_id() -> str:
    return "dev_" + base64.urlsafe_b64encode(os.urandom(9)).decode().rstrip("=")

def load_or_create() -> Config:
    cfg = load_config()
    if not cfg.device_id:
        cfg.device_id = new_device_id()
    cfg.ensure_key()
    save_config(cfg)
    return cfg
