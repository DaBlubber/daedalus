# -*- coding: utf-8 -*-
"""The ARP collector, against a real sample from a firewall.

The sample in `tests/samples/firewall-arp.txt` was really taken from a firewall
(pseudonymised before publication) - so the tests fail on what the device
delivers, not on what one imagines."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from daedalus.collector import run_once
from daedalus.collector_arp import ArpCollector, is_random_mac, normalize_mac, parse
from daedalus.model import Relation

SAMPLE = (Path(__file__).parent / "samples" / "firewall-arp.txt").read_text(encoding="utf-8")
T0 = datetime(2026, 9, 16, 8, 0, tzinfo=timezone.utc)
def t(n): return T0 + timedelta(minutes=5 * n)


def collector(output=SAMPLE, raises=None):
    def caller():
        if raises:
            raise raises
        return output
    return ArpCollector("172.16.1.1", "secret", caller=caller)


# --------------------------------------------------------------- parsing
def test_sample_is_parsed_completely():
    obs = parse(SAMPLE)
    assert len(obs) >= 30, "the sample contains at least 40 ARP lines"
    assert all(x.relation is Relation.ADDRESS for x in obs)


def test_first_entry_is_right():
    obs = parse(SAMPLE)
    first = [x for x in obs if x.value == "172.16.0.2"][0]
    assert first.obj == "80:f6:0f:9a:ba:dd"      # r2d2


def test_mac_is_normalised():
    assert normalize_mac("80 F6 0F 9A BA DD ") == "80:f6:0f:9a:ba:dd"
    assert normalize_mac("78 62 60 46 A9 3D") == "78:62:60:46:a9:3d", "single-digit bytes"


def test_the_object_key_is_the_mac_not_the_ip():
    """IPs wander, MACs mostly do not."""
    obs = parse(SAMPLE)
    assert all(":" in x.obj for x in obs)
    assert all(x.value.count(".") == 3 for x in obs)


def test_broken_lines_do_not_cost_the_whole_table():
    broken = SAMPLE + "\ncompletely useless line\n.1.3.6.1.2.1.4.22.1.2 = \n"
    assert len(parse(broken)) == len(parse(SAMPLE))


def test_incomplete_mac_is_dropped():
    assert parse('.1.3.6.1.2.1.4.22.1.2.11.172.16.0.9 = "C8 00 84 "') == []


def test_empty_output_yields_nothing():
    assert parse("") == []


# --------------------------------------------------------------- random MAC
@pytest.mark.parametrize("mac,expected", [
    ("80:f6:0f:9a:ba:dd", False),    # vendor-assigned
    ("96:0d:91:1f:48:d9", True),     # locally administered -> randomised
    ("d6:96:81:be:6a:fc", True),
    ("76:1c:16:25:3d:7d", True),
    ("78:62:60:46:a9:3d", False),
])
def test_recognise_random_mac(mac, expected):
    assert is_random_mac(mac) is expected


def test_random_mac_does_not_choke_on_garbage():
    assert is_random_mac("") is False and is_random_mac("no:mac") is False


# --------------------------------------------------------------- in a run
def test_ordinary_run(store):
    r = run_once(store, collector(), t(0))
    assert r.successful and r.observations >= 30
    assert store.open_for(Relation.ADDRESS, "80:f6:0f:9a:ba:dd", "ip:172.16.0.2").value == "172.16.0.2"


def test_unchanged_runs_produce_no_row(store):
    run_once(store, collector(), t(0))
    before = store.interval_count()
    for n in (1, 2, 3, 4):
        assert run_once(store, collector(), t(n)).changes == ()
    assert store.interval_count() == before


def test_no_cheap_precheck_and_that_is_honest(store):
    """Whoever has none says False - and collects. Not: pretends to have one."""
    assert collector().precheck_unchanged() is False


def test_empty_answer_counts_as_error_not_as_empty_table(store):
    """An active firewall always has ARP entries. An empty answer is an error -
    and by rule 1 it must make nothing disappear."""
    run_once(store, collector(), t(0))
    for n in (1, 2, 3):
        r = run_once(store, collector(output=""), t(n))
        assert not r.successful
    assert store.open_for(Relation.ADDRESS, "80:f6:0f:9a:ba:dd", "ip:172.16.0.2") is not None


def test_failed_firewall_makes_nothing_disappear(store):
    run_once(store, collector(), t(0))
    for n in (1, 2, 3, 4):
        assert not run_once(store, collector(raises=TimeoutError("no answer")), t(n)).successful
    assert store.open_for(Relation.ADDRESS, "80:f6:0f:9a:ba:dd", "ip:172.16.0.2") is not None


def test_address_change_is_recognised(store):
    run_once(store, collector(), t(0))
    moved = SAMPLE.replace(".11.172.16.0.2 =", ".11.172.16.0.99 =")
    r = run_once(store, collector(output=moved), t(1))
    # One interval per address: the new one is added immediately, the old one only
    # goes once it has been missing for an hour (ARP ages out, see missing_threshold).
    assert any(c.after == "172.16.0.99" and c.before is None for c in r.changes)
    assert store.open_for(Relation.ADDRESS, "80:f6:0f:9a:ba:dd", "ip:172.16.0.2") is not None


def test_the_source_is_narrowly_scoped():
    """It sees addresses. Its silence must not end an attachment."""
    assert collector().source.responsible_for == frozenset({Relation.ADDRESS})


# ---------------------------------------------------------------------------
# The same OID, two notations - both measured in the same network.
# ---------------------------------------------------------------------------
SAMPLE_ALPINE = (Path(__file__).parent / "samples" / "firewall-arp-alpine.txt").read_text(encoding="utf-8")


def test_colon_notation_is_read_too():
    """The container without MIBs writes `80:f6:f:9a:ba:dd` instead of
    `"80 F6 0F 9A BA DD "`. A collector must not rely on which tool happens to be
    installed."""
    obs = parse(SAMPLE_ALPINE)
    assert len(obs) >= 30
    assert any(x.obj == "80:f6:0f:9a:ba:dd" and x.value == "172.16.0.2" for x in obs)


def test_both_notations_give_the_same_mac():
    assert normalize_mac("80 F6 0F 9A BA DD ") == normalize_mac("80:f6:f:9a:ba:dd")


def test_unpadded_bytes_are_padded():
    """`c8:0:84:...` - the second byte has only one digit."""
    assert normalize_mac("80:f6:f:9a:ba:dd") == "80:f6:0f:9a:ba:dd"


def test_the_same_firewall_gives_the_same_devices_from_both_samples():
    """The cross-check: two tools, the same firewall, the same answer."""
    a = {x.obj: x.value for x in parse(SAMPLE)}
    c = {x.obj: x.value for x in parse(SAMPLE_ALPINE)}
    common = set(a) & set(c)
    assert len(common) >= 30
    assert all(a[m] == c[m] for m in common)


def test_null_address_is_not_an_observation():
    real = '.1.3.6.1.2.1.4.22.1.2.11.172.16.11.232 = "F4 51 4D B1 B3 8F "'
    null = '.1.3.6.1.2.1.4.22.1.2.11.0.0.0.0 = "F4 51 4D B1 B3 8F "'
    assert len(parse(real)) == 1          # the line format is right ...
    assert parse(null) == []              # ... and only the null address is dropped
