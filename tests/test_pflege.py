# -*- coding: utf-8 -*-
"""Gepflegte Angaben. Der Punkt ist nicht das Speichern, sondern dass sie
alles ueberleben, was mit den gesammelten Daten passiert."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from daedalus.abgleich import einspielen
from daedalus.modell import Beobachtung, Beziehung, Lauf, Quelle
from daedalus.pflege import Pflege

T0 = datetime(2026, 9, 16, 8, 0, tzinfo=timezone.utc)
def t(n): return T0 + timedelta(minutes=5 * n)

FDB = Quelle("fdb", frozenset({Beziehung.ANSCHLUSS}), fehlt_schwelle=2)


def anschluss(g, p): return Beobachtung(Beziehung.ANSCHLUSS, g, "anschluss", p)
def lauf(b, n, beob): return einspielen(b, Lauf("fdb", t(n)), FDB, beob)


@pytest.fixture
def pg(bestand):
    if not hasattr(bestand, "pflege"):
        pytest.skip("Pflege gibt es nur mit Datenbank")
    return bestand


def test_dose_an_einem_port(pg):
    pg.pflege.schreiben("c3po:gi12", dose="B2-14", raum="Buero", von="alex")
    p = pg.pflege.lesen("c3po:gi12")
    assert (p.dose, p.raum, p.von) == ("B2-14", "Buero", "alex")
    assert p.zusammenfassung() == "Dose B2-14 \u00b7 Buero"


def test_eine_dose_kann_eingetragen_werden_bevor_etwas_daran_haengt(pg):
    """Man verkabelt zuerst und steckt spaeter etwas ein."""
    pg.pflege.schreiben("l337:gi9", dose="K-03", raum="Keller")
    assert pg.pflege.lesen("l337:gi9").dose == "K-03"


def test_notiz_ueberlebt_dass_das_geraet_verschwindet(pg):
    """Der Kern: gesammelte Daten enden, gepflegte nicht."""
    lauf(pg, 0, [anschluss("laptop", "c3po:gi12")])
    pg.pflege.schreiben("laptop", notiz="gehoert zum Labor, nicht abziehen")
    lauf(pg, 1, [])
    lauf(pg, 2, [])                       # jetzt gilt es als verschwunden
    assert pg.offen_fuer(Beziehung.ANSCHLUSS, "laptop", "anschluss") is None
    assert pg.pflege.lesen("laptop").notiz == "gehoert zum Labor, nicht abziehen"


def test_und_ist_noch_da_wenn_es_wiederkommt(pg):
    lauf(pg, 0, [anschluss("laptop", "c3po:gi12")])
    pg.pflege.schreiben("laptop", notiz="Labor")
    lauf(pg, 1, []); lauf(pg, 2, [])
    lauf(pg, 3, [anschluss("laptop", "c3po:gi16")])
    assert pg.pflege.lesen("laptop").notiz == "Labor"


def test_ein_sammellauf_fasst_die_pflege_nie_an(pg):
    pg.pflege.schreiben("c3po:gi12", dose="B2-14")
    for n in range(4):
        lauf(pg, n, [anschluss("laptop", "c3po:gi12")])
    assert pg.pflege.lesen("c3po:gi12").dose == "B2-14"


def test_nur_uebergebene_felder_werden_angefasst(pg):
    pg.pflege.schreiben("c3po:gi12", dose="B2-14", raum="Buero")
    pg.pflege.schreiben("c3po:gi12", notiz="Drucker haengt hier")
    p = pg.pflege.lesen("c3po:gi12")
    assert (p.dose, p.raum, p.notiz) == ("B2-14", "Buero", "Drucker haengt hier")


def test_leerer_wert_loescht_das_feld(pg):
    """„soll weg" und „geht mich nichts an" muessen unterscheidbar sein."""
    pg.pflege.schreiben("c3po:gi12", dose="B2-14", raum="Buero")
    pg.pflege.schreiben("c3po:gi12", dose="   ")
    p = pg.pflege.lesen("c3po:gi12")
    assert p.dose is None and p.raum == "Buero"


def test_alles_geleert_laesst_keine_leere_huelle_zurueck(pg):
    pg.pflege.schreiben("c3po:gi12", dose="B2-14")
    pg.pflege.schreiben("c3po:gi12", dose="")
    assert pg.pflege.lesen("c3po:gi12").leer


def test_unbekanntes_feld_wird_abgelehnt(pg):
    with pytest.raises(ValueError):
        pg.pflege.schreiben("c3po:gi12", farbe="blau")


def test_suche_findet_dose_und_raum(pg):
    pg.pflege.schreiben("c3po:gi12", dose="B2-14", raum="Buero")
    pg.pflege.schreiben("l337:gi1", dose="K-03", raum="Keller hinten")
    pg.pflege.schreiben("k2so:gi2", notiz="Wanddose defekt, Ader 3 gebrochen")

    assert [x for x, _ in pg.pflege.suchen("Keller")] == ["l337:gi1"]
    assert [x for x, _ in pg.pflege.suchen("B2-14")] == ["c3po:gi12"]
    assert [x for x, _ in pg.pflege.suchen("defekt")] == ["k2so:gi2"]


def test_zusammenfassung_bleibt_lesbar_wenn_nur_eines_gepflegt_ist(pg):
    assert Pflege("x", dose="B2-14").zusammenfassung() == "Dose B2-14"
    assert Pflege("x", raum="Keller").zusammenfassung() == "Keller"
    assert Pflege("x").zusammenfassung() == ""
