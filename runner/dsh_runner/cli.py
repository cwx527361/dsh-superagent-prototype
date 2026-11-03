"""dsh-runner command line."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import platform
import secrets
import signal
import sys
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from . import drivers
from .client import Runner, logger
from .config import Config, config_path, load_config, load_or_create, save_config, state_dir
from .opencode import Opencode, OpencodeError

try:  # installed wheel: report the published version
    from importlib.metadata import version as _pkg_version

    VERSION = _pkg_version("dsh-runner")
except Exception:  # noqa: BLE001 - running from a checkout
    VERSION = "0.1.0"


def _post(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST")
    req.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return json.loads(res.read() or b"{}")
    except urllib.error.HTTPError as err:
        raise SystemExit(f"{url} -> {err.code}: {err.read().decode(errors='replace').strip()}") from err


def _get(url: str, token: str = "") -> Any:
    req = urllib.request.Request(url, method="GET")
    if token:
        req.add_header("authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=10) as res:
        return json.loads(res.read() or b"{}")


def _arch() -> str:
    machine = platform.machine().lower()
    return {"x86_64": "amd64", "amd64": "amd64", "arm64": "arm64", "aarch64": "arm64"}.get(machine, machine)


# --- commands ---------------------------------------------------------------


def cmd_login(args: argparse.Namespace) -> int:
    cfg = load_or_create()
    if args.server:
        cfg.server = args.server.rstrip("/")
    if not cfg.server:
        raise SystemExit("--server is required the first time")
    if not args.token and not args.oauth:
        raise SystemExit("pass --token for dev/CI, or --oauth for the browser flow")

    if args.token:
        reg = _post(
            cfg.server + "/runner/register",
            {
                "accessToken": args.token,
                "deviceId": cfg.device_id,
                "devicePublicKey": cfg.device_public_key,
                "os": platform.system().lower(),
                "arch": _arch(),
                "version": VERSION,
                "maxConcurrency": cfg.max_concurrency,
            },
        )
    else:
        reg = _oauth(cfg)

    cfg.device_id = reg.get("deviceId") or cfg.device_id
    cfg.device_token = reg.get("deviceToken") or cfg.device_token
    cfg.server = reg.get("server") or cfg.server
    save_config(cfg)
    print(f"logged in: device {cfg.device_id} at {cfg.server}")
    return 0


def _oauth(cfg: Config) -> dict[str, Any]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    import http.server
    import threading

    result: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            from urllib.parse import parse_qs, urlparse

            query = parse_qs(urlparse(self.path).query)
            result["code"] = (query.get("code") or [""])[0]
            self.send_response(200)
            self.send_header("content-type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("dsh-runner: signed in, you can close this tab.".encode())

        def log_message(self, *_args: Any) -> None:
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    redirect = f"http://127.0.0.1:{port}/callback"
    start_url = (
        f"{cfg.server}/runner/oauth/start?redirect_uri={urllib.parse.quote(redirect)}"
        f"&code_challenge={urllib.parse.quote(challenge)}&device_id={urllib.parse.quote(cfg.device_id)}"
    )
    print(f"open this URL to sign in:\n  {start_url}")
    webbrowser.open(start_url)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    thread.join(timeout=300)
    server.server_close()
    if not result.get("code"):
        raise SystemExit("timed out waiting for the OAuth callback")
    return _post(
        cfg.server + "/runner/oauth/exchange",
        {"code": result["code"], "codeVerifier": verifier, "deviceId": cfg.device_id, "redirectUri": redirect},
    )


def cmd_drivers(_args: argparse.Namespace) -> int:
    cfg = load_config()
    problems = drivers.configure(cfg.drivers)
    for problem in problems:
        print(f"[warn] custom driver ignored: {problem}", file=sys.stderr)
    rows = []
    for agent, entry in drivers.all_drivers().items():
        where = "custom" if entry.get("custom") else "built-in"
        rows.append({"id": agent, "where": where, "command": drivers.describe(agent)})
    print(json.dumps(rows, indent=2))
    print(f"\nadd one to {config_path()} under \"drivers\": "
          '{"my-agent": {"command": "my-agent", "args": ["--cwd", "{dir}", "{prompt}"]}}', file=sys.stderr)
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.apply_env()
    if args.server:
        cfg.server = args.server.rstrip("/")
    if args.opencode_url:
        cfg.opencode_url = args.opencode_url
    if not cfg.server or not cfg.device_token:
        raise SystemExit("not logged in: run `dsh-runner login --server <url> --token <token>` first")
    for problem in drivers.configure(cfg.drivers):
        print(f"[warn] custom driver ignored: {problem}", file=sys.stderr)

    async def main() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:  # Windows
                pass
        runner = Runner(cfg, logger())
        await runner.serve(stop)
        print("dsh-runner stopped")

    asyncio.run(main())
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.apply_env()
    print(f"state dir:   {state_dir()}")
    print(f"server:      {cfg.server}")
    token = (cfg.device_token[:4] + "…" + cfg.device_token[-4:]) if len(cfg.device_token) > 8 else "***"
    print(f"device:      {cfg.device_id} (token {token})")
    print(f"workspace:   {cfg.workspace_root}")
    print(f"opencode:    {cfg.opencode_url}")
    if cfg.server and cfg.device_token:
        try:
            health = _get(cfg.server + "/health", cfg.device_token)
            print(f"hub:         200 {json.dumps(health)}")
        except Exception as err:  # noqa: BLE001
            print(f"hub:         unreachable ({err})")
    return 0


def cmd_doctor(_args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.apply_env()
    ok = True
    for binary in ("git", "opencode"):
        path = _which(binary)
        if path:
            print(f"✓ {binary} {path}")
        else:
            print(f"✗ {binary} not on PATH")
            ok = False
    try:
        info = asyncio.run(Opencode(cfg.opencode_url, cfg.opencode_password).info())
        print(f"✓ opencode server {cfg.opencode_url} (v{info.get('version')}, pid {info.get('pid')})")
    except OpencodeError as err:
        print(f"✗ opencode server: {err}")
        ok = False
    if cfg.device_token:
        print(f"✓ device {cfg.device_id}")
    else:
        print("✗ not logged in")
        ok = False
    if not ok:
        raise SystemExit("doctor found problems")
    print("all checks passed")
    return 0


def _which(binary: str) -> str:
    import shutil

    return shutil.which(binary) or ""


def cmd_keys(args: argparse.Namespace) -> int:
    key = Ed25519PrivateKey.generate()
    public = base64.b64encode(key.public_key().public_bytes_raw()).decode()
    private = base64.b64encode(key.private_bytes_raw()).decode()
    Path(args.out).write_text(f"pub:{public}\npriv:{private}\n")
    os.chmod(args.out, 0o600)
    print(f"wrote {args.out} (public key {public})")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    public = Ed25519PublicKey.from_public_bytes(base64.b64decode(args.pub))
    signature = _decode_sig(Path(args.sig).read_text().strip())
    payload = Path(args.file).read_bytes()
    try:
        public.verify(signature, payload)
    except InvalidSignature:
        raise SystemExit("signature mismatch") from None
    print(f"ok: {args.file} verified")
    return 0


def _decode_sig(text: str) -> bytes:
    try:
        raw = base64.b64decode(text, validate=True)
        if len(raw) == 64:
            return raw
    except Exception:  # noqa: BLE001
        pass
    if len(text.encode()) == 64:
        return text.encode()
    raise SystemExit("signature must be 64 base64 bytes")


# --- entrypoint -------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dsh-runner", description="local execution agent for dsh-superagent")
    sub = parser.add_subparsers(dest="command", required=True)

    login = sub.add_parser("login", help="register this device")
    login.add_argument("--server", default="")
    login.add_argument("--token", default="", help="dev/CI access token")
    login.add_argument("--oauth", action="store_true", help="browser OAuth 2.0 PKCE flow")
    login.set_defaults(func=cmd_login)

    start = sub.add_parser("start", help="connect and accept jobs")
    start.add_argument("--server", default="")
    start.add_argument("--opencode-url", default="")
    start.set_defaults(func=cmd_start)

    drivers_cmd = sub.add_parser("drivers", help="list built-in and custom agent drivers")
    drivers_cmd.set_defaults(func=cmd_drivers)

    status = sub.add_parser("status", help="show configuration and hub health")
    status.set_defaults(func=cmd_status)

    doctor = sub.add_parser("doctor", help="check git, opencode and credentials")
    doctor.set_defaults(func=cmd_doctor)

    keys = sub.add_parser("keys", help="release signing keys")
    keys_sub = keys.add_subparsers(dest="keys_command", required=True)
    generate = keys_sub.add_parser("generate")
    generate.add_argument("--out", default="runner-key.txt")
    generate.set_defaults(func=cmd_keys)

    verify = sub.add_parser("verify", help="verify an ed25519 signature")
    verify.add_argument("--pub", required=True)
    verify.add_argument("--file", required=True)
    verify.add_argument("--sig", required=True)
    verify.set_defaults(func=cmd_verify)

    version = sub.add_parser("version", help="print the version")
    version.set_defaults(func=lambda _a: print(f"dsh-runner {VERSION} ({platform.system().lower()}/{_arch()}, python {platform.python_version()})") or 0)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
