# -*- coding: utf-8 -*-
"""Kuenstliche Sammellaeufe. Jeder Test ist ein Fall, der im echten Netz vorkommt
und bei dem ein Inventarwerkzeug ueblicherweise falsch liegt."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from daedalus.abgleich import einspielen, marken
from daedalus.modell import Beobachtung, Beziehung, Ereignis, Lauf, Quelle

# Intern wird ausschliesslich in UTC mit Zeitzone gerechnet. Die Flotte laeuft
# auf Europe/Berlin; eine nackte Zeitangabe waere zweimal im Jahr zweideutig,
# und PostgreSQL gibt `timestamptz` ohnehin zeitzonenbehaftet zurueck.
T0 = datetime(2026, 9, 15, 20, 0, tzinfo=timezone.utc)
def t(n: int) -> datetime:
    return T0 + timedelta(minutes=5 * n)

FDB = Quelle(name="fdb", zustaendig_fuer=frozenset({Beziehung.ANSCHLUSS}),
             fehlt_schwelle=2)
ARP = Quelle(name="arp", zustaendig_fuer=frozenset({Beziehung.ADRESSE}),
             fehlt_schwelle=2)


def anschluss(geraet: str, port: str) -> Beobachtung:
    return Beobachtung(Beziehung.ANSCHLUSS, geraet, "anschluss", port)


def adresse(geraet: str, ip: str) -> Beobachtung:
    return Beobachtung(Beziehung.ADRESSE, geraet, "adresse", ip)


def lauf(bestand, quelle, n, beob, erfolgreich=True):
    return einspielen(bestand, Lauf(quelle.name, t(n), erfolgreich), quelle, beob)


# ---------------------------------------------------------------------------
# Regel 4: der erste Lauf ist Bestand, nicht lauter Neuzugaenge
# ---------------------------------------------------------------------------
def test_erster_lauf_meldet_nichts_als_neu(bestand):
    b = bestand
    aend = lauf(b, FDB, 0, [anschluss("laptop", "c3po:gi12"),
                            anschluss("drucker", "c3po:gi14")])
    assert aend == []
    assert len(b.offene(Beziehung.ANSCHLUSS)) == 2


def test_auch_eine_neu_angeschlossene_quelle_beginnt_mit_bestand(bestand):
    b = bestand
    lauf(b, FDB, 0, [anschluss("laptop", "c3po:gi12")])
    lauf(b, FDB, 1, [anschluss("laptop", "c3po:gi12")])
    # ARP kommt spaeter dazu und kennt das Geraet zum ersten Mal
    aend = lauf(b, ARP, 2, [adresse("laptop", "172.16.10.87")])
    assert aend == []


# ---------------------------------------------------------------------------
# Der Normalfall: nichts aendert sich, also waechst nichts
# ---------------------------------------------------------------------------
def test_unveraendert_erzeugt_keine_zeile(bestand):
    b = bestand
    lauf(b, FDB, 0, [anschluss("laptop", "c3po:gi12")])
    vorher = b.anzahl_intervalle()
    for n in range(1, 12):
        assert lauf(b, FDB, n, [anschluss("laptop", "c3po:gi12")]) == []
    assert b.anzahl_intervalle() == vorher, "Intervalle duerfen nicht mit der Zeit wachsen"
    assert b.aenderungen_seit(T0) == []


# ---------------------------------------------------------------------------
# Umzug: ein Geraet wechselt den Port
# ---------------------------------------------------------------------------
def test_umzug_markiert_geraet_und_beide_ports(bestand):
    b = bestand
    lauf(b, FDB, 0, [anschluss("labor-pi", "c3po:gi15")])
    aend = lauf(b, FDB, 1, [anschluss("labor-pi", "c3po:gi16")])

    assert [a.art for a in aend] == [Ereignis.UMGEZOGEN]
    a = aend[0]
    assert a.vorher == "c3po:gi15" and a.nachher == "c3po:gi16"
    assert set(a.betrifft) == {"labor-pi", "c3po:gi15", "c3po:gi16"}

    alt = [i for i in b.verlauf("labor-pi") if i.wert == "c3po:gi15"][0]
    assert alt.bis == t(1), "das alte Intervall muss geschlossen sein"
    assert b.offen_fuer(Beziehung.ANSCHLUSS, "labor-pi", "anschluss").wert == "c3po:gi16"


# ---------------------------------------------------------------------------
# Regel 1: eine gescheiterte Quelle darf NICHTS verschwinden lassen
# ---------------------------------------------------------------------------
def test_gescheiterte_quelle_laesst_nichts_verschwinden(bestand):
    b = bestand
    lauf(b, FDB, 0, [anschluss("laptop", "c3po:gi12"),
                     anschluss("drucker", "c3po:gi14")])
    for n in (1, 2, 3):
        assert lauf(b, FDB, n, [], erfolgreich=False) == []
    assert len(b.offene(Beziehung.ANSCHLUSS)) == 2
    assert b.aenderungen_seit(T0) == []


# ---------------------------------------------------------------------------
# Schwelle: einmal nicht gesehen ist noch kein Verschwinden
# ---------------------------------------------------------------------------
def test_einmaliges_fehlen_reicht_nicht(bestand):
    b = bestand
    lauf(b, FDB, 0, [anschluss("laptop", "c3po:gi12")])
    assert lauf(b, FDB, 1, []) == []                      # 1. Fehlen
    assert b.offen_fuer(Beziehung.ANSCHLUSS, "laptop", "anschluss") is not None


def test_zweimaliges_fehlen_laesst_verschwinden(bestand):
    b = bestand
    lauf(b, FDB, 0, [anschluss("laptop", "c3po:gi12")])
    lauf(b, FDB, 1, [])
    aend = lauf(b, FDB, 2, [])
    # EIN Ereignis: "verschwunden" enthaelt das Ende des Anschlusses. Frueher
    # kam zusaetzlich "umgezogen c3po:gi12 -> —" — ein Umzug, den es nie gab.
    assert [a.art for a in aend] == [Ereignis.VERSCHWUNDEN]
    assert "c3po:gi12" in aend[0].betrifft     # der Port wird trotzdem markiert
    assert aend[0].schluessel == "anschluss"
    assert b.offen_fuer(Beziehung.ANSCHLUSS, "laptop", "anschluss") is None


def test_getrennt_statt_umgezogen_wenn_anderes_bleibt(bestand):
    b = bestand
    lauf(b, FDB, 0, [anschluss("laptop", "c3po:gi12")])
    lauf(b, ARP, 0, [adresse("laptop", "172.16.10.87")])    # die Adresse bleibt
    lauf(b, FDB, 1, [])
    aend = lauf(b, FDB, 2, [])
    assert [(a.art, a.vorher) for a in aend] == [(Ereignis.GETRENNT, "c3po:gi12")]


def test_merkmale_melden_nur_echte_wertwechsel(bestand):
    b = bestand
    q = Quelle("wlan", frozenset({Beziehung.MERKMAL, Beziehung.ANSCHLUSS}), 1)
    m = lambda s, w: Beobachtung(Beziehung.MERKMAL, "handy", s, w)
    a = lambda w: Beobachtung(Beziehung.ANSCHLUSS, "handy", "anschluss", w)
    lauf(b, q, 0, [a("ap:Luke")])
    # Neue Angaben an einem bekannten Objekt: still
    assert lauf(b, q, 1, [a("ap:Luke"), m("name", "Pixel"), m("vlan", "10")]) == []
    # Ein Wert aendert sich: gemeldet, mit Angabe, welcher
    aend = lauf(b, q, 2, [a("ap:Luke"), m("name", "Pixel"), m("vlan", "20")])
    assert [(x.art, x.schluessel, x.vorher, x.nachher) for x in aend] == [
        (Ereignis.MERKMAL_GEAENDERT, "vlan", "10", "20")]
    # Eine Angabe faellt weg: still
    assert lauf(b, q, 3, [a("ap:Luke"), m("name", "Pixel")]) == []


def test_wieder_gesehen_setzt_den_zaehler_zurueck(bestand):
    b = bestand
    lauf(b, FDB, 0, [anschluss("laptop", "c3po:gi12")])
    lauf(b, FDB, 1, [])                                    # 1. Fehlen
    lauf(b, FDB, 2, [anschluss("laptop", "c3po:gi12")])     # wieder da
    assert lauf(b, FDB, 3, []) == []                        # zaehlt wieder von vorn
    assert b.offen_fuer(Beziehung.ANSCHLUSS, "laptop", "anschluss") is not None


# ---------------------------------------------------------------------------
# Regel 2: eine Quelle urteilt nur ueber das, wofuer sie zustaendig ist
# ---------------------------------------------------------------------------
def test_fremde_quelle_beendet_keinen_anschluss(bestand):
    b = bestand
    lauf(b, FDB, 0, [anschluss("laptop", "c3po:gi12")])
    lauf(b, ARP, 1, [adresse("laptop", "172.16.10.87")])
    # ARP laeuft weiter und sieht nie einen Anschluss — das darf nichts beenden
    for n in (2, 3, 4, 5):
        lauf(b, ARP, n, [adresse("laptop", "172.16.10.87")])
    assert b.offen_fuer(Beziehung.ANSCHLUSS, "laptop", "anschluss") is not None


def test_quelle_faellt_nicht_auf_beobachtungen_ausserhalb_ihrer_zustaendigkeit_herein(bestand):
    """Selbst wenn ein Sammler mehr liefert, als er beurteilen kann."""
    b = bestand
    aend = lauf(b, ARP, 0, [adresse("laptop", "172.16.10.87"),
                            anschluss("laptop", "irgendwo")])
    assert aend == []
    assert b.offene(Beziehung.ANSCHLUSS) == []


# ---------------------------------------------------------------------------
# Regel 3: derselbe Lauf zweimal eingespielt aendert nichts
# ---------------------------------------------------------------------------
def test_wiederholter_import_ist_folgenlos(bestand):
    b = bestand
    lauf(b, FDB, 0, [anschluss("laptop", "c3po:gi12")])
    einspielen(b, Lauf("fdb", t(1)), FDB, [anschluss("laptop", "c3po:gi16")])
    stand = (b.anzahl_intervalle(), len(b.aenderungen_seit(T0)))
    # derselbe Lauf noch einmal
    nochmal = einspielen(b, Lauf("fdb", t(1)), FDB, [anschluss("laptop", "c3po:gi16")])
    assert nochmal == []
    assert (b.anzahl_intervalle(), len(b.aenderungen_seit(T0))) == stand


# ---------------------------------------------------------------------------
# Adressen sind Zuordnungen mit Zeitraum, keine Zeichenfolge am Geraet
# ---------------------------------------------------------------------------
def test_adresswechsel_schliesst_das_alte_intervall(bestand):
    b = bestand
    lauf(b, ARP, 0, [adresse("handy", "172.16.10.91")])
    aend = lauf(b, ARP, 1, [adresse("handy", "172.16.12.41")])
    assert [a.art for a in aend] == [Ereignis.ADRESSE_DAZU]
    alt = [i for i in b.verlauf("handy") if i.wert == "172.16.10.91"][0]
    assert alt.bis == t(1)
    # und die Historie ist damit lueckenlos beantwortbar
    assert alt.ab == t(0)


# ---------------------------------------------------------------------------
# Neues Geraet ausserhalb des Erstlaufs
# ---------------------------------------------------------------------------
def test_neues_geraet_wird_als_erstmals_gesehen_gemeldet(bestand):
    b = bestand
    lauf(b, FDB, 0, [anschluss("laptop", "c3po:gi12")])
    aend = lauf(b, FDB, 1, [anschluss("laptop", "c3po:gi12"),
                            anschluss("neuling", "c3po:gi31")])
    arten = [a.art for a in aend]
    assert Ereignis.ERSTMALS_GESEHEN in arten
    erst = [a for a in aend if a.art is Ereignis.ERSTMALS_GESEHEN][0]
    assert erst.objekt == "neuling"
    assert "c3po:gi31" in erst.betrifft, "der Port muss mitmarkiert werden"


# ---------------------------------------------------------------------------
# Die Marke am Knoten ist eine Ableitung, keine gespeicherte Farbe
# ---------------------------------------------------------------------------
def test_marken_zeigen_die_gewichtigste_aenderung(bestand):
    b = bestand
    lauf(b, FDB, 0, [anschluss("a", "sw:1"), anschluss("b", "sw:2")])
    lauf(b, FDB, 1, [anschluss("a", "sw:3"), anschluss("b", "sw:2"),
                     anschluss("c", "sw:4")])          # a zieht um, c ist neu
    lauf(b, FDB, 2, [anschluss("a", "sw:3"), anschluss("c", "sw:4")])
    lauf(b, FDB, 3, [anschluss("a", "sw:3"), anschluss("c", "sw:4")])  # b weg

    m = marken(b, seit=t(1))
    assert m["a"] is Ereignis.UMGEZOGEN
    assert m["b"] is Ereignis.VERSCHWUNDEN
    assert m["c"] is Ereignis.ERSTMALS_GESEHEN
    # die beteiligten Ports tragen die Marke mit
    assert m["sw:1"] is Ereignis.UMGEZOGEN and m["sw:3"] is Ereignis.UMGEZOGEN


def test_marken_beruecksichtigen_den_zeitpunkt(bestand):
    b = bestand
    lauf(b, FDB, 0, [anschluss("a", "sw:1")])
    lauf(b, FDB, 1, [anschluss("a", "sw:2")])
    assert "a" in marken(b, seit=t(1))
    assert marken(b, seit=t(2)) == {}, "aeltere Aenderungen duerfen nicht mehr markieren"


# ---------------------------------------------------------------------------
# Fluechtige Objekte: Handys wuerfeln ihre MAC. Sie werden verfolgt, aber ihr
# Auftauchen und Verschwinden ist keine Nachricht (Entscheidung 3.A).
# ---------------------------------------------------------------------------
def fluechtig(g, p):
    return Beobachtung(Beziehung.ANSCHLUSS, g, "anschluss", p, fluechtig=True)


def test_fluechtiges_geraet_wird_nicht_als_neu_gemeldet(bestand):
    b = bestand
    lauf(b, FDB, 0, [anschluss("laptop", "c3po:gi12")])
    aend = lauf(b, FDB, 1, [anschluss("laptop", "c3po:gi12"),
                            fluechtig("96:0d:91:1f:48:d9", "ap:AP FLUR")])
    assert [a.art for a in aend] == [], "eine gewuerfelte MAC ist kein neues Geraet"
    # verfolgt wird es trotzdem
    assert b.offen_fuer(Beziehung.ANSCHLUSS, "96:0d:91:1f:48:d9", "anschluss") is not None


def test_fluechtiges_geraet_wird_nicht_als_verschwunden_gemeldet(bestand):
    b = bestand
    lauf(b, FDB, 0, [fluechtig("96:0d:91:1f:48:d9", "ap:AP FLUR")])
    lauf(b, FDB, 1, [])
    aend = lauf(b, FDB, 2, [])
    assert Ereignis.VERSCHWUNDEN not in [a.art for a in aend]


def test_wlan_roaming_ist_historie_aber_keine_meldung(bestand):
    """Frueher galt ein AP-Wechsel als Umzug und wurde gemeldet. Ueber Nacht
    zum 17.09.2026 waren das rund hundert Zeilen — Roaming ist Alltag. Die
    Historie behaelt den Wechsel, die Liste nicht."""
    b = bestand
    lauf(b, FDB, 0, [fluechtig("96:0d:91:1f:48:d9", "ap:AP FLUR")])
    aend = lauf(b, FDB, 1, [fluechtig("96:0d:91:1f:48:d9", "ap:AP GARTEN")])
    assert aend == []
    offen = b.offen_fuer(Beziehung.ANSCHLUSS, "96:0d:91:1f:48:d9", "anschluss")
    assert offen.wert == "ap:AP GARTEN"


def test_kabelumzug_bleibt_sichtbar_auch_bei_zufalls_mac(bestand):
    b = bestand
    lauf(b, FDB, 0, [fluechtig("96:0d:91:1f:48:d9", "c3po:gi12")])
    aend = lauf(b, FDB, 1, [fluechtig("96:0d:91:1f:48:d9", "c3po:gi13")])
    assert [a.art for a in aend] == [Ereignis.UMGEZOGEN]


def test_ein_festes_geraet_bleibt_meldepflichtig(bestand):
    b = bestand
    lauf(b, FDB, 0, [anschluss("laptop", "c3po:gi12")])
    aend = lauf(b, FDB, 1, [anschluss("laptop", "c3po:gi12"),
                            anschluss("drucker", "c3po:gi14")])
    assert Ereignis.ERSTMALS_GESEHEN in [a.art for a in aend]


def test_umstieg_auf_adressschluessel_und_zweite_quelle_sind_still(bestand):
    """Dieselbe Adresse unter anderem Schluessel ist keine Aenderung — weder beim
    Umstieg von "adresse" auf "ip:<adresse>" noch, wenn eine zweite Quelle sie sieht."""
    b = bestand
    alt = lambda: Beobachtung(Beziehung.ADRESSE, "laptop", "adresse", "172.16.10.87")
    neu = lambda: Beobachtung(Beziehung.ADRESSE, "laptop", "ip:172.16.10.87", "172.16.10.87")
    lauf(b, ARP, 0, [alt()])
    assert lauf(b, ARP, 1, [neu()]) == []          # neuer Schluessel: still
    assert lauf(b, ARP, 2, [neu()]) == []          # alter faellt weg: still
    assert lauf(b, ARP, 3, [neu()]) == []
    assert b.offen_fuer(Beziehung.ADRESSE, "laptop", "ip:172.16.10.87") is not None


def test_unveraenderte_meldung_schreibt_die_letzte_sichtung_fort(bestand):
    """19.09.2026: 160 Geraete standen auf „zuletzt Do 06:29", obwohl sie gerade
    gemeldet hatten — `letzte_sicht` wurde nur beim Oeffnen eines Intervalls gesetzt."""
    lauf(bestand, ARP, 0, [adresse("c6:3c:d0:13:86:00", "172.16.10.5")])
    lauf(bestand, ARP, 1, [adresse("c6:3c:d0:13:86:00", "172.16.10.5")])
    lauf(bestand, ARP, 2, [adresse("c6:3c:d0:13:86:00", "172.16.10.5")])
    assert bestand.sichtungen()["c6:3c:d0:13:86:00"][1] == t(2)
    # Ein gescheiterter Lauf ist keine Sichtung.
    lauf(bestand, ARP, 3, [adresse("c6:3c:d0:13:86:00", "172.16.10.5")], erfolgreich=False)
    assert bestand.sichtungen()["c6:3c:d0:13:86:00"][1] == t(2)
