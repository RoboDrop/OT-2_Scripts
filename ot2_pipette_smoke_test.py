"""
OT-2 pipette smoke test over the robot-server HTTP API (no SSH).

Flow:
1) Connect to one OT-2 robot-server host.
2) Detect attached pipettes.
3) Home robot.
4) For each selected mount: move to slot 5, lower 3 inches, wait for manual tip confirm.
5) Register tip state and run aspirate/dispense cycles at 10% max volume until operator stops.
6) Drop tip in trash labware.
7) Test the other mount if attached.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib import error as url_error
from urllib import request as url_request

from opentrons import types

TEST_FRACTION = 0.10

MOUNT_ENV_KEY = "OT2_SMOKE_TEST_MOUNT"
HOST_ENV_KEY = "OT2_HOST"
DEFAULT_HOST = "opentrons.local"

TARGET_SLOT = "5"
TRASH_SLOT = "11"
TIPRACK_SLOT = "5"
TARGET_APPROACH_Z_MM = 120.0
LOWER_DISTANCE_MM = 40.0  # 3 inches
API_PORT = 31950
API_VERSION = "2"
HTTP_TIMEOUT_SECONDS = 20.0
COMMAND_TIMEOUT_SECONDS = 180.0
POLL_INTERVAL_SECONDS = 0.20

TIPRACK_LOAD_NAME = "opentrons_96_tiprack_300ul"
TIPRACK_NAMESPACE = "opentrons"
TIPRACK_VERSION = 1
TIP_WELL_BY_MOUNT = {
    "left": "A1",
    "right": "B1",
}

TRASH_LOAD_NAME = "opentrons_1_trash_1100ml_fixed"
TRASH_NAMESPACE = "opentrons"
TRASH_VERSION = 1
TRASH_WELL = "A1"
DEFINITIONS_DIR = Path(__file__).resolve().parent / "definitions"
HOST_RESOLVER = Path(__file__).resolve().parent / "ot2_resolve_host.py"


def _resolve_host() -> str:
    explicit = os.getenv(HOST_ENV_KEY, "").strip()
    if explicit:
        return explicit
    if not HOST_RESOLVER.is_file():
        return DEFAULT_HOST
    proc = subprocess.run(
        [
            sys.executable,
            str(HOST_RESOLVER),
            "--port",
            str(API_PORT),
            "--api-version",
            str(API_VERSION),
        ],
        check=False,
        text=True,
        capture_output=True,
    )
    if proc.returncode == 0:
        resolved = (proc.stdout or "").strip()
        if resolved:
            return resolved
    detail = (proc.stderr or proc.stdout or "").strip() or f"exit code {proc.returncode}"
    raise RuntimeError(f"Failed to auto-resolve OT-2 host. Set {HOST_ENV_KEY} or pass --host via wrapper.\n{detail}")


def _log_stderr(level: str, message: str) -> None:
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"{timestamp} [{level}] {message}", file=sys.stderr, flush=True)


def _input_required() -> None:
    if not sys.stdin.isatty():
        raise RuntimeError(
            "Interactive stdin is required for operator prompts. "
            "Run this script from an interactive terminal."
        )


def _selected_mounts() -> List[str]:
    raw_value = os.getenv(MOUNT_ENV_KEY, "both").strip().lower()
    if raw_value in ("", "both"):
        return ["left", "right"]
    if raw_value in ("left", "right"):
        return [raw_value]
    raise RuntimeError(
        f"Invalid {MOUNT_ENV_KEY} value: {raw_value!r}. Use left, right, or both."
    )


def _ordered_mount_tests(selected_mounts: List[str], attached: Dict[str, "InstrumentInfo"]) -> List[str]:
    if len(selected_mounts) == 1:
        first = selected_mounts[0]
        other = "right" if first == "left" else "left"
        ordered = [first, other]
    else:
        ordered = ["left", "right"]
    return [mount for mount in ordered if mount in attached]


def _prompt_tip_confirmation(mount_name: str, pipette_name: str) -> bool:
    _input_required()
    prompt = (
        f"{pipette_name}@{mount_name}: add a tip, then type 'r' to continue "
        "(or 'k' to skip this mount): "
    )
    while True:
        response = input(prompt).strip().lower()
        if response == "r":
            return True
        if response == "k":
            return False
        print("Please type 'r' to continue or 'k' to skip.", file=sys.stderr, flush=True)


def _prompt_continue_cycles() -> bool:
    _input_required()
    response = input(
        "Press Enter for another aspirate/dispense cycle, or type 's' to move on: "
    ).strip().lower()
    return response != "s"


def _prompt_manual_tip_discard(mount_name: str, pipette_name: str) -> None:
    _input_required()
    prompt = (
        f"{pipette_name}@{mount_name}: put tip in physical trash now, then type 'r' to continue: "
    )
    while True:
        response = input(prompt).strip().lower()
        if response == "r":
            return
        print("Please type 'r' once the tip is in trash.", file=sys.stderr, flush=True)


def _slot_center(slot_id: str) -> types.Point:
    for deck_name in ("ot2_short_trash", "ot2_standard"):
        for version in (3, 4, 5):
            try:
                deck_path = DEFINITIONS_DIR / str(version) / f"{deck_name}.json"
                with deck_path.open("r", encoding="utf-8") as f:
                    deck = json.load(f)
            except Exception:  # noqa: BLE001
                continue
            slots = deck.get("locations", {}).get("orderedSlots", [])
            if not isinstance(slots, list):
                continue
            for slot in slots:
                if str(slot.get("id")) != slot_id:
                    continue
                position = slot.get("position", [0, 0, 0])
                bbox = slot.get("boundingBox", {})
                x = float(position[0]) + float(bbox.get("xDimension", 0)) / 2.0
                y = float(position[1]) + float(bbox.get("yDimension", 0)) / 2.0
                z = float(position[2])
                return types.Point(x=x, y=y, z=z)
    raise RuntimeError(
        f"Unable to resolve slot {slot_id} center from local OT-2 deck definitions in {DEFINITIONS_DIR}."
    )


@dataclass(frozen=True)
class InstrumentInfo:
    mount: str
    name: str
    max_volume: float


class ApiRequestError(RuntimeError):
    pass


class CommandExecutionError(RuntimeError):
    def __init__(self, command_type: str, error_payload: Dict[str, Any]):
        detail = str(error_payload.get("detail", "Unknown command failure"))
        super().__init__(f"{command_type} failed: {detail}")
        self.command_type = command_type
        self.error_payload = error_payload
        self.error_type = str(error_payload.get("errorType", ""))
        self.error_code = str(error_payload.get("errorCode", ""))


class RobotServerClient:
    def __init__(self, host: str, api_version: str = API_VERSION) -> None:
        normalized_host = host.strip()
        if not normalized_host:
            raise RuntimeError("Robot host is empty.")
        self._base_url = f"http://{normalized_host}:{API_PORT}"
        self._headers = {
            "opentrons-version": api_version,
            "Content-Type": "application/json",
            "Connection": "close",
        }

    def _request_json(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        expected_statuses: tuple[int, ...] = (200,),
        retries: int = 0,
    ) -> Dict[str, Any]:
        payload_bytes = None if body is None else json.dumps(body).encode("utf-8")
        url = f"{self._base_url}{path}"

        for attempt in range(retries + 1):
            req = url_request.Request(
                url=url,
                data=payload_bytes,
                method=method,
                headers=self._headers,
            )
            try:
                with url_request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
                    status = resp.getcode()
                    raw = resp.read().decode("utf-8")
                    payload = json.loads(raw) if raw else {}
            except url_error.HTTPError as exc:
                status = exc.code
                raw = exc.read().decode("utf-8")
                try:
                    payload = json.loads(raw) if raw else {}
                except Exception:  # noqa: BLE001
                    payload = {"raw": raw}
            except Exception as exc:  # noqa: BLE001
                if attempt < retries:
                    time.sleep(POLL_INTERVAL_SECONDS)
                    continue
                raise ApiRequestError(f"{method} {path} failed: {exc}") from exc

            if status in expected_statuses:
                return payload
            if attempt < retries and status >= 500:
                time.sleep(POLL_INTERVAL_SECONDS)
                continue
            raise ApiRequestError(
                f"{method} {path} returned {status}: {self._error_message(payload)}"
            )

        raise ApiRequestError(f"{method} {path} failed after retries.")

    @staticmethod
    def _error_message(payload: Dict[str, Any]) -> str:
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            detail = errors[0].get("detail")
            if detail:
                return str(detail)
        detail = payload.get("detail")
        if detail:
            return str(detail)
        return json.dumps(payload) if payload else "no error payload"

    def health(self) -> Dict[str, Any]:
        return self._request_json("GET", "/health", expected_statuses=(200,), retries=1)

    def instruments(self) -> List[Dict[str, Any]]:
        payload = self._request_json("GET", "/instruments", expected_statuses=(200,), retries=1)
        data = payload.get("data")
        if isinstance(data, list):
            return data
        raise ApiRequestError("Invalid /instruments response.")

    def current_run_id(self) -> Optional[str]:
        payload = self._request_json(
            "GET",
            "/maintenance_runs/current_run",
            expected_statuses=(200, 404),
            retries=1,
        )
        data = payload.get("data")
        if isinstance(data, dict) and data.get("id"):
            return str(data["id"])
        return None

    def create_run(self) -> str:
        payload = self._request_json(
            "POST",
            "/maintenance_runs",
            body={"data": {}},
            expected_statuses=(201,),
            retries=1,
        )
        run_id = payload.get("data", {}).get("id")
        if not run_id:
            raise ApiRequestError("Maintenance run created without run id.")
        return str(run_id)

    def get_run_status(self, run_id: str) -> Optional[str]:
        payload = self._request_json(
            "GET",
            f"/maintenance_runs/{run_id}",
            expected_statuses=(200, 404),
            retries=1,
        )
        data = payload.get("data")
        if isinstance(data, dict):
            status = data.get("status")
            if isinstance(status, str):
                return status
        return None

    def wait_until_run_idle(self, run_id: str, timeout_seconds: float) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            status = self.get_run_status(run_id)
            if status is None:
                return
            if status == "idle":
                return
            time.sleep(POLL_INTERVAL_SECONDS)
        raise ApiRequestError(f"Run {run_id} did not become idle within timeout.")

    def delete_run(self, run_id: str) -> None:
        self._request_json(
            "DELETE",
            f"/maintenance_runs/{run_id}",
            expected_statuses=(200, 404),
            retries=1,
        )

    def ensure_no_current_run(self) -> None:
        run_id = self.current_run_id()
        if not run_id:
            return
        _log_stderr("WARN", f"Found existing maintenance run {run_id}; waiting for idle then deleting it.")
        self.wait_until_run_idle(run_id, timeout_seconds=60.0)
        self.delete_run(run_id)

    def post_command(
        self,
        run_id: str,
        command_type: str,
        params: Dict[str, Any],
        timeout_seconds: float = COMMAND_TIMEOUT_SECONDS,
    ) -> Dict[str, Any]:
        create_payload = self._request_json(
            "POST",
            f"/maintenance_runs/{run_id}/commands",
            body={"data": {"commandType": command_type, "params": params}},
            expected_statuses=(201,),
            retries=1,
        )
        command_id = create_payload.get("data", {}).get("id")
        if not command_id:
            raise ApiRequestError(f"{command_type}: missing command id from create response.")
        return self._wait_for_command(run_id, str(command_id), command_type, timeout_seconds)

    def _wait_for_command(
        self,
        run_id: str,
        command_id: str,
        command_type: str,
        timeout_seconds: float,
    ) -> Dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            payload = self._request_json(
                "GET",
                f"/maintenance_runs/{run_id}/commands/{command_id}",
                expected_statuses=(200,),
                retries=1,
            )
            data = payload.get("data", {})
            status = str(data.get("status", ""))
            if status == "succeeded":
                return data
            if status == "failed":
                error_payload = data.get("error", {})
                if not isinstance(error_payload, dict):
                    error_payload = {"detail": str(error_payload)}
                raise CommandExecutionError(command_type, error_payload)
            time.sleep(POLL_INTERVAL_SECONDS)
        raise ApiRequestError(f"{command_type} command {command_id} timed out.")


def _attached_by_mount(instrument_rows: List[Dict[str, Any]]) -> Dict[str, InstrumentInfo]:
    attached: Dict[str, InstrumentInfo] = {}
    for row in instrument_rows:
        if not isinstance(row, dict):
            continue
        if row.get("instrumentType") != "pipette":
            continue
        if not bool(row.get("ok")):
            continue
        mount = str(row.get("mount", "")).strip().lower()
        name = str(row.get("instrumentName", "")).strip()
        if mount not in ("left", "right") or not name:
            continue
        max_volume_raw = row.get("data", {}).get("max_volume")
        try:
            max_volume = float(max_volume_raw)
        except (TypeError, ValueError):
            continue
        if max_volume <= 0:
            continue
        attached[mount] = InstrumentInfo(mount=mount, name=name, max_volume=max_volume)
    return attached


def _volume_test_settings(max_volume: float) -> tuple[float, float, float]:
    test_volume = round(max_volume * TEST_FRACTION, 2)
    aspirate_flow = max(2.0, min(50.0, round(test_volume / 6.0, 2)))
    dispense_flow = max(4.0, min(100.0, round(test_volume / 3.0, 2)))
    return test_volume, aspirate_flow, dispense_flow


def _ensure_labware(
    client: RobotServerClient,
    run_id: str,
    cache: Dict[str, str],
    *,
    cache_key: str,
    load_name: str,
    namespace: str,
    version: int,
    slot_name: str,
) -> str:
    existing = cache.get(cache_key)
    if existing:
        return existing
    result = client.post_command(
        run_id,
        "loadLabware",
        {
            "location": {"slotName": slot_name},
            "loadName": load_name,
            "namespace": namespace,
            "version": version,
        },
    )
    labware_id = str(result.get("result", {}).get("labwareId", "")).strip()
    if not labware_id:
        raise RuntimeError(f"loadLabware for {cache_key} returned no labwareId.")
    cache[cache_key] = labware_id
    return labware_id


def _load_pipette(
    client: RobotServerClient,
    run_id: str,
    mount_name: str,
    pipette_name: str,
) -> str:
    result = client.post_command(
        run_id,
        "loadPipette",
        {"pipetteName": pipette_name, "mount": mount_name},
    )
    pipette_id = str(result.get("result", {}).get("pipetteId", "")).strip()
    if not pipette_id:
        raise RuntimeError(f"loadPipette returned no pipetteId for {pipette_name}@{mount_name}.")
    return pipette_id


def _move_mount_to_slot(
    client: RobotServerClient,
    run_id: str,
    pipette_id: str,
    mount_name: str,
    slot_id: str,
    z_height: float,
) -> None:
    center = _slot_center(slot_id)
    _log_stderr(
        "INFO",
        f"Moving {mount_name} mount to slot {slot_id} at z={z_height:.1f} mm.",
    )
    client.post_command(
        run_id,
        "moveToCoordinates",
        {
            "pipetteId": pipette_id,
            "coordinates": {"x": center.x, "y": center.y, "z": z_height},
        },
    )


def _move_to_slot_and_lower(
    client: RobotServerClient,
    run_id: str,
    pipette_id: str,
    mount_name: str,
) -> None:
    _move_mount_to_slot(
        client=client,
        run_id=run_id,
        pipette_id=pipette_id,
        mount_name=mount_name,
        slot_id=TARGET_SLOT,
        z_height=TARGET_APPROACH_Z_MM,
    )
    _log_stderr(
        "INFO",
        f"Lowering {mount_name} mount by {LOWER_DISTANCE_MM:.1f} mm (3 inches).",
    )
    client.post_command(
        run_id,
        "moveRelative",
        {
            "pipetteId": pipette_id,
            "axis": "z",
            "distance": -LOWER_DISTANCE_MM,
        },
    )


def _register_tip_state(
    client: RobotServerClient,
    run_id: str,
    pipette_id: str,
    tiprack_labware_id: str,
    tip_well: str,
    mount_name: str,
    pipette_name: str,
) -> None:
    _log_stderr("INFO", f"Registering tip state via pickUpTip on virtual tiprack well {tip_well}.")
    try:
        client.post_command(
            run_id,
            "pickUpTip",
            {
                "pipetteId": pipette_id,
                "labwareId": tiprack_labware_id,
                "wellName": tip_well,
            },
        )
    except CommandExecutionError as exc:
        if exc.error_type == "TipAttachedError":
            _log_stderr(
                "WARN",
                f"{pipette_name}@{mount_name} already reported a tip attached; continuing.",
            )
            return
        raise


def _discard_tip_in_trash(
    client: RobotServerClient,
    run_id: str,
    pipette_id: str,
    mount_name: str,
    pipette_name: str,
    tiprack_id: str,
    tip_well: str,
    trash_id: str,
) -> None:
    _log_stderr("INFO", f"Dropping {pipette_name}@{mount_name} tip into trash labware.")
    try:
        client.post_command(
            run_id,
            "dropTip",
            {
                "pipetteId": pipette_id,
                "labwareId": trash_id,
                "wellName": TRASH_WELL,
                "homeAfter": False,
            },
        )
        return
    except CommandExecutionError as exc:
        _log_stderr("WARN", f"dropTip to trash failed: {exc}")

    _log_stderr("WARN", "Falling back to dropTip in tiprack location to clear tip state.")
    try:
        client.post_command(
            run_id,
            "dropTip",
            {
                "pipetteId": pipette_id,
                "labwareId": tiprack_id,
                "wellName": tip_well,
                "homeAfter": False,
            },
        )
    except CommandExecutionError as exc:
        _log_stderr("WARN", f"Fallback dropTip failed: {exc}")
    _prompt_manual_tip_discard(mount_name, pipette_name)


def _exercise_mount(
    client: RobotServerClient,
    run_id: str,
    mount_name: str,
    instrument: InstrumentInfo,
    labware_cache: Dict[str, str],
) -> None:
    pipette_name = instrument.name
    test_volume, aspirate_flow, dispense_flow = _volume_test_settings(instrument.max_volume)
    if test_volume <= 0:
        raise RuntimeError(f"Computed non-positive test volume for {pipette_name}@{mount_name}.")

    pipette_id = _load_pipette(
        client=client,
        run_id=run_id,
        mount_name=mount_name,
        pipette_name=pipette_name,
    )
    _log_stderr("INFO", f"Loaded {pipette_name}@{mount_name} as pipette id {pipette_id}.")

    _move_to_slot_and_lower(
        client=client,
        run_id=run_id,
        pipette_id=pipette_id,
        mount_name=mount_name,
    )
    if not _prompt_tip_confirmation(mount_name, pipette_name):
        _log_stderr("WARN", f"Skipping {pipette_name}@{mount_name} at operator request.")
        return

    tiprack_id = _ensure_labware(
        client=client,
        run_id=run_id,
        cache=labware_cache,
        cache_key="tiprack",
        load_name=TIPRACK_LOAD_NAME,
        namespace=TIPRACK_NAMESPACE,
        version=TIPRACK_VERSION,
        slot_name=TIPRACK_SLOT,
    )
    trash_id = _ensure_labware(
        client=client,
        run_id=run_id,
        cache=labware_cache,
        cache_key="trash",
        load_name=TRASH_LOAD_NAME,
        namespace=TRASH_NAMESPACE,
        version=TRASH_VERSION,
        slot_name=TRASH_SLOT,
    )
    tip_well = TIP_WELL_BY_MOUNT.get(mount_name, "A1")

    _register_tip_state(
        client=client,
        run_id=run_id,
        pipette_id=pipette_id,
        tiprack_labware_id=tiprack_id,
        tip_well=tip_well,
        mount_name=mount_name,
        pipette_name=pipette_name,
    )

    _move_to_slot_and_lower(
        client=client,
        run_id=run_id,
        pipette_id=pipette_id,
        mount_name=mount_name,
    )

    _log_stderr(
        "INFO",
        f"Starting cycle loop for {pipette_name}@{mount_name}: "
        f"{test_volume} uL (10% of {instrument.max_volume} uL), "
        f"aspirate flow {aspirate_flow} uL/s, dispense flow {dispense_flow} uL/s.",
    )

    cycle = 1
    while True:
        client.post_command(run_id, "prepareToAspirate", {"pipetteId": pipette_id})
        _log_stderr("INFO", f"{pipette_name}@{mount_name} cycle {cycle}: aspirate")
        try:
            client.post_command(
                run_id,
                "aspirateInPlace",
                {
                    "pipetteId": pipette_id,
                    "volume": test_volume,
                    "flowRate": aspirate_flow,
                },
            )
        except CommandExecutionError as exc:
            if exc.error_type == "TipNotAttachedError":
                _log_stderr("WARN", f"Tip tracking lost on {pipette_name}@{mount_name}; re-registering tip.")
                _register_tip_state(
                    client=client,
                    run_id=run_id,
                    pipette_id=pipette_id,
                    tiprack_labware_id=tiprack_id,
                    tip_well=tip_well,
                    mount_name=mount_name,
                    pipette_name=pipette_name,
                )
                client.post_command(
                    run_id,
                    "aspirateInPlace",
                    {
                        "pipetteId": pipette_id,
                        "volume": test_volume,
                        "flowRate": aspirate_flow,
                    },
                )
            else:
                raise
        _log_stderr("INFO", f"{pipette_name}@{mount_name} cycle {cycle}: dispense")
        client.post_command(
            run_id,
            "dispenseInPlace",
            {
                "pipetteId": pipette_id,
                "volume": test_volume,
                "flowRate": dispense_flow,
                "pushOut": 0.0,
            },
        )
        if not _prompt_continue_cycles():
            _log_stderr("INFO", f"Stopping cycle loop for {pipette_name}@{mount_name}.")
            break
        cycle += 1

    _discard_tip_in_trash(
        client=client,
        run_id=run_id,
        pipette_id=pipette_id,
        mount_name=mount_name,
        pipette_name=pipette_name,
        tiprack_id=tiprack_id,
        tip_well=tip_well,
        trash_id=trash_id,
    )


def _run_impl() -> None:
    _input_required()
    selected_mounts = _selected_mounts()
    host = _resolve_host()
    _log_stderr("INFO", f"Requested mount selection: {','.join(selected_mounts)}")
    _log_stderr("INFO", f"Using OT-2 robot-server host: {host}:{API_PORT}")

    client = RobotServerClient(host=host)
    health = client.health()
    _log_stderr(
        "INFO",
        "Connected to robot-server: "
        f"{health.get('name', 'unknown')} "
        f"({health.get('robot_model', 'unknown')}), "
        f"API {health.get('api_version', 'unknown')}",
    )

    attached = _attached_by_mount(client.instruments())
    if not attached:
        raise RuntimeError("No attached pipettes detected. Install a pipette and retry.")

    mounts_to_test = _ordered_mount_tests(selected_mounts, attached)
    if not mounts_to_test:
        raise RuntimeError(
            "No attached pipettes matched selected mount(s): "
            + ", ".join(selected_mounts)
            + "."
        )
    _log_stderr(
        "INFO",
        "Testing pipettes: "
        + ", ".join(f"{attached[m].name}@{m}" for m in mounts_to_test),
    )

    run_id: Optional[str] = None
    labware_cache: Dict[str, str] = {}
    try:
        client.ensure_no_current_run()
        run_id = client.create_run()
        _log_stderr("INFO", f"Created maintenance run {run_id}.")

        _log_stderr("INFO", "Homing robot before test sequence.")
        client.post_command(run_id, "home", {})

        for mount_name in mounts_to_test:
            _log_stderr("INFO", f"Starting mount test for {mount_name}.")
            _exercise_mount(
                client=client,
                run_id=run_id,
                mount_name=mount_name,
                instrument=attached[mount_name],
                labware_cache=labware_cache,
            )
            _log_stderr("INFO", f"Homing robot after {mount_name} mount test.")
            client.post_command(run_id, "home", {})

        _log_stderr("INFO", "Pipette smoke test complete.")
    finally:
        if run_id:
            try:
                client.wait_until_run_idle(run_id, timeout_seconds=60.0)
            except Exception as exc:  # noqa: BLE001
                _log_stderr("WARN", f"Run {run_id} did not become idle before cleanup: {exc}")
            try:
                client.delete_run(run_id)
                _log_stderr("INFO", f"Deleted maintenance run {run_id}.")
            except Exception as exc:  # noqa: BLE001
                _log_stderr("WARN", f"Failed to delete maintenance run {run_id}: {exc}")


def main() -> int:
    try:
        _run_impl()
    except Exception as exc:  # noqa: BLE001
        _log_stderr("ERROR", f"Pipette smoke test failed: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
