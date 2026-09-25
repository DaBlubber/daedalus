# -*- coding: utf-8 -*-
"""Apply database migrations when the collector service starts.

Running the files in `migrations/` by hand before every rollout is a step that is
easily forgotten - and it would only show when the UI fails with "column does not
exist". The service therefore applies them itself at start, in name order, each
exactly once.

Only the collector migrates, not the web UI: two processes altering the same
table at the same time is a trap there is no need to build. The advisory lock
(`pg_advisory_lock`) still protects against a deployment that runs two
collectors.
"""
from __future__ import annotations

from pathlib import Path

DIRECTORY = Path(__file__).resolve().parents[1] / "migrations"
LOCK = 0x6461_6564   # "daed" - arbitrary but fixed


def apply(db, directory: Path = DIRECTORY) -> list[str]:
    """Run all migrations not applied yet. Returns their names."""
    files = sorted(directory.glob("*.sql")) if directory.is_dir() else []
    new: list[str] = []
    with db.cursor() as c:
        c.execute("SELECT pg_advisory_lock(%s)", (LOCK,))
        try:
            c.execute("""CREATE TABLE IF NOT EXISTS schema_migration (
                             name        text PRIMARY KEY,
                             applied_at  timestamptz NOT NULL DEFAULT now())""")
            c.execute("SELECT name FROM schema_migration")
            known = {r[0] for r in c.fetchall()}
            for file in files:
                if file.name in known:
                    continue
                # Every file is written to be idempotent on its own - if it runs on
                # a database that already got it by hand, nothing happens.
                c.execute(file.read_text(encoding="utf-8"))
                c.execute("INSERT INTO schema_migration (name) VALUES (%s)", (file.name,))
                new.append(file.name)
        finally:
            c.execute("SELECT pg_advisory_unlock(%s)", (LOCK,))
    return new
