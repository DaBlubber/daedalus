# -*- coding: utf-8 -*-
"""The switch collectors, against a real sample from c3po (pseudonymised)."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from daedalus.collector import run_once
from daedalus.collector_switch import (OID, SwitchMacs, SwitchNeighbors, is_port_channel,
                                       mac_from_index, split_oid, vlan_from_index)
from daedalus.model import Relation

RAW = (Path(__file__).parent / "samples" / "switch-c3po.txt").read_text(encoding="utf-8")
T0 = datetime(2026, 9, 16, 8, 0, tzinfo=timezone.utc)
def t(n): return T0 + timedelta(minutes=5 * n)

# The sample is divided into sections; the caller serves from them.
SECTION = {"fdb": "### FDB", "bridgeport": "### BRIDGEPORT", "ifname": "### IFNAME",
           "lldpname": "### LLDPNAME", "lldpport": "### LLDPPORT",
           "lldplocal": "### LLDPLOCAL", "lastchange": "### IFLASTCHANGE"}


def part(name: str) -> str:
    mark = SECTION[name]
    rest = RAW.split(mark, 1)[1]
    return rest.split("###", 1)[0]


def caller(changes=None, raises=None):
    """Serves every OID from the sample. `changes` replaces single sections."""
    changes = changes or {}
    reverse = {v: k for k, v in OID.items()}
    def fetch(oid):
        if raises:
            raise raises
        name = reverse[oid]
        return changes.get(name, part(name))
    return fetch


def neighbors(**kw):
    return SwitchNeighbors("c3po", "172.16.0.3", "x", caller=caller(**kw))
def macs(uplinks=(), **kw):
    return SwitchMacs("c3po", "172.16.0.3", "x", uplinks=uplinks, caller=caller(**kw))


# --------------------------------------------------------------- splitting
def test_mac_from_the_fdb_index():
    assert mac_from_index("1.60.222.152.72.101.20") == "3c:de:98:48:65:14"
    assert vlan_from_index("1.60.222.152.72.101.20") == "1"


def test_short_index_gives_no_mac():
    assert mac_from_index("1.2.3") == ""


def test_split_only_takes_the_requested_oid():
    assert all(k.isdigit() or "." in k for k in split_oid(RAW, OID["ifname"]))
    assert split_oid(RAW, OID["ifname"])["49"] == "gi1"


# --------------------------------------------------------------- LLDP
def test_neighbors_are_recognised():
    n = neighbors().neighbor_ports()
    assert n["gi25"] == ("bb8", "gi1")
    assert "r2d2" in {x[0] for x in n.values()}


def test_neighbor_without_name_is_dropped():
    """An LLDP entry without a SysName is not a neighbourhood but noise."""
    n = neighbors().neighbor_ports()
    assert all(name for name, _ in n.values())


def test_lldp_runs_through(store):
    r = run_once(store, neighbors(), t(0))
    assert r.successful and r.observations >= 2
    assert store.open_for(Relation.CONNECTION, "c3po:gi25", "connection").value == "bb8:gi1"


# --------------------------------------------------- the cheap precheck
def test_precheck_applies_the_second_time():
    c = neighbors()
    assert c.precheck_unchanged() is False, "the first time there is nothing to compare"
    assert c.precheck_unchanged() is True,  "unchanged timestamps -> nothing to do"


def test_precheck_fires_when_a_port_flaps():
    c = neighbors()
    c.precheck_unchanged()
    c._call = caller(changes={"lastchange":
        part("lastchange").replace("6:10:51:35.78", "0:0:00:04.11")})
    assert c.precheck_unchanged() is False


def test_precheck_without_answer_rather_collects():
    c = neighbors()
    c._call = caller(changes={"lastchange": ""})
    assert c.precheck_unchanged() is False


def test_skipped_runs_keep_the_neighbors(store):
    c = neighbors()
    run_once(store, c, t(0))
    for n in (1, 2, 3, 4, 5):
        assert run_once(store, c, t(n)).skipped
    assert store.open_for(Relation.CONNECTION, "c3po:gi25", "connection") is not None


# --------------------------------------------------------------- MAC table
def test_on_c3po_the_channel_rule_is_already_enough(store):
    """Measured: on c3po ALL transit MACs sit on the channels Po1/Po2, none directly
    on gi25-gi28. The LLDP list therefore changes nothing here - it is still needed,
    see the next test."""
    without = macs().collect()
    with_ = macs(uplinks={"gi25", "gi26", "gi27", "gi28"}).collect()
    assert len(without) == len(with_)


def test_without_channel_the_lldp_list_is_needed(store):
    """Not every uplink is bundled. If a foreign MAC sits directly on gi25, only the
    neighbour list catches it."""
    foreign = part("fdb") + chr(10) + ".1.3.6.1.2.1.17.7.1.2.2.1.2.1.240.30.107.238.228.218 = 73" + chr(10)
    without = macs(changes={"fdb": foreign}).collect()
    with_ = macs(uplinks={"gi25"}, changes={"fdb": foreign}).collect()
    assert len(without) == len(with_) + 1
    assert any(x.value == "c3po:gi25" for x in without)
    assert all(x.value != "c3po:gi25" for x in with_)


def test_macs_on_uplinks_are_dropped(store):
    obs = macs(uplinks={"gi25", "gi26", "gi27", "gi28"}).collect()
    assert all(not x.value.endswith((":gi25", ":gi26", ":gi27", ":gi28")) for x in obs)


def test_a_device_lands_on_its_access_port(store):
    r = run_once(store, macs(uplinks={"gi25", "gi26"}), t(0))
    assert r.successful and r.observations >= 1
    one = store.all_open(Relation.ATTACHMENT)[0]
    assert one.value.startswith("c3po:gi")


def test_mac_table_has_no_cheap_precheck():
    """A MAC wanders without a link changing. Asking `ifLastChange` here loses
    exactly the moves."""
    assert macs().precheck_unchanged() is False


def test_empty_mac_table_counts_as_error(store):
    run_once(store, macs(uplinks={"gi25"}), t(0))
    before = len(store.all_open(Relation.ATTACHMENT))
    for n in (1, 2, 3):
        assert not run_once(store, macs(uplinks={"gi25"},
                            changes={"fdb": ""}), t(n)).successful
    assert len(store.all_open(Relation.ATTACHMENT)) == before


def test_failed_switch_makes_nothing_disappear(store):
    run_once(store, macs(uplinks={"gi25"}), t(0))
    before = len(store.all_open(Relation.ATTACHMENT))
    for n in (1, 2, 3, 4):
        assert not run_once(store, macs(uplinks={"gi25"},
                            raises=TimeoutError("no answer")), t(n)).successful
    assert len(store.all_open(Relation.ATTACHMENT)) == before


def test_the_sources_are_narrowly_scoped():
    assert neighbors().source.responsible_for == frozenset({Relation.CONNECTION})
    assert macs().source.responsible_for == frozenset({Relation.ATTACHMENT})
    assert neighbors().source.name != macs().source.name


# ---------------------------------------------------------------------------
# The finding from the real data: 52 of 60 MACs sit on a PORT CHANNEL.
# ---------------------------------------------------------------------------
def test_port_channel_is_recognised():
    assert is_port_channel("Po1") and is_port_channel("po12")
    assert not is_port_channel("gi1") and not is_port_channel("Port") and not is_port_channel("")


def test_the_port_channel_counts_as_uplink_even_without_lldp_entry():
    """LLDP reports the MEMBERS (gi25, gi26), the MAC table names the CHANNEL (Po1).
    Following only the LLDP list plugs 52 devices into one socket."""
    obs = macs(uplinks=set()).collect()          # deliberately WITHOUT uplink knowledge
    assert all(not x.value.lower().endswith(":po1") for x in obs)


def test_without_the_channel_rule_it_would_be_wrong():
    """Cross-check against the real numbers: the channel carries the majority."""
    fdb = split_oid(part("fdb"), OID["fdb"])
    bp = split_oid(part("bridgeport"), OID["bridgeport"])
    nm = split_oid(part("ifname"), OID["ifname"])
    on_channel = sum(1 for v in fdb.values() if is_port_channel(nm.get(bp.get(v, v), "")))
    assert on_channel > len(fdb) / 2, "the majority of MACs sits on the channel"


# ---------------------------------------------------------------------------
# The finding from the first real run: lldpLocPortId reports a MAC instead of a
# port name on access point ports.
# ---------------------------------------------------------------------------
RAW_R2D2 = (Path(__file__).parent / "samples" / "switch-r2d2.txt").read_text(encoding="utf-8")


def part_r2d2(name: str) -> str:
    mark = SECTION[name]
    return RAW_R2D2.split(mark, 1)[1].split("###", 1)[0]


def r2d2_neighbors():
    reverse = {v: k for k, v in OID.items()}
    return SwitchNeighbors("r2d2", "172.16.0.2", "x",
                           caller=lambda oid: part_r2d2(reverse[oid]))


def test_ap_ports_are_resolved_despite_mac_id():
    """On r2d2 four ports report their local id as a MAC (subtype 3), six as a port
    name (subtype 5). Both resolve through ifName."""
    n = r2d2_neighbors().neighbor_ports()
    assert n["gi5"][0] == "Leia", "the AP port must be resolved"
    assert n["gi25"][0] == "bb8", "the switch port still as well"
    assert not any(" " in p for p in n), "no MAC may pass as a port name"


def test_ap_ports_count_as_uplinks():
    """An AP port is an uplink like any other: devices are behind it, not on it.
    Without this rule the first real run reported 131 Wi-Fi clients as moved from
    ap:Leia to r2d2:gi5."""
    up = r2d2_neighbors().uplink_ports()
    assert {"gi5", "gi12", "gi15", "gi24"} <= up, "the four AP ports"
    assert {"gi25", "gi26", "gi27", "gi28", "gi7", "gi10"} <= up, "the switch ports"


# ---------------------------------------------------------------------------
# In the service LLDP and the MAC table run at different intervals (30 min vs.
# 5 min). The uplink list therefore has to travel between them.
# ---------------------------------------------------------------------------
def test_mac_collector_reads_the_uplinks_from_the_neighbor_collector():
    nb = neighbors()
    mc = SwitchMacs("c3po", "172.16.0.3", "x", neighbors=nb, caller=caller())
    nb.collect()                                  # LLDP ran
    assert "gi25" in mc.uplinks


def test_without_a_previous_lldp_run_the_mac_table_reports_nothing(store):
    """Better nothing at all than every device behind the uplink on the uplink."""
    nb = neighbors()
    mc = SwitchMacs("c3po", "172.16.0.3", "x", neighbors=nb, caller=caller())
    r = run_once(store, mc, t(0))
    assert not r.successful and "uplinks unknown yet" in r.error
    assert store.all_open(Relation.ATTACHMENT) == []


def test_and_afterwards_it_reports_again(store):
    nb = neighbors()
    mc = SwitchMacs("c3po", "172.16.0.3", "x", neighbors=nb, caller=caller())
    run_once(store, mc, t(0))                     # fails, as it should
    run_once(store, nb, t(1))                     # LLDP runs
    r = run_once(store, mc, t(2))
    assert r.successful and r.observations > 0
