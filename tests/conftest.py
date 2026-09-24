# -*- coding: utf-8 -*-
"""Dieselben Regeln, zwei Speicher.

Jeder Test laeuft zweimal: einmal im Arbeitsspeicher und einmal gegen das echte
PostgreSQL. Damit ist nicht nur bewiesen, dass die Regeln stimmen, sondern auch,
dass **Schema und Regeln zusammenpassen** — der haeufigste Ort, an dem so etwas
auseinanderlaeuft.

Ohne `DAEDALUS_DSN` laeuft nur der Speicher-Durchgang; die Datenbanktests werden
dann uebersprungen statt rot zu werden.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from daedalus.modell import Beziehung
from daedalus.speicher import ImSpeicher, PgSpeicher
from daedalus.zeit import mit_zeitzone

# ACHTUNG, und das ist teuer gelernt: die Vorrichtung unten macht TRUNCATE.
# Am 16.09.2026 lief sie versehentlich gegen die PRODUKTIVdatenbank und hat
# einen kompletten Sammellauf geloescht — 1105 Beobachtungen, weg.
#
# Deshalb zwei Sperren:
#   1. Die Tests lesen `DAEDALUS_TEST_DSN`, NICHT `DAEDALUS_DSN`.
#   2. Auch dann wird nur geraeumt, wenn der Datenbankname auf `_test` endet.
# Eine Kenntnis allein reicht nicht; es braucht beides.
DSN = os.environ.get("DAEDALUS_TEST_DSN")


def _ist_testdatenbank(dsn: str) -> bool:
    from urllib.parse import urlsplit
    name = urlsplit(dsn).path.lstrip("/")
    return name.endswith("_test")

# Die Quellen der Tests. In der Datenbank haengen `lauf` und `zuordnung` per
# Fremdschluessel daran — das ist Absicht: eine Beobachtung ohne bekannte
# Herkunft darf es nicht geben.
QUELLEN = [
    ("fdb", ["anschluss"], 2, 300),
    ("arp", ["adresse"], 2, 300),
    ("sophos-arp", ["adresse"], 2, 300),
    ("fdb-c3po", ["anschluss"], 2, 300),
    ("lldp-c3po", ["verbindung"], 3, 1800),
    ("lldp", ["verbindung"], 3, 1800),
    ("a", ["anschluss"], 2, 300),
    ("b", ["anschluss"], 2, 300),
    ("q0", ["anschluss"], 2, 300),
    ("q1", ["anschluss"], 2, 300),
    ("q2", ["anschluss"], 2, 300),
    ("q3", ["anschluss"], 2, 300),
    ("q4", ["anschluss"], 2, 300),

    ("inventar", ["merkmal"], 2, 300),
    ("wlan", ["adresse", "anschluss", "merkmal"], 2, 300),
    ("kea", ["merkmal"], 2, 900),
    ("dhcp-konfig", ["merkmal"], 1, 900),
]


@pytest.fixture(params=["speicher", "postgres"])
def bestand(request):
    if request.param == "speicher":
        yield ImSpeicher()
        return

    if not DSN:
        pytest.skip("DAEDALUS_TEST_DSN nicht gesetzt — Datenbankdurchgang uebersprungen")
    if not _ist_testdatenbank(DSN):
        pytest.fail("DAEDALUS_TEST_DSN zeigt nicht auf eine Datenbank, deren Name "
                    "auf `_test` endet. Die Tests raeumen die Datenbank leer — "
                    "das darf niemals die produktive treffen.")
    try:
        import psycopg
    except ImportError:
        pytest.skip("psycopg nicht installiert")

    try:
        verbindung = psycopg.connect(mit_zeitzone(DSN), connect_timeout=6, autocommit=True)
    except Exception as e:                      # noqa: BLE001
        pytest.skip(f"Datenbank nicht erreichbar: {e}")

    with verbindung:
        with verbindung.cursor() as c:
            c.execute("TRUNCATE aenderung, zuordnung, lauf, pflege, objekt, quelle CASCADE")
            c.executemany(
                "INSERT INTO quelle (name, zustaendig_fuer, fehlt_schwelle, takt_sekunden)"
                " VALUES (%s,%s,%s,%s)", QUELLEN)
        yield PgSpeicher(verbindung)
