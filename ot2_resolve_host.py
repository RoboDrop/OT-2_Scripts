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
_MDNS_OT2_NAME_RE = re.compile(r"^(OT2|OPENTRONS)", re.IGNORECASE)


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


def _format_url_host(address: str) -> str:
    """Return a URL-ready host string for IPv4 or IPv6 addresses.

    IPv4 passes through. Bare IPv6 is wrapped in brackets. IPv6 link-local
    with a zone id (``%eth0``) has its ``%`` percent-encoded to ``%25`` for
    RFC 6874 compliance.
    """
    raw = address.strip()
    if not raw:
        return raw
    # Already bracketed or a hostname.
    if raw.startswith("["):
        return raw
    # IPv4 dotted quad: pass through.
    if _IPV4_RE.match(raw):
        return raw
    # Hostnames (no colon) pass through.
    if ":" not in raw:
        return raw
    # Split off zone id for IPv6 link-local.
    core, _, zone = raw.partition("%")
    try:
        ipaddress.IPv6Address(core)
    except Exception:
        # Unknown token; return as-is so downstream errors are clear.
        return raw
    if zone:
        return f"[{core}%25{zone}]"
    return f"[{core}]"


def _run_dns_sd_with_timeout(args: Sequence[str], timeout_seconds: float) -> str:
    """Run a ``dns-sd`` command that would otherwise run forever, capturing output.

    macOS ``dns-sd`` does not exit on its own; kill it after ``timeout_seconds``
    and return whatever it printed.
    """
    try:
        proc = subprocess.run(
            list(args),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", errors="replace")
        return out
    except Exception:
        return ""
    return proc.stdout or ""


def _mdns_browse_http(timeout_seconds: float) -> List[str]:
    """Return mDNS ``_http._tcp`` instance names that look like OT-2 robots."""
    out = _run_dns_sd_with_timeout(
        ["dns-sd", "-B", "_http._tcp", "local."], timeout_seconds
    )
    names: List[str] = []
    for line in out.splitlines():
        tokens = line.split()
        if len(tokens) < 6:
            continue
        # Typical line:
        # "HH:MM:SS.mmm  Add        2  30 local.  _http._tcp.  OT2CEP20220228R03"
        if "Add" not in tokens[:6]:
            continue
        name = tokens[-1]
        if _MDNS_OT2_NAME_RE.match(name):
            names.append(name)
    return _dedupe_keep_order(names)


def _mdns_resolve(hostname: str, timeout_seconds: float) -> List[str]:
    """Resolve a ``.local`` hostname to URL-ready host strings via ``dns-sd``.

    The input may be a bare instance name (``OT2XYZ``), a short local hostname
    (``OT2XYZ.local``), or fully qualified (``OT2XYZ.local.``).
    """
    host = hostname.strip()
    if not host:
        return []
    if not host.endswith("."):
        if not host.endswith(".local"):
            host = f"{host}.local"
        host = f"{host}."
    out = _run_dns_sd_with_timeout(
        ["dns-sd", "-G", "v4v6", host], timeout_seconds
    )
    addrs: List[str] = []
    for line in out.splitlines():
        # Typical line:
        # "HH:MM:SS.mmm  Add  2  30  OT2XYZ.local.  FE80:0000:...%en8  120"
        # "HH:MM:SS.mmm  Add  2  30  OT2XYZ.local.  169.254.191.57  120"
        # Negative answers (``No Such Record``) contain sentinel 0.0.0.0 / ::
        # addresses and must be skipped.
        if " Add " not in line:
            continue
        if "No Such Record" in line or "No Such Name" in line:
            continue
        for token in line.split():
            if _IPV4_RE.match(token):
                try:
                    ipv4 = ipaddress.IPv4Address(token)
                except Exception:
                    continue
                if ipv4.is_unspecified or ipv4.is_multicast:
                    continue
                addrs.append(token)
                continue
            core, _, zone = token.partition("%")
            if ":" not in core:
                continue
            try:
                ipv6 = ipaddress.IPv6Address(core)
            except Exception:
                continue
            if ipv6.is_unspecified or ipv6.is_multicast:
                continue
            # Drop synthetic ``%<0>`` zone id that dns-sd emits for global addrs.
            if zone and (zone.startswith("<") or not re.match(r"^[A-Za-z0-9_-]+$", zone)):
                zone = ""
            addrs.append(core if not zone else f"{core}%{zone}")
    return _dedupe_keep_order(_format_url_host(a) for a in addrs)


def _mdns_candidates(timeout_seconds: float) -> List[str]:
    """Discover OT-2 candidate addresses via mDNS service browsing.

    Returns URL-ready host strings (IPv4 bare, IPv6 bracketed with ``%25`` zone
    encoding for link-local). Best-effort; returns ``[]`` on any failure.
    """
    # dns-sd is standard on macOS. On Linux, avahi-browse is the equivalent
    # but uses a different CLI; skip silently if unavailable.
    if sys.platform != "darwin":
        return []
    try:
        names = _mdns_browse_http(timeout_seconds)
    except Exception:
        return []
    if not names:
        return []
    out: List[str] = []
    for name in names:
        try:
            out.extend(_mdns_resolve(name, timeout_seconds))
        except Exception:
            continue
    # Also try the generic "opentrons.local" alias some robots publish.
    try:
        out.extend(_mdns_resolve("opentrons.local", timeout_seconds))
    except Exception:
        pass
    return _dedupe_keep_order(out)


def _probe_health(host: str, port: int, api_version: str, timeout_seconds: float) -> bool:
    url = f"http://{host}:{port}/health"
    req = url_request.Request(url, headers={"opentrons-version": api_version})
    try:
        with url_request.urlopen(req, timeout=timeout_seconds) as resp:
            return 200 <= int(getattr(resp, "status", 0) or 0) < 300
    except (url_error.URLError, socket.timeout, ValueError):
        return False


def _resolve(
    host_arg: str,
    port: int,
    api_version: str,
    timeout_seconds: float,
    pick_first: bool,
    mdns_timeout_seconds: float = 3.0,
) -> str:
    explicit = host_arg.strip()
    if explicit:
        if _probe_health(_format_url_host(explicit), port, api_version, timeout_seconds):
            return explicit
        raise RuntimeError(f"Unable to reach OT-2 robot-server at {explicit}:{port} (/health).")

    # Gather candidates from every cheap discovery source before probing. mDNS
    # is last because ``dns-sd`` has to be killed at a wall-clock timeout.
    candidates = _dedupe_keep_order([
        *_arp_candidates(),
        *_mdns_candidates(mdns_timeout_seconds),
    ])
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
        "--mdns-timeout",
        type=float,
        default=3.0,
        help="mDNS discovery timeout seconds (default: 3.0)",
    )
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
            mdns_timeout_seconds=float(args.mdns_timeout),
        )
    except Exception as exc:
        _eprint(f"[ERROR] {exc}")
        raise SystemExit(2)

    print(host, flush=True)


if __name__ == "__main__":
    main()
