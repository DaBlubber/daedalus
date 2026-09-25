# -*- coding: utf-8 -*-
"""Why everything is computed in UTC and displayed in the local zone.

The tests use Europe/Berlin as the local zone, because it has a daylight-saving
change to prove the point."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from daedalus import timeutil
from daedalus.timeutil import UTC, display, duration, short, to_utc, with_timezone

BERLIN = ZoneInfo("Europe/Berlin")


@pytest.fixture(autouse=True)
def berlin():
    old = timeutil.LOCAL
    timeutil.set_local_timezone("Europe/Berlin")
    yield
    timeutil.LOCAL = old


# 2026-10-25 is the night the clocks go back: 03:00 CEST becomes 02:00 CET.
# 00:30 UTC is then 02:30 CEST, 01:30 UTC is 02:30 CET - two different points in
# time, an hour apart, and both are called "02:30" locally.
EARLY = datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
LATE = datetime(2026, 10, 25, 1, 30, tzinfo=UTC)


def test_the_double_hour_looks_the_same_locally():
    assert display(EARLY, with_date=False) == "02:30"
    assert display(LATE, with_date=False) == "02:30"


def test_but_in_utc_they_are_two_points_in_time():
    """Exactly why storing and computing happen in UTC. In local time the order of
    these two events could not be decided."""
    assert LATE - EARLY == timedelta(hours=1)
    assert EARLY < LATE


def test_display_follows_daylight_saving_time():
    summer = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    winter = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert display(summer, with_date=False) == "14:00"   # CEST, UTC+2
    assert display(winter, with_date=False) == "13:00"   # CET,  UTC+1


def test_naive_timestamp_is_read_as_local_time():
    """That is how it comes from device sources that send no time zone."""
    naive = datetime(2026, 7, 1, 14, 0)
    assert to_utc(naive) == datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


def test_aware_timestamp_stays_the_same_point_in_time():
    aware = datetime(2026, 7, 1, 14, 0, tzinfo=BERLIN)
    assert to_utc(aware) == datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize("minutes,expected", [
    (0, "just now"),
    (3, "3 min ago"),
    (59, "59 min ago"),
])
def test_short_form_within_an_hour(minutes, expected):
    ref = datetime(2026, 9, 15, 18, 51, tzinfo=UTC)
    assert short(ref - timedelta(minutes=minutes), ref) == expected


def test_short_form_today_and_yesterday():
    ref = datetime(2026, 9, 15, 18, 51, tzinfo=UTC)          # 20:51 local
    assert short(datetime(2026, 9, 15, 6, 0, tzinfo=UTC), ref) == "today 08:00"
    assert short(datetime(2026, 9, 14, 6, 0, tzinfo=UTC), ref) == "yesterday 08:00"


def test_short_form_computes_the_day_border_locally():
    """23:30 UTC is already the next day locally - "today", not "yesterday"."""
    ref = datetime(2026, 9, 16, 6, 0, tzinfo=UTC)            # 08:00 local, 16th
    late = datetime(2026, 9, 15, 22, 30, tzinfo=UTC)         # 00:30 local, 16th
    assert short(late, ref) == "today 00:30"


def test_duration():
    now = datetime(2026, 9, 15, 18, 0, tzinfo=UTC)
    assert duration(now - timedelta(minutes=20), now) == "for 20 min"
    assert duration(now - timedelta(hours=5), now) == "for 5 h"
    assert duration(now - timedelta(days=1), now) == "for 1 day"
    assert duration(now - timedelta(days=4), now) == "for 4 days"


# ---------------------------------------------------------------------------
# PgBouncer overrides the time zone of the server. Measured, not assumed.
# ---------------------------------------------------------------------------
def test_time_zone_is_written_into_the_connection():
    d = with_timezone("postgresql://u:p@172.16.1.5:5432/daedalus")
    assert "TimeZone" in d and "Europe%2FBerlin" in d.replace("/", "%2F")


def test_existing_options_are_kept():
    d = with_timezone("postgresql://u:p@h/db?options=-c+statement_timeout%3D5000")
    assert "statement_timeout" in d and "TimeZone" in d


def test_is_not_set_twice():
    once = with_timezone("postgresql://u:p@h/db")
    assert with_timezone(once).count("TimeZone") == 1


def test_session_dsn_adds_the_search_path():
    from daedalus.db import session_dsn
    d = session_dsn("postgresql://u:p@h/db")
    assert "search_path" in d and "TimeZone" in d
    assert session_dsn(d).count("search_path") == 1
