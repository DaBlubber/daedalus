# -*- coding: utf-8 -*-
"""The service: run collectors at their own pace.

**A running service, not a recurring batch job** - and that is a decision, not
convenience. Two reasons:

1. **The sources age differently.** A MAC table ages out in minutes, an LLDP
   neighbourhood changes when cables are re-plugged, a port configuration almost
   never. A shared interval is the most expensive of all options: either the MAC
   table is asked too rarely or the neighbourhood too often.

2. **The cheap precheck needs a memory.** `ifLastChange` only works in comparison
   with the last state. A batch job starts a fresh process every time - the
   comparison would come to nothing and the savings would be exactly zero. Saving
   requires remembering.

The collectors run **one after the other**, never at the same time: querying five
switches at once produces exactly the load spike that should be avoided.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass

from .collector import Result, run_once
from .timeutil import display, now


@dataclass
class Job:
    """A collector with its interval."""
    collector: object
    interval: float                  # seconds between two runs
    due_at: float = 0.0              # monotonic clock
    failures_in_a_row: int = 0

    @property
    def name(self) -> str:
        return self.collector.source.name

    def next_interval(self) -> float:
        """After errors, ask more reluctantly.

        A switch that does not answer does not get chattier by being asked more
        often - that only produces load and fills logs. Double up to at most eight
        times, then stay there.
        """
        if not self.failures_in_a_row:
            return self.interval
        return self.interval * min(8, 2 ** self.failures_in_a_row)


class Service:
    """Runs jobs when they are due."""

    def __init__(self, jobs: list[Job], *, jitter: float = 0.1,
                 clock=time.monotonic, sleep=time.sleep, wallclock=now) -> None:
        self.jobs = jobs
        self.jitter = jitter
        self._clock = clock            # monotonic, for scheduling
        self._wallclock = wallclock    # wall clock, for the run timestamp
        self._sleep = sleep
        self.running = True
        start = self._clock()
        # Do not start everything at once: otherwise all queries meet at the same
        # instant on the first pass.
        for i, j in enumerate(jobs):
            j.due_at = start + i * 0.5

    def due(self) -> list[Job]:
        t = self._clock()
        return [j for j in self.jobs if j.due_at <= t]

    def wait_time(self) -> float:
        if not self.jobs:
            return 1.0
        return max(0.0, min(j.due_at for j in self.jobs) - self._clock())

    def once(self, store, report=None) -> list[Result]:
        """Process everything that is due once. Sequentially, never concurrently."""
        results = []
        for job in self.due():
            r = run_once(store, job.collector, self._wallclock())
            results.append(r)

            job.failures_in_a_row = 0 if r.successful else job.failures_in_a_row + 1
            interval = job.next_interval()
            # A little jitter so the intervals do not lock in and the queries run
            # in lockstep forever.
            interval *= 1 + random.uniform(-self.jitter, self.jitter)
            job.due_at = self._clock() + interval

            if report:
                report(r, job)
        return results

    def run(self, store, report=None, at_most: int | None = None) -> None:
        """Until someone stops it. `at_most` limits the passes (tests)."""
        passes = 0
        while self.running:
            self.once(store, report)
            passes += 1
            if at_most is not None and passes >= at_most:
                return
            self._sleep(min(self.wait_time(), 5.0))


def summary(r: Result, job: Job) -> str:
    """One line per run - but only if there is something to say.

    A service that logs "nothing changed" every five minutes is no longer read
    after a week. Skipped and unchanged runs therefore stay silent.
    """
    if r.skipped or (r.successful and not r.changes):
        return ""
    head = f"{display(now())}  {r.source}"
    if not r.successful:
        return f"{head}: ERROR ({job.failures_in_a_row} in a row) - {r.error}"
    lines = [f"{head}: {len(r.changes)} change"
             f"{'s' if len(r.changes) != 1 else ''}"]
    for c in r.changes[:10]:
        lines.append(f"    {c.kind.value:<18} {c.obj}  "
                     f"{c.before or '-'} -> {c.after or '-'}")
    if len(r.changes) > 10:
        lines.append(f"    ... and {len(r.changes) - 10} more")
    return "\n".join(lines)
