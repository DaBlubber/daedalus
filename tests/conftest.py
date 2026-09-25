# -*- coding: utf-8 -*-
"""The same rules, two stores.

Every test runs twice: once in memory and once against real PostgreSQL. That
proves not only that the rules are right but also that **schema and rules fit
together** - the most common place where such things drift apart.

Without `DAEDALUS_TEST_DSN` only the memory pass runs; the database tests are
skipped instead of turning red.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from daedalus.db import ensure_schema, session_dsn
from daedalus.store import MemoryStore, PgStore

# CAUTION, and this was learned the hard way: the fixture below runs TRUNCATE. It
# once ran against the PRODUCTION database by accident and deleted a complete
# collection run - 1105 observations, gone.
#
# Hence two locks:
#   1. The tests read `DAEDALUS_TEST_DSN`, NOT `DAEDALUS_DSN`.
#   2. Even then the database is only emptied if its name ends in `_test`.
# One of them alone is not enough; both are required.
DSN = os.environ.get("DAEDALUS_TEST_DSN")


def _is_test_database(dsn: str) -> bool:
    from urllib.parse import urlsplit
    name = urlsplit(dsn).path.lstrip("/")
    return name.endswith("_test")

# The sources of the tests. In the database `run` and `assignment` reference them
# by foreign key - that is intentional: an observation without a known origin
# must not exist.
SOURCES = [
    ("fdb", ["attachment"], 2, 300),
    ("arp", ["address"], 2, 300),
    ("firewall-arp", ["address"], 2, 300),
    ("fdb-c3po", ["attachment"], 2, 300),
    ("lldp-c3po", ["connection"], 3, 1800),
    ("lldp", ["connection"], 3, 1800),
    ("a", ["attachment"], 2, 300),
    ("b", ["attachment"], 2, 300),
    ("q0", ["attachment"], 2, 300),
    ("q1", ["attachment"], 2, 300),
    ("q2", ["attachment"], 2, 300),
    ("q3", ["attachment"], 2, 300),
    ("q4", ["attachment"], 2, 300),

    ("inventory", ["attribute"], 2, 300),
    ("wifi", ["address", "attachment", "attribute"], 2, 300),
    ("kea", ["attribute"], 2, 900),
    ("dhcp-config", ["attribute"], 1, 900),
]


@pytest.fixture(params=["memory", "postgres"])
def store(request):
    if request.param == "memory":
        yield MemoryStore()
        return

    if not DSN:
        pytest.skip("DAEDALUS_TEST_DSN not set - database pass skipped")
    if not _is_test_database(DSN):
        pytest.fail("DAEDALUS_TEST_DSN does not point to a database whose name ends "
                    "in `_test`. The tests empty the database - that must never hit "
                    "the production one.")
    try:
        import psycopg
    except ImportError:
        pytest.skip("psycopg not installed")

    try:
        connection = psycopg.connect(session_dsn(DSN), connect_timeout=6, autocommit=True)
    except Exception as e:                      # noqa: BLE001
        pytest.skip(f"database not reachable: {e}")

    with connection:
        ensure_schema(connection)
        with connection.cursor() as c:
            c.execute("TRUNCATE change, assignment, run, annotation, object, source CASCADE")
            c.executemany(
                "INSERT INTO source (name, responsible_for, missing_threshold, interval_seconds)"
                " VALUES (%s,%s,%s,%s)", SOURCES)
        yield PgStore(connection)
