#!/usr/bin/env python3
"""Ensure a usable SSH key exists (and is authorized) for an OT-2.

This helper is meant for workflows that need SSH access to the OT-2 (e.g.
copying calibration files). It can generate a keypair and attempt to install
the public key into the OT-2 user's authorized_keys.

Notes:
- You do NOT need a unique SSH key per OT-2. A single key can be authorized on
  multiple robots. This helper defaults to a per-robot key to keep access
  scoped, but you can use --scope shared to reuse one key across robots.
- OT-2 robot-server exposes an HTTP endpoint to add SSH keys when connected
  locally. This script prefers that flow so it does not require password auth.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict
from urllib import error as url_error
from urllib import request as url_request


def _eprint(*args: object) -> None:
    print(*args, file=sys.stderr, flush=True)


def _run(cmd: list[str], *, check: bool = True, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        cmd,
        check=False,
        text=True,
        input=input_text,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip() or f"exit code {proc.returncode}"
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\n{detail}")
    return proc


def _slug(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return value or "ot2"


def _health(host: str, port: int, api_version: str, timeout_seconds: float) -> Dict[str, Any]:
    url = f"http://{host}:{port}/health"
    req = url_request.Request(url, headers={"opentrons-version": api_version})
    try:
        with url_request.urlopen(req, timeout=timeout_seconds) as resp:
            raw = resp.read().decode("utf-8")
    except (url_error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Unable to reach robot-server at {host}:{port} (/health).") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid /health response from {host}:{port}.") from exc


def _resolve_host(repo_dir: Path, host: str, port: int, api_version: str) -> str:
    if host.strip():
        return host.strip()
    resolver = repo_dir / "ot2_resolve_host.py"
    if not resolver.is_file():
        raise RuntimeError(f"--host not provided and resolver not found: {resolver}")
    proc = _run(
        [
            sys.executable,
            str(resolver),
            "--port",
            str(port),
            "--api-version",
            str(api_version),
        ],
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip() or f"exit code {proc.returncode}"
        raise RuntimeError(f"Failed to resolve OT-2 host.\n{detail}")
    resolved = (proc.stdout or "").strip()
    if not resolved:
        raise RuntimeError("Host resolver returned empty host.")
    return resolved


def _default_key_dir() -> Path:
    base = Path(os.getenv("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / "opentrons-tools" / "ssh"


def _key_paths(key_dir: Path, key_name: str) -> tuple[Path, Path]:
    private_key = key_dir / key_name
    public_key = key_dir / f"{key_name}.pub"
    return private_key, public_key


def _ensure_keypair(private_key: Path, public_key: Path, comment: str) -> None:
    private_key.parent.mkdir(parents=True, exist_ok=True)
    try:
        private_key.parent.chmod(0o700)
    except Exception:
        pass

    if private_key.exists() and public_key.exists():
        return
    if private_key.exists() != public_key.exists():
        raise RuntimeError(f"Keypair is incomplete; delete and retry: {private_key} / {public_key}")

    # OT-2 robot-server's SSH key installation endpoint accepts RSA public keys
    # (ssh-rsa). Ed25519 keys are rejected by the robot-server validation.
    _run(
        ["ssh-keygen", "-t", "rsa", "-b", "4096", "-f", str(private_key), "-N", "", "-C", comment],
        check=True,
    )
    try:
        private_key.chmod(0o600)
        public_key.chmod(0o644)
    except Exception:
        pass


def _ssh_base(host: str, user: str, port: int) -> list[str]:
    return [
        "ssh",
        "-p",
        str(port),
        "-o",
        "StrictHostKeyChecking=accept-new",
        f"{user}@{host}",
    ]


def _can_auth_with_key(host: str, user: str, port: int, private_key: Path) -> bool:
    cmd = _ssh_base(host, user, port)
    cmd[1:1] = ["-i", str(private_key)]
    cmd[1:1] = ["-o", "BatchMode=yes"]
    proc = _run(cmd + ["true"], check=False)
    return proc.returncode == 0


def _install_pubkey_via_http(host: str, api_port: int, api_version: str, public_key: Path) -> None:
    pub = public_key.read_text(encoding="utf-8").strip()
    if not pub:
        raise RuntimeError(f"Public key is empty: {public_key}")

    url = f"http://{host}:{api_port}/server/ssh_keys"
    body = json.dumps({"key": pub}).encode("utf-8")
    req = url_request.Request(
        url,
        method="POST",
        headers={"opentrons-version": str(api_version), "Content-Type": "application/json"},
        data=body,
    )
    try:
        with url_request.urlopen(req, timeout=5.0) as resp:
            resp.read()
    except url_error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Failed to install public key via robot-server at {url}.\n{detail}") from exc
    except (url_error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Failed to reach robot-server SSH key endpoint at {url}.") from exc


def main() -> None:
    repo_dir = Path(__file__).resolve().parent

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="", help="OT-2 host/IP (auto-discovered if omitted)")
    parser.add_argument("--api-port", type=int, default=31950, help="robot-server port (default: 31950)")
    parser.add_argument("--api-version", default="2", help="opentrons-version header value (default: 2)")
    parser.add_argument("--health-timeout", type=float, default=2.0, help="HTTP /health probe timeout seconds")

    parser.add_argument("--ssh-user", default="root")
    parser.add_argument("--ssh-port", type=int, default=22)
    parser.add_argument("--key-dir", default="", help="Directory to store generated SSH keys")
    parser.add_argument(
        "--scope",
        choices=("per-robot", "shared"),
        default="per-robot",
        help="Key scope: per-robot (default) or shared across robots",
    )
    parser.add_argument(
        "--ensure-authorized",
        action="store_true",
        help="Attempt to install the public key into authorized_keys if key auth fails",
    )
    args = parser.parse_args()

    host = _resolve_host(repo_dir, args.host, args.api_port, str(args.api_version))
    health = _health(host, args.api_port, str(args.api_version), float(args.health_timeout))
    robot_name = str(health.get("name") or "opentrons")

    key_dir = Path(args.key_dir).expanduser() if args.key_dir else _default_key_dir()
    if args.scope == "shared":
        key_name = "ot2_shared_rsa"
    else:
        key_name = f"ot2_{_slug(robot_name)}_rsa"

    private_key, public_key = _key_paths(key_dir, key_name)
    _ensure_keypair(private_key, public_key, comment=f"ot2:{robot_name}")

    if _can_auth_with_key(host, args.ssh_user, args.ssh_port, private_key):
        print(str(private_key), flush=True)
        return

    if args.ensure_authorized:
        _install_pubkey_via_http(host, args.api_port, str(args.api_version), public_key)
        if not _can_auth_with_key(host, args.ssh_user, args.ssh_port, private_key):
            raise RuntimeError("SSH key was installed but key authentication still fails.")
        print(str(private_key), flush=True)
        return

    raise RuntimeError(
        "SSH key exists but is not authorized on the robot. Re-run with --ensure-authorized "
        "to install it via robot-server, or provide --ssh-key to a working key."
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        _eprint(f"[ERROR] {exc}")
        raise SystemExit(2)
