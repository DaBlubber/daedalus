# -*- coding: utf-8 -*-
"""Migrationen: jede genau einmal, und eine schon von Hand eingespielte schadet nicht."""
import os
from pathlib import Path

import pytest

from daedalus import migration


@pytest.fixture
def db():
    dsn = os.environ.get("DAEDALUS_TEST_DSN", "")
    if not dsn:
        pytest.skip("DAEDALUS_TEST_DSN nicht gesetzt — Datenbankdurchgang uebersprungen")
    if not dsn.rstrip("/").split("?")[0].endswith("_test"):
        pytest.fail("DAEDALUS_TEST_DSN muss auf eine *_test-Datenbank zeigen")
    import psycopg
    from daedalus.zeit import mit_zeitzone
    with psycopg.connect(mit_zeitzone(dsn), autocommit=True) as verbindung:
        yield verbindung


def test_echte_migrationen_sind_idempotent_und_werden_gemerkt(db, tmp_path):
    # Die echten Dateien — auf daedalus_test schon einmal von Hand gelaufen.
    with db.cursor() as c:
        c.execute("DROP TABLE IF EXISTS schema_migration")
    ersterlauf = migration.einspielen(db)
    assert ersterlauf == sorted(p.name for p in migration.VERZEICHNIS.glob("*.sql"))
    assert migration.einspielen(db) == []                 # zweiter Start: nichts


def test_neue_datei_wird_nachgezogen(db, tmp_path):
    for p in migration.VERZEICHNIS.glob("*.sql"):
        (tmp_path / p.name).write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
    migration.einspielen(db, tmp_path)
    (tmp_path / "2099-01-01_probe.sql").write_text(
        "SET search_path TO daedalus; CREATE TABLE IF NOT EXISTS migrationsprobe (x int);",
        encoding="utf-8")
    assert migration.einspielen(db, tmp_path) == ["2099-01-01_probe.sql"]
    with db.cursor() as c:
        c.execute("DROP TABLE daedalus.migrationsprobe")
        c.execute("DELETE FROM schema_migration WHERE name = '2099-01-01_probe.sql'")
