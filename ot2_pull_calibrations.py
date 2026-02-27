#!/usr/bin/env python3
"""Pull OT-2 calibration data (API snapshots + on-disk calibration files) from a connected robot.

This downloads:
- API JSON snapshots:
  - /health
  - /instruments
  - /calibration/pipette_offset
  - /calibration/tip_length
  - /calibration/status
  - /labware/calibrations
- On-disk calibration directories via SSH (tar.gz streams):
  - robot_calibration_dir (deck + pipette calibrations)
  - tip_length_calibration_dir

Output defaults to: ./offsets/pulled/<robot>_<timestamp>/
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List
from urllib import error as url_error
from urllib import request as url_request


def _eprint(*args: object) -> None:
    print(*args, file=sys.stderr, flush=True)


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _slug(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return value or "ot2"


def _run(cmd: List[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(cmd, check=False, text=True, capture_output=True)
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip() or f"exit code {proc.returncode}"
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\n{detail}")
    return proc


def _http_json(host: str, api_port: int, api_version: str, path: str, timeout: float = 10.0) -> Dict[str, Any]:
    url = f"http://{host}:{api_port}{path}"
    req = url_request.Request(url, headers={"opentrons-version": str(api_version)})
    with url_request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _wait_health(host: str, api_port: int, api_version: str, timeout_seconds: float = 60.0) -> Dict[str, Any]:
    start = time.time()
    last: str | None = None
    while True:
        if time.time() - start > timeout_seconds:
            raise RuntimeError(f"Timed out waiting for robot-server /health.\n{last or ''}".strip())
        try:
            return _http_json(host, api_port, api_version, "/health", timeout=3.0)
        except url_error.HTTPError as exc:
            last = f"HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')[:200]}"
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(2.0)


def _resolve_host(repo_dir: Path, host: str, api_port: int, api_version: str) -> str:
    if host.strip():
        return host.strip()
    resolver = repo_dir / "ot2_resolve_host.py"
    if not resolver.is_file():
        raise RuntimeError(f"--host not provided and resolver not found: {resolver}")
    proc = _run(
        [sys.executable, str(resolver), "--port", str(api_port), "--api-version", str(api_version)],
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip() or f"exit code {proc.returncode}"
        raise RuntimeError(f"Failed to resolve OT-2 host.\n{detail}")
    resolved = (proc.stdout or "").strip()
    if not resolved:
        raise RuntimeError("Host resolver returned empty host.")
    return resolved


def _ssh_base(host: str, user: str, port: int, ssh_key: str) -> List[str]:
    base = ["ssh", "-p", str(port), "-o", "StrictHostKeyChecking=accept-new"]
    if ssh_key:
        base += ["-i", ssh_key]
    base += [f"{user}@{host}"]
    return base


def _default_key_dir() -> Path:
    base = Path(os.getenv("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / "opentrons-tools" / "ssh"


def _ensure_ssh_key(repo_dir: Path, host: str, api_port: int, api_version: str, ssh_user: str, ssh_port: int) -> str:
    helper = repo_dir / "ot2_ensure_ssh_key.py"
    if not helper.is_file():
        raise RuntimeError(f"SSH key helper not found: {helper}")
    proc = _run(
        [
            sys.executable,
            str(helper),
            "--host",
            host,
            "--api-port",
            str(api_port),
            "--api-version",
            str(api_version),
            "--ssh-user",
            str(ssh_user),
            "--ssh-port",
            str(ssh_port),
            "--ensure-authorized",
        ],
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip() or f"exit code {proc.returncode}"
        raise RuntimeError(f"Failed to ensure SSH key.\n{detail}")
    key_path = (proc.stdout or "").strip()
    if not key_path:
        raise RuntimeError("SSH key helper returned empty key path.")
    return key_path


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _stream_to_file(proc: subprocess.Popen[bytes], dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    assert proc.stdout is not None
    with dst.open("wb") as f:
        for chunk in iter(lambda: proc.stdout.read(1024 * 128), b""):
            f.write(chunk)
    rc = proc.wait()
    if rc != 0:
        raise RuntimeError(f"Remote stream command failed (exit {rc}): {dst}")


def _remote_python_expr(ssh_cmd: List[str], expr: str) -> str:
    remote_cmd = "python -c " + sh_quote(expr)
    proc = subprocess.run(ssh_cmd + [remote_cmd], check=False, text=True, capture_output=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip() or f"exit code {proc.returncode}"
        raise RuntimeError(f"Remote python failed.\n{detail}")
    return (proc.stdout or "").strip()


def _pull_tar_gz(ssh_cmd: List[str], remote_path: str, out_file: Path) -> None:
    # remote_path can be a directory or a file.
    remote_cmd = f"set -euo pipefail; tar -C {sh_quote(remote_path)} -czf - ."
    proc = subprocess.Popen(ssh_cmd + [remote_cmd], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        _stream_to_file(proc, out_file)
    finally:
        if proc.stderr is not None:
            err = proc.stderr.read().decode("utf-8", errors="replace").strip()
            if err:
                # best-effort: helpful context in case tar produced warnings
                _eprint(err)


def sh_quote(value: str) -> str:
    # minimal POSIX shell quoting
    return "'" + value.replace("'", "'\"'\"'") + "'"


def main() -> None:
    repo_dir = Path(__file__).resolve().parent

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="", help="OT-2 host/IP (auto-discovered if omitted)")
    parser.add_argument("--api-port", type=int, default=31950)
    parser.add_argument("--api-version", default="2")
    parser.add_argument("--ssh-user", default="root")
    parser.add_argument("--ssh-port", type=int, default=22)
    parser.add_argument("--ssh-key", default="", help="SSH private key path (auto-set up if omitted)")
    parser.add_argument("--out-dir", default="", help="Output folder (default: ./offsets/pulled/<robot>_<timestamp>)")
    parser.add_argument("--api-only", action="store_true", help="Only save API JSON; skip SSH file pulls")
    args = parser.parse_args()

    host = _resolve_host(repo_dir, args.host, args.api_port, str(args.api_version))
    health = _wait_health(host, args.api_port, str(args.api_version), timeout_seconds=60.0)
    robot_name = str(health.get("name") or "opentrons")

    out_dir = Path(args.out_dir).expanduser() if args.out_dir else (repo_dir / "offsets" / "pulled" / f"{_slug(robot_name)}_{_utc_stamp()}")
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Robot: {robot_name} ({host})")
    print(f"Saving to: {out_dir}")

    # API snapshots
    _write_json(out_dir / "health.json", health)
    for name, path in [
        ("instruments.json", "/instruments"),
        ("calibration_pipette_offset.json", "/calibration/pipette_offset"),
        ("calibration_tip_length.json", "/calibration/tip_length"),
        ("calibration_status.json", "/calibration/status"),
        ("labware_calibrations.json", "/labware/calibrations"),
    ]:
        _write_json(out_dir / name, _http_json(host, args.api_port, str(args.api_version), path, timeout=20.0))

    if args.api_only:
        print("Done (API only).")
        return

    if not args.ssh_key:
        args.ssh_key = _ensure_ssh_key(repo_dir, host, args.api_port, str(args.api_version), args.ssh_user, args.ssh_port)

    ssh_cmd = _ssh_base(host, args.ssh_user, args.ssh_port, args.ssh_key)

    # Resolve on-disk calibration directories using the robot's own config.
    cal_dir = _remote_python_expr(
        ssh_cmd,
        'from opentrons.config import get_opentrons_path; print(get_opentrons_path("robot_calibration_dir"))',
    )
    tip_dir = _remote_python_expr(
        ssh_cmd,
        'from opentrons.config import get_opentrons_path; print(get_opentrons_path("tip_length_calibration_dir"))',
    )

    _write_json(out_dir / "paths.json", {"robot_calibration_dir": cal_dir, "tip_length_calibration_dir": tip_dir})

    print(f"Pulling robot calibration dir: {cal_dir}")
    _pull_tar_gz(ssh_cmd, cal_dir, out_dir / "robot_calibration_dir.tar.gz")

    print(f"Pulling tip length dir: {tip_dir}")
    _pull_tar_gz(ssh_cmd, tip_dir, out_dir / "tip_length_calibration_dir.tar.gz")

    print("Done.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        _eprint(f"[ERROR] {exc}")
        raise SystemExit(2)
