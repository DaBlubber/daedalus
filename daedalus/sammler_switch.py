# -*- coding: utf-8 -*-
"""Sammler: MAC-Tabelle und LLDP-Nachbarn eines Switches.

**Zwei Sammler, nicht einer** — weil sie sich in beidem unterscheiden, worauf es
ankommt:

| | MAC-Tabelle | LLDP-Nachbarn |
|---|---|---|
| Takt | 3–5 Minuten (altert nach ~5 aus) | 30 Minuten |
| billige Vorabfrage | **keine** | `ifLastChange` |
| Umfang bei c3po | 182 Eintraege | 4 Eintraege |

Der Unterschied bei der Vorabfrage ist der Kern: **eine MAC wandert, ohne dass
sich ein Link aendert** — ein Laptop, der von WLAN auf Kabel wechselt, laesst
`ifLastChange` unberuehrt. Fuer die Nachbarschaft gilt das Gegenteil: sie kann
sich nur aendern, wenn jemand umsteckt, und dann aendert sich `ifLastChange`.
Wer die Vorabfrage auf die MAC-Tabelle anwendet, verliert Umzuege.

## Die wichtigste Ableitung

**Eine MAC auf einem Zugangsport haengt dort. Eine MAC auf einem Uplink haengt
weiter hinten.** Ein Uplink sieht alle MACs, die hinter ihm liegen — wer das
nicht trennt, behauptet, an c3po gi25 haengen 140 Geraete.

Woran ein Uplink erkannt wird: **er hat einen LLDP-Nachbarn.** Das ist die
verlaesslichste Auskunft, die es gibt, denn sie kommt vom Nachbargeraet selbst.
Deshalb braucht der MAC-Sammler die Nachbarliste — er bekommt sie als Menge
herein und muss sie nicht selbst holen.

**Dazu kommen Portkanaele** (`Po1`, `Po2`). Am echten c3po sitzen **52 von 60
MACs auf `Po1`** — der Uplink zu bb8 ist ein Buendel aus zwei Leitungen, und die
MAC-Tabelle nennt als Port den Kanal, nicht seine Mitglieder. LLDP dagegen
meldet die **Mitglieder** (gi25, gi26). Wer nur der LLDP-Liste folgt, haelt
`Po1` fuer einen Zugangsport und behauptet, dort haengen 52 Geraete.

*Bekannte Grenze:* ein Portkanal gilt hier immer als Uplink. Wer einen Server
mit zwei Leitungen buendelt, wird dadurch nicht gefunden. Das ist im Haus-Netz
kein Fall — kaeme er vor, muesste die Kanalzugehoerigkeit
(`dot3adAggPortListPorts`) dazugelesen werden.
"""
from __future__ import annotations

import subprocess

from .modell import Beobachtung, Beziehung, Quelle

OID = {
    "fdb":        "1.3.6.1.2.1.17.7.1.2.2.1.2",     # dot1qTpFdbPort
    "bridgeport": "1.3.6.1.2.1.17.1.4.1.2",         # dot1dBasePortIfIndex
    "ifname":     "1.3.6.1.2.1.31.1.1.1.1",         # ifName
    "lldpname":   "1.0.8802.1.1.2.1.4.1.1.9",       # lldpRemSysName
    "lldpport":   "1.0.8802.1.1.2.1.4.1.1.7",       # lldpRemPortId
    "lldplocal":  "1.0.8802.1.1.2.1.3.7.1.3",       # lldpLocPortId
    "lastchange": "1.3.6.1.2.1.2.2.1.9",            # ifLastChange
}

def zerlegen(ausgabe: str, oid: str) -> dict[str, str]:
    """`{index: wert}` fuer alle Zeilen einer OID. Unverstaendliches faellt weg.

    Am bekannten Praefix abgeschnitten, nicht per Muster geraten: eine OID und
    ihr Index bestehen beide nur aus Ziffern und Punkten, ein Muster kann die
    Grenze zwischen ihnen nicht finden.
    """
    praefix = "." + oid + "."
    aus: dict[str, str] = {}
    for zeile in ausgabe.splitlines():
        z = zeile.strip()
        if not z.startswith(praefix) or "=" not in z:
            continue
        links, rechts = z.split("=", 1)
        aus[links.strip()[len(praefix):]] = rechts.strip().strip('"').strip()
    return aus


def mac_aus_index(index: str) -> str:
    """Der FDB-Index ist `<vlan>.<sechs dezimale Bytes>`.

    `1.60.222.152.72.101.20` wird zu `3c:de:98:48:65:14` — dieselbe
    Schreibweise wie beim ARP-Sammler, sonst steht ein Geraet zweimal im Bestand.
    """
    teile = index.split(".")
    if len(teile) < 7:
        return ""
    return ":".join(f"{int(x):02x}" for x in teile[-6:])


def ist_kanal(portname: str) -> bool:
    """Ist das ein Portkanal (`Po1`) statt eines einzelnen Ports?

    Die MAC-Tabelle nennt bei einem Buendel den Kanal, LLDP nennt die
    Mitglieder. Ohne diese Regel gilt der Kanal als Zugangsport — und am
    echten c3po haengen dann 52 Geraete an einem Stecker.
    """
    p = portname.strip().lower()
    return p.startswith("po") and p[2:].isdigit()


def vlan_aus_index(index: str) -> str:
    teile = index.split(".")
    return teile[0] if len(teile) >= 7 else ""


class _Switch:
    """Gemeinsames: reden mit einem Switch."""

    def __init__(self, switch: str, ziel: str, gemeinschaft: str,
                 zeitlimit: int = 8, aufrufer=None) -> None:
        self.switch = switch
        self.ziel = ziel
        self.gemeinschaft = gemeinschaft
        self.zeitlimit = zeitlimit
        self._aufrufen = aufrufer or self._walk

    def _walk(self, oid: str) -> str:
        ergebnis = subprocess.run(
            ["snmpbulkwalk", "-v2c", "-c", self.gemeinschaft, "-Cr40", "-OQn",
             "-t", str(self.zeitlimit), "-r", "1", self.ziel, oid],
            capture_output=True, text=True, timeout=self.zeitlimit * 5)
        if ergebnis.returncode != 0:
            raise RuntimeError(
                f"{self.switch} ({self.ziel}) antwortet nicht: "
                f"{(ergebnis.stderr or 'keine Ausgabe').strip()[:160]}")
        return ergebnis.stdout

    def portnamen(self) -> dict[str, str]:
        """`{ifIndex: 'gi12'}`"""
        return zerlegen(self._aufrufen(OID["ifname"]), OID["ifname"])

    def nachbarports(self) -> dict[str, tuple[str, str]]:
        """`{lokaler Portname: (Nachbarname, Nachbarport)}` aus LLDP.

        Der LLDP-Index ist `<zeitmarke>.<lokale Portnummer>.<lfd>`. Die lokale
        Portnummer entspricht auf dieser Hardware dem `ifIndex` — und darueber
        wird aufgeloest, **nicht** ueber `lldpLocPortId`.

        Warum das wichtig ist: `lldpLocPortId` hat je Port einen anderen
        Untertyp. Am 16.09.2026 an r2d2 gemessen:

            Untertyp 5 (interfaceName)  ->  "gi25"        Switch an Switch
            Untertyp 3 (macAddress)     ->  "C8 00 84 …"  Access Point

        Wer `lldpLocPortId` blind als Portnamen nimmt, verliert **genau die
        Ports, an denen die Access Points haengen**. Die Folge war im ersten
        echten Lauf zu sehen: 131 WLAN-Clients wurden als „umgezogen" von
        `ap:Leia` auf `r2d2:gi5` gemeldet, weil dieser Port nicht als Uplink
        galt. Ein AP-Port ist ein Uplink wie jeder andere — dahinter liegen
        Geraete, sie haengen nicht dort.
        """
        namen = zerlegen(self._aufrufen(OID["lldpname"]), OID["lldpname"])
        ports = zerlegen(self._aufrufen(OID["lldpport"]), OID["lldpport"])
        lokal = zerlegen(self._aufrufen(OID["lldplocal"]), OID["lldplocal"])
        ifname = self.portnamen()

        aus: dict[str, tuple[str, str]] = {}
        for index, nachbar in namen.items():
            teile = index.split(".")
            if len(teile) < 2 or not nachbar:
                continue                      # ohne Namen kein Nachbar
            nr = teile[1]
            # ifName zuerst, lldpLocPortId nur als Rueckfallebene.
            portname = ifname.get(nr) or lokal.get(nr)
            if not portname or " " in portname:
                continue                      # eine MAC ist kein Portname
            aus[portname] = (nachbar, ports.get(index, ""))
        return aus


class SwitchNachbarn(_Switch):
    """LLDP: wer haengt an welchem Port dieses Switches.

    **Hier greift die billige Vorabfrage.** Die Nachbarschaft kann sich nur
    aendern, wenn jemand ein Kabel umsteckt — und dann aendert sich
    `ifLastChange` an mindestens einem Port. Ein Walk ueber ~50 Zeitstempel
    ist deutlich billiger als vier Walks fuer die Nachbarliste.
    """

    def __init__(self, *a, **k) -> None:
        super().__init__(*a, **k)
        self._letzter_stand: str | None = None
        # Zuletzt gesehene Uplinkliste. Der MAC-Sammler liest sie hier ab —
        # im Dienst laufen beide in verschiedenen Takten (30 Min gegen 5 Min),
        # und die MAC-Tabelle darf nicht bei jedem Lauf neu nach Nachbarn fragen.
        self.letzte_uplinks: set[str] | None = None
        self.quelle = Quelle(
            name=f"lldp-{self.switch}",
            zustaendig_fuer=frozenset({Beziehung.VERBINDUNG}),
            # Ein Nachbar, der einmal fehlt, ist meist ein neu gestarteter
            # Switch. Dreimal fehlen ist ein Kabel.
            fehlt_schwelle=3,
        )

    def _stand(self) -> str:
        roh = self._aufrufen(OID["lastchange"])
        werte = zerlegen(roh, OID["lastchange"])
        return "|".join(f"{k}={v}" for k, v in sorted(werte.items()))

    def vorab_unveraendert(self) -> bool:
        stand = self._stand()
        if not stand:
            return False                       # nichts gelesen -> lieber sammeln
        unveraendert = stand == self._letzter_stand
        self._letzter_stand = stand
        return unveraendert

    def uplinkports(self) -> set[str]:
        """Alle Ports, ueber die es weitergeht: die mit Nachbarn und die Kanaele."""
        return set(self.nachbarports())

    def sammeln(self) -> list[Beobachtung]:
        aus = []
        gefunden = self.nachbarports()
        self.letzte_uplinks = set(gefunden)
        for portname, (nachbar, nachbarport) in gefunden.items():
            ziel = f"{nachbar}:{nachbarport}" if nachbarport else nachbar
            aus.append(Beobachtung(Beziehung.VERBINDUNG,
                                   f"{self.switch}:{portname}", "verbindung", ziel))
        if not aus:
            raise RuntimeError(f"{self.switch}: keine LLDP-Nachbarn, das kann nicht sein")
        return aus


class SwitchMacs(_Switch):
    """Die MAC-Tabelle: welches Geraet haengt an welchem Zugangsport.

    **Keine billige Vorabfrage** — eine MAC wandert, ohne dass sich ein Link
    aendert. Wer hier `ifLastChange` abfragt, verliert genau die Umzuege, wegen
    derer das Werkzeug gebaut wird.
    """

    def __init__(self, *a, uplinks=None, nachbarn=None, **k) -> None:
        super().__init__(*a, **k)
        # Entweder fest gesetzt (Einmallauf, Tests) oder aus dem Nachbarsammler
        # gelesen (Dienst). Ohne Uplinkwissen gilt jeder Port als Zugangsport —
        # dann haengt an einem Uplink faelschlich das halbe Netz. Das Setzen ist
        # deshalb Pflicht und nicht Kuer.
        self._uplinks: set[str] = set(uplinks or ())
        self.nachbarn = nachbarn
        self.quelle = Quelle(
            name=f"fdb-{self.switch}",
            zustaendig_fuer=frozenset({Beziehung.ANSCHLUSS}),
            # Ein stilles Geraet faellt nach 300 s Aging aus der MAC-Tabelle und
            # taucht beim naechsten Paket wieder auf. Mit Schwelle 2 (10 Minuten)
            # meldeten jetkvm und die Nintendo Switch ueber Nacht je 30-mal
            # "getrennt / wieder da" (17.09.2026). Zwoelf Laeufe sind eine Stunde:
            # ein echt abgezogenes Kabel steht spaeter in der Liste, aber es steht
            # dort nicht zwischen hundert falschen.
            fehlt_schwelle=12,
        )

    @property
    def uplinks(self) -> set[str]:
        if self.nachbarn is not None and self.nachbarn.letzte_uplinks is not None:
            return self.nachbarn.letzte_uplinks
        return self._uplinks

    @uplinks.setter
    def uplinks(self, wert) -> None:
        self._uplinks = set(wert or ())

    def vorab_unveraendert(self) -> bool:
        return False

    def sammeln(self) -> list[Beobachtung]:
        if self.nachbarn is not None and self.nachbarn.letzte_uplinks is None:
            # Der Nachbarsammler war noch nicht dran. Lieber gar nichts melden
            # als jedes Geraet hinter dem Uplink an den Uplink zu haengen —
            # als Fehler laesst das nach Regel 1 nichts verschwinden.
            raise RuntimeError(
                f"{self.switch}: Uplinks noch unbekannt, LLDP lief noch nicht")
        namen = self.portnamen()
        brueckenport = zerlegen(self._aufrufen(OID["bridgeport"]), OID["bridgeport"])
        fdb = zerlegen(self._aufrufen(OID["fdb"]), OID["fdb"])
        if not fdb:
            raise RuntimeError(f"{self.switch}: MAC-Tabelle leer, das kann nicht sein")

        aus: dict[str, Beobachtung] = {}
        for index, bport in fdb.items():
            mac = mac_aus_index(index)
            if not mac:
                continue
            ifindex = brueckenport.get(bport, bport)
            portname = namen.get(ifindex)
            if not portname:
                continue                       # Buendel oder unbekannter Port
            if portname in self.uplinks or ist_kanal(portname):
                # Ueber einen Uplink sieht man alles, was dahinter liegt. Das ist
                # kein Anschluss, sondern Durchgangsverkehr.
                continue
            aus[mac] = Beobachtung(Beziehung.ANSCHLUSS, mac, "anschluss",
                                   f"{self.switch}:{portname}")
        return list(aus.values())
