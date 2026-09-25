# -*- coding: utf-8 -*-
"""The state for the canvas: intervals become a picture.

The UI is data-driven through a handful of tables (NETS, APS, SWITCHES, PORTS,
DEV, CHANGES, ANNOTATIONS, DHCP, FINDINGS). This module builds **exactly these
tables** from the inventory. The UI therefore stays the same; only the origin of
its data changes.

What does NOT happen here: guessing. A device without an attachment hangs nowhere
in the topology, only in its network. A port without an observation is not drawn
- the map shows what is measured, not what exists on the device.

What no source delivers - the list of networks with VLAN and name, and the list of
switches with model - comes from the configuration (`configure()`, see
`config.py`). The defaults below describe the example network of the tests and
of `daedalus.example.toml`.
"""
from __future__ import annotations

import ipaddress
from datetime import datetime, timedelta

from .chain import _node, _port, distances, neighborhood
from .collector_arp import normalize_mac
from .model import Event, Relation, is_reportable
from .timeutil import now, short

# --- What no source delivers ------------------------------------------------------
# (cidr, vlan or None, display name)
NETWORKS: tuple = (
    ("172.16.0.0/24",  None, "MANAGEMENT"),
    ("172.16.1.0/24",  5,    "SERVER"),
    ("172.16.10.0/24", 10,   "INTERNAL"),
    ("172.16.11.0/24", 20,   "SMART"),
    ("172.16.12.0/24", 30,   "GUEST"),
    ("172.16.13.0/24", 40,   "VPN"),
)
NO_NETWORK = "net:none"          # no address known
OTHER = "net:other"              # an address, but in no maintained network

# The switches: name -> (management IP, model, number of front ports).
SWITCHES: dict = {
    "bb8":  ("172.16.0.4", "SG200-26", 26),
    "r2d2": ("172.16.0.2", "SG300-28PP", 28),
    "c3po": ("172.16.0.3", "SG300-28", 28),
    "l337": ("172.16.0.5", "SG200-26", 26),
    "k2so": ("172.16.0.6", "SG200-26", 26),
}
COPPER = 24                  # ports above this number are drawn as fibre/combo ports

# How the top of the map is labelled: the gateway above the root switch and the
# DHCP servers. Pure display, no source delivers it.
SITE: dict = {
    "gateway": {"label": "GATEWAY", "model": "", "addresses": []},
    "dhcp": {"label": "KEA DHCP", "nodes": []},
}

CHANGE_DAYS = 7

# What the port-state collector delivers per port and the map shows
PORT_ATTRIBUTES = frozenset({"link", "link_access", "speed", "duplex", "vlans", "pvid",
                             "stp", "poe"})
FLAPPING_FROM = 4            # link changes in 24 hours from which a port stands out

_MARK = {
    Event.FIRST_SEEN: "new",
    Event.DISAPPEARED: "gone",
}


def configure(networks=None, switches=None, copper_ports: int | None = None,
              site: dict | None = None) -> None:
    """Set the site description from the configuration.

    `networks`  [(cidr, vlan, name), ...]
    `switches`  {name: (ip, model, ports)}
    `site`      {"gateway": {label, model, addresses}, "dhcp": {label, nodes}}
    """
    global NETWORKS, SWITCHES, COPPER, SITE
    if networks is not None:
        NETWORKS = tuple(networks)
    if switches is not None:
        SWITCHES = dict(switches)
    if copper_ports is not None:
        COPPER = copper_ports
    if site is not None:
        SITE = {k: dict(v) for k, v in site.items()}


def network_of(ip: str) -> str:
    """The network id of an address, `net:other` or `net:none`."""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return NO_NETWORK
    for cidr, _vlan, _name in NETWORKS:
        if a in ipaddress.ip_network(cidr):
            return f"net:{cidr}"
    return OTHER


def port_number(name: str) -> int | None:
    """`gi12` -> 12, `Po1` -> None (a bundle has no front panel position)."""
    digits = "".join(ch for ch in name if ch.isdigit())
    if not digits or not name[:2].lower() in ("gi", "fa", "te"):
        return None
    return int(digits)


def _port_order(p: dict) -> tuple:
    first = str(p["p"]).split("+")[0]
    return (port_number(first) or 10_000, first)


def _local(t: datetime | None) -> str:
    return short(t) if t else ""


def build(store, *, root: str = "bb8", annotations: dict | None = None,
          sightings: dict | None = None, timestamp: datetime | None = None,
          health: dict | None = None, transitions: dict | None = None) -> dict:
    """Build the state from the inventory.

    `annotations`  {key: {outlet, room, note, ...}} - maintained by hand
    `sightings`    {key: (first_seen, last_seen)}
    `health`       {port: {errors_1h, errors_24h, discards_24h, poe_w}} from the counters
    `transitions`  {port: link changes in 24 hours}
    """
    timestamp = timestamp or now()
    annotations = annotations or {}
    sightings = sightings or {}
    health = health or {}
    transitions = transitions or {}

    addresses = store.all_open(Relation.ADDRESS)
    attachments = store.all_open(Relation.ATTACHMENT)
    attributes = store.all_open(Relation.ATTRIBUTE)

    # --- devices: everything that is a MAC --------------------------------------
    devices: dict[str, dict] = {}

    def device(mac: str) -> dict:
        if mac not in devices:
            first, last = sightings.get(mac, (None, None))
            devices[mac] = {
                "id": mac, "mac": mac, "label": mac, "type": "", "vendor": "",
                "ip": "", "ips": [], "net": NO_NETWORK, "port": None, "ap": None,
                "state": "seen", "first": _local(first), "last": _local(last),
                "traffic": "-", "vlan": "", "essid": "", "random": False,
                "volatile": False,
            }
        return devices[mac]

    def is_mac(s: str) -> bool:
        return s.count(":") == 5 and len(s) == 17

    for i in addresses:
        if not is_mac(i.obj):
            continue
        d = device(i.obj)
        # The same address can be open under two keys (two sources, or after
        # switching to "ip:<address>"). Counted twice this produced more than 200
        # false findings "reported by 2 devices".
        if i.value not in d["ips"]:
            d["ips"].append(i.value)
        if i.missing_since:
            d["state"] = "quiet"
        d["volatile"] = d["volatile"] or i.volatile

    for i in attachments:
        if not is_mac(i.obj):
            continue
        d = device(i.obj)
        if i.value.startswith("ap:"):
            d["ap"] = i.value
        else:
            d["port"] = i.value
        if i.missing_since:
            d["state"] = "quiet"

    # Names from the inventory do not hang off a MAC but off an address: 172.16.1.6
    # is a particular host. They are matched further down via the IP.
    inventory_names = {i.value: i.obj for i in attributes
                       if i.key == "inventory_address"}
    names: dict[str, dict[str, str]] = {}      # mac -> {source: name}

    # DHCP details do NOT create a device: a reservation for a device nobody has
    # seen for months belongs in the DHCP overview, not on the map as "seen".
    dhcp: dict[str, dict[str, str]] = {}       # mac -> {lease, reservation, ...}
    net_dhcp: dict[str, dict[str, str]] = {}   # net:cidr -> {pools, router}
    for i in attributes:
        if i.key in ("dhcp_lease", "dhcp_reservation", "reservation_name",
                     "dhcp_name") and is_mac(i.obj):
            dhcp.setdefault(i.obj, {})[i.key] = i.value
        elif i.key in ("dhcp_pools", "dhcp_router"):
            net_dhcp.setdefault(i.obj, {})[i.key] = i.value

    # Whoever ever reported to Kea speaks DHCP - even if the lease has just expired
    # and the firewall still keeps old ARP entries.
    lease_ever = store.ever("dhcp_lease")

    for mac, details in dhcp.items():
        if "reservation_name" in details:
            names.setdefault(mac, {})["Reservation"] = details["reservation_name"]
        if "dhcp_name" in details:
            names.setdefault(mac, {})["DHCP"] = details["dhcp_name"]

    for i in attributes:
        if not is_mac(i.obj) or i.key in (
                "dhcp_lease", "dhcp_reservation", "reservation_name", "dhcp_name"):
            continue
        d = device(i.obj)
        if i.key == "name":
            names.setdefault(i.obj, {})["Wi-Fi"] = i.value
        elif i.key == "oui":
            d["vendor"] = i.value
        elif i.key in ("vlan", "essid"):
            d[i.key] = i.value
        elif i.key == "random_mac":
            d["random"] = True

    # Access points report themselves via LLDP with their MAC as port id
    # (`Luke:44 2B 62 63 B3 51`). The same MAC is in the ARP table with the address
    # of the AP - so the APs get name and address without asking another source.
    graph = neighborhood(store)
    ap_macs: dict[str, str] = {}                 # mac -> AP name
    for sw, neighbors in graph.items():
        if sw not in SWITCHES:
            continue
        for neighbor, _p_here, p_there in neighbors:
            mac = normalize_mac(p_there)
            if neighbor not in SWITCHES and len(mac) == 17:
                ap_macs[mac] = neighbor

    for d in devices.values():
        # Several addresses: the one from a known network first, then sorted. An
        # address outside all networks is usually a second leg.
        d["ips"].sort(key=lambda ip: (network_of(ip) == OTHER, ip))
        if d["ips"]:
            d["ip"] = d["ips"][0]
            d["net"] = network_of(d["ip"])
        d["type"] = "Wi-Fi client" if d["ap"] else ("wired" if d["port"] else "")

        # Who names a device? Manually maintained beats everything; then the
        # inventory, because it is the authoritative list for servers; then the DHCP
        # reservation (also maintained by hand, just elsewhere); then what the device
        # reports itself (UniFi, then DHCP). DNS does not appear: typical home DNS
        # servers know no PTR records.
        # A switch is known via SNMP under its address - the same address is in the
        # ARP table with its MAC. Without this match the switch showed up in the IP
        # lens only as its MAC.
        switch_here = next((sw for sw, (sw_ip, _m, _p) in SWITCHES.items()
                            if sw_ip in d["ips"]), None)
        if switch_here:
            d["switch"] = switch_here
            d["type"] = "Switch"
        candidates = [("manual", (annotations.get(d["id"]) or {}).get("name")),
                      ("Switch", switch_here.upper() if switch_here else None),
                      ("Inventory", next((inventory_names[ip] for ip in d["ips"]
                                          if ip in inventory_names), None)),
                      ("LLDP", ap_macs.get(d["id"])),
                      ("Reservation", names.get(d["id"], {}).get("Reservation")),
                      ("Wi-Fi", names.get(d["id"], {}).get("Wi-Fi")),
                      ("DHCP", names.get(d["id"], {}).get("DHCP"))]
        d["names"] = {s: n for s, n in candidates if n}
        d["name_source"] = ""
        for source, name in candidates:
            # UniFi reports the MAC as the name of nameless clients - that is none.
            if name and name != d["id"]:
                d["label"], d["name_source"] = name, source
                break
        details = dhcp.get(d["id"], {})
        d["dhcp"] = {"lease": details.get("dhcp_lease"),
                     "reservation": details.get("dhcp_reservation"),
                     # Ever had a Kea lease, even a long expired one.
                     "ever": d["id"] in lease_ever}

    # --- networks -----------------------------------------------------------------
    used: dict[str, int] = {}
    for d in devices.values():
        used[d["net"]] = used.get(d["net"], 0) + 1
    nets = []
    for cidr, vlan, name in NETWORKS:
        nid = f"net:{cidr}"
        size = ipaddress.ip_network(cidr).num_addresses - 2
        nets.append({"id": nid, "vlan": vlan, "label": name, "cidr": cidr,
                     "free": max(size - used.get(nid, 0), 0)})
    if used.get(OTHER):
        nets.append({"id": OTHER, "vlan": None, "label": "OTHER",
                     "cidr": "", "free": None})
    if used.get(NO_NETWORK):
        nets.append({"id": NO_NETWORK, "vlan": None, "label": "NO ADDRESS",
                     "cidr": "", "free": None})

    dhcp_overview = _dhcp(net_dhcp, dhcp, devices, nets)

    # --- switches and ports ---------------------------------------------------------
    distance, previous = distances(graph, root)

    # Who is the child of whom? Only the tree of the breadth-first search draws an
    # edge. Every further connection (c3po on bb8 AND on r2d2) is real, but is
    # noted at the port as a cross connection instead of drawn as a second path.
    child_ports: dict[str, dict[str, set]] = {}    # switch -> child -> {ports}
    to_parent: dict[str, set] = {}                 # switch -> {own ports}
    for sw, parents in previous.items():
        if not parents:
            continue
        par, port_on_child, _p_par = parents[0]
        to_parent.setdefault(sw, set())
        for neighbor, p_here, p_there in graph.get(sw, []):
            if neighbor == par:
                to_parent[sw].add(p_here)
                child_ports.setdefault(par, {}).setdefault(sw, set()).add(p_there)

    ports: dict[str, dict[str, dict]] = {sw: {} for sw in SWITCHES}
    aps_on_cable: dict[str, str] = {}              # ap:Luke -> r2d2:gi15

    def port(sw: str, name: str) -> dict:
        table = ports.setdefault(sw, {})
        if name not in table:
            nr = port_number(name)
            fibre = nr is not None and nr > COPPER
            table[name] = {"p": name, "n": (nr - COPPER) if fibre else (nr if nr is not None else name),
                           "fiber": fibre, "to": None, "bundle": 1, "speed": 1000}
            # "speed" comes as text from the collector when measured; a number here.
        return table[name]

    for sw, neighbors in graph.items():
        if sw not in SWITCHES:
            continue            # an access point has no port strip
        for neighbor, p_here, _p_there in neighbors:
            if not p_here:
                continue
            entry = port(sw, p_here)
            if neighbor not in SWITCHES:
                # Not a switch, but reported via LLDP: an access point. That finally
                # connects the Wi-Fi to its cable - r2d2 port 15 -> Luke.
                entry["to"] = f"ap:{neighbor}"
                aps_on_cable[f"ap:{neighbor}"] = f"{sw}:{p_here}"
            elif p_here in child_ports.get(sw, {}).get(neighbor, set()):
                entry["to"] = f"sw:{neighbor}"
            elif p_here in to_parent.get(sw, set()):
                entry["to"] = "up"
            else:
                entry["to"] = "up"
                entry["cross"] = neighbor

    for d in devices.values():
        if d["port"]:
            sw, pn = _node(d["port"]), _port(d["port"])
            entry = port(sw, pn)
            if entry["to"] is None:
                entry["to"] = "dev"

    # State and health per port (collector_portstate). This also shows the free
    # ports - the map shows the whole front panel, not only what happened to report
    # a MAC.
    for i in attributes:
        if i.key not in PORT_ATTRIBUTES or ":" not in i.obj:
            continue
        sw, pn = i.obj.split(":", 1)
        if sw not in SWITCHES or pn.lower().startswith("po"):
            continue
        entry = port(sw, pn)
        field = "link" if i.key == "link_access" else i.key
        entry[field] = i.value
    for sw, table in ports.items():
        for pn, entry in table.items():
            values = health.get(f"{sw}:{pn}", {})
            for field in ("errors_1h", "errors_24h", "discards_24h", "poe_w"):
                if field in values:
                    entry[field] = values[field]
            if f"{sw}:{pn}" in transitions:
                entry["changes_24h"] = transitions[f"{sw}:{pn}"]

    # Bundles: two ports of the same switch to the same child become ONE entry,
    # otherwise the map draws two paths where there is one.
    port_list: dict[str, list] = {}
    renamed: dict[str, str] = {}
    for sw, table in ports.items():
        by_target: dict[str, list] = {}
        rest = []
        for entry in table.values():
            if str(entry["to"]).startswith("sw:"):
                by_target.setdefault(entry["to"], []).append(entry)
            else:
                rest.append(entry)
        for target, group in by_target.items():
            if len(group) == 1:
                rest.append(group[0])
                continue
            group.sort(key=_port_order)
            p = "+".join(e["p"] for e in group)
            for e in group:
                renamed[f"{sw}:{e['p']}"] = f"{sw}:{p}"
            bundle = {"p": p, "n": "+".join(str(e["n"]) for e in group),
                      "fiber": all(e["fiber"] for e in group), "to": target,
                      "bundle": len(group),
                      "speed": sum(_number(e.get("speed"), 1000) for e in group),
                      "members": [dict(e) for e in group]}
            links = {e.get("link") for e in group if e.get("link")}
            if links:
                # Half a bundle is the most dangerous state: it works, but without a
                # reserve - and nobody notices.
                bundle["link"] = "up" if links == {"up"} else (
                    "down" if "up" not in links else "partial")
            for field in ("errors_1h", "errors_24h", "discards_24h", "changes_24h"):
                total = [e[field] for e in group if field in e]
                if total:
                    bundle[field] = sum(total)
            rest.append(bundle)
        for e in rest:
            if e["bundle"] == 1:
                e["speed"] = _number(e.get("speed"), 1000)
        rest.sort(key=lambda e: (e["fiber"], _port_order(e)))
        port_list[sw] = rest

    # --- access points ----------------------------------------------------------------
    aps = sorted({d["ap"] for d in devices.values() if d["ap"]} | set(aps_on_cable))
    ap_addresses = {f"ap:{name}": devices[mac]["ip"]
                    for mac, name in ap_macs.items() if mac in devices}
    for mac, name in ap_macs.items():
        # The AP itself as a device: its location is the switch port from LLDP.
        # Before this its detail card said "location unknown" although the map
        # showed it.
        if mac in devices:
            cable = aps_on_cable.get(f"ap:{name}")
            devices[mac]["ap_cable"] = renamed.get(cable, cable)
            devices[mac]["type"] = "Access Point"
    ap_list = [{"id": a, "label": a[3:].upper(), "ip": ap_addresses.get(a, ""),
                "port": renamed.get(aps_on_cable.get(a, ""), aps_on_cable.get(a))}
               for a in aps]

    switch_list = []
    for sw, (ip, model, front_ports) in SWITCHES.items():
        values = health.get(sw, {})
        switch_list.append({"id": sw, "label": sw.upper(), "model": model, "ip": ip,
                            "ports": front_ports,
                            "reachable": sw in distance,
                            "poe_budget_w": values.get("poe_budget_w"),
                            "poe_w": values.get("poe_w")})

    # --- changes -----------------------------------------------------------------------
    changes = []
    for c in reversed(_without_legacy(
            store.changes_since(timestamp - timedelta(days=CHANGE_DAYS)))):
        if c.obj not in devices and not any(x in devices for x in c.affects):
            continue
        # Whatever was stored before the rule gets it at display time.
        d_c = devices.get(c.obj)
        if c.kind not in (Event.FIRST_SEEN, Event.DISAPPEARED) and \
                not is_reportable(c.kind, c.before, c.after,
                                  bool(d_c and (d_c["random"] or d_c["volatile"]
                                                or _random(c.obj)))):
            continue
        mark = _MARK.get(c.kind, "chg")
        affects = []
        for x in c.affects:
            if x == c.obj:
                continue
            affects.append(renamed.get(x, x))
        d = devices.get(c.obj)
        if d:
            if d["port"]:
                affects.append(renamed.get(d["port"], d["port"]))
            if d["ap"]:
                affects.append(d["ap"])
            affects.append(d["net"])
        changes.append([mark, c.obj, short(c.timestamp), _description(c),
                        sorted(set(affects)), category(c)])

    return {
        "as_of": timestamp.isoformat(),
        "shown": short(timestamp),
        "root": root,
        "SITE": SITE,
        "NETS": nets,
        "APS": ap_list,
        "SWITCHES": switch_list,
        "PORTS": port_list,
        "DEV": sorted(devices.values(), key=lambda d: (d["label"].lower(), d["id"])),
        "CHANGES": changes,
        "ANNOTATIONS": {renamed.get(k, k): v for k, v in annotations.items()},
        "DHCP": dhcp_overview,
        "FINDINGS": collect_findings(store, devices, port_list, switch_list, nets,
                                     dhcp_overview, annotations),
    }


def _ip_number(ip: str) -> int:
    try:
        return int(ipaddress.ip_address(ip))
    except ValueError:
        return -1


def _dhcp(net_dhcp: dict, dhcp: dict, devices: dict, nets: list) -> list[dict]:
    """The DHCP overview per network - and whatever does not fit together.

    The findings are the real value: a reservation whose device is seen with a
    different address; a static address in the middle of the pool that Kea can hand
    out a second time at any moment; a network whose size is different in Kea than
    in the configuration.
    """
    known_nets = {n["id"]: n for n in nets}
    out = []
    for nid in sorted(net_dhcp, key=lambda n: _ip_number(n[4:].split("/")[0])):
        cidr = nid[4:]
        try:
            net = ipaddress.ip_network(cidr)
        except ValueError:
            continue
        pools = []
        for part in (net_dhcp[nid].get("dhcp_pools") or "").split(","):
            if "-" not in part:
                continue
            start, end = part.split("-", 1)
            pools.append((_ip_number(start), _ip_number(end), start, end))

        def in_pool(ip: str) -> bool:
            n = _ip_number(ip)
            return any(a <= n <= b for a, b, _, _ in pools)

        def in_net(ip: str) -> bool:
            try:
                return ipaddress.ip_address(ip) in net
            except ValueError:
                return False

        entries, findings = [], []
        for mac, details in dhcp.items():
            lease, res = details.get("dhcp_lease"), details.get("dhcp_reservation")
            if not (lease and in_net(lease)) and not (res and in_net(res)):
                continue
            d = devices.get(mac)
            seen = list(d["ips"]) if d else []
            e = {"mac": mac, "label": d["label"] if d else
                 (details.get("reservation_name") or details.get("dhcp_name") or mac),
                 "lease": lease, "reservation": res, "seen": seen,
                 "known": bool(d), "kind": "reserved" if res else "pool"}
            entries.append(e)
            if res and seen and res not in seen:
                findings.append({"kind": "reservation_differs", "mac": mac,
                                 "text": f"{e['label']}: reserved {res}, seen {', '.join(seen)}"})
            if lease and not res and pools and not in_pool(lease):
                findings.append({"kind": "lease_outside_pool", "mac": mac,
                                 "text": f"{e['label']}: lease {lease} is outside the pool"})

        # Static addresses in the pool: seen, in the pool, but neither lease nor
        # reservation
        static = []
        with_dhcp = {e["mac"] for e in entries}
        for d in devices.values():
            # If the device ever had a Kea lease, a pool address without a valid
            # lease is a leftover ARP entry of the firewall, not a static address.
            if (d.get("dhcp") or {}).get("ever"):
                continue
            for ip in d["ips"]:
                if not in_net(ip) or d["id"] in with_dhcp:
                    continue
                static.append({"mac": d["id"], "label": d["label"], "ip": ip,
                               "in_pool": in_pool(ip)})
                if in_pool(ip):
                    # Worded carefully: all that is measured is "in the pool, without a
                    # lease". Whether the address is configured statically or the lease
                    # is only missing on the primary node, no source says.
                    findings.append({"kind": "static_in_pool", "mac": d["id"],
                                     "text": f"{d['label']}: {ip} is inside the pool, Kea knows no lease for it"})

        pool_list = []
        for a, b, start, end in pools:
            used = sum(1 for e in entries if e["lease"] and a <= _ip_number(e["lease"]) <= b)
            pool_list.append({"from": start, "to": end, "size": b - a + 1,
                              "used": used, "free": max(b - a + 1 - used, 0)})

        overview = known_nets.get(nid)
        if overview is None:
            # Same network address, different size? Then the configuration says
            # something different than Kea - say so, do not pick silently.
            for n in nets:
                if n["cidr"] and n["cidr"].split("/")[0] == cidr.split("/")[0]:
                    findings.append({"kind": "net_size_differs", "mac": "",
                                     "text": f"Kea has {cidr}, the configuration {n['cidr']}"})
                    overview = n

        entries.sort(key=lambda e: _ip_number(e["lease"] or e["reservation"] or ""))
        static.sort(key=lambda s: _ip_number(s["ip"]))
        out.append({"id": f"dhcp:{cidr}", "net": overview["id"] if overview else nid,
                    "cidr": cidr, "label": overview["label"] if overview else cidr,
                    "vlan": overview["vlan"] if overview else None,
                    "router": net_dhcp[nid].get("dhcp_router"),
                    "pools": pool_list, "entries": entries, "static": static,
                    "findings": findings})
    return out


# --- making changes readable ---------------------------------------------------------

CATEGORIES = {
    "port": "Port",           # MAC table and LLDP: where something is plugged in
    "ip": "IP",               # ARP: which address
    "wifi": "Wi-Fi",          # UniFi: at which access point
    "dhcp": "DHCP",           # Kea: leases, reservations, pools
    "inventory": "Inventory", # Prometheus targets
}

_ATTRIBUTE = {
    "name": "name", "vlan": "VLAN", "essid": "SSID", "oui": "vendor",
    "dhcp_name": "DHCP name", "dhcp_lease": "DHCP lease",
    "dhcp_reservation": "reservation", "reservation_name": "reservation name",
    "dhcp_pools": "pool", "dhcp_router": "router",
    "inventory_address": "inventory address", "random_mac": "random MAC",
}


def category(c) -> str:
    """What is a change about? Derived from source and kind.

    Not a stored column: the source is on every change anyway, and this way the
    classification also applies to everything created before it existed.
    """
    s = c.source or ""
    if s in ("kea", "dhcp-config"):
        return "dhcp"
    if c.kind in (Event.ADDRESS_ADDED, Event.ADDRESS_REMOVED) or s.endswith("-arp"):
        return "ip"
    if s == "wifi":
        return "wifi"
    if s == "inventory":
        return "inventory"
    return "port"


def _place(value: str | None) -> str:
    """`c3po:gi12` -> `C3PO port 12`, `ap:Luke` -> `LUKE`."""
    if not value:
        return "?"
    if value.startswith("ap:"):
        return value[3:].upper()
    if ":" not in value:
        return value
    sw, port = value.split(":", 1)
    nr = port_number(port)
    if nr is None:
        return f"{sw.upper()} {port}"
    if nr > COPPER:
        return f"{sw.upper()} fiber {nr - COPPER}"
    return f"{sw.upper()} port {nr}"


def _without_legacy(changes: list) -> list:
    """Do not show again what was stored before reconciliation was cleaned up.

    Back then every attribute that went away or appeared got its own line
    ("attribute_changed - -> 20"), and a device that left brought "address removed"
    and "moved x -> -" along with "disappeared". The lines stay stored, they are just
    no longer shown - history is never deleted.
    """
    gone = {(c.obj, c.timestamp) for c in changes if c.kind is Event.DISAPPEARED}
    out = []
    for c in changes:
        if c.kind is Event.ATTRIBUTE_CHANGED and (c.before is None or c.after is None):
            continue
        if (c.obj, c.timestamp) in gone and c.kind in (
                Event.ADDRESS_REMOVED, Event.MOVED, Event.DISCONNECTED):
            continue
        out.append(c)
    return _without_flapping(out)


LEAVING = (Event.DISCONNECTED, Event.ADDRESS_REMOVED, Event.DISAPPEARED, Event.MOVED)
COMING = (Event.BACK, Event.ADDRESS_ADDED)
FLAP_WINDOW = timedelta(hours=1)


def _without_flapping(changes: list) -> list:
    """Gone and back within less than an hour: not a finding but aging.

    A device used to count as disconnected after ten minutes of silence; the MAC
    table, however, forgets quiet devices after five. That produced hundreds of
    "disconnected / back" pairs overnight. Reconciliation now waits an hour. For
    what was stored before that, every pair whose return lies within the hour is
    dropped here.
    """
    per_object: dict[str, list] = {}
    for c in changes:
        per_object.setdefault(c.obj, []).append(c)
    dropped: set[int] = set()
    for series in per_object.values():
        series.sort(key=lambda c: c.timestamp)
        for i, leaving in enumerate(series):
            if leaving.kind not in LEAVING or leaving.kind is Event.MOVED and leaving.after:
                continue
            returns = [k for k in series[i + 1:]
                       if k.kind in COMING and k.timestamp - leaving.timestamp <= FLAP_WINDOW]
            if returns:
                dropped.add(id(leaving))
                # everything that came back in the same run (address AND attachment)
                dropped.update(id(k) for k in returns
                               if k.timestamp == returns[0].timestamp)
        # An address this device already had before is not a new one - that is what
        # the back and forth between several ARP entries of one MAC looked like.
        known: set[str] = set()
        for c in series:
            if c.kind is Event.ADDRESS_ADDED and c.after in known:
                dropped.add(id(c))
            known.update(x for x in (c.before, c.after) if x)
    return [c for c in changes if id(c) not in dropped]


def _description(c) -> str:
    """One line for the change list, in the language of the UI."""
    if c.kind == Event.FIRST_SEEN:
        where = _place(c.after) if c.after and ":" in c.after else c.after
        return f"first seen · {where}" if where else "first seen"
    if c.kind == Event.DISAPPEARED:
        where = _place(c.before) if c.before and ":" in c.before else c.before
        last = f" · last at {where}" if where else ""
        return f"disappeared{last} · source was reachable"
    if c.kind == Event.BACK:
        return f"back · {_place(c.after)}"
    if c.kind == Event.MOVED:
        if c.after is None:                    # legacy: "moved x -> -"
            return f"disconnected from {_place(c.before)}"
        return f"moved: {_place(c.before)} → {_place(c.after)}"
    if c.kind == Event.DISCONNECTED:
        return f"disconnected from {_place(c.before)}"
    if c.kind == Event.ADDRESS_ADDED:
        return f"new address {c.after or ''}".strip()
    if c.kind == Event.ADDRESS_REMOVED:
        return f"address removed: {c.before or ''}".strip()
    attribute = _ATTRIBUTE.get(getattr(c, "key", ""), "")
    if c.before and ":" in c.before and c.after and ":" in c.after and not attribute:
        return f"connection: {_place(c.before)} → {_place(c.after)}"
    prefix = f"{attribute}: " if attribute else ""
    return f"{prefix}{c.before or '-'} → {c.after or '-'}"


def _random(mac: str) -> bool:
    try:
        return bool(int(mac.split(":")[0], 16) & 0b10) and mac.count(":") == 5
    except ValueError:
        return False


def _number(value, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


# --- contradictions ---------------------------------------------------------------

def collect_findings(store, devices: dict, port_list: dict, switch_list: list,
                     nets: list, dhcp: list, annotations: dict | None = None) -> list[dict]:
    """What does not fit together - the actual findings of the tool.

    Each finding: {category, severity ('warn'|'info'), text, object, affects}.
    The categories are the same as for the changes, so the UI can filter both the
    same way.
    """
    findings: list[dict] = []

    def finding(cat, severity, text, obj="", affects=()):
        findings.append({"category": cat, "severity": severity, "text": text,
                         "object": obj, "affects": sorted(set(affects) | ({obj} - {""}))})

    # Duplicate addresses
    per_ip: dict[str, list[str]] = {}
    for d in devices.values():
        for ip in d["ips"]:
            per_ip.setdefault(ip, []).append(d["id"])
    for ip, macs in sorted(per_ip.items()):
        if len(macs) > 1:
            names = {devices[m]["label"] for m in macs}
            if len(names) == 1 and not all(n == m for n, m in zip(names, macs)):
                # One host with two interfaces under one address - worth mentioning,
                # but not a conflict.
                finding("ip", "info", f"{ip}: {next(iter(names))} reports with "
                        f"{len(macs)} MAC addresses", macs[0], macs)
            else:
                finding("ip", "warn", f"{ip} is reported by {len(macs)} devices: "
                        f"{', '.join(sorted(names))}", macs[0], macs)

    for d in devices.values():
        binding = [ip for ip in d["ips"] if not ip.startswith("169.254.")]
        kea = d.get("dhcp") or {}
        for ip in d["ips"]:
            if not ip.startswith("169.254."):
                continue
            if binding or kea.get("lease") or kea.get("ever"):
                # A device can have its reserved address via DHCP AND a link-local
                # address next to it - many gateways keep both. Or it has a Kea lease
                # while the firewall keeps the old link-local address in ARP for
                # days. In both cases "got no DHCP address" would simply be wrong.
                continue
            finding("ip", "warn", f"{d['label']}: {ip} - got no DHCP address",
                    d["id"])
        # Wi-Fi VLAN against the network of the address
        if d["vlan"] and d["ip"]:
            net = next((n for n in nets if n["id"] == d["net"]), None)
            if net and net["vlan"] is not None and str(net["vlan"]) != str(d["vlan"]):
                finding("wifi", "warn",
                        f"{d['label']}: Wi-Fi reports VLAN {d['vlan']}, but the address {d['ip']} "
                        f"is in {net['label']} (VLAN {net['vlan']})", d["id"])

    # Ports: errors, flapping, half bundles, slow uplinks
    for sw, ports in port_list.items():
        for e in ports:
            pid = f"{sw}:{e['p']}"
            place = _place(f"{sw}:{e['p'].split('+')[0]}") + ("+" if e["bundle"] > 1 else "")
            onward = str(e.get("to") or "")
            if e.get("errors_24h"):
                finding("port", "warn", f"{place}: {e['errors_24h']} errors in 24 hours", pid)
            if e.get("changes_24h", 0) >= FLAPPING_FROM:
                finding("port", "warn", f"{place}: link changed {e['changes_24h']} times (24 h)",
                        pid)
            if e.get("link") == "partial":
                finding("port", "warn", f"{place}: bundle only half up - one cable is down",
                        pid)
            if onward.startswith(("sw:", "ap:")) or onward == "up":
                if e.get("duplex") == "half":
                    finding("port", "warn", f"{place}: uplink in half duplex", pid)
                if e["bundle"] == 1 and str(e.get("speed")).isdigit() and \
                        e.get("link") == "up" and int(e["speed"]) < 1000:
                    finding("port", "warn", f"{place}: uplink only at {e['speed']} Mbit/s", pid)
                if e.get("stp") == "blocking":
                    finding("port", "info",
                            f"{place}: spanning tree blocking - expected on a redundant path",
                            pid)

    for sw in switch_list:
        if sw.get("poe_budget_w") and sw.get("poe_w") is not None:
            share = sw["poe_w"] / sw["poe_budget_w"]
            if share >= 0.8:
                finding("port", "warn",
                        f"{sw['label']}: PoE at {round(share * 100)} % "
                        f"({sw['poe_w']} of {sw['poe_budget_w']} W)", sw["id"])

    # One-sided LLDP: only one switch sees the other
    reported = {(i.obj, i.value) for i in store.all_open(Relation.CONNECTION)}
    for here, there in sorted(reported):
        sw_h, sw_t = here.split(":", 1)[0], there.split(":", 1)[0].lower()
        if sw_h in SWITCHES and sw_t in SWITCHES and \
                not any(a.split(":", 1)[0] == sw_t and b.split(":", 1)[0].lower() == sw_h
                        for a, b in reported):
            finding("port", "info", f"{_place(here)} sees {sw_t.upper()}, but not the other way round",
                    here)

    # File the DHCP findings
    for net in dhcp:
        for f in net.get("findings", []):
            finding("dhcp", "warn", f["text"], f.get("mac", ""))

    # Marked as expected by hand (annotation field `expected`): the finding stays
    # visible but no longer counts and moves to the end - e.g. a game console that
    # flaps every hour in standby, and that is fine.
    annotations = annotations or {}
    for f in findings:
        reason = next(((annotations.get(o) or {}).get("expected")
                       for o in [f["object"], *f["affects"]]
                       if (annotations.get(o) or {}).get("expected")), None)
        if reason:
            f["expected"] = reason

    rank = {"warn": 0, "info": 1}
    findings.sort(key=lambda f: ("expected" in f, rank[f["severity"]], f["category"], f["text"]))
    return findings
