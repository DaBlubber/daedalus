# -*- coding: utf-8 -*-
"""Datenmodell mit Zeitachse (Plan Block 24).

**Alle Zeitangaben sind zeitzonenbehaftet und intern UTC.** Die Flotte laeuft auf
Europe/Berlin; eine nackte Zeitangabe waere in der Nacht der Zeitumstellung
zweideutig, und genau dann Historie zu verlieren waere besonders aergerlich.

Der Grundsatz: **Intervalle statt Abzuege.** Bleibt ein Zustand gleich, bleibt
sein Intervall offen. Erst bei einer Aenderung wird das alte geschlossen und ein
neues begonnen. Der Bestand waechst damit mit den *Aenderungen*, nicht mit der
*Zeit* — und jedes Aenderungsereignis faellt dabei von selbst an, statt hinterher
aus zwei Vollbildern errechnet zu werden.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class Art(str, Enum):
    """Was fuer ein Ding ein Objekt ist."""
    GERAET = "geraet"
    SCHNITTSTELLE = "schnittstelle"
    NETZ = "netz"
    ACCESS_POINT = "access_point"


class Beziehung(str, Enum):
    """Welche Art von Zuordnung ein Intervall beschreibt."""
    ADRESSE = "adresse"        # Geraet <-> IP
    ANSCHLUSS = "anschluss"    # Geraet <-> Switch-Port oder Access Point
    VERBINDUNG = "verbindung"  # Schnittstelle <-> Schnittstelle
    MERKMAL = "merkmal"        # wechselndes Restattribut


class Ereignis(str, Enum):
    """Fachliche Aenderungen. Die Oberflaeche markiert danach."""
    ERSTMALS_GESEHEN = "erstmals_gesehen"
    VERSCHWUNDEN = "verschwunden"
    WIEDER_DA = "wieder_da"
    ADRESSE_DAZU = "adresse_dazu"
    ADRESSE_WEG = "adresse_weg"
    UMGEZOGEN = "umgezogen"
    # Ein Anschluss endet, ohne dass ein neuer beginnt: vom Port abgezogen oder
    # vom Access Point abgemeldet. Frueher als "umgezogen x -> —" gemeldet, was
    # nach einem Umzug klang, den es nie gab.
    GETRENNT = "getrennt"
    MERKMAL_GEAENDERT = "merkmal_geaendert"


# Angaben, deren Wechsel verfolgt, aber nie gemeldet wird. `link_wechsel` ist
# der rohe Zeitstempel des letzten Linkwechsels — er zaehlt das Flattern eines
# Ports, jeder einzelne Wechsel waere aber eine Zeile. `link_zugang` ist der
# Link eines Ports ohne LLDP-Nachbarn: ein PC, der abends ausgeht, ist keine
# Nachricht (siehe sammler_portzustand.py).
STILLE_SCHLUESSEL = frozenset({"link_zugang", "link_wechsel"})


def adress_schluessel(ip: str) -> str:
    """Der Schluessel einer Adresszuordnung: EIN Intervall je Adresse.

    Bis zum 17.09.2026 hiess er fuer jede Adresse "adresse". Die ARP-Tabelle der
    Sophos kennt aber fuer manche MAC mehrere Adressen (40:c1:ee:2a:c7:98 mit
    .54, .179, .212, .218, .222) — mit einem gemeinsamen Schluessel gewann in
    jedem Lauf eine andere, und die Liste meldete jede Nacht dutzendfach "neue
    Adresse". Dazu stritten ARP und WLAN um denselben Schluessel.
    """
    return f"ip:{ip}"


def meldenswert(art: "Ereignis", vorher: str | None, nachher: str | None,
                fluechtig: bool) -> bool:
    """Ist eine Aenderung eine Nachricht — oder nur Bewegung?

    Am 17.09.2026 standen ueber Nacht 397 Zeilen in der Liste, fast alle davon
    Alltag, keine davon ein Befund:

    - WLAN-Clients wandern zwischen Access Points (LUKE -> LEIA -> LUKE). Das ist
      Roaming, gewollt und staendig.
    - Handys melden sich im WLAN an und ab ("wieder da · LUKE", "getrennt").
    - Handys mit gewuerfelter MAC holen und verlieren Adressen.

    Die Historie behaelt alles davon — nur die Liste bekommt es nicht mehr.
    Gemeldet bleiben: Kabelumzuege, ein Geraet, das erstmals auftaucht oder
    verschwindet, Adresswechsel echter Geraete und geaenderte Werte.
    """
    ap_vorher = bool(vorher and vorher.startswith("ap:"))
    ap_nachher = bool(nachher and nachher.startswith("ap:"))
    if art in (Ereignis.UMGEZOGEN, Ereignis.WIEDER_DA, Ereignis.GETRENNT) and \
            (ap_vorher or ap_nachher) and not (vorher and nachher and ap_vorher != ap_nachher):
        return False                          # WLAN: Roaming, Anmelden, Abmelden
    if fluechtig and art is not Ereignis.UMGEZOGEN:
        return False
    return True


@dataclass(frozen=True)
class Quelle:
    """Eine Sammelquelle.

    `zustaendig_fuer` sagt, welche Beziehungen diese Quelle *beurteilen* darf.
    Nur eine zustaendige Quelle darf ein Intervall beenden — das ist die
    wichtigste Regel im ganzen Modell (siehe `abgleich.py`).

    `fehlt_schwelle` ist die Zahl **erfolgreicher** Laeufe, in denen etwas nicht
    mehr auftauchen muss, bevor es als verschwunden gilt. Eine MAC-Tabelle altert
    nach Minuten aus; ein Geraet ist deshalb nicht weg, nur weil es einmal fehlt.
    """
    name: str
    zustaendig_fuer: frozenset[Beziehung]
    fehlt_schwelle: int = 2


@dataclass(frozen=True)
class Lauf:
    """Ein Sammellauf. `erfolgreich=False` heisst: diese Quelle sagt nichts aus."""
    quelle: str
    zeitpunkt: datetime
    erfolgreich: bool = True


@dataclass(frozen=True)
class Beobachtung:
    """Was ein Lauf gesehen hat — eine einzelne Aussage.

    `schluessel` ist der natuerliche Schluessel der Zuordnung, also das, was
    gleich bleibt, solange sich nichts aendert. `wert` ist, was daran haengt.
    """
    beziehung: Beziehung
    objekt: str          # stabile Objekt-Kennung (UUID im Betrieb)
    schluessel: str      # z.B. "adresse" oder "anschluss"
    wert: str            # z.B. "172.16.10.87" oder "c3po:gi12"
    # Fluechtig heisst: das Objekt ist echt und wird verfolgt, sein Auftauchen
    # und Verschwinden ist aber keine Nachricht. Handys wuerfeln ihre MAC je
    # Netz — jedes waere sonst taeglich ein „neues Geraet" und der
    # Veraenderungsbericht bestuende bald nur noch aus ihnen (Entscheidung 3.A).
    # Umzuege und Adresswechsel bleiben sichtbar, nur Erstsichtung und
    # Verschwinden werden nicht gemeldet.
    fluechtig: bool = False


@dataclass
class Intervall:
    """Ein Zustand mit Gueltigkeitszeitraum. `bis is None` heisst: gilt noch."""
    beziehung: Beziehung
    objekt: str
    schluessel: str
    wert: str
    ab: datetime
    bis: datetime | None = None
    quelle: str = ""
    fehlt_seit: int = 0     # erfolglose Sichtungen in Folge
    fluechtig: bool = False # siehe Beobachtung.fluechtig

    @property
    def offen(self) -> bool:
        return self.bis is None


@dataclass
class Aenderung:
    """Ein fachlicher Unterschied. Genau das, was die Oberflaeche markiert."""
    art: Ereignis
    objekt: str
    zeitpunkt: datetime
    vorher: str | None = None
    nachher: str | None = None
    quelle: str = ""
    # Eine Aenderung betrifft nie nur ein Objekt: wandert ein Geraet von Port 15
    # auf Port 16, sind Geraet UND beide Ports betroffen.
    betrifft: tuple[str, ...] = ()
    # Welche Angabe sich geaendert hat ("name", "vlan", "dhcp_lease"). Ohne sie
    # stand in der Liste nur "— -> 20", und niemand wusste, wovon.
    schluessel: str = ""


