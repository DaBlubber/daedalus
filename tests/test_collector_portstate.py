# -*- coding: utf-8 -*-
"""Port state on real walks of r2d2 (with PoE) and bb8 (without)."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from daedalus.collector_portstate import (OID, SwitchPortState, bitfield_ports, parse,
                                          vlan_text)
from daedalus.store import compute_health

SAMPLES = Path(__file__).parent / "samples"


def _raw(name):
    text = (SAMPLES / f"portstate-{name}.txt").read_text(encoding="utf-8")
    return {k: text for k in OID}          # split_oid filters per OID itself


def _attributes(obs):
    return {(o.obj, o.key): o.value for o in obs}


def test_r2d2_delivers_link_vlan_and_poe():
    obs, counters = parse("r2d2", _raw("r2d2"), uplinks={"gi5", "gi25"})
    a = _attributes(obs)
    # gi5 is Leia's port: uplink (via LLDP), PoE delivering
    assert a[("r2d2:gi5", "link")] == "up"
    assert a[("r2d2:gi5", "poe")] == "delivering"
    assert ("r2d2:gi5", "link_access") not in a
    assert a[("r2d2:gi5", "speed")] == "1000"
    assert a[("r2d2:gi5", "duplex")] == "full"
    assert a[("r2d2:gi5", "vlans")]            # some membership
    # budget of the switch
    assert a[("r2d2", "poe_budget")] == "180"
    leia = next(c for c in counters if c.port == "r2d2:gi5")
    assert leia.poe_mw and 1000 < leia.poe_mw < 30000
    assert next(c for c in counters if c.port == "r2d2").poe_budget_w == 180


def test_access_port_reports_link_silently():
    obs, _ = parse("r2d2", _raw("r2d2"), uplinks=set())
    keys = {o.key for o in obs if o.obj == "r2d2:gi1"}
    assert "link_access" in keys and "link" not in keys


def test_bb8_without_poe_and_without_foreign_lines():
    obs, counters = parse("bb8", _raw("bb8"), uplinks=set())
    assert not any(o.key in ("poe", "poe_budget") for o in obs)
    ports = {o.obj for o in obs}
    assert "bb8:gi1" in ports and all(p.startswith("bb8:") for p in ports)
    # real ports only: no VLAN interfaces or the like
    assert all(p.split(":")[1][:2] in ("gi", "fa", "te", "Po", "po") for p in ports)


def test_missing_table_is_checked_once_and_then_skipped():
    walked = []
    sample = (SAMPLES / "portstate-bb8.txt").read_text(encoding="utf-8")

    def walk(oid):
        walked.append(oid)
        return sample

    def prober(oid):
        # bb8 has no PoE: GETNEXT lands outside the table
        return ".1.3.6.1.2.1.105.2.1.0" if oid.startswith("1.3.6.1.2.1.105") or \
            oid.startswith("1.3.6.1.4.1.9.6.1.101.108") else "." + oid + ".1"

    c = SwitchPortState("bb8", "172.16.0.4", "x", caller=walk, prober=prober)
    c.collect()
    c.collect()
    assert OID["poe_status"] not in walked and OID["poe_mw"] not in walked
    assert walked.count(OID["oper"]) == 2


def test_bitfield_and_vlan_text():
    assert bitfield_ports("80 01") == {1, 16}
    assert vlan_text([1], [20, 5, 10]) == "u:1 t:5,10,20"


def test_rates_with_counter_restart():
    t0 = datetime(2026, 9, 16, 10, 0, tzinfo=timezone.utc)
    rows = [("r2d2:gi24", t0 - timedelta(hours=23), 100, 0, 5, 0, None, None),
            ("r2d2:gi24", t0 - timedelta(minutes=50), 110, 0, 5, 0, None, None),
            ("r2d2:gi24", t0, 121, 2, 7, 0, 3300, None),
            # restart: the counter starts at zero again
            ("c3po:gi3", t0 - timedelta(hours=2), 500, 0, 0, 0, None, None),
            ("c3po:gi3", t0, 4, 0, 0, 0, None, None)]
    h = compute_health(rows, t0)
    assert h["r2d2:gi24"]["errors_1h"] == 13
    assert h["r2d2:gi24"]["errors_24h"] == 23
    assert h["r2d2:gi24"]["discards_24h"] == 2
    assert h["r2d2:gi24"]["poe_w"] == 3.3
    assert h["c3po:gi3"]["errors_24h"] == 4
