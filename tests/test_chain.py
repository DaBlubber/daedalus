# -*- coding: utf-8 -*-
"""The chain. Built from three sources that are useless on their own."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from daedalus.chain import chain, distances, neighborhood, who_was_here
from daedalus.model import Observation, Relation, Run, Source
from daedalus.reconcile import ingest

T0 = datetime(2026, 9, 16, 8, 0, tzinfo=timezone.utc)
def t(n): return T0 + timedelta(minutes=5 * n)

FDB = Source("fdb", frozenset({Relation.ATTACHMENT}), missing_threshold=2)
LLDP = Source("lldp", frozenset({Relation.CONNECTION}), missing_threshold=3)


def attachment(mac, port): return Observation(Relation.ATTACHMENT, mac, "attachment", port)
def connection(a, b): return Observation(Relation.CONNECTION, a, "connection", b)


def build_network(s, n=0):
    """The measured reality: bb8 is the root, c3po hangs off bb8 AND off r2d2,
    r2d2 hangs off bb8."""
    ingest(s, Run("lldp", t(n)), LLDP, [
        connection("bb8:gi1", "c3po:gi25"),
        connection("bb8:gi13", "c3po:gi26"),
        connection("bb8:gi2", "r2d2:gi25"),
        connection("r2d2:gi27", "c3po:gi27"),
    ])
    ingest(s, Run("fdb", t(n)), FDB, [
        attachment("64:28:aa:9e:1c:b2", "c3po:gi12"),
        attachment("e8:ec:76:2f:6d:53", "r2d2:gi1"),
    ])


# --------------------------------------------------------------- graph
def test_neighborhood_applies_in_both_directions(store):
    """LLDP is often reported by one side only. A switch whose SNMP is silent would
    otherwise be cut off although its neighbour sees it."""
    build_network(store)
    g = neighborhood(store)
    assert "c3po" in g and "bb8" in g
    assert any(nb == "bb8" for nb, _, _ in g["c3po"])
    assert any(nb == "c3po" for nb, _, _ in g["bb8"])


def test_distances_from_the_root(store):
    build_network(store)
    d, _ = distances(neighborhood(store), "bb8")
    assert d == {"bb8": 0, "c3po": 1, "r2d2": 1}


# --------------------------------------------------------------- chain
def test_the_chain_of_a_device(store):
    build_network(store)
    c = chain(store, "64:28:aa:9e:1c:b2", "bb8")
    assert [link.name for link in c.links] == ["bb8", "c3po", "64:28:aa:9e:1c:b2"]
    assert c.links[1].port == "gi12", "the access port is shown at the last switch"
    assert c.complete


def test_the_chain_reads_well(store):
    build_network(store)
    s = str(chain(store, "e8:ec:76:2f:6d:53", "bb8"))
    assert "bb8" in s and "r2d2 gi1" in s and "->" in s


def test_several_equally_short_paths_are_admitted(store):
    """c3po hangs off bb8 (directly) and off r2d2 (which hangs off bb8). Via bb8 it
    is one step, via r2d2 two - so unambiguous. Only when two paths are EQUALLY long
    may the map no longer guess silently."""
    s = store
    ingest(s, Run("lldp", t(0)), LLDP, [
        connection("core:gi1", "a:gi1"),
        connection("core:gi2", "b:gi1"),
        connection("a:gi2", "z:gi1"),
        connection("b:gi2", "z:gi2"),
    ])
    ingest(s, Run("fdb", t(0)), FDB, [attachment("aa:bb:cc:dd:ee:ff", "z:gi5")])
    c = chain(s, "aa:bb:cc:dd:ee:ff", "core")
    assert not c.unambiguous, "two equally short paths - the chain must say so"
    assert c.links[0].name == "core" and c.links[-1].name == "aa:bb:cc:dd:ee:ff"


def test_device_without_attachment_has_no_chain(store):
    build_network(store)
    c = chain(store, "00:00:00:00:00:01", "bb8")
    assert not c and not c.complete


def test_unknown_switch_gives_an_honest_partial_answer(store):
    """"attached to foreign port 3, path there unknown" is better than nothing and
    much better than an invented path."""
    s = store
    build_network(s)
    ingest(s, Run("fdb", t(1)), FDB, [
        attachment("64:28:aa:9e:1c:b2", "c3po:gi12"),
        attachment("e8:ec:76:2f:6d:53", "r2d2:gi1"),
        attachment("8e:00:24:39:66:1d", "foreign:gi3")])
    c = chain(s, "8e:00:24:39:66:1d", "bb8")
    assert not c.complete
    assert [link.name for link in c.links] == ["foreign", "8e:00:24:39:66:1d"]


def test_the_chain_of_a_switch_ends_there(store):
    build_network(store)
    c = chain(store, "c3po", "bb8")
    assert [link.name for link in c.links] == ["bb8", "c3po"]


def test_the_root_itself(store):
    build_network(store)
    assert [link.name for link in chain(store, "bb8", "bb8").links] == ["bb8"]


# --------------------------------------------------------------- port history
def test_who_was_here_before(store):
    """Comes for free with the interval model."""
    s = store
    ingest(s, Run("fdb", t(0)), FDB, [attachment("8a:72:0b:f6:ad:d1", "c3po:gi12")])
    ingest(s, Run("fdb", t(1)), FDB, [attachment("cf:d0:4d:c4:7d:be", "c3po:gi12")])
    ingest(s, Run("fdb", t(2)), FDB, [attachment("cf:d0:4d:c4:7d:be", "c3po:gi12")])

    history = who_was_here(s, "c3po:gi12")
    assert [x[0] for x in history] == ["cf:d0:4d:c4:7d:be", "8a:72:0b:f6:ad:d1"]
    assert history[0][2] is None, "the newest one still applies"
    # Not t(1) but t(2): the old device was missing for the FIRST time at t(1), and
    # missing once is no proof (missing_threshold=2). The port history therefore
    # shows an overlap of exactly one collection interval - intended and more honest
    # than closing an interval on suspicion.
    assert history[1][2] == t(2)


def test_empty_port_has_no_history(store):
    build_network(store)
    assert who_was_here(store, "c3po:gi99") == []


# ---------------------------------------------------------------------------
# The finding from the first real run: `ap:Luke` and `c3po:gi12` share the same
# colon but mean different things.
# ---------------------------------------------------------------------------
def test_wifi_client_gets_the_chain_through_its_access_point(store):
    """The access point is attached to a switch port, the client to the access
    point. The chain must go through both."""
    s = store
    ingest(s, Run("lldp", t(0)), LLDP, [
        connection("bb8:gi1", "r2d2:gi25"),
        connection("r2d2:gi15", "Luke:port1"),
    ])
    ingest(s, Run("fdb", t(0)), FDB, [
        attachment("04:c5:81:9f:c2:ea", "ap:Luke"),
    ])
    c = chain(s, "04:c5:81:9f:c2:ea", "bb8")
    names = [link.name for link in c.links]
    assert names == ["bb8", "r2d2", "Luke", "04:c5:81:9f:c2:ea"], names
    assert c.complete


def test_the_access_point_does_not_become_a_node_called_ap(store):
    """`ap:Luke` must not be read as the node "ap" with the port "Luke"."""
    from daedalus.chain import _node, _port
    assert _node("ap:Luke") == "Luke" and _port("ap:Luke") == ""
    assert _node("c3po:gi12") == "c3po" and _port("c3po:gi12") == "gi12"


def test_every_switch_shows_its_own_port(store):
    """Not the neighbour's. On the first real run r2d2 showed the port id of the
    access point - which is a MAC address there."""
    s = store
    ingest(s, Run("lldp", t(0)), LLDP, [
        connection("bb8:gi1", "r2d2:gi25"),
        connection("r2d2:gi15", "Luke:AA BB CC DD EE FF"),
    ])
    ingest(s, Run("fdb", t(0)), FDB, [attachment("04:c5:81:9f:c2:ea", "ap:Luke")])
    c = chain(s, "04:c5:81:9f:c2:ea", "bb8")
    pairs = [(link.name, link.port) for link in c.links]
    assert pairs[0] == ("bb8", "gi1"), "bb8 reaches r2d2 through its gi1"
    assert pairs[1] == ("r2d2", "gi15"), "r2d2 reaches Luke through its gi15"
    assert all(" " not in p for _, p in pairs), "no MAC may appear as a port"
