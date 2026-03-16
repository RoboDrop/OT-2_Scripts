#!/usr/bin/env python3
"""Apply standard OT-2 deck/pipette/tip calibrations to currently attached pipettes.

This script:
1) Reads attached pipette serials from robot-server /instruments.
2) Rewrites standard calibration templates to those serials.
3) Uploads and applies files on the OT-2 over SSH.

Default template files are expected in the `offsets/` folder next to this script:
  - offsets/pipette_offsets_all.json
  - offsets/tip_length_offsets_all.json
  - offsets/calibration_status_with_deck_offset.json (or offsets/deck_offset.json)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib import parse, request
from urllib import error as url_error


def _utc_now() -> str:
    # Keep offset form (`+00:00`) instead of `Z` because the OT-2's Python 3.10
    # calibration JSON decoder uses `datetime.fromisoformat()`, which does not
    # accept a `Z` suffix.
    return datetime.now(timezone.utc).isoformat()


def _http_json(url: str, api_version: str, timeout: float = 20.0) -> Dict[str, Any]:
    req = request.Request(url, headers={"opentrons-version": api_version})
    with request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _run(cmd: List[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(cmd, check=False, text=True, capture_output=True)
    if check and proc.returncode != 0:
        cmd_str = " ".join(cmd)
        stderr = (proc.stderr or "").strip()
        stdout = (proc.stdout or "").strip()
        detail = stderr or stdout or f"exit code {proc.returncode}"
        raise RuntimeError(f"Command failed: {cmd_str}\n{detail}")
    return proc


def _ssh_base(args: argparse.Namespace) -> List[str]:
    base = ["ssh", "-p", str(args.ssh_port), "-o", "StrictHostKeyChecking=accept-new"]
    if args.ssh_key:
        base += ["-i", args.ssh_key]
    base += [f"{args.ssh_user}@{args.host}"]
    return base


def _scp_base(args: argparse.Namespace) -> List[str]:
    base = ["scp", "-P", str(args.ssh_port), "-o", "StrictHostKeyChecking=accept-new"]
    if args.ssh_key:
        base += ["-i", args.ssh_key]
    return base


def _can_auth_with_default_ssh(args: argparse.Namespace) -> bool:
    """Check if SSH works without specifying an identity (ssh-agent/config/default keys)."""
    cmd = [
        "ssh",
        "-p",
        str(args.ssh_port),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        f"{args.ssh_user}@{args.host}",
        "true",
    ]
    proc = subprocess.run(cmd, check=False, text=True, capture_output=True)
    return proc.returncode == 0


def _ssh_copy_file(args: argparse.Namespace, src: Path, dst: str) -> None:
    """Copy a local file to remote over plain ssh, avoiding scp subsystem requirements."""
    ssh_cmd = _ssh_base(args) + [f"cat > {parse.quote(dst)}"]
    with src.open("rb") as f:
        data = f.read()
    proc = subprocess.run(ssh_cmd, input=data, capture_output=True)
    if proc.returncode != 0:
        cmd_str = " ".join(ssh_cmd)
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        stdout = proc.stdout.decode("utf-8", errors="replace").strip()
        detail = stderr or stdout or f"exit code {proc.returncode}"
        raise RuntimeError(f"Upload failed: {cmd_str}\n{detail}")


def _load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _slug(value: str) -> str:
    value = value.strip().lower()
    out = []
    prev_dash = False
    for ch in value:
        keep = ("a" <= ch <= "z") or ("0" <= ch <= "9") or ch in "._-"
        if keep:
            out.append(ch)
            prev_dash = False
        else:
            if not prev_dash:
                out.append("-")
            prev_dash = True
    s = "".join(out).strip("-")
    while "--" in s:
        s = s.replace("--", "-")
    return s or "ot2"


def _default_key_dir() -> Path:
    base = Path(os.getenv("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / "opentrons-tools" / "ssh"


def _robot_name(host: str, api_port: int, api_version: str) -> str:
    url = f"http://{host}:{api_port}/health"
    payload = _http_json(url, api_version, timeout=5.0)
    return str(payload.get("name") or "opentrons")


def _wait_for_robot_server_ready(host: str, api_port: int, api_version: str, timeout_seconds: float) -> None:
    """Wait for robot-server to return 200 /health after a restart."""
    url = f"http://{host}:{api_port}/health"
    start = time.time()
    last_err: str | None = None
    while True:
        if time.time() - start > timeout_seconds:
            detail = last_err or "timeout waiting for /health"
            raise RuntimeError(f"Timed out waiting for robot-server to become ready at {url}.\n{detail}")
        try:
            _http_json(url, api_version, timeout=2.0)
            return
        except url_error.HTTPError as exc:
            last_err = f"HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')[:200]}"
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {exc}"
        time.sleep(2.0)


def _ssh_preflight(args: argparse.Namespace) -> None:
    """Fail fast with a useful error if SSH auth won't work."""
    proc = subprocess.run(_ssh_base(args) + ["true"], check=False, text=True, capture_output=True)
    if proc.returncode == 0:
        return
    stderr = (proc.stderr or "").strip()
    stdout = (proc.stdout or "").strip()
    detail = stderr or stdout or f"exit code {proc.returncode}"
    extra = ""
    if getattr(args, "_robot_name", ""):
        key_dir = Path(getattr(args, "_ssh_key_dir", "") or _default_key_dir()).expanduser()
        pub = key_dir / f"ot2_{_slug(str(args._robot_name))}_ed25519.pub"
        if pub.is_file():
            extra = (
                "\nIf you need to authorize a key on the robot, you can use this generated public key:\n"
                f"  {pub}\n"
            )
    raise RuntimeError(
        "Unable to SSH to the robot with the current settings.\n"
        f"  target: {args.ssh_user}@{args.host}:{args.ssh_port}\n"
        f"  detail: {detail}\n\n"
        "Fix options:\n"
        "- Provide an authorized key: --ssh-key /path/to/private_key\n"
        "- Or (if the robot allows password auth) run once with: --ensure-ssh-key\n"
        + extra
    )


def _attached_pipettes(host: str, api_port: int, api_version: str) -> Dict[str, Dict[str, str]]:
    url = f"http://{host}:{api_port}/instruments"
    payload = _http_json(url, api_version)
    out: Dict[str, Dict[str, str]] = {}
    for item in payload.get("data", []):
        if item.get("instrumentType") != "pipette":
            continue
        mount = str(item.get("mount", "")).lower()
        serial = str(item.get("serialNumber", "")).strip()
        name = str(item.get("instrumentName", "")).strip()
        if mount in ("left", "right") and serial:
            out[mount] = {"serial": serial, "name": name}
    return out


_PIPETTE_SERIAL_RE = re.compile(r"\bP[0-9A-Z]{8,}\b")


def _preferred_ambiguous_serial(left_serial: str | None, right_serial: str | None) -> str | None:
    # User preference: if ambiguous and both are attached, pick left.
    return left_serial or right_serial


def _collect_serial_mounts_from_pipette_template(pipette_offsets: Dict[str, Any]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for entry in pipette_offsets.get("data", []):
        if not isinstance(entry, dict):
            continue
        mount = str(entry.get("mount", "")).strip().lower()
        serial = str(entry.get("pipette", "")).strip()
        if mount in ("left", "right") and _PIPETTE_SERIAL_RE.fullmatch(serial):
            out[serial] = mount
    return out


def _serial_replacement_map(
    serial_mounts: Dict[str, str], left_serial: str | None, right_serial: str | None
) -> Dict[str, str]:
    out: Dict[str, str] = {}
    ambiguous_serial = _preferred_ambiguous_serial(left_serial, right_serial)
    for serial, mount in serial_mounts.items():
        if mount == "left" and left_serial:
            out[serial] = left_serial
        elif mount == "right" and right_serial:
            out[serial] = right_serial
        elif ambiguous_serial:
            out[serial] = ambiguous_serial
    return out


def _replace_serials_in_template(
    value: Any,
    serial_map: Dict[str, str],
    left_serial: str | None,
    right_serial: str | None,
    current_mount: str | None = None,
) -> Any:
    ambiguous_serial = _preferred_ambiguous_serial(left_serial, right_serial)

    if isinstance(value, dict):
        mount_raw = value.get("mount")
        next_mount = current_mount
        if isinstance(mount_raw, str):
            mount = mount_raw.strip().lower()
            if mount in ("left", "right"):
                next_mount = mount

        out: Dict[str, Any] = {}
        for k, v in value.items():
            replaced = _replace_serials_in_template(
                v,
                serial_map=serial_map,
                left_serial=left_serial,
                right_serial=right_serial,
                current_mount=next_mount,
            )
            if k in ("pipette", "pipetteCalibratedWith", "pipette_calibrated_with", "serialNumber"):
                if isinstance(replaced, str) and _PIPETTE_SERIAL_RE.fullmatch(replaced):
                    if next_mount == "left" and left_serial:
                        replaced = left_serial
                    elif next_mount == "right" and right_serial:
                        replaced = right_serial
                    else:
                        replaced = serial_map.get(replaced, ambiguous_serial or replaced)
            out[k] = replaced
        return out

    if isinstance(value, list):
        return [
            _replace_serials_in_template(
                item,
                serial_map=serial_map,
                left_serial=left_serial,
                right_serial=right_serial,
                current_mount=current_mount,
            )
            for item in value
        ]

    if isinstance(value, str):
        if not _PIPETTE_SERIAL_RE.search(value):
            return value

        def repl(match: re.Match[str]) -> str:
            serial = match.group(0)
            if current_mount == "left" and left_serial:
                return left_serial
            if current_mount == "right" and right_serial:
                return right_serial
            return serial_map.get(serial, ambiguous_serial or serial)

        return _PIPETTE_SERIAL_RE.sub(repl, value)

    return value


def _pipette_volume_from_name(pipette_name: str) -> str | None:
    m = re.search(r"p(\d+)", pipette_name.strip().lower())
    if not m:
        return None
    return m.group(1)


def _find_template_by_mount(
    pipette_offsets: Dict[str, Any], mount: str, pipette_name: str | None = None
) -> Dict[str, Any]:
    candidates: List[Dict[str, Any]] = []
    for entry in pipette_offsets.get("data", []):
        if str(entry.get("mount", "")).lower() == mount:
            candidates.append(entry)
    if not candidates:
        raise RuntimeError(f"No pipette offset template found for mount={mount!r}.")

    # Prefer a tiprack URI that matches the attached pipette volume family
    # (e.g. p20 -> tiprack_20ul) when multiple templates exist for one mount.
    if pipette_name:
        vol = _pipette_volume_from_name(pipette_name)
        if vol:
            needle = f"{vol}ul"
            for entry in candidates:
                uri = str(entry.get("tiprackUri", entry.get("uri", ""))).lower()
                if needle in uri:
                    return entry

    if len(candidates) == 1:
        return candidates[0]
    for entry in candidates:
        serial = str(entry.get("pipette", "")).strip()
        if _PIPETTE_SERIAL_RE.fullmatch(serial):
            return entry
    return candidates[0]


def _find_tip_templates_for_pipette(
    tip_lengths: Dict[str, Any],
    preferred_serial: str | None,
    preferred_tiprack: str | None = None,
    pipette_name: str | None = None,
) -> List[Dict[str, Any]]:
    data = [entry for entry in tip_lengths.get("data", []) if isinstance(entry, dict)]
    if not data:
        raise RuntimeError("No tip length templates found.")

    def has_volume(entry: Dict[str, Any]) -> bool:
        if not pipette_name:
            return False
        vol = _pipette_volume_from_name(pipette_name)
        if not vol:
            return False
        uri = str(entry.get("uri", "")).lower()
        return f"{vol}ul" in uri

    def _sort_key(entry: Dict[str, Any]) -> tuple[int, int, str]:
        serial = str(entry.get("pipette", "")).strip()
        tiprack = str(entry.get("tiprack", "")).strip()
        last_modified = str(entry.get("lastModified", ""))
        return (
            0 if preferred_serial and serial == preferred_serial else 1,
            0 if preferred_tiprack and tiprack == preferred_tiprack else 1,
            last_modified,
        )

    by_volume = [e for e in data if has_volume(e)]
    if by_volume:
        # Keep all URIs for the current pipette size (e.g. 20ul + filter 20ul),
        # choosing one best template per URI.
        by_uri: Dict[str, Dict[str, Any]] = {}
        for entry in sorted(by_volume, key=_sort_key):
            uri = str(entry.get("uri", "")).strip()
            if not uri or uri in by_uri:
                continue
            by_uri[uri] = entry
        if by_uri:
            return list(by_uri.values())

    if preferred_serial and preferred_tiprack:
        exact = [
            e
            for e in data
            if str(e.get("pipette", "")).strip() == preferred_serial
            and str(e.get("tiprack", "")).strip() == preferred_tiprack
        ]
        if exact:
            return [exact[0]]

    if preferred_serial:
        serial_entries = [e for e in data if str(e.get("pipette", "")).strip() == preferred_serial]
        if serial_entries:
            return [sorted(serial_entries, key=_sort_key)[0]]

    if preferred_tiprack:
        by_tiprack = [e for e in data if str(e.get("tiprack", "")).strip() == preferred_tiprack]
        if by_tiprack:
            return [sorted(by_tiprack, key=_sort_key)[0]]

    return [sorted(data, key=_sort_key)[0]]


def _build_pipette_file(template: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "offset": template["offset"],
        "tiprack": template["tiprack"],
        "uri": template.get("tiprackUri", template.get("uri", "")),
        "last_modified": _utc_now(),
        "source": template.get("source", "user"),
        "status": template.get("status", {"markedBad": False, "source": None, "markedAt": None}),
    }


def _build_tip_length_file(templates: List[Dict[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for template in templates:
        uri = str(template.get("uri", "")).strip()
        if not uri:
            continue
        out[uri] = {
            "tipLength": template["tipLength"],
            "lastModified": _utc_now(),
            "source": template.get("source", "user"),
            "status": template.get("status", {"markedBad": False, "source": None, "markedAt": None}),
            "definitionHash": template["tiprack"],
        }
    if not out:
        raise RuntimeError("No valid tip length templates found (missing uri).")
    return out


def _build_deck_file(deck_source: Dict[str, Any], default_pipette: str) -> Dict[str, Any]:
    # OT-2 deck calibration storage uses the v1 DeckCalibrationModel, which expects
    # snake_case keys on disk (attitude + last_modified). If this file is malformed,
    # robot-server can fail to initialize hardware (appearing "unresponsive").
    if "deckCalibration" in deck_source:
        deck = deck_source["deckCalibration"]["data"]
    else:
        deck = deck_source

    attitude = deck.get("attitude") or deck.get("matrix")
    if not attitude:
        raise RuntimeError("Deck template missing calibration attitude/matrix.")

    return {
        "attitude": attitude,
        "last_modified": _utc_now(),
        "source": deck.get("source", "user"),
        "pipette_calibrated_with": default_pipette
        if default_pipette
        else deck.get("pipette_calibrated_with", deck.get("pipetteCalibratedWith", "")),
        "tiprack": deck.get("tiprack"),
        "status": deck.get("status", {"markedBad": False, "source": None, "markedAt": None}),
    }


def sh_quote(value: str) -> str:
    # Minimal POSIX shell quoting for remote command assembly.
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _remote_apply(
    args: argparse.Namespace,
    local_left: Path | None,
    local_right: Path | None,
    local_tip_left: Path | None,
    local_tip_right: Path | None,
    local_deck: Path,
    left_serial: str | None,
    right_serial: str | None,
) -> None:
    remote_tmp = f"/data/{args.remote_tag}"
    remote_deck_final = "/data/robot/deck_calibration.json"
    to_copy: List[Tuple[Path, str]] = [(local_deck, f"{remote_tmp}/deck_calibration.json")]
    if local_left and left_serial:
        to_copy.append((local_left, f"{remote_tmp}/{left_serial}.left.pipette.json"))
    if local_right and right_serial:
        to_copy.append((local_right, f"{remote_tmp}/{right_serial}.right.pipette.json"))
    if local_tip_left and left_serial:
        to_copy.append((local_tip_left, f"{remote_tmp}/{left_serial}.tip_lengths.json"))
    if local_tip_right and right_serial:
        to_copy.append((local_tip_right, f"{remote_tmp}/{right_serial}.tip_lengths.json"))

    script_lines = [
        "set -euo pipefail",
        f"mkdir -p /data/robot/pipettes/left /data/robot/pipettes/right /data/tip_lengths /data/robot",
        # deck calibration path is resolved by opentrons.config.get_opentrons_path("robot_calibration_dir")
        # on the robot. Use that when available, but also mirror to /data/robot for legacy tooling.
        "CAL_DIR=\"$(python -c 'from opentrons.config import get_opentrons_path; print(get_opentrons_path(\"robot_calibration_dir\"))' 2>/dev/null || true)\"",
        "if [ -n \"$CAL_DIR\" ]; then mkdir -p \"$CAL_DIR\"; cp "
        + f"{remote_tmp}/deck_calibration.json \"$CAL_DIR/deck_calibration.json\"; "
        + "fi",
        f"cp {remote_tmp}/deck_calibration.json {remote_deck_final}",
        # Validate deck calibration using the robot's own model before restarting services.
        "python -c 'from opentrons.calibration_storage.ot2.models import v1; "
        "from opentrons.config import get_opentrons_path; "
        "from pathlib import Path; "
        "p = Path(get_opentrons_path(\"robot_calibration_dir\")) / \"deck_calibration.json\"; "
        "v1.DeckCalibrationModel.model_validate_json(p.read_text(encoding=\"utf-8\")); "
        "print(\"deck_calibration_valid\", str(p))'",
    ]
    if left_serial and local_left:
        script_lines.append(
            f"cp {remote_tmp}/{left_serial}.left.pipette.json /data/robot/pipettes/left/{left_serial}.json"
        )
    if right_serial and local_right:
        script_lines.append(
            f"cp {remote_tmp}/{right_serial}.right.pipette.json /data/robot/pipettes/right/{right_serial}.json"
        )
    if left_serial and local_tip_left:
        script_lines.append(
            f"cp {remote_tmp}/{left_serial}.tip_lengths.json /data/tip_lengths/{left_serial}.json"
        )
    if right_serial and local_tip_right:
        script_lines.append(
            f"cp {remote_tmp}/{right_serial}.tip_lengths.json /data/tip_lengths/{right_serial}.json"
        )

    remote_script = " && ".join(script_lines)
    if args.dry_run:
        print("[dry-run] files that would be uploaded:")
        for src, dst in to_copy:
            print(f"  {src} -> {dst}")
        print("[dry-run] remote apply script:")
        print(remote_script)
        return

    _run(_ssh_base(args) + [f"mkdir -p {remote_tmp}"], check=True)
    for src, dst in to_copy:
        _ssh_copy_file(args, src, dst)
    _run(_ssh_base(args) + [remote_script], check=True)
    if getattr(args, "restart_robot_server", False):
        _run(_ssh_base(args) + ["systemctl restart opentrons-robot-server"], check=True)
        _wait_for_robot_server_ready(args.host, args.api_port, args.api_version, float(args.restart_wait_seconds))


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")


def main() -> None:
    script_dir = Path(__file__).resolve().parent
    repo_dir = script_dir
    host_resolver = repo_dir / "ot2_resolve_host.py"
    ssh_key_helper = repo_dir / "ot2_ensure_ssh_key.py"
    default_offsets_dir = repo_dir / "offsets"

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="", help="OT-2 hostname or IP (auto-discovered if omitted)")
    parser.add_argument("--api-port", type=int, default=31950)
    parser.add_argument("--api-version", default="2")
    parser.add_argument("--ssh-user", default="root")
    parser.add_argument("--ssh-port", type=int, default=22)
    parser.add_argument("--ssh-key", default="", help="SSH private key path")
    parser.add_argument(
        "--ssh-key-dir",
        default="",
        help="Directory to store/generated SSH keys (passed through to ot2_ensure_ssh_key.py)",
    )
    parser.add_argument(
        "--ssh-key-scope",
        choices=("per-robot", "shared"),
        default="per-robot",
        help="When auto-setting up a key, choose per-robot (default) or shared",
    )
    parser.add_argument(
        "--ensure-ssh-key",
        action="store_true",
        help="If --ssh-key is omitted, try to generate + authorize a key via ot2_ensure_ssh_key.py (requires password auth).",
    )
    parser.add_argument(
        "--no-ensure-ssh-key",
        action="store_true",
        help="Disable SSH key auto-setup even if --ssh-key is omitted.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--remote-tag", default="standard-offsets-upload")
    parser.add_argument(
        "--restart-robot-server",
        action="store_true",
        help="Restart opentrons-robot-server after writing calibration files (default: no restart).",
    )
    parser.add_argument(
        "--restart-wait-seconds",
        type=float,
        default=120.0,
        help="Seconds to wait for robot-server to become ready after restart (default: 120).",
    )
    parser.add_argument(
        "--offsets-dir",
        default=str(default_offsets_dir),
        help="Folder containing standard calibration templates (default: ./offsets next to this script)",
    )
    parser.add_argument(
        "--pipette-offsets-template",
        default="pipette_offsets_all.json",
        help="Template file downloaded from /calibration/pipette_offset",
    )
    parser.add_argument(
        "--tip-length-template",
        default="tip_length_offsets_all.json",
        help="Template file downloaded from /calibration/tip_length",
    )
    parser.add_argument(
        "--deck-template",
        default="calibration_status_with_deck_offset.json",
        help="Template file from /calibration/status (or deck_offset.json)",
    )
    args = parser.parse_args()

    offsets_dir = Path(args.offsets_dir).expanduser().resolve()
    pipette_template_path = Path(args.pipette_offsets_template).expanduser()
    tip_template_path = Path(args.tip_length_template).expanduser()
    deck_template_path = Path(args.deck_template).expanduser()

    if not pipette_template_path.is_absolute():
        pipette_template_path = offsets_dir / pipette_template_path
    if not tip_template_path.is_absolute():
        tip_template_path = offsets_dir / tip_template_path
    if not deck_template_path.is_absolute():
        deck_template_path = offsets_dir / deck_template_path

    if not offsets_dir.is_dir():
        raise RuntimeError(
            f"Offsets directory not found: {offsets_dir}. "
            "Pass --offsets-dir PATH, or ensure an offsets/ folder exists next to this script."
        )
    for p in [pipette_template_path, tip_template_path, deck_template_path]:
        if not p.is_file():
            raise RuntimeError(f"Template file not found: {p}")

    if not args.host:
        if not host_resolver.is_file():
            raise RuntimeError(
                f"--host was omitted and host resolver was not found: {host_resolver}. "
                "Pass --host HOST."
            )
        proc = subprocess.run(
            [
                sys.executable,
                str(host_resolver),
                "--port",
                str(args.api_port),
                "--api-version",
                str(args.api_version),
            ],
            check=False,
            text=True,
            capture_output=True,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip() or f"exit code {proc.returncode}"
            raise RuntimeError(f"Failed to auto-resolve OT-2 host. Pass --host HOST.\n{detail}")
        args.host = (proc.stdout or "").strip()
        if not args.host:
            raise RuntimeError("Host resolver returned empty host. Pass --host HOST.")

    try:
        robot_name = _robot_name(args.host, args.api_port, args.api_version)
    except Exception:
        robot_name = "opentrons"
    args._robot_name = robot_name  # for preflight error messages
    args._ssh_key_dir = args.ssh_key_dir

    # If a per-robot key already exists (typically generated by ot2_ensure_ssh_key.py),
    # use it by default so workflows don't require passing --ssh-key every time.
    if not args.ssh_key:
        key_dir = Path(args.ssh_key_dir).expanduser() if args.ssh_key_dir else _default_key_dir()
        candidate = key_dir / f"ot2_{_slug(robot_name)}_rsa"
        if candidate.is_file():
            args.ssh_key = str(candidate)

    auto_ensure = args.ensure_ssh_key or not args.no_ensure_ssh_key

    if not args.ssh_key and auto_ensure and not args.dry_run:
        if not ssh_key_helper.is_file():
            raise RuntimeError(
                f"SSH key helper was not found: {ssh_key_helper}. "
                "Provide --ssh-key PATH or pass --no-ensure-ssh-key."
            )
        ensure_cmd = [
            sys.executable,
            str(ssh_key_helper),
            "--host",
            str(args.host),
            "--api-port",
            str(args.api_port),
            "--api-version",
            str(args.api_version),
            "--ssh-user",
            str(args.ssh_user),
            "--ssh-port",
            str(args.ssh_port),
            "--scope",
            str(args.ssh_key_scope),
            "--ensure-authorized",
        ]
        if args.ssh_key_dir:
            ensure_cmd += ["--key-dir", str(args.ssh_key_dir)]
        proc = subprocess.run(
            ensure_cmd,
            check=False,
            text=True,
            capture_output=True,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip() or f"exit code {proc.returncode}"
            if "Permission denied (publickey)" in detail:
                raise RuntimeError(
                    f"Failed to set up SSH key for {args.ssh_user}@{args.host}:{args.ssh_port}.\n"
                    f"{detail}\n\n"
                    "This usually means the robot is configured for publickey-only SSH and does not allow password-based login, "
                    "so the helper cannot install a new key automatically.\n\n"
                    "Fix options:\n"
                    "- Provide an already-authorized private key with --ssh-key\n"
                    "- Or authorize the generated public key on the robot out-of-band (reimage / console access), then re-run\n"
                )
            raise RuntimeError(
                f"Failed to ensure SSH key for {args.ssh_user}@{args.host}:{args.ssh_port}.\n{detail}"
            )
        args.ssh_key = (proc.stdout or "").strip()
        if not args.ssh_key:
            raise RuntimeError("SSH key helper returned empty key path.")

    if not args.dry_run:
        _ssh_preflight(args)

    attached = _attached_pipettes(args.host, args.api_port, args.api_version)
    left_serial = attached.get("left", {}).get("serial")
    right_serial = attached.get("right", {}).get("serial")
    left_name = attached.get("left", {}).get("name")
    right_name = attached.get("right", {}).get("name")
    if not left_serial and not right_serial:
        raise RuntimeError("No attached left/right pipettes were found via /instruments.")

    pipette_tpl = _load_json(pipette_template_path)
    tip_tpl = _load_json(tip_template_path)
    deck_tpl = _load_json(deck_template_path)

    serial_mounts = _collect_serial_mounts_from_pipette_template(pipette_tpl)
    serial_map = _serial_replacement_map(
        serial_mounts=serial_mounts,
        left_serial=left_serial,
        right_serial=right_serial,
    )

    # Normalize template serial references for currently attached mounts.
    pipette_tpl = _replace_serials_in_template(
        pipette_tpl,
        serial_map=serial_map,
        left_serial=left_serial,
        right_serial=right_serial,
    )
    # Keep tip-length templates as-is to avoid cross-size remapping (e.g. p20 run
    # accidentally mutating p300/p1000 entries to the current serial).
    deck_tpl = _replace_serials_in_template(
        deck_tpl,
        serial_map=serial_map,
        left_serial=left_serial,
        right_serial=right_serial,
    )

    print("Detected pipettes:")
    print(f"  left:  {left_serial or '<none>'}")
    print(f"  right: {right_serial or '<none>'}")

    with tempfile.TemporaryDirectory(prefix="apply-standard-offsets-") as td:
        td_path = Path(td)
        left_p_file = right_p_file = left_t_file = right_t_file = None

        if left_serial:
            lp = _find_template_by_mount(pipette_tpl, "left", left_name)
            left_p_file = td_path / f"{left_serial}.left.pipette.json"
            _write_json(left_p_file, _build_pipette_file(lp))

            lt = _find_tip_templates_for_pipette(
                tip_tpl,
                left_serial,
                preferred_tiprack=str(lp.get("tiprack", "")).strip(),
                pipette_name=left_name,
            )
            left_t_file = td_path / f"{left_serial}.tip_lengths.json"
            _write_json(left_t_file, _build_tip_length_file(lt))

        if right_serial:
            rp = _find_template_by_mount(pipette_tpl, "right", right_name)
            right_p_file = td_path / f"{right_serial}.right.pipette.json"
            _write_json(right_p_file, _build_pipette_file(rp))

            rt = _find_tip_templates_for_pipette(
                tip_tpl,
                right_serial,
                preferred_tiprack=str(rp.get("tiprack", "")).strip(),
                pipette_name=right_name,
            )
            right_t_file = td_path / f"{right_serial}.tip_lengths.json"
            _write_json(right_t_file, _build_tip_length_file(rt))

        default_pipette_for_deck = left_serial or right_serial or ""
        deck_file = td_path / "deck_calibration.json"
        _write_json(deck_file, _build_deck_file(deck_tpl, default_pipette_for_deck))
        if not deck_file.is_file():
            raise RuntimeError(f"Failed to write deck calibration file: {deck_file}")

        print("Prepared local payloads:")
        for p in [left_p_file, right_p_file, left_t_file, right_t_file, deck_file]:
            if p:
                print(f"  {p}")

        if not args.dry_run and args.restart_robot_server:
            print(
                "Will restart opentrons-robot-server after copying files (expect ~1–3 minutes of 502/503 responses)."
            )

        _remote_apply(
            args=args,
            local_left=left_p_file,
            local_right=right_p_file,
            local_tip_left=left_t_file,
            local_tip_right=right_t_file,
            local_deck=deck_file,
            left_serial=left_serial,
            right_serial=right_serial,
        )

    print("Done.")
    if not args.dry_run and not args.restart_robot_server:
        print("Recommend rebooting or restarting robot-server before running tests.")
    if not args.dry_run and args.restart_robot_server:
        print("robot-server was restarted to load deck calibration (API may have been unavailable briefly).")


if __name__ == "__main__":
    main()
