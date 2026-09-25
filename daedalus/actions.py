# -*- coding: utf-8 -*-
"""Actions from the detail card: ping, traceroute, port check, Wake-on-LAN, port scan.

Checking things without a command line. Three rules every action follows:

1. **Addresses only - no names, no free text.** The target is parsed as an IPv4
   address and passed as a separate argument - there is no place where user input
   is glued into a command line.
2. **Own networks only.** Everything outside the configured networks
   (`actions.own_networks`) is refused. Networks listed in
   `actions.ping_only_networks` (e.g. a customer or guest network) allow ping and
   nothing else - that restriction is stated explicitly so that a later widening
   of the own networks does not silently lift it.
3. **Little at a time.** At most two actions at once, each with a fixed timeout.
   A double click must not flood a network.

The port scan (nmap) is at the bottom and switched off until someone enables it.
"""
from __future__ import annotations

import ipaddress
import socket
import subprocess
import threading
import time

OWN_NETWORKS: tuple = (ipaddress.ip_network("172.16.0.0/16"),)
PING_ONLY_NETWORKS: tuple = ()

_CONCURRENT = threading.BoundedSemaphore(2)


def configure(own_networks: list[str], ping_only_networks: list[str] = ()) -> None:
    """Set the allowed networks (from `config.py`)."""
    global OWN_NETWORKS, PING_ONLY_NETWORKS
    OWN_NETWORKS = tuple(ipaddress.ip_network(n) for n in own_networks)
    PING_ONLY_NETWORKS = tuple(ipaddress.ip_network(n) for n in ping_only_networks)


class Refused(ValueError):
    """An action that must not run - with a reason for the person."""


def check_target(target: str, action: str) -> ipaddress.IPv4Address:
    try:
        ip = ipaddress.IPv4Address(target.strip())
    except (ipaddress.AddressValueError, AttributeError):
        raise Refused("target must be an IPv4 address") from None
    if action != "ping" and any(ip in n for n in PING_ONLY_NETWORKS):
        raise Refused("ping-only network - only ping is allowed here")
    if not any(ip in n for n in OWN_NETWORKS) and not any(ip in n for n in PING_ONLY_NETWORKS):
        own = ", ".join(str(n) for n in OWN_NETWORKS)
        raise Refused(f"only addresses from the own networks ({own})")
    if ip.is_multicast or ip.is_unspecified or str(ip).endswith(".255"):
        raise Refused("no broadcast or collective address")
    return ip


def _execute(command: list[str], timeout: int) -> dict:
    if not _CONCURRENT.acquire(blocking=False):
        raise Refused("two actions are already running - wait a moment")
    started = time.monotonic()
    try:
        r = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        output, code = (r.stdout + r.stderr).strip(), r.returncode
    except subprocess.TimeoutExpired as t:
        output = ((t.stdout or b"").decode(errors="replace") if isinstance(t.stdout, bytes)
                  else (t.stdout or "")) + "\n... timeout reached"
        code = -1
    except FileNotFoundError:
        raise Refused(f"{command[0]} is not available in the image") from None
    finally:
        _CONCURRENT.release()
    return {"ok": code == 0, "code": code, "output": output[-4000:],
            "duration_ms": int((time.monotonic() - started) * 1000)}


def ping(target: str) -> dict:
    ip = check_target(target, "ping")
    result = _execute(["ping", "-c", "4", "-i", "0.3", "-W", "1", str(ip)], 10)
    # Summary for the card: "4/4 · 0.4 ms"
    lines = result["output"].splitlines()
    loss = next((line for line in lines if "packet loss" in line), "")
    rtt = next((line for line in lines if "min/avg/max" in line), "")
    result["summary"] = " · ".join(x for x in (loss.strip(), rtt.split("=")[-1].strip()) if x)
    return result


def traceroute(target: str) -> dict:
    ip = check_target(target, "traceroute")
    # -I (ICMP instead of UDP): from inside the container the UDP answers of the
    # hops did not make it back through the NAT of the Docker bridge - every hop
    # was a star. With ICMP they answer.
    return _execute(["traceroute", "-I", "-n", "-w", "1", "-q", "1", "-m", "12", str(ip)], 20)


def check_port(target: str, port: int) -> dict:
    ip = check_target(target, "port")
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise Refused("port must be between 1 and 65535")
    if not _CONCURRENT.acquire(blocking=False):
        raise Refused("two actions are already running - wait a moment")
    started = time.monotonic()
    try:
        with socket.create_connection((str(ip), port), timeout=2):
            ok, text = True, f"{ip}:{port} is open"
    except socket.timeout:
        ok, text = False, f"{ip}:{port} does not answer (timeout - filtered or device off)"
    except ConnectionRefusedError:
        ok, text = False, f"{ip}:{port} is closed (connection refused)"
    except OSError as e:
        ok, text = False, f"{ip}:{port} not reachable: {e.strerror or e}"
    finally:
        _CONCURRENT.release()
    return {"ok": ok, "output": text, "summary": text,
            "duration_ms": int((time.monotonic() - started) * 1000)}


def wake_on_lan(mac: str, ip: str | None = None) -> dict:
    """Magic packet to the device - first by unicast to its last address.

    Measured with a documentation MAC and tcpdump in two VLANs: **broadcasts
    arrived nowhere**, not even in the own network - they do not leave the Docker
    bridge of the container, and a directed broadcast into another VLAN would be
    dropped by the firewall anyway. **Unicast to the IP arrived**, across the VLAN
    border.

    A sleeping device receives the packet as long as the firewall still has its MAC
    in the ARP table; the switch then floods it even if the port has forgotten the
    MAC. Once the ARP entry has expired nothing arrives - that is why this action
    reports "sent", never "woken up".
    """
    parts = mac.lower().replace("-", ":").split(":")
    if len(parts) != 6 or not all(len(p) == 2 and all(c in "0123456789abcdef" for c in p)
                                  for p in parts):
        raise Refused("MAC in the format aa:bb:cc:dd:ee:ff")
    if not ip:
        raise Refused("without a known address the packet does not get out of the container")
    address = check_target(ip, "wol")
    net = ipaddress.ip_network(f"{address}/24", strict=False)
    targets = [str(address), str(net.broadcast_address)]
    packet = b"\xff" * 6 + bytes.fromhex("".join(parts)) * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for t in targets:
            try:
                s.sendto(packet, (t, 9))
            except OSError:
                pass                      # the broadcast may fail, unicast is what counts
    text = (f"magic packet sent to {address} (unicast) - it arrives as long as the "
            f"firewall still has the device in its ARP table; nobody reports whether it wakes up")
    return {"ok": True, "output": text, "summary": "sent"}


# --- port scan (nmap) -------------------------------------------------------------
#
# The most delicate part, therefore with more locks than the others:
#
# - **Off until someone enables it** (`DAEDALUS_SCAN_ENABLED=yes`). A scan from the
#   container goes through the firewall, and its intrusion prevention may block the
#   scanning host - that would be the node the service is running on. Put an
#   exception there BEFORE the first scan.
# - Exactly one target, exactly one scan at a time, no ranges.
# - `-sT` (connect scan): needs no special privileges and is the politest.
# - The 100 most common ports, 90 seconds timeout per host, cancellable.
# - Ping-only networks: refused (check_target only allows ping there).

import os
import re
import tempfile
import uuid
import xml.etree.ElementTree as ET

_SCANS: dict[str, dict] = {}
_SCAN_LOCK = threading.Lock()


def scan_enabled() -> bool:
    return os.environ.get("DAEDALUS_SCAN_ENABLED", "").strip().lower() in ("yes", "true", "1")


def _running_scan() -> dict | None:
    return next((s for s in _SCANS.values() if s["state"] == "running"), None)


def start_scan(target: str) -> dict:
    if not scan_enabled():
        raise Refused("port scan is disabled until the firewall has an exception for the "
                      "scanning node (DAEDALUS_SCAN_ENABLED)")
    ip = check_target(target, "scan")
    with _SCAN_LOCK:
        if _running_scan():
            raise Refused("a scan is already running - wait or cancel it first")
        ident = uuid.uuid4().hex[:12]
        xml = tempfile.NamedTemporaryFile(prefix="scan-", suffix=".xml", delete=False)
        xml.close()
        command = ["nmap", "-sT", "-Pn", "--top-ports", "100", "-T3",
                   "--host-timeout", "90s", "--stats-every", "2s", "-oX", xml.name, str(ip)]
        try:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True)
        except FileNotFoundError:
            raise Refused("nmap is not available in the image") from None
        entry = {"id": ident, "target": str(ip), "state": "running", "progress": 0,
                 "started": time.monotonic(), "process": process, "xml": xml.name,
                 "ports": [], "message": ""}
        _SCANS[ident] = entry
    threading.Thread(target=_follow_scan, args=(entry,), daemon=True).start()
    return scan_status(ident)


def _follow_scan(entry: dict) -> None:
    process = entry["process"]
    for line in process.stdout:
        m = re.search(r"About ([\d.]+)% done", line)
        if m:
            entry["progress"] = min(99, int(float(m.group(1))))
    process.wait()
    if entry["state"] == "cancelled":
        return
    try:
        root = ET.parse(entry["xml"]).getroot()
        for p in root.iter("port"):
            state = p.find("state")
            service = p.find("service")
            if state is not None and state.get("state") == "open":
                entry["ports"].append({
                    "port": int(p.get("portid")), "protocol": p.get("protocol"),
                    "service": service.get("name") if service is not None else ""})
        entry["state"] = "done" if process.returncode == 0 else "error"
        if process.returncode != 0:
            entry["message"] = f"nmap ended with code {process.returncode}"
    except (ET.ParseError, OSError) as e:
        entry["state"], entry["message"] = "error", f"result not readable: {e}"
    finally:
        entry["progress"] = 100
        try:
            os.unlink(entry["xml"])
        except OSError:
            pass


def scan_status(ident: str) -> dict:
    s = _SCANS.get(ident)
    if not s:
        raise Refused("unknown scan")
    return {"id": s["id"], "target": s["target"], "state": s["state"],
            "progress": s["progress"], "ports": s["ports"], "message": s["message"],
            "seconds": int(time.monotonic() - s["started"])}


def cancel_scan(ident: str) -> dict:
    s = _SCANS.get(ident)
    if not s:
        raise Refused("unknown scan")
    if s["state"] == "running":
        s["state"] = "cancelled"
        s["process"].terminate()
        try:
            s["process"].wait(timeout=5)
        except subprocess.TimeoutExpired:
            s["process"].kill()
    return scan_status(ident)
