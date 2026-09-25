# -*- coding: utf-8 -*-
"""Schema and migrations: the schema only on an empty database, every migration
exactly once, and one already applied by hand does no harm."""
import os

import pytest

from daedalus import db as database
from daedalus import migration


@pytest.fixture
def db():
    dsn = os.environ.get("DAEDALUS_TEST_DSN", "")
    if not dsn:
        pytest.skip("DAEDALUS_TEST_DSN not set - database pass skipped")
    if not dsn.rstrip("/").split("?")[0].endswith("_test"):
        pytest.fail("DAEDALUS_TEST_DSN must point to a *_test database")
    import psycopg
    with psycopg.connect(database.session_dsn(dsn), autocommit=True) as connection:
        database.ensure_schema(connection)
        yield connection


def test_schema_is_only_created_once(db):
    assert database.ensure_schema(db) is False, "an existing schema is left alone"


def test_real_migrations_are_idempotent_and_remembered(db):
    with db.cursor() as c:
        c.execute("DROP TABLE IF EXISTS schema_migration")
    first = migration.apply(db)
    assert first == sorted(p.name for p in migration.DIRECTORY.glob("*.sql"))
    assert migration.apply(db) == []                 # second start: nothing


def test_new_file_is_applied_later(db, tmp_path):
    for p in migration.DIRECTORY.glob("*.sql"):
        (tmp_path / p.name).write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
    migration.apply(db, tmp_path)
    (tmp_path / "2099-01-01_probe.sql").write_text(
        "SET search_path TO daedalus; CREATE TABLE IF NOT EXISTS migration_probe (x int);",
        encoding="utf-8")
    assert migration.apply(db, tmp_path) == ["2099-01-01_probe.sql"]
    with db.cursor() as c:
        c.execute("DROP TABLE daedalus.migration_probe")
        c.execute("DELETE FROM schema_migration WHERE name = '2099-01-01_probe.sql'")
