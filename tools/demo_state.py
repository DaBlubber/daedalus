# -*- coding: utf-8 -*-
"""Build the state of a small made-up network - no database, no devices.

    python tools/demo_state.py demo.json
    python tools/preview.py demo.json preview.html

The network is the example site of `daedalus.example.toml`: a gateway, the core
switch bb8, r2d2 behind it on a two-cable bundle, c3po and l337 on r2d2, one
access point, a handful of devices, a DHCP pool with a reservation that does not
match, and one change. Good enough to click through every lens and card.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from daedalus import state  # noqa: E402
from daedalus.model import Change, Event, Interval, Relation  # noqa: E402
from daedalus.store import MemoryStore  # noqa: E402

NOW = datetime(2026, 9, 16, 10, 0, tzinfo=timezone.utc)


def build_demo() -> dict:
    s = MemoryStore()

    def put(relation, obj, value, key=None, since=NOW - timedelta(days=3)):
        s.open(Interval(relation, obj, key or relation.value, value, since=since))
        s.known.add(obj)

    # --- cabling as LLDP reports it --------------------------------------------
    for here, there in [("bb8:gi25", "r2d2:gi25"), ("bb8:gi26", "r2d2:gi26"),
                        ("r2d2:gi25", "bb8:gi25"), ("r2d2:gi26", "bb8:gi26"),
                        ("r2d2:gi10", "l337:gi25"), ("l337:gi25", "r2d2:gi10"),
                        ("r2d2:gi11", "c3po:gi25"), ("c3po:gi25", "r2d2:gi11"),
                        ("r2d2:gi15", "Luke:44 2B 62 63 B3 51")]:
        put(Relation.CONNECTION, here, there)

    # --- devices ---------------------------------------------------------------------
    devices = [
        # mac, address, attachment, name (key, value), vendor
        ("64:28:aa:9e:1c:b2", "172.16.1.20", "c3po:gi3", ("inventory_address", None), "Intel"),
        ("00:11:32:aa:bb:01", "172.16.1.30", "c3po:gi4", None, "Synology"),
        ("b8:27:eb:12:34:56", "172.16.11.40", "l337:gi7", ("dhcp_name", "sensor-hub"), "Raspberry Pi"),
        ("ec:fa:bc:00:00:01", "172.16.11.41", "ap:Luke", ("name", "living-room-light"), "Espressif"),
        ("3c:22:fb:10:20:30", "172.16.10.50", "ap:Luke", ("name", "laptop-alex"), "Apple"),
        ("7a:11:22:33:44:55", "172.16.12.60", "ap:Luke", None, ""),        # random MAC
        ("30:05:5c:aa:00:10", "172.16.10.70", "l337:gi12", ("reservation_name", "printer"), "Brother"),
    ]
    for mac, ip, where, name, vendor in devices:
        put(Relation.ADDRESS, mac, ip)
        put(Relation.ATTACHMENT, mac, where)
        if vendor:
            put(Relation.ATTRIBUTE, mac, vendor, key="oui")
        if name and name[1]:
            put(Relation.ATTRIBUTE, mac, name[1], key=name[0])
        if mac.startswith("7a"):
            put(Relation.ATTRIBUTE, mac, "1", key="random_mac")
    put(Relation.ADDRESS, "44:2b:62:63:b3:51", "172.16.1.101")
    put(Relation.ATTRIBUTE, "server-01", "172.16.1.20", key="inventory_address")

    # --- DHCP -------------------------------------------------------------------------
    put(Relation.ATTRIBUTE, "net:172.16.10.0/24", "172.16.10.100-172.16.10.199", key="dhcp_pools")
    put(Relation.ATTRIBUTE, "net:172.16.10.0/24", "172.16.10.1", key="dhcp_router")
    put(Relation.ATTRIBUTE, "net:172.16.11.0/24", "172.16.11.100-172.16.11.199", key="dhcp_pools")
    put(Relation.ATTRIBUTE, "3c:22:fb:10:20:30", "172.16.10.150", key="dhcp_lease")
    put(Relation.ATTRIBUTE, "30:05:5c:aa:00:10", "172.16.10.71", key="dhcp_reservation")
    put(Relation.ATTRIBUTE, "30:05:5c:aa:00:10", "printer", key="reservation_name")
    put(Relation.ATTRIBUTE, "de:ad:be:ef:00:01", "172.16.11.20", key="dhcp_reservation")
    put(Relation.ATTRIBUTE, "de:ad:be:ef:00:01", "old-camera", key="reservation_name")

    # --- port state ----------------------------------------------------------------------
    for port, attrs in {
        "c3po:gi3": {"link_access": "up", "speed": "1000", "duplex": "full", "vlans": "u:5", "pvid": "5"},
        "c3po:gi4": {"link_access": "up", "speed": "1000", "duplex": "full", "vlans": "u:5", "pvid": "5",
                     "poe": "delivering"},
        "c3po:gi9": {"link_access": "down"},
        "l337:gi7": {"link_access": "up", "speed": "100", "duplex": "full", "vlans": "u:20", "pvid": "20"},
        "r2d2:gi15": {"link": "up", "speed": "1000", "duplex": "full", "vlans": "u:1 t:5,10,20,30",
                      "poe": "delivering"},
    }.items():
        for key, value in attrs.items():
            put(Relation.ATTRIBUTE, port, value, key=key)

    # --- one change: the printer moved ------------------------------------------------------
    s.changes.append(Change(kind=Event.MOVED, obj="30:05:5c:aa:00:10",
                            timestamp=NOW - timedelta(hours=2), before="l337:gi11",
                            after="l337:gi12", source="fdb-l337",
                            affects=("30:05:5c:aa:00:10", "l337:gi11", "l337:gi12"),
                            key="attachment"))

    state.configure(site={
        "gateway": {"label": "GATEWAY", "model": "Example firewall",
                    "addresses": ["172.16.1.1", "172.16.0.1"]},
        "dhcp": {"label": "KEA DHCP", "nodes": ["http://172.16.1.3:8000", "http://172.16.1.4:8000"]},
    })
    sightings = {mac: (NOW - timedelta(days=3), NOW - timedelta(minutes=4))
                 for mac, *_ in devices}
    return state.build(
        s, root="bb8", timestamp=NOW, sightings=sightings,
        annotations={"c3po:gi3": {"outlet": "B2-14", "room": "Office"}},
        health={"c3po:gi4": {"errors_1h": 0, "errors_24h": 0, "discards_24h": 0, "poe_w": 6.2},
                "l337:gi7": {"errors_1h": 2, "errors_24h": 14, "discards_24h": 0}},
    )


def main() -> None:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("demo.json")
    target.write_text(json.dumps(build_demo(), ensure_ascii=False, indent=1), encoding="utf-8")
    print(target.resolve())


if __name__ == "__main__":
    main()
