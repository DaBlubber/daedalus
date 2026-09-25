# -*- coding: utf-8 -*-
"""Collector: state and health of the switch ports.

The topology says WHAT is plugged in where. This collector says HOW it is doing
there: link, speed, duplex, VLANs, spanning tree, PoE - and the error counters.
A port that flaps every two minutes explains more than any map.

What Cisco small-business switches deliver (measured on a real one):

| Attribute | OID | Example |
|---|---|---|
| link | ifOperStatus | `up` |
| speed | ifHighSpeed | `1000` |
| duplex | dot3StatsDuplexStatus | `3` (full) |
| last link change | ifLastChange | `18:11:22:45.72` |
| PVID | dot1qPvid | `1` |
| VLAN membership | dot1qVlanStaticEgress/UntaggedPorts | bit field per VLAN |
| spanning tree | dot1dStpPortState | `5` (forwarding) |
| PoE state | pethPsePortDetectionStatus | `3` (delivering) |
| PoE power | CISCOSB rlPethPsePort column 5 | `7700` mW |
| PoE budget | pethMainPsePower / ConsumptionPower | `180` W / `20` W |

## Two kinds of values, two places

**States** (link, VLAN, PoE on/off) change rarely and mean something when they
do. They become observations like everything else - their history then builds
itself, and an uplink that goes down shows up in the change list.

**Counters and measurements** (errors, discarded packets, watts) change on every
run. As intervals every run would be a "change". They go into a separate,
short-lived table (`port_counter`, 48 hours) from which the UI computes rates.
Time series over days belong in Prometheus - for "is the port flapping right
now?" two days are enough.

## What stays silent

A link change on an **access port** is not news: a PC switched off in the
evening would otherwise be two lines every day. On an **uplink or AP port** (a port
with an LLDP neighbour) it is. Hence two keys: `link` is reported, `link_access` is
only tracked. The raw link-change timestamp (`link_change`) is always silent - it
counts the flapping but does not report it.
"""
from __future__ import annotations

from dataclasses import dataclass

from .collector_switch import OID as SWITCH_OID, _Switch, split_oid
from .model import SILENT_KEYS, Observation, Relation, Source  # noqa: F401

OID = {
    "ifname":      SWITCH_OID["ifname"],
    "bridgeport":  SWITCH_OID["bridgeport"],
    "oper":        "1.3.6.1.2.1.2.2.1.8",           # ifOperStatus
    "speed":       "1.3.6.1.2.1.31.1.1.1.15",       # ifHighSpeed (Mbit/s)
    "lastchange":  "1.3.6.1.2.1.2.2.1.9",           # ifLastChange
    "duplex":      "1.3.6.1.2.1.10.7.2.1.19",       # dot3StatsDuplexStatus
    "pvid":        "1.3.6.1.2.1.17.7.1.4.5.1.1",    # dot1qPvid (index: bridge port)
    "egress":      "1.3.6.1.2.1.17.7.1.4.3.1.2",    # dot1qVlanStaticEgressPorts
    "untagged":    "1.3.6.1.2.1.17.7.1.4.3.1.4",    # dot1qVlanStaticUntaggedPorts
    "stp":         "1.3.6.1.2.1.17.2.15.1.3",       # dot1dStpPortState (bridge port)
    "in_errors":   "1.3.6.1.2.1.2.2.1.14",          # ifInErrors
    "out_errors":  "1.3.6.1.2.1.2.2.1.20",          # ifOutErrors
    "in_discards":  "1.3.6.1.2.1.2.2.1.13",         # ifInDiscards
    "out_discards": "1.3.6.1.2.1.2.2.1.19",         # ifOutDiscards
    "poe_status":  "1.3.6.1.2.1.105.1.1.1.6",       # pethPsePortDetectionStatus
    "poe_mw":      "1.3.6.1.4.1.9.6.1.101.108.1.1.5",  # CISCOSB rlPethPsePort power
    "poe_main":    "1.3.6.1.2.1.105.1.3.1.1",       # pethMainPseTable
}

_OPER = {"1": "up", "2": "down", "3": "testing", "5": "dormant", "6": "notPresent",
         "7": "lowerLayerDown"}
_DUPLEX = {"2": "half", "3": "full"}
_STP = {"1": "disabled", "2": "blocking", "3": "listening", "4": "learning",
        "5": "forwarding", "6": "broken"}
_POE = {"1": "off", "2": "searching", "3": "delivering", "4": "fault", "5": "test",
        "6": "fault"}


def _text(value: str, table: dict[str, str]) -> str:
    """net-snmp returns `up` or `1` depending on the loaded MIBs - understand both."""
    v = value.strip()
    if v in table:
        return table[v]
    # "up(1)" or "up"
    return v.split("(")[0] if v else ""


def is_physical_port(name: str) -> bool:
    n = name.strip().lower()
    return (n.startswith(("gi", "fa", "te")) and n[2:].isdigit()) or \
           (n.startswith("po") and n[2:].isdigit())


def bitfield_ports(hex_value: str) -> set[int]:
    """`"00 00 09 12"` -> bridge ports whose bit is set (MSB = port 1)."""
    ports: set[int] = set()
    try:
        bytes_ = [int(b, 16) for b in hex_value.replace('"', "").split()]
    except ValueError:
        return ports
    for i, byte in enumerate(bytes_):
        for bit in range(8):
            if byte & (0x80 >> bit):
                ports.add(i * 8 + bit + 1)
    return ports


def vlan_text(untagged: list[int], tagged: list[int]) -> str:
    """`u:1 t:5,10,20` - short, comparable and readable enough for the history."""
    parts = []
    if untagged:
        parts.append("u:" + ",".join(str(v) for v in sorted(untagged)))
    if tagged:
        parts.append("t:" + ",".join(str(v) for v in sorted(tagged)))
    return " ".join(parts)


@dataclass
class CounterReading:
    port: str                  # "r2d2:gi5", or "r2d2" for the whole switch
    in_errors: int | None = None
    out_errors: int | None = None
    in_discards: int | None = None
    out_discards: int | None = None
    poe_mw: int | None = None
    poe_budget_w: int | None = None


def parse(switch: str, raw: dict[str, str], uplinks: set[str] | None
          ) -> tuple[list[Observation], list[CounterReading]]:
    """Turn the walks into observations and counter readings."""
    t = {k: split_oid(v, OID[k]) for k, v in raw.items()}
    ifname = t.get("ifname", {})
    if not ifname:
        raise RuntimeError(f"{switch}: no port names")
    # bridge port -> ifIndex. They are equal on this hardware, but relying on that
    # would be guessing; the table costs one walk.
    bp_to_if = {bp: ifi for bp, ifi in t.get("bridgeport", {}).items()}
    if_to_bp = {ifi: bp for bp, ifi in bp_to_if.items()}

    # VLAN membership per bridge port
    vlans_u: dict[int, list[int]] = {}
    vlans_t: dict[int, list[int]] = {}
    for vlan, bits in t.get("egress", {}).items():
        if not vlan.isdigit():
            continue
        unt = bitfield_ports(t.get("untagged", {}).get(vlan, ""))
        for bp in bitfield_ports(bits):
            (vlans_u if bp in unt else vlans_t).setdefault(bp, []).append(int(vlan))

    uplinks = uplinks or set()
    obs: list[Observation] = []
    counters: list[CounterReading] = []

    def attr(port: str, key: str, value: str) -> None:
        if value != "":
            obs.append(Observation(Relation.ATTRIBUTE, f"{switch}:{port}", key, value))

    for ifi, name in sorted(ifname.items(), key=lambda x: int(x[0]) if x[0].isdigit() else 0):
        if not is_physical_port(name):
            continue
        link = _text(t.get("oper", {}).get(ifi, ""), _OPER)
        attr(name, "link" if name in uplinks else "link_access", link)
        attr(name, "link_change", t.get("lastchange", {}).get(ifi, ""))
        if link == "up":
            # Ports that are down report 10 Mbit/s or 0 - no information.
            attr(name, "speed", t.get("speed", {}).get(ifi, ""))
            attr(name, "duplex", _text(t.get("duplex", {}).get(ifi, ""), _DUPLEX))
            bp = if_to_bp.get(ifi, "")
            attr(name, "stp", _text(t.get("stp", {}).get(bp, ""), _STP))
        bp = if_to_bp.get(ifi, "")
        if bp.isdigit():
            attr(name, "pvid", t.get("pvid", {}).get(bp, ""))
            attr(name, "vlans", vlan_text(vlans_u.get(int(bp), []), vlans_t.get(int(bp), [])))
        poe = t.get("poe_status", {}).get(f"1.{ifi}")
        if poe is not None:
            attr(name, "poe", _text(poe, _POE))

        def number(key: str, index: str = ifi) -> int | None:
            v = t.get(key, {}).get(index)
            try:
                return int(v) if v is not None else None
            except ValueError:
                return None

        counters.append(CounterReading(
            port=f"{switch}:{name}",
            in_errors=number("in_errors"), out_errors=number("out_errors"),
            in_discards=number("in_discards"), out_discards=number("out_discards"),
            poe_mw=number("poe_mw", f"1.{ifi}")))

    main = t.get("poe_main", {})
    if main.get("2.1"):
        # pethMainPseTable: column 2 budget (W), column 4 consumption (W)
        obs.append(Observation(Relation.ATTRIBUTE, switch, "poe_budget", main["2.1"]))
        try:
            counters.append(CounterReading(port=switch, poe_mw=int(main.get("4.1", "0")) * 1000,
                                           poe_budget_w=int(main["2.1"])))
        except ValueError:
            pass
    return obs, counters


class SwitchPortState(_Switch):
    """Link, VLAN, STP, PoE and error counters of all ports of a switch."""

    REQUIRED = ("ifname", "bridgeport", "oper", "lastchange")
    # Not every switch knows every table. A switch without PoE does not answer a
    # walk for a missing table with "empty" - it walks on past the end instead
    # (3500 unrelated lines on a real SG200-26). Every optional table is therefore
    # checked once with ONE request and never asked again if it does not exist.
    OPTIONAL = ("poe_status", "poe_mw", "poe_main", "duplex", "stp", "pvid",
                "egress", "untagged")

    def __init__(self, *a, neighbors=None, counter_sink=None, prober=None, **k) -> None:
        super().__init__(*a, **k)
        self.neighbors = neighbors
        self.counter_sink = counter_sink
        self._probe = prober or self._getnext
        self._missing: set[str] | None = None     # determined on the first run
        self.source = Source(
            name=f"port-{self.switch}",
            responsible_for=frozenset({Relation.ATTRIBUTE}),
            missing_threshold=2,
        )

    def _getnext(self, oid: str) -> str:
        """The OID a GETNEXT on `oid` returns - one packet there, one back."""
        import subprocess
        r = subprocess.run(["snmpgetnext", "-v2c", "-c", self.community, "-OQn",
                            "-t", str(self.timeout), "-r", "1", self.target, oid],
                           capture_output=True, text=True, timeout=self.timeout * 3)
        if r.returncode != 0:
            raise RuntimeError(f"{self.switch}: {r.stderr.strip()[:120]}")
        return r.stdout.split("=", 1)[0].strip()

    def determine_missing(self) -> set[str]:
        missing: set[str] = set()
        for key in self.OPTIONAL:
            oid = OID[key]
            try:
                following = self._probe(oid)
            except Exception:
                continue                     # when in doubt ask, do not skip
            if not following.startswith("." + oid + "."):
                missing.add(key)
        return missing

    def precheck_unchanged(self) -> bool:
        # Counters keep moving - nothing is cheaper than reading them.
        return False

    def collect(self) -> list[Observation]:
        if self._missing is None:
            self._missing = self.determine_missing()
        raw: dict[str, str] = {}
        for key, oid in OID.items():
            if key in self._missing:
                raw[key] = ""
                continue
            try:
                raw[key] = self._call(oid)
            except Exception:
                # Not every switch has PoE; a missing required table, on the other
                # hand, means the run failed.
                if key in self.REQUIRED:
                    raise
                raw[key] = ""
        uplinks = getattr(self.neighbors, "last_uplinks", None) if self.neighbors else None
        obs, counters = parse(self.switch, raw, uplinks)
        if self.counter_sink and counters:
            self.counter_sink(counters)
        return obs
