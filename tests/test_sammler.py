# -*- coding: utf-8 -*-
"""Das Geruest. Gepruefte Frage: verhaelt sich ein Sammler richtig, wenn die
Quelle schweigt, luegt oder gar nicht erst gefragt wird?"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from daedalus.modell import Beobachtung, Beziehung, Quelle
from daedalus.sammler import durchlauf, runde

T0 = datetime(2026, 9, 16, 8, 0, tzinfo=timezone.utc)
def t(n): return T0 + timedelta(minutes=5 * n)

FDB = Quelle("fdb", frozenset({Beziehung.ANSCHLUSS}), fehlt_schwelle=2)


class Attrappe:
    """Ein Sammler, dessen Verhalten der Test vorgibt."""
    def __init__(self, quelle=FDB, unveraendert=False, wirft=None):
        self.quelle = quelle
        self.unveraendert = unveraendert
        self.wirft = wirft
        self.werte: list[Beobachtung] = []
        self.gefragt = 0
        self.vorab_gefragt = 0

    def vorab_unveraendert(self):
        self.vorab_gefragt += 1
        if callable(self.unveraendert):
            return self.unveraendert()
        return self.unveraendert

    def sammeln(self):
        self.gefragt += 1
        if self.wirft:
            raise self.wirft
        return list(self.werte)


def anschluss(g, p): return Beobachtung(Beziehung.ANSCHLUSS, g, "anschluss", p)


def test_gewoehnlicher_lauf(bestand):
    s = Attrappe(); s.werte = [anschluss("laptop", "c3po:gi12")]
    e = durchlauf(bestand, s, t(0))
    assert e.erfolgreich and e.beobachtungen == 1 and not e.uebersprungen
    assert bestand.offen_fuer(Beziehung.ANSCHLUSS, "laptop", "anschluss") is not None


def test_vorabfrage_spart_die_teure_abfrage(bestand):
    s = Attrappe(unveraendert=True)
    e = durchlauf(bestand, s, t(0))
    assert e.uebersprungen and e.erfolgreich
    assert s.gefragt == 0, "die teure Abfrage darf gar nicht erst laufen"


def test_ein_uebersprungener_lauf_laesst_nichts_verschwinden(bestand):
    """Der wichtigste Test hier: Sparsamkeit darf keine Geraete kosten."""
    s = Attrappe(); s.werte = [anschluss("laptop", "c3po:gi12")]
    durchlauf(bestand, s, t(0))
    s.unveraendert = True
    for n in (1, 2, 3, 4, 5):
        assert durchlauf(bestand, s, t(n)).uebersprungen
    assert bestand.offen_fuer(Beziehung.ANSCHLUSS, "laptop", "anschluss") is not None
    assert bestand.aenderungen_seit(T0) == []


def test_kaputte_vorabfrage_fuehrt_zur_teuren_abfrage(bestand):
    """Im Zweifel sammeln. Ein zu Unrecht ausgelassener Lauf verliert eine
    Aenderung, ein zu Unrecht ausgefuehrter kostet ein paar Pakete."""
    def kaputt(): raise RuntimeError("SNMP-Zeitueberschreitung")
    s = Attrappe(unveraendert=kaputt); s.werte = [anschluss("laptop", "c3po:gi12")]
    e = durchlauf(bestand, s, t(0))
    assert e.erfolgreich and s.gefragt == 1


def test_gescheiterte_quelle_wird_festgehalten_aendert_aber_nichts(bestand):
    s = Attrappe(); s.werte = [anschluss("laptop", "c3po:gi12")]
    durchlauf(bestand, s, t(0))

    kaputt = Attrappe(wirft=TimeoutError("keine Antwort von 172.16.0.3"))
    for n in (1, 2, 3):
        e = durchlauf(bestand, kaputt, t(n))
        assert not e.erfolgreich and "TimeoutError" in e.fehler
    assert bestand.offen_fuer(Beziehung.ANSCHLUSS, "laptop", "anschluss") is not None
    assert bestand.aenderungen_seit(T0) == []


def test_ein_gescheiterter_sammler_reisst_die_anderen_nicht_mit(bestand):
    gut = Attrappe(); gut.werte = [anschluss("laptop", "c3po:gi12")]
    schlecht = Attrappe(quelle=Quelle("arp", frozenset({Beziehung.ADRESSE})),
                        wirft=OSError("Netz nicht erreichbar"))
    ergebnisse = runde(bestand, [schlecht, gut], t(0))
    assert [e.erfolgreich for e in ergebnisse] == [False, True]
    assert bestand.offen_fuer(Beziehung.ANSCHLUSS, "laptop", "anschluss") is not None


def test_aenderungen_kommen_im_ergebnis_zurueck(bestand):
    s = Attrappe(); s.werte = [anschluss("pi", "c3po:gi15")]
    durchlauf(bestand, s, t(0))
    s.werte = [anschluss("pi", "c3po:gi16")]
    e = durchlauf(bestand, s, t(1))
    assert len(e.aenderungen) == 1
    assert e.aenderungen[0].vorher == "c3po:gi15"


def test_ergebnis_liest_sich_als_zeile(bestand):
    s = Attrappe(); s.werte = [anschluss("a", "sw:1")]
    assert "1 Beobachtungen" in str(durchlauf(bestand, s, t(0)))
    assert "uebersprungen" in str(durchlauf(bestand, Attrappe(unveraendert=True), t(1)))
