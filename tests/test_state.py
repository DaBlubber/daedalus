# -*- coding: utf-8 -*-
"""The state for the canvas - tested against the traps the first real build showed.

Every test here stands for something that went wrong, or would have gone wrong,
on real data.
"""
from datetime import datetime, timezone

from daedalus.model import Change, Event, Interval, Relation
from daedalus.state import NO_NETWORK, OTHER, build, network_of, port_number
from daedalus.store import MemoryStore

T = datetime(2026, 9, 16, 10, 0, tzinfo=timezone.utc)


def _open(s, relation, obj, value, key=None):
    s.open(Interval(relation, obj, key or relation.value, value, since=T))


def _network(s):
    """bb8 is the root; r2d2 hangs off it with two cables, l337 off r2d2.
    r2d2 reports l337 the way LLDP names it: `L337`."""
    for here, there in [("bb8:gi25", "r2d2:gi25"), ("bb8:gi26", "r2d2:gi26"),
                        ("r2d2:gi25", "bb8:gi25"), ("r2d2:gi26", "bb8:gi26"),
                        ("r2d2:gi10", "L337:gi25"), ("l337:gi25", "r2d2:gi10"),
                        ("r2d2:gi15", "Luke:44 2B 62 63 B3 51")]:
        _open(s, Relation.CONNECTION, here, there)


def test_switch_in_two_spellings_is_one_node():
    s = MemoryStore()
    _network(s)
    st = build(s, timestamp=T)
    assert "L337" not in st["PORTS"]
    r2d2 = {p["p"]: p for p in st["PORTS"]["r2d2"]}
    assert r2d2["gi10"]["to"] == "sw:l337"
    assert [p["to"] for p in st["PORTS"]["l337"]] == ["up"]


def test_access_point_hangs_off_the_switch_port():
    s = MemoryStore()
    _network(s)
    _open(s, Relation.ATTACHMENT, "c6:3c:d0:13:86:00", "ap:Luke")
    st = build(s, timestamp=T)
    r2d2 = {p["p"]: p for p in st["PORTS"]["r2d2"]}
    assert r2d2["gi15"]["to"] == "ap:Luke"
    assert {"id": "ap:Luke", "label": "LUKE", "ip": "", "port": "r2d2:gi15"} in st["APS"]
    # The MAC from the LLDP port id names the device and delivers the address
    _open(s, Relation.ADDRESS, "44:2b:62:63:b3:51", "172.16.1.101")
    st = build(s, timestamp=T)
    luke = {d["id"]: d for d in st["DEV"]}["44:2b:62:63:b3:51"]
    assert (luke["label"], luke["name_source"]) == ("Luke", "LLDP")
    assert next(a for a in st["APS"] if a["id"] == "ap:Luke")["ip"] == "172.16.1.101"
    # An AP gets no port strip of its own
    assert "Luke" not in st["PORTS"]


def test_two_cables_to_the_same_switch_are_one_bundle():
    s = MemoryStore()
    _network(s)
    st = build(s, timestamp=T)
    to_r2d2 = [p for p in st["PORTS"]["bb8"] if p["to"] == "sw:r2d2"]
    assert len(to_r2d2) == 1
    assert to_r2d2[0]["p"] == "gi25+gi26"
    assert to_r2d2[0]["bundle"] == 2
    assert to_r2d2[0]["fiber"] is True
    # On the child side they are uplinks, not a second path
    assert {p["to"] for p in st["PORTS"]["r2d2"] if p["p"] in ("gi25", "gi26")} == {"up"}


def test_device_is_assembled_from_all_relations():
    s = MemoryStore()
    _network(s)
    mac = "64:28:aa:9e:1c:b2"
    _open(s, Relation.ADDRESS, mac, "172.16.10.87")
    _open(s, Relation.ADDRESS, mac, "172.30.32.1")
    _open(s, Relation.ATTACHMENT, mac, "l337:gi4")
    _open(s, Relation.ATTRIBUTE, mac, "Intel", key="oui")
    st = build(s, timestamp=T, annotations={mac: {"name": "device-08ef"}})
    d = next(d for d in st["DEV"] if d["id"] == mac)
    assert d["label"] == "device-08ef"          # maintained beats measured
    assert d["ip"] == "172.16.10.87"            # known network before foreign address
    assert d["net"] == "net:172.16.10.0/24"
    assert d["vendor"] == "Intel"
    assert {p["p"]: p["to"] for p in st["PORTS"]["l337"]}["gi4"] == "dev"


def test_address_groups_stay_separate():
    assert network_of("172.16.13.14") == "net:172.16.13.0/24"
    assert network_of("172.16.14.20") == OTHER
    assert network_of("") == NO_NETWORK
    s = MemoryStore()
    _open(s, Relation.ATTRIBUTE, "26:ad:17:a2:e9:42", "x", key="name")
    st = build(s, timestamp=T)
    ids = [n["id"] for n in st["NETS"]]
    assert NO_NETWORK in ids and OTHER not in ids


def test_port_numbers():
    assert port_number("gi12") == 12
    assert port_number("Po1") is None


def test_change_marks_the_bundle_not_the_single_port():
    s = MemoryStore()
    _network(s)
    mac = "0e:a9:d0:bf:9d:8c"
    _open(s, Relation.ATTACHMENT, mac, "r2d2:gi3")
    s.remember_change(Change(Event.FIRST_SEEN, mac, T, affects=(mac, "bb8:gi25")))
    st = build(s, timestamp=T)
    mark, obj, _when, what, affects, cat = st["CHANGES"][0]
    assert cat == "port"
    assert (mark, obj, what) == ("new", mac, "first seen")
    assert "bb8:gi25+gi26" in affects and "bb8:gi25" not in affects
    assert "r2d2:gi3" in affects


def test_names_by_priority_and_with_origin():
    s = MemoryStore()
    server, laptop, phone = "6c:44:8c:7e:4f:ed", "44:4f:91:f9:32:cc", "2a:70:66:44:08:90"
    _open(s, Relation.ADDRESS, server, "172.16.1.6")
    _open(s, Relation.ATTRIBUTE, "host-59a8", "172.16.1.6", key="inventory_address")
    _open(s, Relation.ATTRIBUTE, server, "server-dhcp", key="dhcp_name")
    _open(s, Relation.ADDRESS, laptop, "172.16.10.13")
    _open(s, Relation.ATTRIBUTE, laptop, "device-08ef", key="dhcp_name")
    # UniFi reports a nameless client with its MAC as the name
    _open(s, Relation.ATTRIBUTE, phone, phone, key="name")
    st = build(s, timestamp=T)
    d = {x["id"]: x for x in st["DEV"]}
    assert (d[server]["label"], d[server]["name_source"]) == ("host-59a8", "Inventory")
    assert d[server]["names"] == {"Inventory": "host-59a8", "DHCP": "server-dhcp"}
    assert (d[laptop]["label"], d[laptop]["name_source"]) == ("device-08ef", "DHCP")
    assert (d[phone]["label"], d[phone]["name_source"]) == (phone, "")
    st = build(s, timestamp=T, annotations={laptop: {"name": "Alex's laptop"}})
    assert {x["id"]: x["label"] for x in st["DEV"]}[laptop] == "Alex's laptop"


def test_dhcp_overview_with_findings():
    s = MemoryStore()
    A = Relation.ATTRIBUTE
    _open(s, A, "net:172.16.10.0/24", "172.16.10.150-172.16.10.248", key="dhcp_pools")
    # Size differs: Kea has GUEST as /25, the configuration as /24
    _open(s, A, "net:172.16.12.0/25", "172.16.12.50-172.16.12.120", key="dhcp_pools")
    phone, laptop, static, gone = ("12:30:bd:6a:36:d2", "44:4f:91:f9:32:cc",
                                   "4a:52:01:1f:df:2d", "66:60:fb:d0:45:8b")
    # Reserved on .33, but seen with .34
    _open(s, A, phone, "172.16.10.33", key="dhcp_reservation")
    _open(s, A, phone, "device-6807", key="reservation_name")
    _open(s, Relation.ADDRESS, phone, "172.16.10.34")
    # Normal lease in the pool
    _open(s, A, laptop, "172.16.10.160", key="dhcp_lease")
    _open(s, Relation.ADDRESS, laptop, "172.16.10.160")
    # Static address in the middle of the pool, without a lease
    _open(s, Relation.ADDRESS, static, "172.16.10.200")
    # Reservation for a device never seen: no device on the map
    _open(s, A, gone, "172.16.10.40", key="dhcp_reservation")

    st = build(s, timestamp=T)
    assert gone not in {d["id"] for d in st["DEV"]}
    assert {d["id"]: d["label"] for d in st["DEV"]}[phone] == "device-6807"
    nets = {n["cidr"]: n for n in st["DHCP"]}
    internal = nets["172.16.10.0/24"]
    assert internal["label"] == "INTERNAL"
    assert internal["pools"] == [{"from": "172.16.10.150", "to": "172.16.10.248",
                                  "size": 99, "used": 1, "free": 98}]
    kinds = sorted(f["kind"] for f in internal["findings"])
    assert kinds == ["reservation_differs", "static_in_pool"]
    assert [e["mac"] for e in internal["entries"]] == [phone, gone, laptop]   # by IP
    assert [f["kind"] for f in nets["172.16.12.0/25"]["findings"]] == ["net_size_differs"]


def test_changes_categorised_readable_and_without_legacy():
    from daedalus.state import _description, _without_legacy, category
    C = Change
    E = Event
    port = C(E.MOVED, "m", T, "c3po:gi12", "c3po:gi13", source="fdb-c3po")
    ap = C(E.DISCONNECTED, "m", T, "ap:Luke", None, source="wifi")
    lease = C(E.ATTRIBUTE_CHANGED, "m", T, "172.16.10.160", "172.16.10.161",
              source="kea", key="dhcp_lease")
    ip = C(E.ADDRESS_ADDED, "m", T, None, "172.16.10.5", source="wifi")
    assert [category(x) for x in (port, ap, lease, ip)] == ["port", "wifi", "dhcp", "ip"]
    assert _description(port) == "moved: C3PO port 12 → C3PO port 13"
    assert _description(ap) == "disconnected from LUKE"
    assert _description(lease) == "DHCP lease: 172.16.10.160 → 172.16.10.161"

    t2 = datetime(2026, 9, 16, 11, 0, tzinfo=timezone.utc)
    legacy = [C(E.ATTRIBUTE_CHANGED, "x", t2, "wlan0", None, source="wifi"),
              C(E.ATTRIBUTE_CHANGED, "x", t2, None, "20", source="wifi"),
              C(E.MOVED, "x", t2, "ap:Luke", None, source="wifi"),
              C(E.ADDRESS_REMOVED, "x", t2, "172.16.11.239", None, source="wifi"),
              C(E.DISAPPEARED, "x", t2, "20", None, source="wifi")]
    assert [c.kind for c in _without_legacy(legacy)] == [E.DISAPPEARED]


def test_port_state_switch_names_and_findings():
    s = MemoryStore()
    _network(s)
    A = Relation.ATTRIBUTE
    # r2d2 as a device under its management address
    _open(s, Relation.ADDRESS, "80:f6:0f:9a:ba:dd", "172.16.0.2")
    # bundle bb8 -> r2d2: one cable down
    _open(s, A, "bb8:gi25", "up", key="link")
    _open(s, A, "bb8:gi26", "down", key="link")
    _open(s, A, "bb8:gi25", "1000", key="speed")
    # free port, known only through the port state
    _open(s, A, "bb8:gi9", "down", key="link_access")
    # duplicate address and APIPA
    _open(s, Relation.ADDRESS, "4a:52:01:1f:df:2d", "172.16.10.50")
    _open(s, Relation.ADDRESS, "66:60:fb:d0:45:8b", "172.16.10.50")
    _open(s, Relation.ADDRESS, "6e:eb:ed:58:76:e8", "169.254.78.106")

    st = build(s, timestamp=T,
               health={"bb8:gi25": {"errors_24h": 7}},
               transitions={"bb8:gi26": 5})
    d = {x["id"]: x for x in st["DEV"]}
    assert (d["80:f6:0f:9a:ba:dd"]["label"], d["80:f6:0f:9a:ba:dd"]["name_source"]) \
        == ("R2D2", "Switch")
    bb8 = {p["p"]: p for p in st["PORTS"]["bb8"]}
    assert bb8["gi9"]["link"] == "down" and bb8["gi9"]["to"] is None
    bundle = bb8["gi25+gi26"]
    assert bundle["link"] == "partial"
    assert bundle["errors_24h"] == 7 and bundle["changes_24h"] == 5
    assert [m["p"] for m in bundle["members"]] == ["gi25", "gi26"]
    texts = " | ".join(f["text"] for f in st["FINDINGS"])
    assert "half up" in texts and "7 errors" in texts and "5 times" in texts
    assert "172.16.10.50 is reported by 2 devices" in texts
    assert "169.254.78.106" in texts
    assert all(f["category"] in ("ip", "port", "wifi", "dhcp") for f in st["FINDINGS"])


def test_flapping_from_before_the_threshold_is_not_shown():
    from datetime import timedelta
    from daedalus.state import _without_legacy
    C, E = Change, Event
    t0 = datetime(2026, 9, 17, 1, 0, tzinfo=timezone.utc)
    series = [C(E.DISCONNECTED, "kvm", t0, "k2so:gi11", None, source="fdb-k2so"),
              C(E.BACK, "kvm", t0 + timedelta(minutes=15), None, "k2so:gi11",
                source="fdb-k2so"),
              # really unplugged: only comes back after three hours
              C(E.DISCONNECTED, "printer", t0, "c3po:gi14", None, source="fdb-c3po"),
              C(E.BACK, "printer", t0 + timedelta(hours=3), None, "c3po:gi14",
                source="fdb-c3po")]
    remaining = [(c.obj, c.kind) for c in _without_legacy(series)]
    assert remaining == [("printer", E.DISCONNECTED), ("printer", E.BACK)]


def test_same_address_under_two_keys_counts_once():
    s = MemoryStore()
    _open(s, Relation.ADDRESS, "16:ed:ae:b0:fe:f3", "172.16.10.9", key="address")
    _open(s, Relation.ADDRESS, "16:ed:ae:b0:fe:f3", "172.16.10.9", key="ip:172.16.10.9")
    st = build(s, timestamp=T)
    assert {d["id"]: d["ips"] for d in st["DEV"]}["16:ed:ae:b0:fe:f3"] == ["172.16.10.9"]
    assert not any("172.16.10.9" in f["text"] for f in st["FINDINGS"])


def test_link_local_next_to_a_real_address_is_no_finding():
    s = MemoryStore()
    _open(s, Relation.ADDRESS, "0c:85:db:07:df:09", "172.16.11.92", key="ip:172.16.11.92")
    _open(s, Relation.ADDRESS, "0c:85:db:07:df:09", "169.254.78.106",
          key="ip:169.254.78.106")
    _open(s, Relation.ADDRESS, "ce:3c:9f:0d:54:1c", "169.254.1.2", key="ip:169.254.1.2")
    texts = [f["text"] for f in build(s, timestamp=T)["FINDINGS"]]
    assert not any("169.254.78.106" in t for t in texts)
    assert any("169.254.1.2" in t for t in texts)


def _pool(s):
    _open(s, Relation.ATTRIBUTE, "net:172.16.11.0/24", "172.16.11.200-172.16.11.250",
          key="dhcp_pools")


def test_pool_address_with_earlier_lease_is_arp_leftover_not_a_finding():
    """A device with four old addresses from the firewall ARP table - Kea had given
    it all four before. Only whoever NEVER talked to Kea has a static address in
    the pool (the IoT device on .222)."""
    s = MemoryStore()
    _pool(s)
    iot_old, iot_static = "fc:01:d2:73:29:6e", "9c:5a:b2:30:c3:fd"
    s.open(Interval(Relation.ATTRIBUTE, iot_old, "dhcp_lease", "172.16.11.229",
                    since=T, until=T))
    _open(s, Relation.ADDRESS, iot_old, "172.16.11.229", key="ip:172.16.11.229")
    _open(s, Relation.ADDRESS, iot_old, "172.16.11.247", key="ip:172.16.11.247")
    _open(s, Relation.ADDRESS, iot_static, "172.16.11.222")
    st = build(s, timestamp=T)
    texts = [f["text"] for f in st["FINDINGS"]]
    assert not any(iot_old in t for t in texts)
    assert any("172.16.11.222" in t for t in texts)
    assert {d["id"]: d["dhcp"]["ever"] for d in st["DEV"]} == {iot_old: True, iot_static: False}


def test_link_local_with_kea_lease_is_no_finding():
    """A lease from Kea, but the firewall still had the link-local address from
    three days earlier."""
    s = MemoryStore()
    _open(s, Relation.ATTRIBUTE, "14:9a:07:77:ea:ee", "172.16.10.217", key="dhcp_lease")
    _open(s, Relation.ADDRESS, "14:9a:07:77:ea:ee", "169.254.212.204",
          key="ip:169.254.212.204")
    assert not build(s, timestamp=T)["FINDINGS"]


def test_finding_marked_as_expected_moves_to_the_end_and_carries_the_reason():
    """A game console on C3PO port 15 flaps every hour in standby."""
    from daedalus.state import collect_findings
    ports = {"c3po": [{"p": "gi15", "bundle": 1, "changes_24h": 67},
                      {"p": "gi16", "bundle": 1, "changes_24h": 40}]}
    findings = collect_findings(MemoryStore(), {}, ports, [], [], [],
                                {"c3po:gi15": {"expected": "console in standby"}})
    assert [(f["object"], f.get("expected")) for f in findings] == \
        [("c3po:gi16", None), ("c3po:gi15", "console in standby")]
