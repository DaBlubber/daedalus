# -*- coding: utf-8 -*-
"""Time: computed in UTC, displayed in the local time zone.

That is not a contradiction but exactly the separation such tools otherwise fail at:

* **Stored and computed in UTC.** An interval that ends at 02:30 must be
  unambiguous. In the night of the daylight-saving change, however, 02:30 exists
  **twice** in a zone like Europe/Berlin - once in summer time and an hour later
  again in winter time. Computing in local time there loses history in exactly that
  hour or produces intervals that run backwards.
* **Displayed in local time.** "3 min ago" and "yesterday 18:51" should match the
  clock the person next to the screen is looking at.

The local zone comes from the configuration (`timezone`, see `config.py`) and
defaults to UTC. The database stores `timestamptz`, i.e. an absolute point in
time; the session time zone is set to the local zone so that a manual `psql` look
shows the right clock time - it does not change the stored value.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc
LOCAL = ZoneInfo(os.environ.get("DAEDALUS_TIMEZONE", "UTC"))

_WEEKDAY = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def set_local_timezone(name: str) -> None:
    """Set the display zone (IANA name, e.g. "Europe/Berlin")."""
    global LOCAL
    LOCAL = ZoneInfo(name)


def now() -> datetime:
    """The current point in time - always timezone-aware, always UTC."""
    return datetime.now(UTC)


def to_utc(dt: datetime) -> datetime:
    """Bring everything that comes in to UTC.

    A naive timestamp is read as local time - that is how it comes from device
    sources that do not send a time zone (SNMP, Kea, switch logs).
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL)
    return dt.astimezone(UTC)


def display(dt: datetime, with_date: bool = True) -> str:
    """How a point in time appears on screen - in local time."""
    o = dt.astimezone(LOCAL)
    return o.strftime("%Y-%m-%d %H:%M") if with_date else o.strftime("%H:%M")


def short(dt: datetime, reference: datetime | None = None) -> str:
    """Human short form: "3 min ago", "today 18:51", "yesterday 06:31".

    Exactly the form shown on the cards of the canvas.
    """
    reference = reference or now()
    o, r = dt.astimezone(LOCAL), reference.astimezone(LOCAL)
    d = r - o

    if d < timedelta(0):
        return display(dt)
    if d < timedelta(minutes=1):
        return "just now"
    if d < timedelta(hours=1):
        return f"{int(d.total_seconds() // 60)} min ago"
    if o.date() == r.date():
        return f"today {o:%H:%M}"
    if (r.date() - o.date()).days == 1:
        return f"yesterday {o:%H:%M}"
    if (r.date() - o.date()).days < 7:
        return f"{_WEEKDAY[o.weekday()]} {o:%H:%M}"
    return display(dt)


def duration(since: datetime, until: datetime | None = None) -> str:
    """How long an interval has been valid - "for 4 days", "for 2 h"."""
    d = (until or now()) - since
    days = d.days
    if days >= 365:
        years = days // 365
        return f"for {years} year" + ("s" if years > 1 else "")
    if days >= 1:
        return f"for {days} day" + ("s" if days > 1 else "")
    hours = int(d.total_seconds() // 3600)
    if hours >= 1:
        return f"for {hours} h"
    return f"for {max(1, int(d.total_seconds() // 60))} min"


# ---------------------------------------------------------------------------
# Database connection
# ---------------------------------------------------------------------------
# `ALTER DATABASE ... SET timezone` works at the server - but NOT through
# PgBouncer: the pool builds its server connections with its own startup
# parameters and sets Etc/UTC. Measured behind PgBouncer:
#
#     directly at the leader  -> the configured zone
#     through PgBouncer       -> Etc/UTC
#
# The reliable way is the startup parameter in the connection string. PgBouncer
# keys its pools by startup parameters, so the setting holds for every
# connection of the pool.
def with_timezone(dsn: str) -> str:
    """Make sure the connection displays in the local zone."""
    from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit
    option = f"-c TimeZone={LOCAL.key}"
    parts = urlsplit(dsn)
    p = parse_qs(parts.query, keep_blank_values=True)
    if "options" not in p:
        p["options"] = [option]
    elif "TimeZone" not in p["options"][0]:
        p["options"] = [p["options"][0] + " " + option]
    # quote, not the default quote_plus: libpq does not read "+" as a space in a
    # connection URI and would reject the option as "+TimeZone".
    return urlunsplit(parts._replace(query=urlencode(p, doseq=True, quote_via=quote)))
