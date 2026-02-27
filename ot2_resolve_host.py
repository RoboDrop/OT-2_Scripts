#!/usr/bin/env python3
"""Resolve the reachable OT-2 robot-server host for a USB-connected OT-2.

This helper is intended to be called by other scripts in this repo so host
discovery is consistent across workflows.

Resolution rules:
1) If --host is provided, verify it's reachable via GET /health and return it.
2) Otherwise, build a candidate list from:
   - opentrons.local
   - entries from the local ARP/neigh table on likely USB interfaces (USB-to-ethernet adapter
     and direct USB 2.0 connection), preferring *.local and private/link-local IPv4s
   - peer IP guesses derived from the local USB interface IPv4 address (when ARP is empty)
3) Probe candidates via GET http://HOST:PORT/health with opentrons-version header.
4) If exactly one host is reachable, print it.
5) If multiple are reachable, fail unless --pick-first is passed.
"""

from __future__ import annotations

import argparse
import ipaddress
import re
import socket
import subprocess
import sys
from typing import Dict, Iterable, List, Sequence, Set, Tuple
from urllib import error as url_error
from urllib import request as url_request


def _eprint(*args: object) -> None:
    print(*args, file=sys.stderr, flush=True)


def _run_quiet(cmd: Sequence[str]) -> str:
    proc = subprocess.run(cmd, check=False, text=True, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "").strip() or f"exit code {proc.returncode}")
    return proc.stdout


def _dedupe_keep_order(items: Iterable[str]) -> List[str]:
    seen: Set[str] = set()
    out: List[str] = []
    for item in items:
        item = item.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


_ARP_HOST_IP_RE = re.compile(r"(?P<host>[^\s]+)\s+\((?P<ip>\d+\.\d+\.\d+\.\d+)\)")
_IPV4_RE = re.compile(r"^(\d+\.\d+\.\d+\.\d+)$")
_MACOS_IFCONFIG_INET_RE = re.compile(
    r"^\s+inet\s+(?P<ip>\d+\.\d+\.\d+\.\d+)\s+netmask\s+(?P<netmask>0x[0-9a-fA-F]+|\d+\.\d+\.\d+\.\d+)"
)


def _macos_ifconfig_blocks() -> Dict[str, str]:
    """Return a map of interface name -> raw ifconfig block (macOS)."""
    try:
        out = _run_quiet(["ifconfig"])
    except Exception:
        return {}

    cur_name: str | None = None
    cur_lines: List[str] = []
    blocks: Dict[str, str] = {}

    def flush() -> None:
        nonlocal cur_name, cur_lines
        if not cur_name:
            return
        blocks[cur_name] = "\n".join(cur_lines)
        cur_name = None
        cur_lines = []

    for line in out.splitlines():
        if line and not line.startswith(("\t", " ")):
            flush()
            cur_name = line.split(":", 1)[0].strip()
        cur_lines.append(line)
    flush()

    return blocks


def _macos_link_local_ifaces(blocks: Dict[str, str]) -> List[str]:
    """Return active interfaces with a 169.254/16 IPv4 address (macOS)."""
    ifaces: List[str] = []
    for name, block in blocks.items():
        if "status: active" in block and re.search(r"\n\s+inet 169\.254\.", block):
            ifaces.append(name)
    return _dedupe_keep_order(ifaces)


def _macos_usb_ifaces() -> List[str]:
    """Return interface names for macOS hardware ports that look like USB networking."""
    try:
        out = _run_quiet(["networksetup", "-listallhardwareports"])
    except Exception:
        return []

    ifaces: List[str] = []
    cur_hw_port: str | None = None
    for raw in out.splitlines():
        line = raw.strip()
        if not line:
            cur_hw_port = None
            continue
        if line.startswith("Hardware Port:"):
            cur_hw_port = line.split(":", 1)[1].strip()
            continue
        if line.startswith("Device:") and cur_hw_port:
            device = line.split(":", 1)[1].strip()
            hw = cur_hw_port.lower()
            if "usb" in hw or "rndis" in hw or "ethernet gadget" in hw:
                ifaces.append(device)
    return _dedupe_keep_order(ifaces)


def _macos_iface_ipv4(block: str) -> List[Tuple[str, int]]:
    """Return (ip, prefixlen) tuples parsed from an ifconfig block."""
    out: List[Tuple[str, int]] = []
    for line in block.splitlines():
        m = _MACOS_IFCONFIG_INET_RE.match(line)
        if not m:
            continue
        ip = m.group("ip").strip()
        netmask = m.group("netmask").strip().lower()
        try:
            if netmask.startswith("0x"):
                mask_int = int(netmask, 16) & 0xFFFFFFFF
                mask_bytes = mask_int.to_bytes(4, "big")
                mask = ".".join(str(b) for b in mask_bytes)
                prefix = ipaddress.IPv4Network(f"0.0.0.0/{mask}").prefixlen
            else:
                prefix = ipaddress.IPv4Network(f"0.0.0.0/{netmask}").prefixlen
        except Exception:
            continue
        out.append((ip, int(prefix)))
    return out


def _peer_ip_guesses(ip: str, prefixlen: int) -> List[str]:
    """Guess likely peer IPs on a point-to-point / small USB subnet."""
    try:
        addr = ipaddress.IPv4Address(ip)
        network = ipaddress.IPv4Network(f"{ip}/{prefixlen}", strict=False)
    except Exception:
        return []

    # Never guess on loopback or huge public networks.
    if addr.is_loopback or (not addr.is_private and not addr.is_link_local):
        return []

    candidates: List[ipaddress.IPv4Address] = []

    # If the subnet is small, just try the other host addresses.
    try:
        hosts = list(network.hosts())
        if len(hosts) <= 16:
            candidates.extend([h for h in hosts if h != addr])
        else:
            addr_int = int(addr)
            for delta in (-1, 1):
                try:
                    other = ipaddress.IPv4Address(addr_int + delta)
                except Exception:
                    continue
                if other != addr and other in network:
                    candidates.append(other)
            # Also try the first couple of usable addresses in the subnet.
            for extra in (1, 2):
                other = ipaddress.IPv4Address(int(network.network_address) + extra)
                if other != addr and other in network:
                    candidates.append(other)
    except Exception:
        return []

    return _dedupe_keep_order(str(c) for c in candidates)


def _arp_candidates() -> List[str]:
    """Return discovery candidates from local neighbor/ARP table."""
    candidates: List[str] = []

    # macOS / BSD
    try:
        blocks = _macos_ifconfig_blocks()
        ifaces = _dedupe_keep_order([*_macos_usb_ifaces(), *_macos_link_local_ifaces(blocks)])
        arp_cmds = [["arp", "-a", "-i", iface] for iface in ifaces] or [["arp", "-a"]]

        # When the connection is direct USB, ARP may be empty until we probe. Seed
        # candidates based on the local USB interface IP configuration.
        for iface in ifaces:
            block = blocks.get(iface)
            if not block or "status: active" not in block:
                continue
            for ip, prefix in _macos_iface_ipv4(block):
                candidates.extend(_peer_ip_guesses(ip, prefix))

        for cmd in arp_cmds:
            out = _run_quiet(cmd)
            for line in out.splitlines():
                if "incomplete" in line.lower():
                    continue
                m = _ARP_HOST_IP_RE.search(line)
                if not m:
                    continue
                host = m.group("host").strip()
                ip = m.group("ip").strip()
                # Prefer link-local / private IPs for USB connections, and avoid adding both
                # hostname and IP for the same ARP line (which would look like "multiple robots").
                try:
                    ip_addr = ipaddress.IPv4Address(ip)
                except Exception:
                    continue
                if ip_addr.is_link_local or ip_addr.is_private:
                    candidates.append(ip)
                elif host.endswith(".local"):
                    candidates.append(host)
        return _dedupe_keep_order(candidates)
    except Exception:
        pass

    # Linux (iproute2)
    try:
        try:
            out = _run_quiet(["ip", "-o", "link", "show"])
            usb_ifaces: List[str] = []
            for line in out.splitlines():
                # Example: "2: enx001122...: <BROADCAST,...>"
                m = re.match(r"^\d+:\s+(?P<name>[^:@]+)", line)
                if not m:
                    continue
                name = m.group("name").strip()
                lower = name.lower()
                if lower.startswith(("usb", "enx", "rndis")):
                    usb_ifaces.append(name)
            usb_ifaces = _dedupe_keep_order(usb_ifaces)
        except Exception:
            usb_ifaces = []

        for iface in usb_ifaces:
            try:
                out = _run_quiet(["ip", "-4", "neigh", "show", "dev", iface])
                for line in out.splitlines():
                    parts = line.split()
                    if not parts:
                        continue
                    ip = parts[0].strip()
                    if not _IPV4_RE.match(ip):
                        continue
                    try:
                        ip_addr = ipaddress.IPv4Address(ip)
                    except Exception:
                        continue
                    if ip_addr.is_link_local or ip_addr.is_private:
                        candidates.append(ip)
            except Exception:
                pass
            try:
                out = _run_quiet(["ip", "-o", "-4", "addr", "show", "dev", iface])
                for line in out.splitlines():
                    m = re.search(r"\binet\s+(?P<ip>\d+\.\d+\.\d+\.\d+)/(?P<prefix>\d+)", line)
                    if not m:
                        continue
                    candidates.extend(_peer_ip_guesses(m.group("ip"), int(m.group("prefix"))))
            except Exception:
                pass

        if candidates:
            return _dedupe_keep_order(candidates)

        out = _run_quiet(["ip", "neigh"])
        for line in out.splitlines():
            parts = line.split()
            if not parts:
                continue
            ip = parts[0]
            if _IPV4_RE.match(ip) and ip.startswith("169.254."):
                candidates.append(ip)
        return _dedupe_keep_order(candidates)
    except Exception:
        return []


def _probe_health(host: str, port: int, api_version: str, timeout_seconds: float) -> bool:
    url = f"http://{host}:{port}/health"
    req = url_request.Request(url, headers={"opentrons-version": api_version})
    try:
        with url_request.urlopen(req, timeout=timeout_seconds) as resp:
            return 200 <= int(getattr(resp, "status", 0) or 0) < 300
    except (url_error.URLError, socket.timeout, ValueError):
        return False


def _resolve(host_arg: str, port: int, api_version: str, timeout_seconds: float, pick_first: bool) -> str:
    explicit = host_arg.strip()
    if explicit:
        if _probe_health(explicit, port, api_version, timeout_seconds):
            return explicit
        raise RuntimeError(f"Unable to reach OT-2 robot-server at {explicit}:{port} (/health).")

    # Probe link-local / ARP-derived candidates first to avoid slow DNS timeouts on
    # networks where opentrons.local is not resolvable.
    candidates = _arp_candidates()
    reachable = [c for c in candidates if _probe_health(c, port, api_version, timeout_seconds)]
    if reachable:
        if len(reachable) > 1 and not pick_first:
            raise RuntimeError(
                "Multiple reachable OT-2 hosts found; pass --host to select one:\n  "
                + "\n  ".join(reachable)
            )
        return reachable[0]

    mdns_default = "opentrons.local"
    if _probe_health(mdns_default, port, api_version, timeout_seconds):
        return mdns_default

    raise RuntimeError(
        "No reachable OT-2 robot-server found. "
        "Connect via USB (USB-to-ethernet adapter or direct USB 2.0) and/or pass --host HOST."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="", help="OT-2 host/IP to verify and use")
    parser.add_argument("--port", type=int, default=31950, help="robot-server port (default: 31950)")
    parser.add_argument("--api-version", default="2", help="opentrons-version header value (default: 2)")
    parser.add_argument("--timeout", type=float, default=2.0, help="probe timeout seconds (default: 2.0)")
    parser.add_argument(
        "--pick-first",
        action="store_true",
        help="If multiple reachable hosts are found, choose the first instead of failing.",
    )
    args = parser.parse_args()

    try:
        host = _resolve(
            host_arg=args.host,
            port=args.port,
            api_version=str(args.api_version),
            timeout_seconds=float(args.timeout),
            pick_first=bool(args.pick_first),
        )
    except Exception as exc:
        _eprint(f"[ERROR] {exc}")
        raise SystemExit(2)

    print(host, flush=True)


if __name__ == "__main__":
    main()
