# -*- coding: utf-8 -*-
"""Der Dienst. Geprueft wird nicht, dass er laeuft, sondern dass er sich
zurueckhaelt: verschiedene Takte, Ruhe nach Fehlern, nichts gleichzeitig."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from daedalus.dienst import Auftrag, Dienst, bericht
from daedalus.modell import Beobachtung, Beziehung, Quelle

FDB = Quelle("fdb", frozenset({Beziehung.ANSCHLUSS}), fehlt_schwelle=2)
LLDP = Quelle("lldp", frozenset({Beziehung.VERBINDUNG}), fehlt_schwelle=3)


T0 = datetime(2026, 9, 16, 10, 0, tzinfo=timezone.utc)


class Uhr:
    """Eine Uhr, die der Test stellt — monoton UND als Wanduhr.

    Beides muss der Test steuern: die monotone Uhr treibt die Taktung, die
    Wanduhr den Zeitstempel des Laufs. Wer nur die eine stellt, laesst zwei
    Laeufe auf denselben Zeitpunkt fallen — und nach Regel 3 ist das derselbe
    Lauf, der nichts bewirkt.
    """
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t
    def vor(self, s): self.t += s
    def wanduhr(self): return T0 + timedelta(seconds=self.t - 1000.0)


class Attrappe:
    def __init__(self, quelle, wirft=None, unveraendert=False):
        self.quelle = quelle
        self.wirft = wirft
        self.unveraendert = unveraendert
        self.laeufe = 0
    def vorab_unveraendert(self): return self.unveraendert
    def sammeln(self):
        self.laeufe += 1
        if self.wirft: raise self.wirft
        return [Beobachtung(Beziehung.ANSCHLUSS, "aa:bb:cc:dd:ee:ff", "anschluss", "c3po:gi1")]


def dienst(auftraege, uhr):
    return Dienst(auftraege, streuung=0.0, uhr=uhr, schlafen=lambda s: None,
                  zeitgeber=uhr.wanduhr)


def test_jede_quelle_haelt_ihren_eigenen_takt(bestand):
    uhr = Uhr()
    schnell = Attrappe(FDB)
    langsam = Attrappe(LLDP)
    d = dienst([Auftrag(schnell, takt=300), Auftrag(langsam, takt=1800)], uhr)

    uhr.vor(1)                             # der Start ist gestreut
    d.einmal(bestand)                      # beide zum Start
    for _ in range(5):                     # fuenfmal fuenf Minuten
        uhr.vor(300)
        d.einmal(bestand)

    assert schnell.laeufe == 6
    assert langsam.laeufe == 1, "die Nachbarschaft wird nicht alle fuenf Minuten gefragt"


def test_nach_fehlern_wird_zurueckhaltender_gefragt(bestand):
    """Ein Switch, der nicht antwortet, wird durch haeufigeres Fragen nicht
    gespraechiger — man erzeugt nur Last."""
    uhr = Uhr()
    kaputt = Attrappe(FDB, wirft=TimeoutError("keine Antwort"))
    a = Auftrag(kaputt, takt=300)
    d = dienst([a], uhr)

    d.einmal(bestand); assert a.fehler_in_folge == 1 and a.naechster_takt() == 600
    uhr.vor(600); d.einmal(bestand); assert a.naechster_takt() == 1200
    uhr.vor(1200); d.einmal(bestand); assert a.naechster_takt() == 2400
    for _ in range(6):
        uhr.vor(3000); d.einmal(bestand)
    assert a.naechster_takt() == 2400, "gedeckelt beim Achtfachen"


def test_nach_erfolg_ist_der_takt_wieder_normal(bestand):
    uhr = Uhr()
    s = Attrappe(FDB, wirft=OSError("weg"))
    a = Auftrag(s, takt=300)
    d = dienst([a], uhr)
    d.einmal(bestand); uhr.vor(600); d.einmal(bestand)
    assert a.fehler_in_folge == 2
    s.wirft = None
    uhr.vor(1200); d.einmal(bestand)
    assert a.fehler_in_folge == 0 and a.naechster_takt() == 300


def test_sammler_laufen_nacheinander_nicht_gleichzeitig(bestand):
    """Fuenf Switches auf einmal zu fragen erzeugt genau die Lastspitze, die
    vermieden werden soll. Der Dienst arbeitet deshalb der Reihe nach ab."""
    uhr = Uhr()
    reihenfolge = []

    class Merker(Attrappe):
        def sammeln(self):
            reihenfolge.append(self.quelle.name)
            return super().sammeln()

    a = Merker(Quelle("a", frozenset({Beziehung.ANSCHLUSS})))
    b = Merker(Quelle("b", frozenset({Beziehung.ANSCHLUSS})))
    d = dienst([Auftrag(a, takt=300), Auftrag(b, takt=300)], uhr)
    uhr.vor(1)                      # der Start ist absichtlich gestreut
    d.einmal(bestand)
    assert reihenfolge == ["a", "b"]


def test_der_start_wird_gestreut(bestand):
    """Sonst treffen sich beim ersten Durchgang alle Abfragen im selben Moment."""
    uhr = Uhr()
    auftraege = [Auftrag(Attrappe(Quelle(f"q{i}", frozenset({Beziehung.ANSCHLUSS}))),
                         takt=300) for i in range(5)]
    Dienst(auftraege, uhr=uhr, schlafen=lambda s: None)
    assert len({a.faellig_ab for a in auftraege}) == 5


def test_uebersprungene_laeufe_schweigen(bestand):
    """Ein Dienst, der alle fuenf Minuten „nichts geaendert" protokolliert,
    wird nach einer Woche nicht mehr gelesen."""
    uhr = Uhr()
    s = Attrappe(FDB, unveraendert=True)
    a = Auftrag(s, takt=300)
    d = dienst([a], uhr)
    [e] = d.einmal(bestand)
    assert bericht(e, a) == ""


def test_unveraenderte_laeufe_schweigen_auch(bestand):
    uhr = Uhr()
    s = Attrappe(FDB)
    a = Auftrag(s, takt=300)
    d = dienst([a], uhr)
    d.einmal(bestand)                       # Erstlauf
    uhr.vor(300)
    [e] = d.einmal(bestand)
    assert bericht(e, a) == ""


def test_aber_fehler_und_aenderungen_werden_gemeldet(bestand):
    uhr = Uhr()
    s = Attrappe(FDB)
    a = Auftrag(s, takt=300)
    d = dienst([a], uhr)
    d.einmal(bestand)

    s.sammeln = lambda: [Beobachtung(Beziehung.ANSCHLUSS, "aa:bb:cc:dd:ee:ff",
                                     "anschluss", "c3po:gi9")]
    uhr.vor(300)
    [e] = d.einmal(bestand)
    assert "umgezogen" in bericht(e, a) and "c3po:gi1" in bericht(e, a)

    s.sammeln = lambda: (_ for _ in ()).throw(TimeoutError("weg"))
    uhr.vor(300)
    [e] = d.einmal(bestand)
    assert "FEHLER" in bericht(e, a) and "1. in Folge" in bericht(e, a)


def test_wartezeit_richtet_sich_nach_dem_naechsten_faelligen(bestand):
    uhr = Uhr()
    a = Auftrag(Attrappe(FDB), takt=300)
    b = Auftrag(Attrappe(LLDP), takt=1800)
    d = dienst([a, b], uhr)
    uhr.vor(1)                      # der Start ist gestreut, beide faellig machen
    d.einmal(bestand)
    # Die Uhr stand waehrend des Durchgangs still: der naechste Faellige ist
    # der schnelle Sammler, in genau seinem Takt.
    assert d.wartezeit() == 300
