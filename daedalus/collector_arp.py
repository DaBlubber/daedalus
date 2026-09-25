# -*- coding: utf-8 -*-
"""Collector: the ARP table of the firewall/router.

The most valuable single source of the whole tool. It answers **IP <-> MAC** for
everything that has talked in the last minutes - and it is the anchor the switch
MAC tables dock onto: they say *MAC <-> port*, only together do they give "which
address is plugged into which port".

Read is `ipNetToMediaPhysAddress` (IP-MIB, 1.3.6.1.2.1.4.22.1.2). The index is
`<ifIndex>.<ip>`, the value the MAC as hex bytes. Any router or firewall with an
SNMP agent that implements IP-MIB works (written against a Sophos firewall).

**No cheap precheck.** There is no counterpart to `ifLastChange` for an ARP
table: no counter changes when an entry is added. The walk is the query. That is
acceptable - it is ONE device, a few hundred entries, and an `snmpbulkwalk` with
a large repeat count needs only a handful of packets for it. The tool saves
elsewhere: asking less often (every 5 minutes) and only writing what changed.
"""
from __future__ import annotations

import re
import subprocess

from .model import Observation, Relation, Source, address_key

# 1.3.6.1.2.1.4.22.1.2 - ipNetToMediaPhysAddress
OID_ARP = "1.3.6.1.2.1.4.22.1.2"

# The same OID looks different depending on the net-snmp installation. Both forms
# were measured in the same network:
#
#   on the host        .1.3.6...11.172.16.0.2 = "80 F6 0F 9A BA DD "   hex with spaces
#   in the container   .1.3.6...11.172.16.0.2 = 80:f6:f:9a:ba:dd       colons, unpadded
#
# A collector must not rely on which tool happens to be installed. The pattern
# therefore accepts both forms.
_LINE = re.compile(
    r"^\." + OID_ARP.replace(".", r"\.") +
    r"\.(?P<ifindex>\d+)\.(?P<ip>\d+\.\d+\.\d+\.\d+)\s*=\s*\"?(?P<mac>[0-9A-Fa-f: ]+?)\"?\s*$")


def normalize_mac(raw: str) -> str:
    """Both notations become `80:f6:0f:9a:ba:dd`.

    One notation, the same everywhere - lower case and padded to two digits.
    Otherwise the same device is in the inventory three times because three
    sources deliver three notations.
    """
    raw = raw.strip()
    parts = raw.split(":") if ":" in raw else raw.split()
    parts = [p for p in parts if p]
    return ":".join(p.lower().zfill(2) for p in parts)


def is_random_mac(mac: str) -> bool:
    """Does the device randomise its MAC? (locally administered bit in the first byte)

    Phones do that per network. Such addresses are flagged and kept out of "new
    device" - otherwise the change report soon consists of nothing else.
    """
    try:
        return bool(int(mac.split(":")[0], 16) & 0b10)
    except (ValueError, IndexError):
        return False


def parse(output: str) -> list[Observation]:
    """Turn the output of `snmpbulkwalk` into observations.

    Lines that cannot be understood are skipped, not raised: a single broken line
    must not cost the whole table.
    """
    out: list[Observation] = []
    for line in output.splitlines():
        m = _LINE.match(line.strip())
        if not m:
            continue
        mac = normalize_mac(m.group("mac"))
        if len(mac) != 17:                       # not a complete MAC
            continue
        # The object key is the MAC, not the IP: IPs wander, MACs mostly do not.
        # The MAC remains an *identity proof* - which device is behind it is
        # decided later by identity resolution.
        # `0.0.0.0` is not an address but a device in the middle of DHCP. On the
        # first service run it showed up as "172.16.11.232 -> 0.0.0.0" in the change
        # report - the same trap as with the Wi-Fi collector.
        if m.group("ip") == "0.0.0.0":
            continue
        out.append(Observation(Relation.ADDRESS, mac, address_key(m.group("ip")),
                               m.group("ip"),
                               volatile=is_random_mac(mac)))
    return out


class ArpCollector:
    """Reads the ARP table of a firewall/router via SNMP."""

    def __init__(self, target: str, community: str, name: str = "firewall-arp",
                 timeout: int = 8, caller=None) -> None:
        self.target = target
        self.community = community
        self.timeout = timeout
        self._call = caller or self._snmpbulkwalk
        self.source = Source(
            name=name,
            # Narrowly scoped: this source sees addresses and nothing else. Its
            # silence must not end a switch attachment.
            responsible_for=frozenset({Relation.ADDRESS}),
            # ARP entries age out after minutes like MAC tables do; a device that
            # sends no packet for an hour is only gone after that.
            missing_threshold=12,
        )

    def _snmpbulkwalk(self) -> str:
        """`-Cr40` fetches 40 values per packet instead of one - with a few hundred
        entries that saves about fifty round trips compared to `snmpwalk`."""
        result = subprocess.run(
            ["snmpbulkwalk", "-v2c", "-c", self.community, "-Cr40", "-OQn",
             "-t", str(self.timeout), "-r", "1", self.target, OID_ARP],
            capture_output=True, text=True, timeout=self.timeout * 4)
        if result.returncode != 0 or not result.stdout.strip():
            raise RuntimeError(
                f"snmpbulkwalk against {self.target} failed: "
                f"{(result.stderr or 'no output').strip()[:200]}")
        return result.stdout

    def precheck_unchanged(self) -> bool:
        """There is none. See the head of this file."""
        return False

    def collect(self) -> list[Observation]:
        observations = parse(self._call())
        if not observations:
            # An empty ARP table does not exist on an active firewall. That is an
            # error, not an observation - and as an error it makes nothing
            # disappear (rule 1).
            raise RuntimeError(f"{self.target}: ARP table empty, that cannot be")
        return observations
