# -*- coding: utf-8 -*-
"""Reconciliation: observations become intervals and changes.

This is the heart of the tool and the only place that decides whether something
is new, has moved or has disappeared. Four rules that are not negotiable:

1. **Absence ends an interval only if the responsible source succeeded.**
   Otherwise one silent switch reports 193 disappeared devices and nobody trusts
   the tool anymore.
2. **Only a responsible source may judge.** The Kea collector sees no switch
   ports; its silence must not end an attachment.
3. **Imports are repeatable.** The same run imported twice produces no second
   observation and no second event.
4. **The first run marks everything as existing inventory, not as new.**
   Otherwise the change view would be worthless from day one. This also applies
   to every newly connected source.
"""
from __future__ import annotations

from .model import (SILENT_KEYS, Change, Event, Interval, Observation, Relation,
                    Run, Source, is_reportable)

# Which event is produced when an assignment of this relation starts or ends
_ADDED = {
    Relation.ADDRESS: Event.ADDRESS_ADDED,
    Relation.ATTACHMENT: Event.MOVED,
    Relation.CONNECTION: Event.ATTRIBUTE_CHANGED,
    Relation.ATTRIBUTE: Event.ATTRIBUTE_CHANGED,
}
_REMOVED = {
    Relation.ADDRESS: Event.ADDRESS_REMOVED,
    Relation.ATTACHMENT: Event.DISCONNECTED,
    Relation.CONNECTION: Event.ATTRIBUTE_CHANGED,
    Relation.ATTRIBUTE: Event.ATTRIBUTE_CHANGED,
}


def ingest(store, run: Run, source: Source,
           observations: list[Observation]) -> list[Change]:
    """Work one collection run into the store. Returns the resulting changes.

    The result is empty if nothing changed - that is the normal case and exactly
    why the store does not grow with time.
    """
    if source.name != run.source:
        raise ValueError("run and source do not match")

    # --- rule 3: repeatable -------------------------------------------------
    if store.run_seen(run.source, run.timestamp):
        return []
    store.remember_run(run)

    # --- rule 1: a failed source says nothing at all --------------------------
    if not run.successful:
        return []

    # Is this the first successful run of THIS source? Then everything is inventory.
    first_run = not store.had_successful_run(run.source, run.timestamp)

    new: list[Change] = []

    # --- rule 2: only touch relations the source is responsible for -----------
    observations = [o for o in observations if o.relation in source.responsible_for]
    seen = {(o.relation, o.obj, o.key): o for o in observations}

    # ---------- What is new or has changed? ----------------------------------
    for (rel, obj, key), o in seen.items():
        current = store.open_for(rel, obj, key)
        object_new = not store.object_known(obj)

        if current is not None and current.value == o.value:
            if current.missing_since:
                store.set_missing(current, 0)   # still there
            continue

        if current is not None and key in SILENT_KEYS:
            store.end(current, run.timestamp)   # history yes, report no
        elif current is not None:              # the value has changed
            store.end(current, run.timestamp)
            new.append(Change(
                kind=_ADDED[rel], obj=obj, timestamp=run.timestamp,
                before=current.value, after=o.value, source=source.name,
                affects=_affected(rel, obj, current.value, o.value),
                key=key))
        elif rel is Relation.ADDRESS and _same_address_open(store, obj, o.value, key):
            # The same address is already open under a different key - after
            # switching to one key per address, or a second source that sees the
            # same thing. Neither is a change.
            pass
        elif rel is Relation.ATTRIBUTE:
            # An attribute the object did not have before is not a change but new
            # knowledge: a Wi-Fi client that comes back brings name, SSID, VLAN and
            # vendor - four lines all saying the same as the one "back". And a
            # source that learns a new attribute would otherwise report every
            # object individually.
            pass
        elif not first_run and not object_new:
            # A known object gets an assignment it did not have before - such as a
            # device that was plugged in again.
            new.append(Change(
                kind=Event.BACK if rel is Relation.ATTACHMENT else _ADDED[rel],
                obj=obj, timestamp=run.timestamp,
                before=None, after=o.value, source=source.name,
                affects=_affected(rel, obj, None, o.value),
                key=key))
        # If the object is entirely new, the single "first seen" event below says
        # it all. An additional "moved" would simply be wrong: something that has
        # never been anywhere cannot have moved anywhere.

        store.open(Interval(
            relation=rel, obj=obj, key=key, value=o.value,
            since=run.timestamp, source=source.name, volatile=o.volatile))

        # First sighting of the object itself - only outside the first run
        # (rule 4) and not for volatile objects: a phone with a randomised MAC is a
        # different device every day, and none of them is news.
        if object_new:
            store.remember_object(obj, run.timestamp)
            if not first_run and not o.volatile:
                new.append(Change(
                    kind=Event.FIRST_SEEN, obj=obj,
                    timestamp=run.timestamp, after=o.value, source=source.name,
                    affects=_affected(rel, obj, None, o.value),
                    key=key))

    # Every report is a sighting, even if nothing changed.
    store.remember_seen({obj for (_, obj, _) in seen}, run.timestamp)

    # ---------- What is missing? ---------------------------------------------
    # Only look at intervals THIS source has set and is responsible for.
    # Everything else is none of its business.
    for i in store.open_from(source.name, source.responsible_for):
        if (i.relation, i.obj, i.key) in seen:
            continue

        store.set_missing(i, i.missing_since + 1)
        if i.missing_since < source.missing_threshold:
            continue                         # not certain enough yet

        store.end(i, run.timestamp)
        # An attribute that goes away is silent for the same reason as one that
        # appears (see above). A changed value stays visible. An address that is
        # still open under a different key has not gone.
        if i.relation is Relation.ADDRESS and _same_address_open(
                store, i.obj, i.value, i.key):
            pass
        elif i.relation is not Relation.ATTRIBUTE:
            new.append(Change(
                kind=_REMOVED[i.relation], obj=i.obj, timestamp=run.timestamp,
                before=i.value, after=None, source=source.name,
                affects=_affected(i.relation, i.obj, i.value, None),
                key=i.key))

        # Does the object have no open assignment at all anymore? Then it has
        # disappeared as a whole, not just one attribute of it. Volatile objects
        # excepted - their disappearing is as little news as their appearing.
        if not i.volatile and not store.has_open(i.obj):
            new.append(Change(
                kind=Event.DISAPPEARED, obj=i.obj,
                timestamp=run.timestamp, before=i.value, source=source.name,
                affects=_affected(i.relation, i.obj, i.value, None),
                key=i.key))

    volatile = {o.obj for o in observations if o.volatile} | {
        i.obj for i in store.open_from(source.name, source.responsible_for)
        if i.volatile}
    new = [c for c in condense(new)
           if c.kind in (Event.FIRST_SEEN, Event.DISAPPEARED)
           or is_reportable(c.kind, c.before, c.after, c.obj in volatile)]
    for c in new:
        store.remember_change(c)
    return new


def _same_address_open(store, obj: str, value: str, except_key: str) -> bool:
    return any(i.until is None and i.relation is Relation.ADDRESS and i.value == value
               and i.key != except_key for i in store.history(obj))


def condense(changes: list[Change]) -> list[Change]:
    """If an object has disappeared, that one event says it all.

    A Wi-Fi client that left used to produce three lines: address removed,
    disconnected from the access point, disappeared. The first two are contained
    in the third.
    """
    gone = {c.obj for c in changes if c.kind is Event.DISAPPEARED}
    return [c for c in changes
            if c.obj not in gone
            or c.kind not in (Event.ADDRESS_REMOVED, Event.DISCONNECTED)]


def _affected(rel: Relation, obj: str, before: str | None,
              after: str | None) -> tuple[str, ...]:
    """Which objects does a change mark?

    If a device moves from port 15 to port 16, **three** things are affected: the
    device and both ports. The UI needs exactly that, so the mark does not only
    stick to the device.
    """
    out = [obj]
    if rel in (Relation.ATTACHMENT, Relation.CONNECTION):
        out += [x for x in (before, after) if x]
    return tuple(dict.fromkeys(out))


def marks(store, since) -> dict[str, Event]:
    """What the UI marks on the nodes.

    Not a stored colour but a derivation: the weightiest not yet acknowledged
    change of an object since a point in time.
    """
    rank = {Event.DISAPPEARED: 3, Event.MOVED: 2,
            Event.ADDRESS_REMOVED: 2, Event.ADDRESS_ADDED: 2, Event.DISCONNECTED: 2,
            Event.ATTRIBUTE_CHANGED: 2, Event.BACK: 1,
            Event.FIRST_SEEN: 1}
    out: dict[str, Event] = {}
    for c in store.changes_since(since):
        for obj in c.affects or (c.obj,):
            existing = out.get(obj)
            if existing is None or rank[c.kind] > rank[existing]:
                out[obj] = c.kind
    return out
