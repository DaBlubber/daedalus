# -*- coding: utf-8 -*-
"""Collector: MAC table and LLDP neighbours of a switch.

**Two collectors, not one** - because they differ in both things that matter:

| | MAC table | LLDP neighbours |
|---|---|---|
| interval | 3-5 minutes (ages out after ~5) | 30 minutes |
| cheap precheck | **none** | `ifLastChange` |
| size on a 48-port switch | ~180 entries | ~4 entries |

The difference in the precheck is the key point: **a MAC wanders without a link
changing** - a laptop that switches from Wi-Fi to cable leaves `ifLastChange`
untouched. The neighbourhood is the opposite: it can only change when someone
re-plugs a cable, and then `ifLastChange` changes. Applying the precheck to the
MAC table loses moves.

## The most important derivation

**A MAC on an access port is plugged in there. A MAC on an uplink is further
behind.** An uplink sees every MAC behind it - without separating the two, the
tool claims 140 devices are plugged into one uplink port.

How an uplink is recognised: **it has an LLDP neighbour.** That is the most
reliable information there is, because it comes from the neighbour device itself.
The MAC collector therefore needs the neighbour list - it receives it as a set and
does not have to fetch it itself.

**Port channels come on top** (`Po1`, `Po2`). On a real switch 52 of 60 MACs sat
on `Po1` - the uplink was a bundle of two cables, and the MAC table names the
channel as the port, not its members. LLDP, on the other hand, reports the
**members** (gi25, gi26). Following only the LLDP list treats `Po1` as an access
port and claims 52 devices are plugged into it.

*Known limitation:* a port channel always counts as an uplink here. A server with
two bonded cables is therefore not found. If you need that, the channel membership
(`dot3adAggPortListPorts`) has to be read as well.
"""
from __future__ import annotations

import subprocess

from .model import Observation, Relation, Source

OID = {
    "fdb":        "1.3.6.1.2.1.17.7.1.2.2.1.2",     # dot1qTpFdbPort
    "bridgeport": "1.3.6.1.2.1.17.1.4.1.2",         # dot1dBasePortIfIndex
    "ifname":     "1.3.6.1.2.1.31.1.1.1.1",         # ifName
    "lldpname":   "1.0.8802.1.1.2.1.4.1.1.9",       # lldpRemSysName
    "lldpport":   "1.0.8802.1.1.2.1.4.1.1.7",       # lldpRemPortId
    "lldplocal":  "1.0.8802.1.1.2.1.3.7.1.3",       # lldpLocPortId
    "lastchange": "1.3.6.1.2.1.2.2.1.9",            # ifLastChange
}

def split_oid(output: str, oid: str) -> dict[str, str]:
    """`{index: value}` for all lines of one OID. Anything unclear is dropped.

    Cut at the known prefix, not guessed by a pattern: an OID and its index both
    consist only of digits and dots, a pattern cannot find the border between them.
    """
    prefix = "." + oid + "."
    out: dict[str, str] = {}
    for line in output.splitlines():
        s = line.strip()
        if not s.startswith(prefix) or "=" not in s:
            continue
        left, right = s.split("=", 1)
        out[left.strip()[len(prefix):]] = right.strip().strip('"').strip()
    return out


def mac_from_index(index: str) -> str:
    """The FDB index is `<vlan>.<six decimal bytes>`.

    `1.60.222.152.72.101.20` becomes `3c:de:98:48:65:14` - the same notation as the
    ARP collector, otherwise a device is in the inventory twice.
    """
    parts = index.split(".")
    if len(parts) < 7:
        return ""
    return ":".join(f"{int(x):02x}" for x in parts[-6:])


def is_port_channel(port_name: str) -> bool:
    """Is this a port channel (`Po1`) rather than a single port?

    The MAC table names the channel for a bundle, LLDP names the members. Without
    this rule the channel counts as an access port - and 52 devices end up plugged
    into one socket.
    """
    p = port_name.strip().lower()
    return p.startswith("po") and p[2:].isdigit()


def vlan_from_index(index: str) -> str:
    parts = index.split(".")
    return parts[0] if len(parts) >= 7 else ""


class _Switch:
    """Shared part: talking to a switch."""

    def __init__(self, switch: str, target: str, community: str,
                 timeout: int = 8, caller=None) -> None:
        self.switch = switch
        self.target = target
        self.community = community
        self.timeout = timeout
        self._call = caller or self._walk

    def _walk(self, oid: str) -> str:
        result = subprocess.run(
            ["snmpbulkwalk", "-v2c", "-c", self.community, "-Cr40", "-OQn",
             "-t", str(self.timeout), "-r", "1", self.target, oid],
            capture_output=True, text=True, timeout=self.timeout * 5)
        if result.returncode != 0:
            raise RuntimeError(
                f"{self.switch} ({self.target}) does not answer: "
                f"{(result.stderr or 'no output').strip()[:160]}")
        return result.stdout

    def port_names(self) -> dict[str, str]:
        """`{ifIndex: 'gi12'}`"""
        return split_oid(self._call(OID["ifname"]), OID["ifname"])

    def neighbor_ports(self) -> dict[str, tuple[str, str]]:
        """`{local port name: (neighbour name, neighbour port)}` from LLDP.

        The LLDP index is `<time mark>.<local port number>.<sequence>`. The local
        port number equals the `ifIndex` on this hardware - and the name is
        resolved through it, **not** through `lldpLocPortId`.

        Why that matters: `lldpLocPortId` has a different subtype per port.
        Measured on a real switch:

            subtype 5 (interfaceName)  ->  "gi25"        switch to switch
            subtype 3 (macAddress)     ->  "C8 00 84 ..."  access point

        Taking `lldpLocPortId` blindly as the port name loses **exactly the ports
        the access points are plugged into**. The consequence was visible on the
        first real run: 131 Wi-Fi clients were reported as "moved" from an access
        point to a switch port, because that port did not count as an uplink. An
        AP port is an uplink like any other - devices are behind it, not on it.
        """
        names = split_oid(self._call(OID["lldpname"]), OID["lldpname"])
        ports = split_oid(self._call(OID["lldpport"]), OID["lldpport"])
        local = split_oid(self._call(OID["lldplocal"]), OID["lldplocal"])
        ifname = self.port_names()

        out: dict[str, tuple[str, str]] = {}
        for index, neighbor in names.items():
            parts = index.split(".")
            if len(parts) < 2 or not neighbor:
                continue                      # no name, no neighbour
            nr = parts[1]
            # ifName first, lldpLocPortId only as a fallback.
            port_name = ifname.get(nr) or local.get(nr)
            if not port_name or " " in port_name:
                continue                      # a MAC is not a port name
            out[port_name] = (neighbor, ports.get(index, ""))
        return out


class SwitchNeighbors(_Switch):
    """LLDP: who is connected to which port of this switch.

    **This is where the cheap precheck applies.** The neighbourhood can only
    change when someone re-plugs a cable - and then `ifLastChange` changes on at
    least one port. A walk over ~50 timestamps is much cheaper than four walks
    for the neighbour list.
    """

    def __init__(self, *a, **k) -> None:
        super().__init__(*a, **k)
        self._last_state: str | None = None
        # Last seen uplink list. The MAC collector reads it from here - in the
        # service both run at different intervals (30 min vs. 5 min), and the MAC
        # table must not ask for neighbours again on every run.
        self.last_uplinks: set[str] | None = None
        self.source = Source(
            name=f"lldp-{self.switch}",
            responsible_for=frozenset({Relation.CONNECTION}),
            # A neighbour missing once is usually a restarted switch. Missing three
            # times is a cable.
            missing_threshold=3,
        )

    def _state(self) -> str:
        raw = self._call(OID["lastchange"])
        values = split_oid(raw, OID["lastchange"])
        return "|".join(f"{k}={v}" for k, v in sorted(values.items()))

    def precheck_unchanged(self) -> bool:
        state = self._state()
        if not state:
            return False                       # read nothing -> rather collect
        unchanged = state == self._last_state
        self._last_state = state
        return unchanged

    def uplink_ports(self) -> set[str]:
        """All ports through which the network continues: those with neighbours."""
        return set(self.neighbor_ports())

    def collect(self) -> list[Observation]:
        out = []
        found = self.neighbor_ports()
        self.last_uplinks = set(found)
        for port_name, (neighbor, neighbor_port) in found.items():
            target = f"{neighbor}:{neighbor_port}" if neighbor_port else neighbor
            out.append(Observation(Relation.CONNECTION,
                                   f"{self.switch}:{port_name}", "connection", target))
        if not out:
            raise RuntimeError(f"{self.switch}: no LLDP neighbours, that cannot be")
        return out


class SwitchMacs(_Switch):
    """The MAC table: which device is plugged into which access port.

    **No cheap precheck** - a MAC wanders without a link changing. Asking
    `ifLastChange` here loses exactly the moves the tool was built for.
    """

    def __init__(self, *a, uplinks=None, neighbors=None, **k) -> None:
        super().__init__(*a, **k)
        # Either set explicitly (one-off run, tests) or read from the neighbour
        # collector (service). Without uplink knowledge every port counts as an
        # access port - and half the network wrongly hangs off an uplink. Setting
        # it is therefore mandatory, not optional.
        self._uplinks: set[str] = set(uplinks or ())
        self.neighbors = neighbors
        self.source = Source(
            name=f"fdb-{self.switch}",
            responsible_for=frozenset({Relation.ATTACHMENT}),
            # A quiet device drops out of the MAC table after 300 s of aging and
            # reappears with the next packet. With a threshold of 2 (10 minutes)
            # some devices reported "disconnected / back" 30 times a night. Twelve
            # runs are an hour: a really unplugged cable shows up in the list
            # later, but not between a hundred false ones.
            missing_threshold=12,
        )

    @property
    def uplinks(self) -> set[str]:
        if self.neighbors is not None and self.neighbors.last_uplinks is not None:
            return self.neighbors.last_uplinks
        return self._uplinks

    @uplinks.setter
    def uplinks(self, value) -> None:
        self._uplinks = set(value or ())

    def precheck_unchanged(self) -> bool:
        return False

    def collect(self) -> list[Observation]:
        if self.neighbors is not None and self.neighbors.last_uplinks is None:
            # The neighbour collector has not run yet. Better report nothing at all
            # than attach every device behind the uplink to the uplink - as an
            # error this makes nothing disappear (rule 1).
            raise RuntimeError(
                f"{self.switch}: uplinks unknown yet, LLDP has not run")
        names = self.port_names()
        bridge_port = split_oid(self._call(OID["bridgeport"]), OID["bridgeport"])
        fdb = split_oid(self._call(OID["fdb"]), OID["fdb"])
        if not fdb:
            raise RuntimeError(f"{self.switch}: MAC table empty, that cannot be")

        out: dict[str, Observation] = {}
        for index, bport in fdb.items():
            mac = mac_from_index(index)
            if not mac:
                continue
            ifindex = bridge_port.get(bport, bport)
            port_name = names.get(ifindex)
            if not port_name:
                continue                       # bundle or unknown port
            if port_name in self.uplinks or is_port_channel(port_name):
                # Through an uplink you see everything behind it. That is not an
                # attachment but transit traffic.
                continue
            out[mac] = Observation(Relation.ATTACHMENT, mac, "attachment",
                                   f"{self.switch}:{port_name}")
        return list(out.values())
