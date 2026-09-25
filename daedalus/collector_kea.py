# -*- coding: utf-8 -*-
"""DHCP: leases and configuration from Kea.

Why Kea and not DNS: in a typical home network the DNS server (e.g. Pi-hole)
knows few or no PTR records. Kea, on the other hand, knows for most leases the
name the device itself sends when requesting its address, and the configuration
lists the reservations with a maintained name.

Two collectors, because the two parts age differently and differ in how cheap
they are to ask:

| Collector | Source | Precheck |
|---|---|---|
| `KeaLeases` | control agent / HA listener `lease4-get-all`, one POST | none |
| `KeaConfig` | the deployed `kea-dhcp4.conf`, fetched over HTTP | conditional GET, 304 |

Only **attributes** are delivered, no addresses. Whether a device is present right
now is judged by the ARP table; a second responsible source for the same relation
would open and close intervals against each other as soon as a lease lives longer
than its ARP entry.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .collector_arp import is_random_mac, normalize_mac
from .model import Observation, Relation, Source

# Your own DNS domain; names in it are shortened (`laptop.example.com` -> `laptop`).
# Set from the configuration (`dhcp.local_domain`).
LOCAL_DOMAIN = "example.com"

Caller = Callable[[str], Any]


def clean_hostname(raw: str) -> str:
    """`device-08ef.example.com.` -> `device-08ef`; foreign domains stay.

    `laptop.corp.example.net.` is a company laptop - that it does not belong to
    us is exactly the information you want to see.
    """
    name = (raw or "").strip().rstrip(".")
    if LOCAL_DOMAIN and name.lower().endswith("." + LOCAL_DOMAIN.lower()):
        name = name[: -len(LOCAL_DOMAIN) - 1]
    return name


# --- leases -----------------------------------------------------------------------

def parse_leases(response: Any) -> list[Observation]:
    """Name and assigned address per MAC from the Kea response."""
    entry = response[0] if isinstance(response, list) and response else response
    if not isinstance(entry, dict) or entry.get("result") not in (0, 3):
        # 3 means "empty" - in a network with dozens of devices that is an outage.
        raise RuntimeError(f"Kea did not answer successfully: {str(entry)[:120]}")
    leases = (entry.get("arguments") or {}).get("leases")
    if not isinstance(leases, list) or not leases:
        raise RuntimeError("Kea returned no leases")

    out: dict[tuple[str, str], Observation] = {}
    for lease in leases:
        if not isinstance(lease, dict) or lease.get("state", 0) != 0:
            continue                      # 1 = declined, 2 = expired
        mac = normalize_mac(str(lease.get("hw-address", "")))
        if len(mac) != 17:
            continue
        # Phones with a randomised MAC get their own lease per network. Its expiry
        # is not news. Same rule as in the Wi-Fi collector.
        vol = is_random_mac(mac)
        name = clean_hostname(str(lease.get("hostname", "")))
        if name:
            out[(mac, "dhcp_name")] = Observation(Relation.ATTRIBUTE, mac, "dhcp_name", name,
                                                  volatile=vol)
        # The assigned address as an attribute: it says "came from the pool", not
        # "is present right now". The expiry time is left out - it changes with
        # every renewal and opened a new interval each time.
        if lease.get("ip-address"):
            out[(mac, "dhcp_lease")] = Observation(Relation.ATTRIBUTE, mac, "dhcp_lease",
                                                   str(lease["ip-address"]), volatile=vol)
    return [out[k] for k in sorted(out)]


class KeaLeases:
    """Reads the lease table from Kea (tries the nodes in order, e.g. HA pair)."""

    def __init__(self, nodes: tuple[str, ...], name: str = "kea",
                 timeout: int = 8, caller: Caller | None = None) -> None:
        self.nodes = nodes
        self.timeout = timeout
        self._caller = caller or self._fetch
        self.source = Source(
            name=name,
            responsible_for=frozenset({Relation.ATTRIBUTE}),
            # Leases live for hours; a name missing twice is really gone.
            missing_threshold=2,
        )

    def _fetch(self, url: str) -> Any:
        body = json.dumps({"command": "lease4-get-all", "service": ["dhcp4"]}).encode()
        request = Request(url, data=body, method="POST",
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=self.timeout) as response:  # noqa: S310
            return json.load(response)

    def precheck_unchanged(self) -> bool:
        # Kea has no cheap question - the whole table already is the cheap one.
        return False

    def collect(self) -> list[Observation]:
        errors = []
        for url in self.nodes:
            try:
                return parse_leases(self._caller(url))
            except Exception as e:  # noqa: BLE001
                errors.append(f"{url}: {e}")
        raise RuntimeError("; ".join(errors))


# --- configuration: networks, pools, reservations ---------------------------------
#
# Behind an HA listener Kea often refuses `config-get` (only HA and lease commands
# are allowed there). The configuration is usually versioned anyway - so the file
# that gets deployed is read, e.g. the raw URL in your Git server.
#
# The ETag of the file is the cheapest precheck imaginable: a conditional GET
# returns 304 and zero bytes as long as nobody touches the DHCP configuration.

_INCLUDE = re.compile(r"<\?include[^>]*\?>")
_LINE_COMMENT = re.compile(r"(?m)^\s*(#|//).*$")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)


def read_config(text: str) -> dict:
    """Kea JSON with comments and includes into a dict.

    Includes point to files that only exist on the nodes (interfaces, own server
    name) - irrelevant for the overview, they become `null`.
    """
    text = _BLOCK_COMMENT.sub("", _LINE_COMMENT.sub("", text))
    text = _INCLUDE.sub("null", text)
    data = json.loads(text)
    if not isinstance(data, dict) or not isinstance(data.get("Dhcp4"), dict):
        raise RuntimeError("no Dhcp4 configuration")
    return data["Dhcp4"]


def parse_config(text: str) -> list[Observation]:
    """Networks with pools and router, reservations per MAC."""
    d = read_config(text)
    networks = list(d.get("subnet4") or [])
    for shared in d.get("shared-networks") or []:
        networks.extend(shared.get("subnet4") or [])
    if not networks:
        raise RuntimeError("configuration without networks")

    out: list[Observation] = []
    for net in networks:
        ident = f"net:{net['subnet']}"
        pools = ",".join(p["pool"].replace(" ", "") for p in net.get("pools") or []
                         if isinstance(p, dict) and p.get("pool"))
        if pools:
            out.append(Observation(Relation.ATTRIBUTE, ident, "dhcp_pools", pools))
        for option in net.get("option-data") or []:
            if option.get("name") == "routers" and option.get("data"):
                out.append(Observation(Relation.ATTRIBUTE, ident, "dhcp_router",
                                       str(option["data"])))
        for r in net.get("reservations") or []:
            mac = normalize_mac(str(r.get("hw-address", "")))
            if len(mac) != 17:
                continue
            if r.get("ip-address"):
                out.append(Observation(Relation.ATTRIBUTE, mac, "dhcp_reservation",
                                       str(r["ip-address"])))
            name = clean_hostname(str(r.get("hostname", "")))
            if name:
                out.append(Observation(Relation.ATTRIBUTE, mac, "reservation_name", name))
    return out


class KeaConfig:
    """Reads the deployed Kea configuration over HTTP."""

    def __init__(self, url: str, name: str = "dhcp-config",
                 timeout: int = 8, fetcher=None) -> None:
        self.url = url
        self.timeout = timeout
        self._fetcher = fetcher or self._fetch
        self._etag = ""
        self.source = Source(
            name=name,
            responsible_for=frozenset({Relation.ATTRIBUTE}),
            # A configuration does not age out: what is missing once was deleted.
            missing_threshold=1,
        )

    def _fetch(self, etag: str) -> tuple[int, str, str]:
        """(status, ETag, text). 304 means: unchanged since `etag`."""
        headers = {"If-None-Match": etag} if etag else {}
        try:
            with urlopen(Request(self.url, headers=headers),  # noqa: S310
                         timeout=self.timeout) as response:
                return (response.status, response.headers.get("ETag", ""),
                        response.read().decode("utf-8"))
        except HTTPError as e:
            if e.code == 304:
                return 304, etag, ""
            raise

    def precheck_unchanged(self) -> bool:
        if not self._etag:
            return False
        try:
            status, _etag, _text = self._fetcher(self._etag)
            return status == 304
        except Exception:  # noqa: BLE001
            return False

    def collect(self) -> list[Observation]:
        status, etag, text = self._fetcher("")
        if status != 200:
            raise RuntimeError(f"configuration not readable: HTTP {status}")
        observations = parse_config(text)
        self._etag = etag
        return observations
