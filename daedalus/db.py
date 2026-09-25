# -*- coding: utf-8 -*-
"""Database connection and first-time schema setup."""
from __future__ import annotations

from pathlib import Path

from .timeutil import with_timezone

SCHEMA = Path(__file__).resolve().parents[1] / "schema.sql"


def session_dsn(dsn: str) -> str:
    """The DSN with the session options Daedalus relies on: the local time zone
    (see timeutil) and the `daedalus` schema first on the search path."""
    from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit
    dsn = with_timezone(dsn)
    parts = urlsplit(dsn)
    p = parse_qs(parts.query, keep_blank_values=True)
    if "search_path" not in p["options"][0]:
        p["options"] = [p["options"][0] + " -c search_path=daedalus,public"]
    return urlunsplit(parts._replace(query=urlencode(p, doseq=True, quote_via=quote)))


def connect(dsn: str):
    import psycopg
    return psycopg.connect(session_dsn(dsn), autocommit=True, connect_timeout=8)


def ensure_schema(db, schema: Path = SCHEMA) -> bool:
    """Create the schema on an empty database. Returns True if it was created.

    An existing schema is left alone - later changes come as migrations.
    """
    with db.cursor() as c:
        c.execute("SELECT to_regclass('daedalus.source')")
        if c.fetchone()[0] is not None:
            return False
        c.execute(schema.read_text(encoding="utf-8"))
    return True
