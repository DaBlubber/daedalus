# -*- coding: utf-8 -*-
"""Prometheus collectors against real excerpts (pseudonymised)."""
from __future__ import annotations

import copy
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from daedalus.collector import run_once
from daedalus.collector_prometheus import (
    QUERY_INVENTORY_PRECHECK,
    QUERY_WIFI,
    QUERY_WIFI_PRECHECK,
    PrometheusInventory,
    PrometheusWifi,
    parse_inventory,
    parse_wifi,
)
from daedalus.model import Event, Relation

SAMPLES = Path(__file__).parent / "samples"
WIFI = json.loads((SAMPLES / "prometheus-wifi.json").read_text(encoding="utf-8"))
TARGETS = json.loads((SAMPLES / "prometheus-targets.json").read_text(encoding="utf-8"))
T0 = datetime(2026, 9, 16, 8, 0, tzinfo=timezone.utc)


def t(n):
    return T0 + timedelta(minutes=5 * n)


def precheck_from_wifi(response=WIFI):
    rows = []
    for r in response["data"]["result"]:
        fields = ("mac", "ip", "name", "vlan", "essid", "ap_name", "oui")
        rows.append({"metric": {f: r["metric"][f] for f in fields if f in r["metric"]},
                     "value": [r["value"][0], "1"]})
    return {"status": "success", "data": {"resultType": "vector", "result": rows}}


def precheck_from_targets(response=TARGETS):
    pairs = sorted({(x["labels"].get("host"), x["labels"].get("instance"))
                    for x in response["data"]["activeTargets"]
                    if x.get("labels", {}).get("host")})
    return {"status": "success", "data": {"resultType": "vector", "result": [
        {"metric": {"host": h, "instance": i}, "value": [1789539972.0, "1"]}
        for h, i in pairs
    ]}}


class Responses:
    def __init__(self, wifi=WIFI, targets=TARGETS, raises=None):
        self.wifi = wifi
        self.targets = targets
        self.raises = raises
        self.calls = []

    def __call__(self, path, params):
        self.calls.append((path, params))
        if self.raises:
            raise self.raises
        if path == "/api/v1/targets":
            return self.targets
        expression = params["query"]
        if expression == QUERY_WIFI:
            return self.wifi
        if expression == QUERY_WIFI_PRECHECK:
            return precheck_from_wifi(self.wifi)
        if expression == QUERY_INVENTORY_PRECHECK:
            return precheck_from_targets(self.targets)
        raise AssertionError(f"unexpected query: {expression}")


def test_real_wifi_sample_has_all_relations():
    obs = parse_wifi(WIFI)
    assert {x.relation for x in obs} == {
        Relation.ADDRESS, Relation.ATTACHMENT, Relation.ATTRIBUTE,
    }
    assert any(x.obj == "00:85:07:12:dd:fd" and x.value == "ap:Anakin" for x in obs)
    assert any(x.key == "oui" and x.value == "Espressif Inc." for x in obs)


def test_inventory_deduplicates_hosts_and_is_narrowly_scoped():
    obs = parse_inventory(TARGETS)
    assert [(x.obj, x.key, x.value) for x in obs] == [
        ("host-9d56", "name", "host-9d56"), ("sophos", "name", "sophos"),
    ]
    c = PrometheusInventory("http://prometheus", caller=Responses())
    assert c.source.responsible_for == frozenset({Relation.ATTRIBUTE})


def test_random_mac_is_flagged_on_the_client():
    obs = parse_wifi(WIFI)
    flags = [x for x in obs if x.key == "random_mac"]
    assert [(x.obj, x.value) for x in flags] == [("2a:70:66:44:08:90", "yes")]


def test_missing_and_unexpected_fields_do_not_break_anything():
    sample = copy.deepcopy(WIFI)
    sample["data"]["result"][0]["metric"].pop("ip")
    sample["data"]["result"][0]["metric"]["new_and_unknown"] = {"not": "a string"}
    sample["data"]["result"].append({"metric": {"mac": "broken", "ip": 17}})
    obs = parse_wifi(sample)
    assert not any(x.obj == "00:85:07:12:dd:fd" and
                   x.relation is Relation.ADDRESS for x in obs)
    assert any(x.obj == "00:85:07:12:dd:fd" and
               x.relation is Relation.ATTACHMENT for x in obs)


def test_unchanged_run_saves_the_expensive_wifi_query(store):
    responses = Responses()
    c = PrometheusWifi("http://prometheus", caller=responses)
    assert run_once(store, c, t(0)).successful
    before = store.interval_count()
    responses.calls.clear()
    r = run_once(store, c, t(1))
    assert r.skipped and store.interval_count() == before
    assert responses.calls == [("/api/v1/query", {"query": QUERY_WIFI_PRECHECK})]


def test_unchanged_run_saves_the_expensive_target_query(store):
    responses = Responses()
    c = PrometheusInventory("http://prometheus", caller=responses)
    run_once(store, c, t(0))
    responses.calls.clear()
    assert run_once(store, c, t(1)).skipped
    assert responses.calls == [
        ("/api/v1/query", {"query": QUERY_INVENTORY_PRECHECK})]


def test_precheck_error_leads_to_full_query(store):
    class BrokenPrecheck(Responses):
        def __call__(self, path, params):
            if params and params.get("query") == QUERY_WIFI_PRECHECK:
                self.calls.append((path, params))
                raise TimeoutError("precheck failed")
            return super().__call__(path, params)

    responses = BrokenPrecheck()
    c = PrometheusWifi("http://prometheus", caller=responses)
    r = run_once(store, c, t(0))
    assert r.successful
    assert any(p == {"query": QUERY_WIFI} for _, p in responses.calls)


def test_failed_source_makes_nothing_disappear(store):
    c = PrometheusWifi("http://prometheus", caller=Responses())
    run_once(store, c, t(0))
    c._caller = Responses(raises=TimeoutError("Prometheus gone"))
    for n in (1, 2, 3):
        assert not run_once(store, c, t(n)).successful
    assert store.open_for(
        Relation.ATTACHMENT, "00:85:07:12:dd:fd", "attachment") is not None


def test_empty_wifi_answer_is_an_error(store):
    empty = {"status": "success", "data": {"resultType": "vector", "result": []}}
    c = PrometheusWifi("http://prometheus", caller=Responses(wifi=empty))
    assert not run_once(store, c, t(0)).successful


def test_ap_change_is_tracked_but_not_reported(store):
    responses = Responses()
    c = PrometheusWifi("http://prometheus", caller=responses)
    run_once(store, c, t(0))
    changed = copy.deepcopy(WIFI)
    changed["data"]["result"][0]["metric"]["ap_name"] = "Leia"
    responses.wifi = changed
    r = run_once(store, c, t(1))
    assert [x for x in r.changes if x.obj == "00:85:07:12:dd:fd"] == []
    current = store.open_for(Relation.ATTACHMENT, "00:85:07:12:dd:fd", "attachment")
    assert current.value == "ap:Leia"


def test_new_non_random_mac_is_reported_as_new_normally(store):
    first = copy.deepcopy(WIFI)
    new_row = first["data"]["result"].pop(0)
    responses = Responses(wifi=first)
    c = PrometheusWifi("http://prometheus", caller=responses)
    run_once(store, c, t(0))
    first["data"]["result"].append(new_row)
    r = run_once(store, c, t(1))
    assert any(x.kind is Event.FIRST_SEEN and
               x.obj == "00:85:07:12:dd:fd" for x in r.changes)


def test_random_macs_are_flagged_as_volatile():
    """Not only as an attribute: reconciliation must not report them at all."""
    response = {"status": "success", "data": {"result": [
        {"metric": {"mac": "96:0D:91:1F:48:D9", "ip": "172.16.10.91",
                    "ap_name": "AP HALL", "name": "phone"}},
        {"metric": {"mac": "80:F6:0F:9A:BA:DD", "ip": "172.16.10.92",
                    "ap_name": "AP HALL", "name": "laptop"}},
    ]}}
    obs = parse_wifi(response)
    randomised = [x for x in obs if x.obj == "96:0d:91:1f:48:d9"]
    fixed = [x for x in obs if x.obj == "80:f6:0f:9a:ba:dd"]
    assert randomised and all(x.volatile for x in randomised)
    assert fixed and not any(x.volatile for x in fixed)


def test_null_address_is_not_an_address():
    """A client that has joined but has no DHCP address yet reports 0.0.0.0. On a
    real run that produced the change "0.0.0.0 -> 172.16.11.232" - an intermediate
    state, not news."""
    response = {"status": "success", "data": {"result": [
        {"metric": {"mac": "8A:BF:AB:4D:27:5C", "ip": "0.0.0.0", "ap_name": "Luke"}},
        {"metric": {"mac": "E2:05:40:AF:C7:DC", "ip": "172.16.11.5", "ap_name": "Luke"}},
    ]}}
    obs = parse_wifi(response)
    addresses = [x for x in obs if x.relation is Relation.ADDRESS]
    assert [x.value for x in addresses] == ["172.16.11.5"]
    # the attachment is kept all the same - the device IS there
    assert any(x.obj == "8a:bf:ab:4d:27:5c" and x.relation is Relation.ATTACHMENT
               for x in obs)


def test_inventory_delivers_an_unambiguous_address_per_host():
    targets = {"status": "success", "data": {"activeTargets": [
        {"labels": {"host": "host-59a8", "instance": "172.16.1.6:9100"}},
        {"labels": {"host": "host-59a8", "instance": "172.16.1.6:8080"}},
        {"labels": {"host": "host-9d56", "instance": "127.0.0.1:9100"}},
        {"labels": {"host": "two", "instance": "172.16.1.30:9100"}},
        {"labels": {"host": "two", "instance": "172.16.10.30:9100"}},
    ]}}
    addresses = {(o.obj, o.value) for o in parse_inventory(targets)
                 if o.key == "inventory_address"}
    assert addresses == {("host-59a8", "172.16.1.6")}
