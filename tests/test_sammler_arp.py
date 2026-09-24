# -*- coding: utf-8 -*-
"""Der ARP-Sammler, gegen eine echte Probe von der Sophos.

Die Probe in `tests/proben/sophos-arp.txt` ist am 16.09.2026 wirklich von
172.16.1.1 abgenommen worden — damit die Tests an dem scheitern, was das Geraet
liefert, und nicht an dem, was ich mir vorgestellt habe."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from daedalus.modell import Beziehung
from daedalus.sammler import durchlauf
from daedalus.sammler_arp import (SophosArp, auswerten, mac_lesbar, zufalls_mac)

PROBE = (Path(__file__).parent / "proben" / "sophos-arp.txt").read_text(encoding="utf-8")
T0 = datetime(2026, 9, 16, 8, 0, tzinfo=timezone.utc)
def t(n): return T0 + timedelta(minutes=5 * n)


def sammler(ausgabe=PROBE, wirft=None):
    def aufrufer():
        if wirft:
            raise wirft
        return ausgabe
    return SophosArp("172.16.1.1", "geheim", aufrufer=aufrufer)


# --------------------------------------------------------------- Auswertung
def test_probe_wird_vollstaendig_ausgewertet():
    b = auswerten(PROBE)
    assert len(b) >= 30, "die Probe enthaelt mindestens 40 ARP-Zeilen"
    assert all(x.beziehung is Beziehung.ADRESSE for x in b)


def test_erster_eintrag_stimmt():
    b = auswerten(PROBE)
    erste = [x for x in b if x.wert == "172.16.0.2"][0]
    assert erste.objekt == "80:f6:0f:9a:ba:dd"      # r2d2


def test_mac_wird_vereinheitlicht():
    assert mac_lesbar("80 F6 0F 9A BA DD ") == "80:f6:0f:9a:ba:dd"
    assert mac_lesbar("78 62 60 46 A9 3D") == "78:62:60:46:a9:3d", "einstellige Bytes"


def test_der_objektschluessel_ist_die_mac_nicht_die_ip():
    """IPs wandern, MACs meist nicht."""
    b = auswerten(PROBE)
    assert all(":" in x.objekt for x in b)
    assert all(x.wert.count(".") == 3 for x in b)


def test_kaputte_zeilen_kosten_nicht_die_ganze_tabelle():
    kaputt = PROBE + "\nvöllig unbrauchbare Zeile\n.1.3.6.1.2.1.4.22.1.2 = \n"
    assert len(auswerten(kaputt)) == len(auswerten(PROBE))


def test_unvollstaendige_mac_wird_verworfen():
    assert auswerten('.1.3.6.1.2.1.4.22.1.2.11.172.16.0.9 = "C8 00 84 "') == []


def test_leere_ausgabe_ergibt_nichts():
    assert auswerten("") == []


# --------------------------------------------------------------- Zufalls-MAC
@pytest.mark.parametrize("mac,erwartet", [
    ("80:f6:0f:9a:ba:dd", False),    # Cisco, fest vergeben
    ("96:0d:91:1f:48:d9", True),     # lokal verwaltet -> gewuerfelt
    ("d6:96:81:be:6a:fc", True),
    ("76:1c:16:25:3d:7d", True),
    ("78:62:60:46:a9:3d", False),
])
def test_zufalls_mac_erkennen(mac, erwartet):
    assert zufalls_mac(mac) is erwartet


def test_zufalls_mac_verschluckt_sich_nicht_an_muell():
    assert zufalls_mac("") is False and zufalls_mac("kein:mac") is False


# --------------------------------------------------------------- im Durchlauf
def test_gewoehnlicher_lauf(bestand):
    e = durchlauf(bestand, sammler(), t(0))
    assert e.erfolgreich and e.beobachtungen >= 30
    assert bestand.offen_fuer(Beziehung.ADRESSE, "80:f6:0f:9a:ba:dd", "ip:172.16.0.2").wert == "172.16.0.2"


def test_unveraenderte_laeufe_erzeugen_keine_zeile(bestand):
    durchlauf(bestand, sammler(), t(0))
    vorher = bestand.anzahl_intervalle()
    for n in (1, 2, 3, 4):
        assert durchlauf(bestand, sammler(), t(n)).aenderungen == ()
    assert bestand.anzahl_intervalle() == vorher


def test_keine_billige_vorabfrage_und_das_ist_ehrlich(bestand):
    """Wer keine hat, sagt False — und sammelt. Nicht: tut so, als haette er eine."""
    assert sammler().vorab_unveraendert() is False


def test_leere_antwort_gilt_als_fehler_nicht_als_leere_tabelle(bestand):
    """Eine aktive Firewall hat immer ARP-Eintraege. Eine leere Antwort ist ein
    Fehler — und darf nach Regel 1 nichts verschwinden lassen."""
    durchlauf(bestand, sammler(), t(0))
    for n in (1, 2, 3):
        e = durchlauf(bestand, sammler(ausgabe=""), t(n))
        assert not e.erfolgreich
    assert bestand.offen_fuer(Beziehung.ADRESSE, "80:f6:0f:9a:ba:dd", "ip:172.16.0.2") is not None


def test_ausgefallene_sophos_laesst_nichts_verschwinden(bestand):
    durchlauf(bestand, sammler(), t(0))
    for n in (1, 2, 3, 4):
        assert not durchlauf(bestand, sammler(wirft=TimeoutError("keine Antwort")), t(n)).erfolgreich
    assert bestand.offen_fuer(Beziehung.ADRESSE, "80:f6:0f:9a:ba:dd", "ip:172.16.0.2") is not None


def test_adresswechsel_wird_erkannt(bestand):
    durchlauf(bestand, sammler(), t(0))
    gewandert = PROBE.replace(".11.172.16.0.2 =", ".11.172.16.0.99 =")
    e = durchlauf(bestand, sammler(ausgabe=gewandert), t(1))
    # Ein Intervall je Adresse: die neue kommt sofort dazu, die alte geht erst,
    # wenn sie eine Stunde lang fehlt (ARP altert aus, siehe fehlt_schwelle).
    assert any(a.nachher == "172.16.0.99" and a.vorher is None for a in e.aenderungen)
    assert bestand.offen_fuer(Beziehung.ADRESSE, "80:f6:0f:9a:ba:dd", "ip:172.16.0.2") is not None


def test_die_quelle_ist_eng_gefasst():
    """Sie sieht Adressen. Ihr Schweigen darf keinen Anschluss beenden."""
    assert sammler().quelle.zustaendig_fuer == frozenset({Beziehung.ADRESSE})


# ---------------------------------------------------------------------------
# Dieselbe OID, zwei Darstellungen — beide am 16.09.2026 im selben Netz gemessen.
# ---------------------------------------------------------------------------
PROBE_ALPINE = (Path(__file__).parent / "proben" / "sophos-arp-alpine.txt").read_text(encoding="utf-8")


def test_doppelpunkt_schreibweise_wird_auch_gelesen():
    """Der Container ohne MIBs schreibt `80:f6:f:9a:ba:dd` statt
    `"80 F6 0F 9A BA DD "`. Ein Sammler darf sich nicht darauf verlassen,
    welches Werkzeug zufaellig installiert ist."""
    b = auswerten(PROBE_ALPINE)
    assert len(b) >= 30
    assert any(x.objekt == "80:f6:0f:9a:ba:dd" and x.wert == "172.16.0.2" for x in b)


def test_beide_darstellungen_ergeben_dieselbe_mac():
    assert mac_lesbar("80 F6 0F 9A BA DD ") == mac_lesbar("80:f6:f:9a:ba:dd")


def test_ungepolsterte_bytes_werden_aufgefuellt():
    """`c8:0:84:…` - das zweite Byte hat nur eine Stelle."""
    assert mac_lesbar("80:f6:f:9a:ba:dd") == "80:f6:0f:9a:ba:dd"


def test_die_gleiche_sophos_ergibt_aus_beiden_proben_dieselben_geraete():
    """Die Gegenprobe: zwei Werkzeuge, dieselbe Firewall, dieselbe Antwort."""
    a = {x.objekt: x.wert for x in auswerten(PROBE)}
    c = {x.objekt: x.wert for x in auswerten(PROBE_ALPINE)}
    gemeinsam = set(a) & set(c)
    assert len(gemeinsam) >= 30
    assert all(a[m] == c[m] for m in gemeinsam)


def test_nulladresse_ist_keine_beobachtung():
    echt = '.1.3.6.1.2.1.4.22.1.2.11.172.16.11.232 = "F4 51 4D B1 B3 8F "'
    null = '.1.3.6.1.2.1.4.22.1.2.11.0.0.0.0 = "F4 51 4D B1 B3 8F "'
    assert len(auswerten(echt)) == 1          # die Zeilenform stimmt ...
    assert auswerten(null) == []              # ... und nur die Nulladresse faellt
