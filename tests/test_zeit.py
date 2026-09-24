# -*- coding: utf-8 -*-
"""Der Grund, warum in UTC gerechnet und in Europe/Berlin angezeigt wird."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from daedalus.zeit import (BERLIN, UTC, anzeige, dauer, kurz, mit_zeitzone,
                           nach_utc)

# 25.10.2026 ist die Nacht der Rueckstellung: 03:00 MESZ wird zu 02:00 MEZ.
# 00:30 UTC ist dann 02:30 MESZ, 01:30 UTC ist 02:30 MEZ — zwei verschiedene
# Zeitpunkte, eine Stunde auseinander, und beide heissen oertlich "02:30".
FRUEH = datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
SPAET = datetime(2026, 10, 25, 1, 30, tzinfo=UTC)


def test_die_doppelte_stunde_sieht_ortszeitlich_gleich_aus():
    assert anzeige(FRUEH, mit_datum=False) == "02:30"
    assert anzeige(SPAET, mit_datum=False) == "02:30"


def test_aber_in_utc_sind_es_zwei_zeitpunkte():
    """Genau deshalb wird gespeichert und gerechnet in UTC. Ortszeitlich
    waere die Reihenfolge dieser beiden Ereignisse nicht entscheidbar."""
    assert SPAET - FRUEH == timedelta(hours=1)
    assert FRUEH < SPAET


def test_anzeige_folgt_der_sommerzeit():
    sommer = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    winter = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert anzeige(sommer, mit_datum=False) == "14:00"   # MESZ, UTC+2
    assert anzeige(winter, mit_datum=False) == "13:00"   # MEZ,  UTC+1


def test_nackte_angabe_wird_als_ortszeit_gelesen():
    """So kommt sie aus Geraetequellen, die keine Zeitzone mitliefern."""
    nackt = datetime(2026, 7, 1, 14, 0)
    assert nach_utc(nackt) == datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


def test_bereits_behaftete_angabe_bleibt_derselbe_zeitpunkt():
    drin = datetime(2026, 7, 1, 14, 0, tzinfo=BERLIN)
    assert nach_utc(drin) == datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize("minuten,erwartet", [
    (0, "gerade eben"),
    (3, "vor 3 Min"),
    (59, "vor 59 Min"),
])
def test_kurzform_innerhalb_einer_stunde(minuten, erwartet):
    bezug = datetime(2026, 9, 15, 18, 51, tzinfo=UTC)
    assert kurz(bezug - timedelta(minutes=minuten), bezug) == erwartet


def test_kurzform_heute_und_gestern():
    bezug = datetime(2026, 9, 15, 18, 51, tzinfo=UTC)          # 20:51 Berlin
    assert kurz(datetime(2026, 9, 15, 6, 0, tzinfo=UTC), bezug) == "heute 08:00"
    assert kurz(datetime(2026, 9, 14, 6, 0, tzinfo=UTC), bezug) == "gestern 08:00"


def test_kurzform_rechnet_die_tagesgrenze_oertlich():
    """23:30 UTC ist in Berlin schon der naechste Tag — „heute", nicht „gestern"."""
    bezug = datetime(2026, 9, 16, 6, 0, tzinfo=UTC)            # 08:00 Berlin, 16.09.
    spaet = datetime(2026, 9, 15, 22, 30, tzinfo=UTC)          # 00:30 Berlin, 16.09.
    assert kurz(spaet, bezug) == "heute 00:30"


def test_dauer():
    jetzt = datetime(2026, 9, 15, 18, 0, tzinfo=UTC)
    assert dauer(jetzt - timedelta(minutes=20), jetzt) == "seit 20 Min"
    assert dauer(jetzt - timedelta(hours=5), jetzt) == "seit 5 Std"
    assert dauer(jetzt - timedelta(days=1), jetzt) == "seit 1 Tag"
    assert dauer(jetzt - timedelta(days=4), jetzt) == "seit 4 Tagen"


# ---------------------------------------------------------------------------
# PgBouncer ueberschreibt die Zeitzone des Servers. Gemessen, nicht vermutet.
# ---------------------------------------------------------------------------
def test_zeitzone_wird_in_die_verbindung_geschrieben():
    d = mit_zeitzone("postgresql://u:p@172.16.1.5:5432/daedalus")
    assert "TimeZone" in d and "Europe%2FBerlin" in d.replace("/", "%2F")


def test_vorhandene_optionen_bleiben_erhalten():
    d = mit_zeitzone("postgresql://u:p@h/db?options=-c+statement_timeout%3D5000")
    assert "statement_timeout" in d and "TimeZone" in d


def test_wird_nicht_doppelt_gesetzt():
    einmal = mit_zeitzone("postgresql://u:p@h/db")
    assert mit_zeitzone(einmal).count("TimeZone") == 1
