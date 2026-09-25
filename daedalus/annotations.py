# -*- coding: utf-8 -*-
"""Manually maintained details: wall outlet, room, note, owner, name.

This is the counterpart to everything else in this tool. Everything else is
collected, has a validity period and can end. This comes from a person, applies
until someone changes it, and **survives a device disappearing and coming back**.

Why it is worth it: the map shows that a laptop is attached to `C3PO port 12`. It
does not show where that port comes out of the wall. Exactly this one detail
decides at 11 pm whether you find the cable - and no source in the world provides
it. It has to be entered once, and then it stays forever.

`ifAlias` (the port description on the switch) can do the same and could later be
read as a *source* - but it is maintained, not measured, and only exists where
someone entered it on the device. Having both side by side is right: what is
entered here wins over the switch when in doubt.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

FIELDS = ("outlet", "room", "note", "owner", "name", "expected")


@dataclass
class Annotation:
    """What a person has recorded about an object."""
    obj: str
    outlet: str | None = None
    room: str | None = None
    note: str | None = None
    owner: str | None = None
    name: str | None = None
    # Reason why a finding on this object is fine ("switch 2 in standby wakes up
    # every hour"). Set = finding confirmed.
    expected: str | None = None
    changed_at: datetime | None = None
    changed_by: str | None = None

    @property
    def is_empty(self) -> bool:
        return not any(getattr(self, f) for f in FIELDS)

    def summary(self) -> str:
        """One line for the card: "outlet B2-14 · office"."""
        parts = []
        if self.outlet:
            parts.append(f"outlet {self.outlet}")
        if self.room:
            parts.append(self.room)
        return " · ".join(parts)


def _clean(value: str | None) -> str | None:
    """Empty input becomes NULL, not an empty string.

    Otherwise the card later shows a field "room:" without content, and nobody
    knows whether it was never maintained or deliberately cleared.
    """
    if value is None:
        return None
    value = value.strip()
    return value or None


class Annotations:
    """Reading and writing the maintained details."""

    def __init__(self, connection, resolve) -> None:
        self.db = connection
        self._id = resolve            # source key -> object identifier

    def read(self, obj: str) -> Annotation:
        identifier = self._id(obj)
        if not identifier:
            return Annotation(obj=obj)
        with self.db.cursor() as c:
            c.execute("""SELECT outlet, room, note, owner, name, expected, changed_at,
                                changed_by
                           FROM annotation WHERE object = %s""", (identifier,))
            r = c.fetchone()
        if r is None:
            return Annotation(obj=obj)
        return Annotation(obj, *r)

    def write(self, obj: str, changed_by: str = "", **values) -> Annotation:
        """Set details. Only the fields passed are touched.

        `outlet=""` deletes the outlet, leaving `outlet` out keeps it - the
        difference between "should go" and "none of my business".
        """
        unknown = set(values) - set(FIELDS)
        if unknown:
            raise ValueError(f"unknown fields: {sorted(unknown)}")

        identifier = self._id(obj, required=True)
        given = {k: _clean(v) for k, v in values.items()}

        # First compute what would be there afterwards, then write. The database
        # forbids a row in which nothing is maintained anymore - so it cannot be
        # emptied first and cleaned up afterwards.
        before = self.read(obj)
        after = {f: given.get(f, getattr(before, f)) for f in FIELDS}
        exists = not before.is_empty

        with self.db.cursor() as c:
            if not any(after.values()):
                if exists:
                    c.execute("DELETE FROM annotation WHERE object = %s", (identifier,))
            elif exists:
                parts = ", ".join(f"{f} = %s" for f in FIELDS)
                c.execute(f"""UPDATE annotation SET {parts}, changed_at = now(),
                                     changed_by = %s
                               WHERE object = %s""",
                          (*[after[f] for f in FIELDS], changed_by or None, identifier))
            else:
                columns = ", ".join(FIELDS)
                slots = ", ".join(["%s"] * len(FIELDS))
                c.execute(f"""INSERT INTO annotation (object, {columns}, changed_by)
                              VALUES (%s, {slots}, %s)""",
                          (identifier, *[after[f] for f in FIELDS], changed_by or None))

        return self.read(obj)

    def all(self) -> dict[str, dict]:
        """Everything maintained at once, by source key - for the map."""
        with self.db.cursor() as c:
            c.execute(f"""SELECT o.external, {", ".join("a." + f for f in FIELDS)}
                            FROM annotation a JOIN object o ON o.id = a.object""")
            return {r[0]: {f: v for f, v in zip(FIELDS, r[1:]) if v}
                    for r in c.fetchall()}

    def search(self, text: str) -> list[tuple[str, Annotation]]:
        """Full-text search over everything maintained - "where was that outlet in
        the basement again?" """
        with self.db.cursor() as c:
            c.execute("""SELECT o.external, a.outlet, a.room, a.note, a.owner,
                                a.name, a.expected, a.changed_at, a.changed_by
                           FROM annotation a JOIN object o ON o.id = a.object
                          WHERE to_tsvector('simple',
                                  coalesce(a.outlet,'')||' '||coalesce(a.room,'')||' '||
                                  coalesce(a.note,'')||' '||coalesce(a.name,''))
                                @@ plainto_tsquery('simple', %s)
                             OR a.outlet ILIKE %s OR a.room ILIKE %s
                          ORDER BY o.external""",
                      (text, f"%{text}%", f"%{text}%"))
            return [(r[0], Annotation(r[0], *r[1:])) for r in c.fetchall()]
