# -*- coding: utf-8 -*-
"""Where the inventory lives.

`reconcile.py` knows no database. It only knows the handful of questions and
changes defined in this module. There are two reasons for that: the rules can be
tested without a running server, and the very same tests then run unchanged
against real PostgreSQL - which proves that schema and rules fit together, not
just that each works on its own.
"""
from __future__ import annotations

from datetime import datetime
from typing import Protocol

from .model import Change, Interval, Relation, Run


class Store(Protocol):
    """The only interface reconciliation needs."""

    def run_seen(self, source: str, timestamp: datetime) -> bool: ...
    def remember_run(self, run: Run) -> None: ...
    def had_successful_run(self, source: str, before: datetime) -> bool: ...

    def open_for(self, relation: Relation, obj: str, key: str) -> Interval | None: ...
    def open_from(self, source: str,
                  relations: frozenset[Relation]) -> list[Interval]: ...
    def has_open(self, obj: str) -> bool: ...
    def all_open(self, relation: Relation) -> list[Interval]: ...
    def interval_count(self) -> int: ...
    def history(self, obj: str) -> list[Interval]: ...
    def all_with_value(self, relation: Relation, value: str) -> list[Interval]: ...

    def open(self, i: Interval) -> None: ...
    def end(self, i: Interval, until: datetime) -> None: ...
    def set_missing(self, i: Interval, value: int) -> None: ...

    def object_known(self, obj: str) -> bool: ...
    def remember_object(self, obj: str, timestamp: datetime) -> None: ...
    def remember_seen(self, objects: set[str], timestamp: datetime) -> None: ...
    def ever(self, key: str) -> set[str]: ...

    def remember_change(self, c: Change) -> None: ...
    def changes_since(self, since: datetime) -> list[Change]: ...

    def remember_counters(self, timestamp: datetime, readings: list) -> None: ...
    def port_health(self, now: datetime) -> dict[str, dict]: ...
    def transitions_since(self, key: str, since: datetime) -> dict[str, int]: ...


# ===========================================================================
class MemoryStore:
    """In memory. For rule tests and dry runs."""

    def __init__(self) -> None:
        self.intervals: list[Interval] = []
        self.changes: list[Change] = []
        self.runs: list[Run] = []
        self.known: set[str] = set()
        self.last_seen: dict[str, datetime] = {}   # object -> last sighting
        self.counters: list[tuple] = []           # (timestamp, counter reading)

    # --- runs ---
    def run_seen(self, source, timestamp):
        return any(r.source == source and r.timestamp == timestamp for r in self.runs)

    def remember_run(self, run):
        self.runs.append(run)

    def had_successful_run(self, source, before):
        return any(r.source == source and r.successful and r.timestamp < before
                   for r in self.runs)

    # --- intervals ---
    def open_for(self, relation, obj, key):
        for i in self.intervals:
            if (i.is_open and i.relation is relation and i.obj == obj
                    and i.key == key):
                return i
        return None

    def open_from(self, source, relations):
        return [i for i in self.intervals
                if i.is_open and i.source == source and i.relation in relations]

    def has_open(self, obj):
        return any(i.is_open and i.obj == obj for i in self.intervals)

    def all_open(self, relation):
        return [i for i in self.intervals if i.is_open and i.relation is relation]

    def interval_count(self):
        return len(self.intervals)

    def history(self, obj):
        """Everything that ever applied to this object - newest first."""
        return sorted([i for i in self.intervals if i.obj == obj],
                      key=lambda i: i.since, reverse=True)

    def all_with_value(self, relation, value):
        """Asked the other way round: what ever pointed at this value?
        That is "who was plugged into this port before?"."""
        return [i for i in self.intervals
                if i.relation is relation and i.value == value]

    def open(self, i):
        self.intervals.append(i)

    def end(self, i, until):
        i.until = until

    def set_missing(self, i, value):
        i.missing_since = value

    # --- objects ---
    def object_known(self, obj):
        return obj in self.known

    def remember_object(self, obj, timestamp):
        self.known.add(obj)

    def remember_seen(self, objects, timestamp):
        for o in objects:
            if o in self.known and (o not in self.last_seen or self.last_seen[o] < timestamp):
                self.last_seen[o] = timestamp

    def sightings(self):
        return {o: (None, t) for o, t in self.last_seen.items()}

    def ever(self, key):
        """Which objects ever had an attribute under this key?"""
        return {i.obj for i in self.intervals if i.key == key}

    # --- changes ---
    def remember_change(self, c):
        self.changes.append(c)

    def changes_since(self, since):
        return [c for c in self.changes if c.timestamp >= since]

    # --- port counters ---
    def remember_counters(self, timestamp, readings):
        self.counters.extend((timestamp, r) for r in readings)

    def port_health(self, now):
        return compute_health(
            [(r.port, t, r.in_errors, r.out_errors, r.in_discards, r.out_discards,
              r.poe_mw, r.poe_budget_w) for t, r in self.counters], now)

    def transitions_since(self, key, since):
        out: dict[str, int] = {}
        for i in self.intervals:
            if i.key == key and i.until is not None and i.until >= since:
                out[i.obj] = out.get(i.obj, 0) + 1
        return out


# ===========================================================================
class PgStore:
    """PostgreSQL according to `schema.sql`.

    The outside world speaks in source keys (a MAC, `c3po:gi12`), the database in
    stable object identifiers. The translation happens here and only here - which
    keeps the way open for later identity resolution without the rules noticing.
    """

    def __init__(self, connection) -> None:
        self.db = connection
        self._ids: dict[str, str] = {}
        self._external: dict[str, str] = {}   # reverse direction of _ids

    @property
    def annotations(self):
        """The manually maintained details of the same objects."""
        from .annotations import Annotations
        if not hasattr(self, '_annotations'):
            self._annotations = Annotations(self.db, self._id)
        return self._annotations

    # --- translation source key <-> object identifier ---
    def _id(self, external: str, timestamp: datetime | None = None,
            required: bool = False) -> str:
        """Source key to object identifier. `required` creates the object if it
        does not exist yet - annotations need that: a wall socket can be entered
        before any device was ever seen on it."""
        if external in self._ids:
            return self._ids[external]
        kind = 'interface' if ':' in external else 'device'
        if required and timestamp is None:
            from .timeutil import now
            timestamp = now()
        with self.db.cursor() as c:
            c.execute("SELECT id FROM object WHERE external = %s", (external,))
            row = c.fetchone()
            if row is None:
                if timestamp is None:
                    return ""          # does not exist and should not be created
                c.execute("""INSERT INTO object (kind, external, first_seen, last_seen)
                             VALUES (%s, %s, %s, %s) RETURNING id""",
                          (kind, external, timestamp, timestamp))
                row = c.fetchone()
        self._ids[external] = str(row[0])
        self._external[str(row[0])] = external
        return self._ids[external]

    def sightings(self) -> dict[str, tuple]:
        """{source key: (first_seen, last_seen)} - for display."""
        with self.db.cursor() as c:
            c.execute("SELECT external, first_seen, last_seen FROM object "
                      "WHERE external IS NOT NULL")
            return {r[0]: (r[1], r[2]) for r in c.fetchall()}

    def _external_key(self, identifier: str) -> str:
        if identifier in self._external:
            return self._external[identifier]
        with self.db.cursor() as c:
            c.execute("SELECT external FROM object WHERE id = %s", (identifier,))
            r = c.fetchone()
        if not r:
            return identifier
        self._external[identifier] = r[0]
        self._ids.setdefault(r[0], identifier)
        return r[0]

    # --- runs ---
    def run_seen(self, source, timestamp):
        with self.db.cursor() as c:
            c.execute("SELECT 1 FROM run WHERE source=%s AND timestamp=%s",
                      (source, timestamp))
            return c.fetchone() is not None

    def remember_run(self, run):
        with self.db.cursor() as c:
            c.execute("""INSERT INTO run (source, timestamp, successful)
                         VALUES (%s,%s,%s) ON CONFLICT DO NOTHING""",
                      (run.source, run.timestamp, run.successful))

    def had_successful_run(self, source, before):
        with self.db.cursor() as c:
            c.execute("""SELECT 1 FROM run
                          WHERE source=%s AND successful AND timestamp < %s LIMIT 1""",
                      (source, before))
            return c.fetchone() is not None

    # --- intervals ---
    def _to_interval(self, r) -> Interval:
        # The last column is the source key, delivered right away by a subquery.
        # It used to be looked up row by row - building the map took 700 queries
        # and four seconds.
        identifier, external = str(r[2]), r[10]
        if external:
            self._ids.setdefault(external, identifier)
            self._external[identifier] = external
        i = Interval(relation=Relation(r[1]), obj=external or self._external_key(identifier),
                     key=r[3], value=r[4], since=r[5], until=r[6],
                     source=r[7], missing_since=r[8], volatile=bool(r[9]))
        i.db_id = r[0]                       # type: ignore[attr-defined]
        return i

    _COLUMNS = ("id, relation, object, key, value, since, until, source, "
                "missing_since, volatile, "
                "(SELECT o.external FROM object o WHERE o.id = assignment.object)")

    def open_for(self, relation, obj, key):
        identifier = self._id(obj)
        if not identifier:
            return None
        with self.db.cursor() as c:
            c.execute(f"""SELECT {self._COLUMNS} FROM assignment
                           WHERE until IS NULL AND relation=%s AND object=%s AND key=%s""",
                      (relation.value, identifier, key))
            r = c.fetchone()
        return self._to_interval(r) if r else None

    def open_from(self, source, relations):
        with self.db.cursor() as c:
            c.execute(f"""SELECT {self._COLUMNS} FROM assignment
                           WHERE until IS NULL AND source=%s AND relation = ANY(%s)""",
                      (source, [r.value for r in relations]))
            return [self._to_interval(r) for r in c.fetchall()]

    def has_open(self, obj):
        identifier = self._id(obj)
        with self.db.cursor() as c:
            c.execute("SELECT 1 FROM assignment WHERE until IS NULL AND object=%s LIMIT 1",
                      (identifier,))
            return c.fetchone() is not None

    def all_open(self, relation):
        with self.db.cursor() as c:
            c.execute(f"""SELECT {self._COLUMNS} FROM assignment
                           WHERE until IS NULL AND relation=%s""", (relation.value,))
            return [self._to_interval(r) for r in c.fetchall()]

    def interval_count(self):
        with self.db.cursor() as c:
            c.execute("SELECT count(*) FROM assignment")
            return c.fetchone()[0]

    def remember_seen(self, objects, timestamp):
        """Advance the last sighting - on EVERY run that reports the object.

        `last_seen` used to be set only when an interval was opened. A device on
        which nothing changed therefore showed "last seen Thu 06:29" although it had
        just reported (160 devices at once). A single UPDATE per run, not one per
        object.
        """
        if not objects:
            return
        with self.db.cursor() as c:
            c.execute("""UPDATE object SET last_seen = %s
                          WHERE external = ANY(%s)
                            AND (last_seen IS NULL OR last_seen < %s)""",
                      (timestamp, sorted(objects), timestamp))

    def ever(self, key):
        """Which objects ever had an attribute under this key - open or long
        ended: has this device ever talked DHCP?"""
        with self.db.cursor() as c:
            c.execute("""SELECT DISTINCT o.external FROM assignment a
                            JOIN object o ON o.id = a.object
                           WHERE a.key = %s""", (key,))
            return {r[0] for r in c.fetchall()}

    def history(self, obj):
        identifier = self._id(obj)
        if not identifier:
            return []
        with self.db.cursor() as c:
            c.execute(f"""SELECT {self._COLUMNS} FROM assignment
                           WHERE object=%s ORDER BY since DESC""", (identifier,))
            return [self._to_interval(r) for r in c.fetchall()]

    def all_with_value(self, relation, value):
        with self.db.cursor() as c:
            c.execute(f"""SELECT {self._COLUMNS} FROM assignment
                           WHERE relation=%s AND value=%s ORDER BY since DESC""",
                      (relation.value, value))
            return [self._to_interval(r) for r in c.fetchall()]

    def open(self, i):
        identifier = self._id(i.obj, i.since)
        with self.db.cursor() as c:
            c.execute("""INSERT INTO assignment
                         (relation, object, key, value, since, source,
                          missing_since, volatile)
                         VALUES (%s,%s,%s,%s,%s,%s,0,%s) RETURNING id""",
                      (i.relation.value, identifier, i.key, i.value, i.since,
                       i.source, i.volatile))
            i.db_id = c.fetchone()[0]        # type: ignore[attr-defined]
            c.execute("UPDATE object SET last_seen=%s WHERE id=%s", (i.since, identifier))

    def end(self, i, until):
        i.until = until
        with self.db.cursor() as c:
            c.execute("UPDATE assignment SET until=%s WHERE id=%s", (until, i.db_id))

    def set_missing(self, i, value):
        i.missing_since = value
        with self.db.cursor() as c:
            c.execute("UPDATE assignment SET missing_since=%s WHERE id=%s", (value, i.db_id))

    # --- objects ---
    def object_known(self, obj):
        return bool(self._id(obj))

    def remember_object(self, obj, timestamp):
        self._id(obj, timestamp)

    # --- changes ---
    def remember_change(self, c):
        with self.db.cursor() as cur:
            cur.execute("""INSERT INTO change
                           (kind, object, timestamp, before, after, source, key, affects)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (c.kind.value, self._id(c.obj, c.timestamp), c.timestamp,
                         c.before, c.after, c.source, c.key or None,
                         [self._id(x, c.timestamp) for x in (c.affects or (c.obj,))]))

    # --- port counters ---
    def remember_counters(self, timestamp, readings):
        """Store counter readings and drop everything older than 48 hours."""
        with self.db.cursor() as c:
            c.executemany(
                """INSERT INTO port_counter (port, timestamp, in_errors, out_errors,
                          in_discards, out_discards, poe_mw, poe_budget_w)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                [(r.port, timestamp, r.in_errors, r.out_errors, r.in_discards,
                  r.out_discards, r.poe_mw, r.poe_budget_w) for r in readings])
            c.execute("DELETE FROM port_counter WHERE timestamp < %s - interval '48 hours'",
                      (timestamp,))

    def port_health(self, now):
        from datetime import timedelta
        with self.db.cursor() as c:
            c.execute("""SELECT port, timestamp, in_errors, out_errors, in_discards,
                                out_discards, poe_mw, poe_budget_w
                           FROM port_counter WHERE timestamp >= %s""",
                      (now - timedelta(hours=25),))
            return compute_health(c.fetchall(), now)

    def transitions_since(self, key, since):
        """How often has an attribute changed since `since`? Per object.

        Counted are intervals that ENDED in the window - every end is a change.
        Counting the started ones would include the first run as well."""
        with self.db.cursor() as c:
            c.execute("""SELECT o.external, count(*) FROM assignment a
                           JOIN object o ON o.id = a.object
                          WHERE a.key = %s AND a.until >= %s
                          GROUP BY o.external""", (key, since))
            return {r[0]: r[1] for r in c.fetchall()}

    def changes_since(self, since):
        from .model import Event
        with self.db.cursor() as c:
            c.execute("""SELECT ch.kind, o.external, ch.timestamp, ch.before, ch.after,
                                ch.source, ch.key,
                                ARRAY(SELECT b.external FROM object b
                                       WHERE b.id = ANY(ch.affects))
                           FROM change ch JOIN object o ON o.id = ch.object
                          WHERE ch.timestamp >= %s ORDER BY ch.id""", (since,))
            # Source keys via join instead of one lookup per row - the same trap as
            # with the intervals (4 s while building the map).
            return [Change(kind=Event(r[0]), obj=r[1], timestamp=r[2],
                           before=r[3], after=r[4], source=r[5],
                           key=r[6] or "", affects=tuple(r[7] or ()))
                    for r in c.fetchall()]


def compute_health(rows, now) -> dict[str, dict]:
    """Rates from counter readings: errors in the last hour and the last 24 hours,
    plus the latest PoE power.

    A counter that decreases has started again at zero after a switch restart.
    Then the latest reading itself counts as the increase - better to show a few
    errors too many than to swallow a real one.
    """
    from datetime import timedelta
    per_port: dict[str, list] = {}
    for r in rows:
        per_port.setdefault(r[0], []).append(r)
    out: dict[str, dict] = {}
    for port, series in per_port.items():
        series.sort(key=lambda r: r[1])
        latest = series[-1]

        def increase(column: int, window: timedelta) -> int | None:
            old = next((r for r in series if r[1] >= now - window), None)
            if old is None or latest[column] is None or old[column] is None or old is latest:
                return None
            d = latest[column] - old[column]
            return latest[column] if d < 0 else d

        hour, day = timedelta(hours=1), timedelta(hours=24)
        values = {
            "errors_1h": _total(increase(2, hour), increase(3, hour)),
            "errors_24h": _total(increase(2, day), increase(3, day)),
            "discards_24h": _total(increase(4, day), increase(5, day)),
            "poe_w": round(latest[6] / 1000, 1) if latest[6] else None,
            "poe_budget_w": latest[7],
            "measured_since": series[0][1],
        }
        out[port] = {k: v for k, v in values.items() if v is not None}
    return out


def _total(*values):
    present = [v for v in values if v is not None]
    return sum(present) if present else None
