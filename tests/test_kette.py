# -*- coding: utf-8 -*-
"""Die Kette. Gebaut aus drei Quellen, die einzeln nichts taugen."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from daedalus.abgleich import einspielen
from daedalus.kette import (Kette, abstaende, kette, nachbarschaft,
                            wer_hing_hier)
from daedalus.modell import Beobachtung, Beziehung, Lauf, Quelle

T0 = datetime(2026, 9, 16, 8, 0, tzinfo=timezone.utc)
def t(n): return T0 + timedelta(minutes=5 * n)

FDB = Quelle("fdb", frozenset({Beziehung.ANSCHLUSS}), fehlt_schwelle=2)
LLDP = Quelle("lldp", frozenset({Beziehung.VERBINDUNG}), fehlt_schwelle=3)


def anschluss(mac, port): return Beobachtung(Beziehung.ANSCHLUSS, mac, "anschluss", port)
def verbindung(a, b): return Beobachtung(Beziehung.VERBINDUNG, a, "verbindung", b)


def netz_aufbauen(b, n=0):
    """Die gemessene Wirklichkeit vom 16.09.: bb8 ist die Wurzel, c3po haengt
    an bb8 UND an r2d2, r2d2 haengt an bb8."""
    einspielen(b, Lauf("lldp", t(n)), LLDP, [
        verbindung("bb8:gi1", "c3po:gi25"),
        verbindung("bb8:gi13", "c3po:gi26"),
        verbindung("bb8:gi2", "r2d2:gi25"),
        verbindung("r2d2:gi27", "c3po:gi27"),
    ])
    einspielen(b, Lauf("fdb", t(n)), FDB, [
        anschluss("64:28:aa:9e:1c:b2", "c3po:gi12"),
        anschluss("e8:ec:76:2f:6d:53", "r2d2:gi1"),
    ])


# --------------------------------------------------------------- Graph
def test_nachbarschaft_gilt_in_beide_richtungen(bestand):
    """LLDP wird oft nur von einer Seite gemeldet. Ein Switch, dessen SNMP
    schweigt, waere sonst vom Netz abgeschnitten, obwohl sein Nachbar ihn sieht."""
    netz_aufbauen(bestand)
    g = nachbarschaft(bestand)
    assert "c3po" in g and "bb8" in g
    assert any(nb == "bb8" for nb, _, _ in g["c3po"])
    assert any(nb == "c3po" for nb, _, _ in g["bb8"])


def test_abstaende_von_der_wurzel(bestand):
    netz_aufbauen(bestand)
    ab, _ = abstaende(nachbarschaft(bestand), "bb8")
    assert ab == {"bb8": 0, "c3po": 1, "r2d2": 1}


# --------------------------------------------------------------- Kette
def test_die_kette_eines_geraets(bestand):
    netz_aufbauen(bestand)
    k = kette(bestand, "64:28:aa:9e:1c:b2", "bb8")
    assert [g.name for g in k.glieder] == ["bb8", "c3po", "64:28:aa:9e:1c:b2"]
    assert k.glieder[1].port == "gi12", "der Zugangsport steht am letzten Switch"
    assert k.vollstaendig


def test_die_kette_liest_sich(bestand):
    netz_aufbauen(bestand)
    s = str(kette(bestand, "e8:ec:76:2f:6d:53", "bb8"))
    assert "bb8" in s and "r2d2 gi1" in s and "\u2192" in s


def test_mehrere_gleich_kurze_wege_werden_zugegeben(bestand):
    """c3po haengt an bb8 (direkt) und an r2d2 (das an bb8 haengt). Ueber bb8
    ist es ein Schritt, ueber r2d2 zwei - also eindeutig. Erst wenn zwei Wege
    GLEICH lang sind, darf die Karte nicht mehr schweigend raten."""
    b = bestand
    einspielen(b, Lauf("lldp", t(0)), LLDP, [
        verbindung("kern:gi1", "a:gi1"),
        verbindung("kern:gi2", "b:gi1"),
        verbindung("a:gi2", "z:gi1"),
        verbindung("b:gi2", "z:gi2"),
    ])
    einspielen(b, Lauf("fdb", t(0)), FDB, [anschluss("aa:bb:cc:dd:ee:ff", "z:gi5")])
    k = kette(b, "aa:bb:cc:dd:ee:ff", "kern")
    assert not k.eindeutig, "zwei gleich kurze Wege - das muss die Kette sagen"
    assert k.glieder[0].name == "kern" and k.glieder[-1].name == "aa:bb:cc:dd:ee:ff"


def test_geraet_ohne_anschluss_hat_keine_kette(bestand):
    netz_aufbauen(bestand)
    k = kette(bestand, "00:00:00:00:00:01", "bb8")
    assert not k and not k.vollstaendig


def test_unbekannter_switch_gibt_eine_ehrliche_teilauskunft(bestand):
    """„haengt an fremd Port 3, Weg dorthin unbekannt" ist besser als nichts
    und viel besser als ein erfundener Weg."""
    b = bestand
    netz_aufbauen(b)
    einspielen(b, Lauf("fdb", t(1)), FDB, [
        anschluss("64:28:aa:9e:1c:b2", "c3po:gi12"),
        anschluss("e8:ec:76:2f:6d:53", "r2d2:gi1"),
        anschluss("8e:00:24:39:66:1d", "fremd:gi3")])
    k = kette(b, "8e:00:24:39:66:1d", "bb8")
    assert not k.vollstaendig
    assert [g.name for g in k.glieder] == ["fremd", "8e:00:24:39:66:1d"]


def test_die_kette_eines_switches_endet_dort(bestand):
    netz_aufbauen(bestand)
    k = kette(bestand, "c3po", "bb8")
    assert [g.name for g in k.glieder] == ["bb8", "c3po"]


def test_die_wurzel_selbst(bestand):
    netz_aufbauen(bestand)
    assert [g.name for g in kette(bestand, "bb8", "bb8").glieder] == ["bb8"]


# --------------------------------------------------------------- Portgeschichte
def test_wer_hing_hier_vorher(bestand):
    """Faellt mit dem Intervallmodell gratis ab."""
    b = bestand
    einspielen(b, Lauf("fdb", t(0)), FDB, [anschluss("8a:72:0b:f6:ad:d1", "c3po:gi12")])
    einspielen(b, Lauf("fdb", t(1)), FDB, [anschluss("cf:d0:4d:c4:7d:be", "c3po:gi12")])
    einspielen(b, Lauf("fdb", t(2)), FDB, [anschluss("cf:d0:4d:c4:7d:be", "c3po:gi12")])

    verlauf = wer_hing_hier(b, "c3po:gi12")
    assert [x[0] for x in verlauf] == ["cf:d0:4d:c4:7d:be", "8a:72:0b:f6:ad:d1"]
    assert verlauf[0][2] is None, "das juengste gilt noch"
    # Nicht t(1), sondern t(2): das alte Geraet fehlte bei t(1) zum ERSTEN Mal,
    # und einmal Fehlen ist kein Beweis (fehlt_schwelle=2). Die Portgeschichte
    # zeigt deshalb eine Ueberlappung von genau einem Sammeltakt - das ist
    # gewollt und ehrlicher, als ein Intervall auf Verdacht zu schliessen.
    assert verlauf[1][2] == t(2)


def test_leerer_port_hat_keine_geschichte(bestand):
    netz_aufbauen(bestand)
    assert wer_hing_hier(bestand, "c3po:gi99") == []


# ---------------------------------------------------------------------------
# Der Fund aus dem ersten echten Lauf: `ap:Luke` und `c3po:gi12` teilen sich
# denselben Doppelpunkt, bedeuten aber Verschiedenes.
# ---------------------------------------------------------------------------
def test_wlan_client_bekommt_die_kette_ueber_seinen_access_point(bestand):
    """Der Access Point haengt an einem Switch-Port, der Client am Access Point.
    Die Kette muss durch beides gehen."""
    b = bestand
    einspielen(b, Lauf("lldp", t(0)), LLDP, [
        verbindung("bb8:gi1", "r2d2:gi25"),
        verbindung("r2d2:gi15", "Luke:port1"),
    ])
    einspielen(b, Lauf("fdb", t(0)), FDB, [
        anschluss("04:c5:81:9f:c2:ea", "ap:Luke"),
    ])
    k = kette(b, "04:c5:81:9f:c2:ea", "bb8")
    namen = [g.name for g in k.glieder]
    assert namen == ["bb8", "r2d2", "Luke", "04:c5:81:9f:c2:ea"], namen
    assert k.vollstaendig


def test_der_access_point_wird_nicht_zum_knoten_namens_ap(bestand):
    """`ap:Luke` darf nicht als Knoten „ap" mit Port „Luke" gelesen werden."""
    from daedalus.kette import _knoten, _port
    assert _knoten("ap:Luke") == "Luke" and _port("ap:Luke") == ""
    assert _knoten("c3po:gi12") == "c3po" and _port("c3po:gi12") == "gi12"


def test_jeder_switch_zeigt_seinen_eigenen_port(bestand):
    """Nicht den des Nachbarn. Beim ersten echten Lauf stand an r2d2 die
    Portkennung des Access Points — und die ist dort eine MAC-Adresse."""
    b = bestand
    einspielen(b, Lauf("lldp", t(0)), LLDP, [
        verbindung("bb8:gi1", "r2d2:gi25"),
        verbindung("r2d2:gi15", "Luke:AA BB CC DD EE FF"),
    ])
    einspielen(b, Lauf("fdb", t(0)), FDB, [anschluss("04:c5:81:9f:c2:ea", "ap:Luke")])
    k = kette(b, "04:c5:81:9f:c2:ea", "bb8")
    paare = [(g.name, g.port) for g in k.glieder]
    assert paare[0] == ("bb8", "gi1"), "bb8 erreicht r2d2 ueber seinen gi1"
    assert paare[1] == ("r2d2", "gi15"), "r2d2 erreicht Luke ueber seinen gi15"
    assert all(" " not in p for _, p in paare), "keine MAC darf als Port erscheinen"
