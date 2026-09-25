# -*- coding: utf-8 -*-
"""The skeleton for collectors.

A collector is deliberately thin: it answers two questions and returns a list of
observations. Everything difficult - when something counts as gone, what an
absence means, how history is created - lives in `reconcile.py` and is none of
its business.

**The delta logic lives in `precheck_unchanged()`.** Before the expensive query
comes a cheap one: has anything moved at all since the last run? `sysUpTime` and
`ifLastChange` are ONE OID each; an HTTP HEAD with `If-None-Match` costs almost
nothing. If the precheck says "unchanged", the expensive part is skipped entirely.

**Two rules for every precheck:**

1. It must be **cautious**. When in doubt return `False` - then we simply
   collect. A run wrongly skipped loses a change; a run wrongly executed only
   costs a few packets.
2. "Unchanged" is **not** the same as "nothing seen". A skipped run must not
   increase the missing counter - otherwise thrift itself makes devices disappear.
   It is therefore not ingested at all.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

from .model import Change, Observation, Run, Source
from .reconcile import ingest
from .timeutil import now


class Collector(Protocol):
    """What a collector must be able to do. Nothing more."""

    source: Source

    def precheck_unchanged(self) -> bool:
        """Cheap question: can nothing have changed since the last run?

        When in doubt `False`. A collector without a cheap question always returns
        `False` - there is none for the MAC table, for example.
        """

    def collect(self) -> list[Observation]:
        """The expensive query. Raises if the source does not answer."""


@dataclass
class Result:
    """What a run produced."""
    source: str
    successful: bool
    skipped: bool = False
    observations: int = 0
    changes: tuple[Change, ...] = ()
    duration_ms: int = 0
    error: str = ""

    def __str__(self) -> str:
        if self.skipped:
            return f"{self.source}: skipped (precheck: unchanged)"
        if not self.successful:
            return f"{self.source}: ERROR after {self.duration_ms} ms - {self.error}"
        n = len(self.changes)
        return (f"{self.source}: {self.observations} observations, "
                f"{n if n else 'no'} change{'s' if n != 1 else ''}, "
                f"{self.duration_ms} ms")


def run_once(store, collector: Collector, timestamp=None) -> Result:
    """Run one collector once and work the result into the store."""
    s = collector.source
    timestamp = timestamp or now()
    started = time.monotonic()

    # --- cheap question first -----------------------------------------------
    try:
        if collector.precheck_unchanged():
            # NO run. "Unchanged" does not mean "nothing seen" - an ingested empty
            # run would increase the missing counter.
            return Result(s.name, True, skipped=True,
                          duration_ms=int((time.monotonic() - started) * 1000))
    except Exception:                                    # noqa: BLE001
        pass          # precheck broken? Then take the expensive route.

    # --- expensive query -----------------------------------------------------
    try:
        observations = collector.collect()
    except Exception as e:                               # noqa: BLE001
        ms = int((time.monotonic() - started) * 1000)
        # Rule 1: a failed run is recorded but changes nothing. Exactly this
        # prevents a silent switch from making 193 devices disappear.
        ingest(store, Run(s.name, timestamp, successful=False), s, [])
        return Result(s.name, False, duration_ms=ms, error=f"{type(e).__name__}: {e}")

    changes = ingest(store, Run(s.name, timestamp), s, observations)
    return Result(s.name, True, observations=len(observations),
                  changes=tuple(changes),
                  duration_ms=int((time.monotonic() - started) * 1000))


def run_round(store, collectors: list[Collector], timestamp=None) -> list[Result]:
    """Several collectors one after the other. One that fails takes none down.

    Sequential rather than parallel on purpose: querying five switches at once
    produces exactly the load spike that should be avoided.
    """
    return [run_once(store, c, timestamp) for c in collectors]
