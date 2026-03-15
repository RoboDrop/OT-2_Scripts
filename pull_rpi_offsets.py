#!/usr/bin/env python3
"""Pull selected calibration snapshots from the currently connected Raspberry Pi/OT-2.

Writes files into: ./offsets/pulled/<robot>_<timestamp>/

Files written:
- calibration_status_with_deck_offset.json
- deck_offset.json
- pipette_offsets_all.json
- tip_length_offsets_all.json
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict
from urllib import request as url_request


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=False, text=True, capture_output=True)


def _slug(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return value or "opentrons"


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _http_json(host: str, api_port: int, api_version: str, path: str, timeout: float = 20.0) -> Dict[str, Any]:
    url = f"http://{host}:{api_port}{path}"
    req = url_request.Request(url, headers={"opentrons-version": str(api_version)})
    with url_request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _resolve_host(repo_dir: Path, host: str, api_port: int, api_version: str) -> str:
    if host.strip():
        return host.strip()
    resolver = repo_dir / "ot2_resolve_host.py"
    if not resolver.is_file():
        raise RuntimeError(f"--host not provided and resolver not found: {resolver}")

    proc = _run([sys.executable, str(resolver), "--port", str(api_port), "--api-version", str(api_version)])
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip() or f"exit code {proc.returncode}"
        raise RuntimeError(f"Failed to resolve host.\n{detail}")

    resolved = (proc.stdout or "").strip()
    if not resolved:
        raise RuntimeError("Resolved host was empty.")
    return resolved


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    repo_dir = Path(__file__).resolve().parent

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="", help="Pi/OT-2 host or IP (auto-discovered if omitted)")
    parser.add_argument("--api-port", type=int, default=31950)
    parser.add_argument("--api-version", default="2")
    parser.add_argument("--out-root", default="", help="Root output directory (default: ./offsets/pulled)")
    args = parser.parse_args()

    host = _resolve_host(repo_dir, args.host, args.api_port, str(args.api_version))
    health = _http_json(host, args.api_port, str(args.api_version), "/health", timeout=10.0)
    robot_name = str(health.get("name") or "opentrons")

    out_root = Path(args.out_root).expanduser() if args.out_root else (repo_dir / "offsets" / "pulled")
    out_dir = out_root / f"{_slug(robot_name)}_{_utc_stamp()}"
    out_dir.mkdir(parents=True, exist_ok=True)

    files = [
        ("/calibration/status", "calibration_status_with_deck_offset.json"),
        ("/calibration/status", "deck_offset.json"),
        ("/calibration/pipette_offset", "pipette_offsets_all.json"),
        ("/calibration/tip_length", "tip_length_offsets_all.json"),
    ]

    print(f"Robot: {robot_name} ({host})")
    print(f"Output: {out_dir}")

    for endpoint, filename in files:
        payload = _http_json(host, args.api_port, str(args.api_version), endpoint)
        _write_json(out_dir / filename, payload)
        print(f"Wrote {filename}")

    print("Done.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        raise SystemExit(2)
