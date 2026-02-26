#!/usr/bin/env python3
"""Apply standard OT-2 deck/pipette/tip calibrations to currently attached pipettes.

This script:
1) Reads attached pipette serials from robot-server /instruments.
2) Rewrites standard calibration templates to those serials.
3) Uploads and applies files on the OT-2 over SSH.

Default template files are expected in the current directory:
  - pipette_offsets_all.json
  - tip_length_offsets_all.json
  - calibration_status_with_deck_offset.json (or deck_offset.json)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib import parse, request


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


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


def _attached_pipette_serials(host: str, api_port: int, api_version: str) -> Dict[str, str]:
    url = f"http://{host}:{api_port}/instruments"
    payload = _http_json(url, api_version)
    out: Dict[str, str] = {}
    for item in payload.get("data", []):
        if item.get("instrumentType") != "pipette":
            continue
        mount = str(item.get("mount", "")).lower()
        serial = str(item.get("serialNumber", "")).strip()
        if mount in ("left", "right") and serial:
            out[mount] = serial
    return out


def _find_template_by_mount(pipette_offsets: Dict[str, Any], mount: str) -> Dict[str, Any]:
    for entry in pipette_offsets.get("data", []):
        if str(entry.get("mount", "")).lower() == mount:
            return entry
    raise RuntimeError(f"No pipette offset template found for mount={mount!r}.")


def _find_tip_template_for_pipette(
    tip_lengths: Dict[str, Any], preferred_serial: str | None
) -> Dict[str, Any]:
    if preferred_serial:
        for entry in tip_lengths.get("data", []):
            if str(entry.get("pipette", "")).strip() == preferred_serial:
                return entry
    data = tip_lengths.get("data", [])
    if not data:
        raise RuntimeError("No tip length templates found.")
    return data[0]


def _build_pipette_file(template: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "offset": template["offset"],
        "tiprack": template["tiprack"],
        "uri": template.get("tiprackUri", template.get("uri", "")),
        "last_modified": _utc_now(),
        "source": template.get("source", "user"),
        "status": template.get("status", {"markedBad": False, "source": None, "markedAt": None}),
    }


def _build_tip_length_file(template: Dict[str, Any]) -> Dict[str, Any]:
    uri = template["uri"]
    return {
        uri: {
            "tipLength": template["tipLength"],
            "lastModified": _utc_now(),
            "source": template.get("source", "user"),
            "status": template.get("status", {"markedBad": False, "source": None, "markedAt": None}),
            "definitionHash": template["tiprack"],
        }
    }


def _build_deck_file(deck_source: Dict[str, Any], default_pipette: str) -> Dict[str, Any]:
    if "deckCalibration" in deck_source:
        deck = deck_source["deckCalibration"]["data"]
    else:
        deck = deck_source

    if "attitude" in deck:
        attitude = deck["attitude"]
    else:
        attitude = deck["matrix"]

    return {
        "attitude": attitude,
        "last_modified": _utc_now(),
        "source": deck.get("source", "user"),
        "pipette_calibrated_with": deck.get("pipette_calibrated_with", deck.get("pipetteCalibratedWith", default_pipette)),
        "tiprack": deck.get("tiprack"),
        "status": deck.get("status", {"markedBad": False, "source": None, "markedAt": None}),
    }


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
        f"cp {remote_tmp}/deck_calibration.json /data/robot/deck_calibration.json",
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


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")


def main() -> None:
    script_dir = Path(__file__).resolve().parent

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="OT-2 hostname or IP")
    parser.add_argument("--api-port", type=int, default=31950)
    parser.add_argument("--api-version", default="2")
    parser.add_argument("--ssh-user", default="root")
    parser.add_argument("--ssh-port", type=int, default=22)
    parser.add_argument("--ssh-key", default="", help="SSH private key path")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--remote-tag", default="standard-offsets-upload")
    parser.add_argument(
        "--pipette-offsets-template",
        default=str(script_dir / "pipette_offsets_all.json"),
        help="Template file downloaded from /calibration/pipette_offset",
    )
    parser.add_argument(
        "--tip-length-template",
        default=str(script_dir / "tip_length_offsets_all.json"),
        help="Template file downloaded from /calibration/tip_length",
    )
    parser.add_argument(
        "--deck-template",
        default=str(script_dir / "calibration_status_with_deck_offset.json"),
        help="Template file from /calibration/status (or deck_offset.json)",
    )
    args = parser.parse_args()

    mounts = _attached_pipette_serials(args.host, args.api_port, args.api_version)
    left_serial = mounts.get("left")
    right_serial = mounts.get("right")
    if not left_serial and not right_serial:
        raise RuntimeError("No attached left/right pipettes were found via /instruments.")

    pipette_tpl = _load_json(Path(args.pipette_offsets_template))
    tip_tpl = _load_json(Path(args.tip_length_template))
    deck_tpl = _load_json(Path(args.deck_template))

    print("Detected pipettes:")
    print(f"  left:  {left_serial or '<none>'}")
    print(f"  right: {right_serial or '<none>'}")

    with tempfile.TemporaryDirectory(prefix="apply-standard-offsets-") as td:
        td_path = Path(td)
        left_p_file = right_p_file = left_t_file = right_t_file = None

        if left_serial:
            lp = _find_template_by_mount(pipette_tpl, "left")
            left_p_file = td_path / f"{left_serial}.left.pipette.json"
            _write_json(left_p_file, _build_pipette_file(lp))

            lt = _find_tip_template_for_pipette(tip_tpl, left_serial)
            left_t_file = td_path / f"{left_serial}.tip_lengths.json"
            _write_json(left_t_file, _build_tip_length_file(lt))

        if right_serial:
            rp = _find_template_by_mount(pipette_tpl, "right")
            right_p_file = td_path / f"{right_serial}.right.pipette.json"
            _write_json(right_p_file, _build_pipette_file(rp))

            rt = _find_tip_template_for_pipette(tip_tpl, right_serial)
            right_t_file = td_path / f"{right_serial}.tip_lengths.json"
            _write_json(right_t_file, _build_tip_length_file(rt))

        default_pipette_for_deck = right_serial or left_serial or ""
        deck_file = td_path / "deck_calibration.json"
        _write_json(deck_file, _build_deck_file(deck_tpl, default_pipette_for_deck))

        print("Prepared local payloads:")
        for p in [left_p_file, right_p_file, left_t_file, right_t_file, deck_file]:
            if p:
                print(f"  {p}")

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
    if not args.dry_run:
        print("Recommend rebooting or restarting robot-server before running tests.")


if __name__ == "__main__":
    main()
