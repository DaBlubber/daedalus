# -*- coding: utf-8 -*-
"""Data model with a time axis.

**All timestamps are timezone-aware and UTC internally.** A naive timestamp would
be ambiguous in the night of the daylight-saving change, and losing history in
exactly that hour would be particularly annoying.

The principle: **intervals, not snapshots.** As long as a state stays the same,
its interval stays open. Only a change closes the old one and starts a new one.
Storage therefore grows with *changes*, not with *time* - and every change event
falls out naturally instead of being computed afterwards from two full snapshots.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class Kind(str, Enum):
    """What kind of thing an object is."""
    DEVICE = "device"
    INTERFACE = "interface"
    NETWORK = "network"
    ACCESS_POINT = "access_point"


class Relation(str, Enum):
    """Which kind of assignment an interval describes."""
    ADDRESS = "address"            # device <-> IP
    ATTACHMENT = "attachment"      # device <-> switch port or access point
    CONNECTION = "connection"      # interface <-> interface
    ATTRIBUTE = "attribute"        # any other changing attribute


class Event(str, Enum):
    """Changes that matter. The UI marks objects accordingly."""
    FIRST_SEEN = "first_seen"
    DISAPPEARED = "disappeared"
    BACK = "back"
    ADDRESS_ADDED = "address_added"
    ADDRESS_REMOVED = "address_removed"
    MOVED = "moved"
    # An attachment ends without a new one starting: unplugged from the port or
    # logged off from the access point. Previously reported as "moved x -> -",
    # which sounded like a move that never happened.
    DISCONNECTED = "disconnected"
    ATTRIBUTE_CHANGED = "attribute_changed"


# Attributes whose changes are tracked but never reported. `link_change` is the
# raw timestamp of the last link change - it counts a flapping port, but every
# single change would be a line. `link_access` is the link of a port without an
# LLDP neighbour: a PC that is switched off in the evening is not news
# (see collector_portstate.py).
SILENT_KEYS = frozenset({"link_access", "link_change"})


def address_key(ip: str) -> str:
    """The key of an address assignment: ONE interval per address.

    It used to be the same key ("address") for every address. The firewall ARP
    table, however, knows several addresses for some MACs (one had five) - with a
    shared key a different one won on every run, and the list reported "new
    address" dozens of times every night. On top of that, ARP and Wi-Fi fought
    over the same key.
    """
    return f"ip:{ip}"


def is_reportable(kind: "Event", before: str | None, after: str | None,
                  volatile: bool) -> bool:
    """Is a change news - or just movement?

    One night the list gained 397 lines, almost all of them everyday life and
    none of them a finding:

    - Wi-Fi clients roam between access points (A -> B -> A). That is roaming,
      intended and constant.
    - Phones join and leave the Wi-Fi ("back", "disconnected").
    - Phones with randomised MACs gain and lose addresses.

    History keeps all of it - only the list no longer gets it. Still reported:
    cable moves, a device that appears for the first time or disappears, address
    changes of real devices and changed values.
    """
    ap_before = bool(before and before.startswith("ap:"))
    ap_after = bool(after and after.startswith("ap:"))
    if kind in (Event.MOVED, Event.BACK, Event.DISCONNECTED) and \
            (ap_before or ap_after) and not (before and after and ap_before != ap_after):
        return False                          # Wi-Fi: roaming, joining, leaving
    if volatile and kind is not Event.MOVED:
        return False
    return True


@dataclass(frozen=True)
class Source:
    """A collection source.

    `responsible_for` says which relations this source may *judge*. Only a
    responsible source may end an interval - the most important rule in the whole
    model (see `reconcile.py`).

    `missing_threshold` is the number of **successful** runs in which something
    must be absent before it counts as gone. A MAC table ages out after minutes;
    a device is not gone just because it is missing once.
    """
    name: str
    responsible_for: frozenset[Relation]
    missing_threshold: int = 2


@dataclass(frozen=True)
class Run:
    """A collection run. `successful=False` means: this source says nothing."""
    source: str
    timestamp: datetime
    successful: bool = True


@dataclass(frozen=True)
class Observation:
    """What a run has seen - one single statement.

    `key` is the natural key of the assignment, i.e. what stays the same as long
    as nothing changes. `value` is what hangs off it.
    """
    relation: Relation
    obj: str             # stable object identifier (a UUID in production)
    key: str             # e.g. "ip:172.16.10.87" or "attachment"
    value: str           # e.g. "172.16.10.87" or "c3po:gi12"
    # Volatile means: the object is real and tracked, but its appearing and
    # disappearing is not news. Phones randomise their MAC per network - each
    # would otherwise be a "new device" every day and the change report would soon
    # consist of nothing else. Moves and address changes stay visible; only the
    # first sighting and the disappearance are not reported.
    volatile: bool = False


@dataclass
class Interval:
    """A state with a validity period. `until is None` means: still valid."""
    relation: Relation
    obj: str
    key: str
    value: str
    since: datetime
    until: datetime | None = None
    source: str = ""
    missing_since: int = 0   # unsuccessful sightings in a row
    volatile: bool = False   # see Observation.volatile

    @property
    def is_open(self) -> bool:
        return self.until is None


@dataclass
class Change:
    """A difference that matters. Exactly what the UI marks."""
    kind: Event
    obj: str
    timestamp: datetime
    before: str | None = None
    after: str | None = None
    source: str = ""
    # A change never concerns only one object: if a device moves from port 15 to
    # port 16, the device AND both ports are affected.
    affects: tuple[str, ...] = ()
    # Which attribute changed ("name", "vlan", "dhcp_lease"). Without it the list
    # only said "- -> 20" and nobody knew what of.
    key: str = ""
