# -*- coding: utf-8 -*-
"""Datenbankmigrationen beim Start des Sammeldienstes einspielen.

Bis zum 17.09.2026 liefen die Dateien in `migrationen/` von Hand vor jedem
Rollout — und ein vergessener Schritt waere erst aufgefallen, wenn die
Oberflaeche mit "Spalte existiert nicht" ausfaellt. Jetzt spielt der Dienst sie
beim Start selbst ein, in Namensreihenfolge, jede genau einmal.

Nur der Sammler migriert, nicht die Weboberflaeche: zwei Prozesse, die
gleichzeitig dieselbe Tabelle aendern, sind eine Falle, die man sich nicht
einbauen muss. Die Beratungssperre (`pg_advisory_lock`) sichert trotzdem ab,
falls der Job einmal mit zwei Sammlern laeuft.
"""
from __future__ import annotations

from pathlib import Path

VERZEICHNIS = Path(__file__).resolve().parents[1] / "migrationen"
SPERRE = 0x6461_6564   # "daed" — beliebig, aber fest


def einspielen(db, verzeichnis: Path = VERZEICHNIS) -> list[str]:
    """Alle noch nicht eingespielten Migrationen ausfuehren. Gibt ihre Namen zurueck."""
    dateien = sorted(verzeichnis.glob("*.sql")) if verzeichnis.is_dir() else []
    neu: list[str] = []
    with db.cursor() as c:
        c.execute("SELECT pg_advisory_lock(%s)", (SPERRE,))
        try:
            c.execute("""CREATE TABLE IF NOT EXISTS schema_migration (
                             name        text PRIMARY KEY,
                             eingespielt timestamptz NOT NULL DEFAULT now())""")
            c.execute("SELECT name FROM schema_migration")
            bekannt = {z[0] for z in c.fetchall()}
            for datei in dateien:
                if datei.name in bekannt:
                    continue
                # Jede Datei ist fuer sich idempotent geschrieben — laeuft sie auf
                # einer Datenbank, die sie schon von Hand bekommen hat, passiert nichts.
                c.execute(datei.read_text(encoding="utf-8"))
                c.execute("INSERT INTO schema_migration (name) VALUES (%s)", (datei.name,))
                neu.append(datei.name)
        finally:
            c.execute("SELECT pg_advisory_unlock(%s)", (SPERRE,))
    return neu
