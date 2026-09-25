# -*- coding: utf-8 -*-
"""The service. What is tested is not that it runs, but that it holds back:
different intervals, calm after errors, nothing at the same time."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from daedalus.model import Observation, Relation, Source
from daedalus.service import Job, Service, summary

FDB = Source("fdb", frozenset({Relation.ATTACHMENT}), missing_threshold=2)
LLDP = Source("lldp", frozenset({Relation.CONNECTION}), missing_threshold=3)


T0 = datetime(2026, 9, 16, 10, 0, tzinfo=timezone.utc)


class Clock:
    """A clock set by the test - monotonic AND as a wall clock.

    The test must control both: the monotonic clock drives the scheduling, the
    wall clock the timestamp of the run. Setting only one lets two runs fall on the
    same timestamp - and by rule 3 that is the same run, which has no effect.
    """
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t
    def advance(self, s): self.t += s
    def wallclock(self): return T0 + timedelta(seconds=self.t - 1000.0)


class Dummy:
    def __init__(self, source, raises=None, unchanged=False):
        self.source = source
        self.raises = raises
        self.unchanged = unchanged
        self.runs = 0
    def precheck_unchanged(self): return self.unchanged
    def collect(self):
        self.runs += 1
        if self.raises: raise self.raises
        return [Observation(Relation.ATTACHMENT, "aa:bb:cc:dd:ee:ff", "attachment", "c3po:gi1")]


def service(jobs, clock):
    return Service(jobs, jitter=0.0, clock=clock, sleep=lambda s: None,
                   wallclock=clock.wallclock)


def test_every_source_keeps_its_own_interval(store):
    clock = Clock()
    fast = Dummy(FDB)
    slow = Dummy(LLDP)
    s = service([Job(fast, interval=300), Job(slow, interval=1800)], clock)

    clock.advance(1)                       # the start is staggered
    s.once(store)                          # both at the start
    for _ in range(5):                     # five times five minutes
        clock.advance(300)
        s.once(store)

    assert fast.runs == 6
    assert slow.runs == 1, "the neighbourhood is not asked every five minutes"


def test_after_errors_asking_becomes_more_reluctant(store):
    """A switch that does not answer does not get chattier by being asked more
    often - it only produces load."""
    clock = Clock()
    broken = Dummy(FDB, raises=TimeoutError("no answer"))
    j = Job(broken, interval=300)
    s = service([j], clock)

    s.once(store); assert j.failures_in_a_row == 1 and j.next_interval() == 600
    clock.advance(600); s.once(store); assert j.next_interval() == 1200
    clock.advance(1200); s.once(store); assert j.next_interval() == 2400
    for _ in range(6):
        clock.advance(3000); s.once(store)
    assert j.next_interval() == 2400, "capped at eight times"


def test_after_success_the_interval_is_normal_again(store):
    clock = Clock()
    d = Dummy(FDB, raises=OSError("gone"))
    j = Job(d, interval=300)
    s = service([j], clock)
    s.once(store); clock.advance(600); s.once(store)
    assert j.failures_in_a_row == 2
    d.raises = None
    clock.advance(1200); s.once(store)
    assert j.failures_in_a_row == 0 and j.next_interval() == 300


def test_collectors_run_one_after_the_other_not_concurrently(store):
    """Asking five switches at once produces exactly the load spike that should be
    avoided. The service therefore works through them in order."""
    clock = Clock()
    order = []

    class Recorder(Dummy):
        def collect(self):
            order.append(self.source.name)
            return super().collect()

    a = Recorder(Source("a", frozenset({Relation.ATTACHMENT})))
    b = Recorder(Source("b", frozenset({Relation.ATTACHMENT})))
    s = service([Job(a, interval=300), Job(b, interval=300)], clock)
    clock.advance(1)                # the start is staggered on purpose
    s.once(store)
    assert order == ["a", "b"]


def test_the_start_is_staggered(store):
    """Otherwise all queries meet at the same moment on the first pass."""
    clock = Clock()
    jobs = [Job(Dummy(Source(f"q{i}", frozenset({Relation.ATTACHMENT}))),
                interval=300) for i in range(5)]
    Service(jobs, clock=clock, sleep=lambda s: None)
    assert len({j.due_at for j in jobs}) == 5


def test_skipped_runs_stay_silent(store):
    """A service that logs "nothing changed" every five minutes is no longer read
    after a week."""
    clock = Clock()
    d = Dummy(FDB, unchanged=True)
    j = Job(d, interval=300)
    s = service([j], clock)
    [r] = s.once(store)
    assert summary(r, j) == ""


def test_unchanged_runs_stay_silent_too(store):
    clock = Clock()
    d = Dummy(FDB)
    j = Job(d, interval=300)
    s = service([j], clock)
    s.once(store)                           # first run
    clock.advance(300)
    [r] = s.once(store)
    assert summary(r, j) == ""


def test_but_errors_and_changes_are_reported(store):
    clock = Clock()
    d = Dummy(FDB)
    j = Job(d, interval=300)
    s = service([j], clock)
    s.once(store)

    d.collect = lambda: [Observation(Relation.ATTACHMENT, "aa:bb:cc:dd:ee:ff",
                                     "attachment", "c3po:gi9")]
    clock.advance(300)
    [r] = s.once(store)
    assert "moved" in summary(r, j) and "c3po:gi1" in summary(r, j)

    d.collect = lambda: (_ for _ in ()).throw(TimeoutError("gone"))
    clock.advance(300)
    [r] = s.once(store)
    assert "ERROR" in summary(r, j) and "1 in a row" in summary(r, j)


def test_wait_time_follows_the_next_due_job(store):
    clock = Clock()
    a = Job(Dummy(FDB), interval=300)
    b = Job(Dummy(LLDP), interval=1800)
    s = service([a, b], clock)
    clock.advance(1)                # the start is staggered, make both due
    s.once(store)
    # The clock stood still during the pass: the next due job is the fast
    # collector, at exactly its interval.
    assert s.wait_time() == 300
