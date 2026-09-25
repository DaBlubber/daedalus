# -*- coding: utf-8 -*-
"""Artificial collection runs. Every test is a case that happens in a real network
and in which inventory tools are usually wrong."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from daedalus.model import Event, Observation, Relation, Run, Source
from daedalus.reconcile import ingest, marks

# Internally everything is computed in UTC with a time zone. A naive timestamp
# would be ambiguous twice a year, and PostgreSQL returns `timestamptz` with a
# time zone anyway.
T0 = datetime(2026, 9, 15, 20, 0, tzinfo=timezone.utc)
def t(n: int) -> datetime:
    return T0 + timedelta(minutes=5 * n)

FDB = Source(name="fdb", responsible_for=frozenset({Relation.ATTACHMENT}),
             missing_threshold=2)
ARP = Source(name="arp", responsible_for=frozenset({Relation.ADDRESS}),
             missing_threshold=2)


def attachment(device: str, port: str) -> Observation:
    return Observation(Relation.ATTACHMENT, device, "attachment", port)


def address(device: str, ip: str) -> Observation:
    return Observation(Relation.ADDRESS, device, "address", ip)


def run(store, source, n, obs, successful=True):
    return ingest(store, Run(source.name, t(n), successful), source, obs)


# ---------------------------------------------------------------------------
# Rule 4: the first run is inventory, not a flood of new arrivals
# ---------------------------------------------------------------------------
def test_first_run_reports_nothing_as_new(store):
    s = store
    ch = run(s, FDB, 0, [attachment("laptop", "c3po:gi12"),
                         attachment("printer", "c3po:gi14")])
    assert ch == []
    assert len(s.all_open(Relation.ATTACHMENT)) == 2


def test_a_newly_connected_source_also_starts_with_inventory(store):
    s = store
    run(s, FDB, 0, [attachment("laptop", "c3po:gi12")])
    run(s, FDB, 1, [attachment("laptop", "c3po:gi12")])
    # ARP joins later and knows the device for the first time
    ch = run(s, ARP, 2, [address("laptop", "172.16.10.87")])
    assert ch == []


# ---------------------------------------------------------------------------
# The normal case: nothing changes, so nothing grows
# ---------------------------------------------------------------------------
def test_unchanged_produces_no_row(store):
    s = store
    run(s, FDB, 0, [attachment("laptop", "c3po:gi12")])
    before = s.interval_count()
    for n in range(1, 12):
        assert run(s, FDB, n, [attachment("laptop", "c3po:gi12")]) == []
    assert s.interval_count() == before, "intervals must not grow with time"
    assert s.changes_since(T0) == []


# ---------------------------------------------------------------------------
# Move: a device changes its port
# ---------------------------------------------------------------------------
def test_move_marks_device_and_both_ports(store):
    s = store
    run(s, FDB, 0, [attachment("lab-pi", "c3po:gi15")])
    ch = run(s, FDB, 1, [attachment("lab-pi", "c3po:gi16")])

    assert [c.kind for c in ch] == [Event.MOVED]
    c = ch[0]
    assert c.before == "c3po:gi15" and c.after == "c3po:gi16"
    assert set(c.affects) == {"lab-pi", "c3po:gi15", "c3po:gi16"}

    old = [i for i in s.history("lab-pi") if i.value == "c3po:gi15"][0]
    assert old.until == t(1), "the old interval must be closed"
    assert s.open_for(Relation.ATTACHMENT, "lab-pi", "attachment").value == "c3po:gi16"


# ---------------------------------------------------------------------------
# Rule 1: a failed source must not make ANYTHING disappear
# ---------------------------------------------------------------------------
def test_failed_source_makes_nothing_disappear(store):
    s = store
    run(s, FDB, 0, [attachment("laptop", "c3po:gi12"),
                    attachment("printer", "c3po:gi14")])
    for n in (1, 2, 3):
        assert run(s, FDB, n, [], successful=False) == []
    assert len(s.all_open(Relation.ATTACHMENT)) == 2
    assert s.changes_since(T0) == []


# ---------------------------------------------------------------------------
# Threshold: not seen once is not yet a disappearance
# ---------------------------------------------------------------------------
def test_missing_once_is_not_enough(store):
    s = store
    run(s, FDB, 0, [attachment("laptop", "c3po:gi12")])
    assert run(s, FDB, 1, []) == []                      # missing once
    assert s.open_for(Relation.ATTACHMENT, "laptop", "attachment") is not None


def test_missing_twice_makes_it_disappear(store):
    s = store
    run(s, FDB, 0, [attachment("laptop", "c3po:gi12")])
    run(s, FDB, 1, [])
    ch = run(s, FDB, 2, [])
    # ONE event: "disappeared" contains the end of the attachment. There used to be
    # an extra "moved c3po:gi12 -> -" - a move that never happened.
    assert [c.kind for c in ch] == [Event.DISAPPEARED]
    assert "c3po:gi12" in ch[0].affects     # the port is marked all the same
    assert ch[0].key == "attachment"
    assert s.open_for(Relation.ATTACHMENT, "laptop", "attachment") is None


def test_disconnected_instead_of_moved_if_something_else_remains(store):
    s = store
    run(s, FDB, 0, [attachment("laptop", "c3po:gi12")])
    run(s, ARP, 0, [address("laptop", "172.16.10.87")])    # the address remains
    run(s, FDB, 1, [])
    ch = run(s, FDB, 2, [])
    assert [(c.kind, c.before) for c in ch] == [(Event.DISCONNECTED, "c3po:gi12")]


def test_attributes_only_report_real_value_changes(store):
    s = store
    src = Source("wifi", frozenset({Relation.ATTRIBUTE, Relation.ATTACHMENT}), 1)
    a = lambda k, v: Observation(Relation.ATTRIBUTE, "phone", k, v)
    att = lambda v: Observation(Relation.ATTACHMENT, "phone", "attachment", v)
    run(s, src, 0, [att("ap:Luke")])
    # New attributes on a known object: silent
    assert run(s, src, 1, [att("ap:Luke"), a("name", "Pixel"), a("vlan", "10")]) == []
    # A value changes: reported, with which attribute
    ch = run(s, src, 2, [att("ap:Luke"), a("name", "Pixel"), a("vlan", "20")])
    assert [(x.kind, x.key, x.before, x.after) for x in ch] == [
        (Event.ATTRIBUTE_CHANGED, "vlan", "10", "20")]
    # An attribute goes away: silent
    assert run(s, src, 3, [att("ap:Luke"), a("name", "Pixel")]) == []


def test_seen_again_resets_the_counter(store):
    s = store
    run(s, FDB, 0, [attachment("laptop", "c3po:gi12")])
    run(s, FDB, 1, [])                                    # missing once
    run(s, FDB, 2, [attachment("laptop", "c3po:gi12")])   # back
    assert run(s, FDB, 3, []) == []                       # counts from zero again
    assert s.open_for(Relation.ATTACHMENT, "laptop", "attachment") is not None


# ---------------------------------------------------------------------------
# Rule 2: a source only judges what it is responsible for
# ---------------------------------------------------------------------------
def test_foreign_source_ends_no_attachment(store):
    s = store
    run(s, FDB, 0, [attachment("laptop", "c3po:gi12")])
    run(s, ARP, 1, [address("laptop", "172.16.10.87")])
    # ARP keeps running and never sees an attachment - that must end nothing
    for n in (2, 3, 4, 5):
        run(s, ARP, n, [address("laptop", "172.16.10.87")])
    assert s.open_for(Relation.ATTACHMENT, "laptop", "attachment") is not None


def test_source_is_not_fooled_by_observations_outside_its_responsibility(store):
    """Even if a collector delivers more than it can judge."""
    s = store
    ch = run(s, ARP, 0, [address("laptop", "172.16.10.87"),
                         attachment("laptop", "somewhere")])
    assert ch == []
    assert s.all_open(Relation.ATTACHMENT) == []


# ---------------------------------------------------------------------------
# Rule 3: the same run imported twice changes nothing
# ---------------------------------------------------------------------------
def test_repeated_import_has_no_effect(store):
    s = store
    run(s, FDB, 0, [attachment("laptop", "c3po:gi12")])
    ingest(s, Run("fdb", t(1)), FDB, [attachment("laptop", "c3po:gi16")])
    state = (s.interval_count(), len(s.changes_since(T0)))
    # the same run once more
    again = ingest(s, Run("fdb", t(1)), FDB, [attachment("laptop", "c3po:gi16")])
    assert again == []
    assert (s.interval_count(), len(s.changes_since(T0))) == state


# ---------------------------------------------------------------------------
# Addresses are assignments with a period, not a string on the device
# ---------------------------------------------------------------------------
def test_address_change_closes_the_old_interval(store):
    s = store
    run(s, ARP, 0, [address("phone", "172.16.10.91")])
    ch = run(s, ARP, 1, [address("phone", "172.16.12.41")])
    assert [c.kind for c in ch] == [Event.ADDRESS_ADDED]
    old = [i for i in s.history("phone") if i.value == "172.16.10.91"][0]
    assert old.until == t(1)
    # and history can be answered without gaps
    assert old.since == t(0)


# ---------------------------------------------------------------------------
# New device outside the first run
# ---------------------------------------------------------------------------
def test_new_device_is_reported_as_first_seen(store):
    s = store
    run(s, FDB, 0, [attachment("laptop", "c3po:gi12")])
    ch = run(s, FDB, 1, [attachment("laptop", "c3po:gi12"),
                         attachment("newcomer", "c3po:gi31")])
    kinds = [c.kind for c in ch]
    assert Event.FIRST_SEEN in kinds
    first = [c for c in ch if c.kind is Event.FIRST_SEEN][0]
    assert first.obj == "newcomer"
    assert "c3po:gi31" in first.affects, "the port must be marked as well"


# ---------------------------------------------------------------------------
# The mark on the node is a derivation, not a stored colour
# ---------------------------------------------------------------------------
def test_marks_show_the_weightiest_change(store):
    s = store
    run(s, FDB, 0, [attachment("a", "sw:1"), attachment("b", "sw:2")])
    run(s, FDB, 1, [attachment("a", "sw:3"), attachment("b", "sw:2"),
                    attachment("c", "sw:4")])          # a moves, c is new
    run(s, FDB, 2, [attachment("a", "sw:3"), attachment("c", "sw:4")])
    run(s, FDB, 3, [attachment("a", "sw:3"), attachment("c", "sw:4")])  # b gone

    m = marks(s, since=t(1))
    assert m["a"] is Event.MOVED
    assert m["b"] is Event.DISAPPEARED
    assert m["c"] is Event.FIRST_SEEN
    # the ports involved carry the mark as well
    assert m["sw:1"] is Event.MOVED and m["sw:3"] is Event.MOVED


def test_marks_respect_the_point_in_time(store):
    s = store
    run(s, FDB, 0, [attachment("a", "sw:1")])
    run(s, FDB, 1, [attachment("a", "sw:2")])
    assert "a" in marks(s, since=t(1))
    assert marks(s, since=t(2)) == {}, "older changes must not mark anymore"


# ---------------------------------------------------------------------------
# Volatile objects: phones randomise their MAC. They are tracked, but their
# appearing and disappearing is not news.
# ---------------------------------------------------------------------------
def volatile(d, p):
    return Observation(Relation.ATTACHMENT, d, "attachment", p, volatile=True)


def test_volatile_device_is_not_reported_as_new(store):
    s = store
    run(s, FDB, 0, [attachment("laptop", "c3po:gi12")])
    ch = run(s, FDB, 1, [attachment("laptop", "c3po:gi12"),
                         volatile("96:0d:91:1f:48:d9", "ap:AP HALL")])
    assert [c.kind for c in ch] == [], "a randomised MAC is not a new device"
    # it is tracked all the same
    assert s.open_for(Relation.ATTACHMENT, "96:0d:91:1f:48:d9", "attachment") is not None


def test_volatile_device_is_not_reported_as_disappeared(store):
    s = store
    run(s, FDB, 0, [volatile("96:0d:91:1f:48:d9", "ap:AP HALL")])
    run(s, FDB, 1, [])
    ch = run(s, FDB, 2, [])
    assert Event.DISAPPEARED not in [c.kind for c in ch]


def test_wifi_roaming_is_history_but_not_a_report(store):
    """An AP change used to count as a move and was reported. Over one night that
    was about a hundred lines - roaming is everyday life. History keeps the change,
    the list does not."""
    s = store
    run(s, FDB, 0, [volatile("96:0d:91:1f:48:d9", "ap:AP HALL")])
    ch = run(s, FDB, 1, [volatile("96:0d:91:1f:48:d9", "ap:AP GARDEN")])
    assert ch == []
    current = s.open_for(Relation.ATTACHMENT, "96:0d:91:1f:48:d9", "attachment")
    assert current.value == "ap:AP GARDEN"


def test_cable_move_stays_visible_even_with_random_mac(store):
    s = store
    run(s, FDB, 0, [volatile("96:0d:91:1f:48:d9", "c3po:gi12")])
    ch = run(s, FDB, 1, [volatile("96:0d:91:1f:48:d9", "c3po:gi13")])
    assert [c.kind for c in ch] == [Event.MOVED]


def test_a_fixed_device_stays_reportable(store):
    s = store
    run(s, FDB, 0, [attachment("laptop", "c3po:gi12")])
    ch = run(s, FDB, 1, [attachment("laptop", "c3po:gi12"),
                         attachment("printer", "c3po:gi14")])
    assert Event.FIRST_SEEN in [c.kind for c in ch]


def test_switch_to_address_key_and_second_source_are_silent(store):
    """The same address under a different key is not a change - neither when
    switching from "address" to "ip:<address>" nor when a second source sees it."""
    s = store
    old = lambda: Observation(Relation.ADDRESS, "laptop", "address", "172.16.10.87")
    new = lambda: Observation(Relation.ADDRESS, "laptop", "ip:172.16.10.87", "172.16.10.87")
    run(s, ARP, 0, [old()])
    assert run(s, ARP, 1, [new()]) == []          # new key: silent
    assert run(s, ARP, 2, [new()]) == []          # old one goes away: silent
    assert run(s, ARP, 3, [new()]) == []
    assert s.open_for(Relation.ADDRESS, "laptop", "ip:172.16.10.87") is not None


def test_unchanged_report_advances_the_last_sighting(store):
    """160 devices once showed "last seen Thu 06:29" although they had just reported
    - `last_seen` was only set when an interval was opened."""
    run(store, ARP, 0, [address("c6:3c:d0:13:86:00", "172.16.10.5")])
    run(store, ARP, 1, [address("c6:3c:d0:13:86:00", "172.16.10.5")])
    run(store, ARP, 2, [address("c6:3c:d0:13:86:00", "172.16.10.5")])
    assert store.sightings()["c6:3c:d0:13:86:00"][1] == t(2)
    # A failed run is not a sighting.
    run(store, ARP, 3, [address("c6:3c:d0:13:86:00", "172.16.10.5")], successful=False)
    assert store.sightings()["c6:3c:d0:13:86:00"][1] == t(2)
