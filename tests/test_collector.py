# -*- coding: utf-8 -*-
"""The skeleton. The question tested: does a collector behave correctly when the
source is silent, lies or is not asked at all?"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from daedalus.collector import run_once, run_round
from daedalus.model import Observation, Relation, Source

T0 = datetime(2026, 9, 16, 8, 0, tzinfo=timezone.utc)
def t(n): return T0 + timedelta(minutes=5 * n)

FDB = Source("fdb", frozenset({Relation.ATTACHMENT}), missing_threshold=2)


class Dummy:
    """A collector whose behaviour the test dictates."""
    def __init__(self, source=FDB, unchanged=False, raises=None):
        self.source = source
        self.unchanged = unchanged
        self.raises = raises
        self.values: list[Observation] = []
        self.asked = 0
        self.prechecked = 0

    def precheck_unchanged(self):
        self.prechecked += 1
        if callable(self.unchanged):
            return self.unchanged()
        return self.unchanged

    def collect(self):
        self.asked += 1
        if self.raises:
            raise self.raises
        return list(self.values)


def attachment(d, p): return Observation(Relation.ATTACHMENT, d, "attachment", p)


def test_ordinary_run(store):
    c = Dummy(); c.values = [attachment("laptop", "c3po:gi12")]
    r = run_once(store, c, t(0))
    assert r.successful and r.observations == 1 and not r.skipped
    assert store.open_for(Relation.ATTACHMENT, "laptop", "attachment") is not None


def test_precheck_saves_the_expensive_query(store):
    c = Dummy(unchanged=True)
    r = run_once(store, c, t(0))
    assert r.skipped and r.successful
    assert c.asked == 0, "the expensive query must not even run"


def test_a_skipped_run_makes_nothing_disappear(store):
    """The most important test here: thrift must not cost devices."""
    c = Dummy(); c.values = [attachment("laptop", "c3po:gi12")]
    run_once(store, c, t(0))
    c.unchanged = True
    for n in (1, 2, 3, 4, 5):
        assert run_once(store, c, t(n)).skipped
    assert store.open_for(Relation.ATTACHMENT, "laptop", "attachment") is not None
    assert store.changes_since(T0) == []


def test_broken_precheck_leads_to_the_expensive_query(store):
    """When in doubt, collect. A run wrongly skipped loses a change, a run wrongly
    executed costs a few packets."""
    def broken(): raise RuntimeError("SNMP timeout")
    c = Dummy(unchanged=broken); c.values = [attachment("laptop", "c3po:gi12")]
    r = run_once(store, c, t(0))
    assert r.successful and c.asked == 1


def test_failed_source_is_recorded_but_changes_nothing(store):
    c = Dummy(); c.values = [attachment("laptop", "c3po:gi12")]
    run_once(store, c, t(0))

    broken = Dummy(raises=TimeoutError("no answer from 172.16.0.3"))
    for n in (1, 2, 3):
        r = run_once(store, broken, t(n))
        assert not r.successful and "TimeoutError" in r.error
    assert store.open_for(Relation.ATTACHMENT, "laptop", "attachment") is not None
    assert store.changes_since(T0) == []


def test_a_failed_collector_does_not_take_the_others_down(store):
    good = Dummy(); good.values = [attachment("laptop", "c3po:gi12")]
    bad = Dummy(source=Source("arp", frozenset({Relation.ADDRESS})),
                raises=OSError("network unreachable"))
    results = run_round(store, [bad, good], t(0))
    assert [r.successful for r in results] == [False, True]
    assert store.open_for(Relation.ATTACHMENT, "laptop", "attachment") is not None


def test_changes_come_back_in_the_result(store):
    c = Dummy(); c.values = [attachment("pi", "c3po:gi15")]
    run_once(store, c, t(0))
    c.values = [attachment("pi", "c3po:gi16")]
    r = run_once(store, c, t(1))
    assert len(r.changes) == 1
    assert r.changes[0].before == "c3po:gi15"


def test_result_reads_as_a_line(store):
    c = Dummy(); c.values = [attachment("a", "sw:1")]
    assert "1 observations" in str(run_once(store, c, t(0)))
    assert "skipped" in str(run_once(store, Dummy(unchanged=True), t(1)))
