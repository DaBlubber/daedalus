# -*- coding: utf-8 -*-
"""Kea: names and addresses as the devices report them, and the configuration."""
from pathlib import Path

import pytest

from daedalus.collector_kea import (KeaConfig, KeaLeases, clean_hostname, parse_config,
                                    parse_leases)
from daedalus.model import Relation

# Shaped like a real answer (lease4-get-all on an HA node)
RESPONSE = [{"result": 0, "text": "4 IPv4 lease(s) found.", "arguments": {"leases": [
    {"hw-address": "44:4f:91:f9:32:cc", "ip-address": "172.16.10.13",
     "hostname": "device-08ef.example.com.", "state": 0},
    {"hw-address": "5c:29:f4:c2:03:f2", "ip-address": "172.16.10.30",
     "hostname": "device-d22d.corp.example.net.", "state": 0},
    {"hw-address": "00:c6:ea:cc:84:6d", "ip-address": "172.16.10.10",
     "hostname": "", "state": 0},
    {"hw-address": "aa:bb:cc:dd:ee:ff", "ip-address": "172.16.10.11",
     "hostname": "expired", "state": 2},
]}}]

# A real deployed configuration (pseudonymised)
CONFIG = (Path(__file__).parent / "samples" / "kea-dhcp4.conf").read_text(encoding="utf-8")

NODES = ("http://172.16.10.253:8000/", "http://172.16.10.251:8000/")


def test_names_are_shortened_foreign_domain_stays():
    assert clean_hostname("device-08ef.example.com.") == "device-08ef"
    assert clean_hostname("device-d22d.corp.example.net.") == "device-d22d.corp.example.net"
    assert clean_hostname("device-a6c1") == "device-a6c1"


def test_only_valid_leases_with_name_and_address():
    obs = parse_leases(RESPONSE)
    assert sorted((x.obj, x.key, x.value) for x in obs) == sorted([
        ("5c:29:f4:c2:03:f2", "dhcp_lease", "172.16.10.30"),
        ("5c:29:f4:c2:03:f2", "dhcp_name", "device-d22d.corp.example.net"),
        ("44:4f:91:f9:32:cc", "dhcp_lease", "172.16.10.13"),
        ("44:4f:91:f9:32:cc", "dhcp_name", "device-08ef"),
        ("00:c6:ea:cc:84:6d", "dhcp_lease", "172.16.10.10"),
    ])
    assert {x.relation for x in obs} == {Relation.ATTRIBUTE}


def test_empty_table_is_an_outage_not_all_gone():
    with pytest.raises(RuntimeError):
        parse_leases([{"result": 3, "arguments": {"leases": []}}])


def test_standby_steps_in():
    asked = []

    def caller(url):
        asked.append(url)
        if "253" in url:
            raise OSError("primary gone")
        return RESPONSE

    c = KeaLeases(NODES, caller=caller)
    assert len(c.collect()) == 5
    assert asked == list(NODES)
    assert c.source.responsible_for == frozenset({Relation.ATTRIBUTE})


def test_real_configuration_with_includes_and_comments():
    obs = parse_config(CONFIG)
    pools = {x.obj: x.value for x in obs if x.key == "dhcp_pools"}
    assert pools["net:172.16.10.0/24"] == "172.16.10.150-172.16.10.248"
    assert len(pools) == 6
    assert sum(1 for x in obs if x.key == "dhcp_reservation") == 40
    names = {x.obj: x.value for x in obs if x.key == "reservation_name"}
    assert names["12:30:bd:6a:36:d2"] == "device-6807"


def test_config_saves_with_unchanged_etag():
    calls = []

    def fetcher(etag):
        calls.append(etag)
        return (304, etag, "") if etag else (200, '"abc"', CONFIG)

    c = KeaConfig("https://git.example.com/kea-dhcp4.conf", fetcher=fetcher)
    assert c.precheck_unchanged() is False          # never read yet
    assert c.collect()
    assert c.precheck_unchanged() is True
    assert calls == ["", '"abc"']


def test_random_mac_is_volatile():
    response = [{"result": 0, "arguments": {"leases": [
        {"hw-address": "9e:3e:bc:71:64:d6", "ip-address": "172.16.10.195",
         "hostname": "", "state": 0}]}}]
    assert [o.volatile for o in parse_leases(response)] == [True]
