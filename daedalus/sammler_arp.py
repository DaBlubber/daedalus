# -*- coding: utf-8 -*-
"""Sammler: die ARP-Tabelle der Sophos.

Die wertvollste einzelne Quelle im ganzen Werkzeug. Sie beantwortet
**IP ↔ MAC** fuer alles, was in den letzten Minuten gesprochen hat — und sie ist
der Anker, an dem die MAC-Tabellen der Switches spaeter andocken: die sagen
*MAC ↔ Port*, erst zusammen ergibt das „welche Adresse haengt an welchem Port".

Gelesen wird `ipNetToMediaPhysAddress` (IP-MIB, 1.3.6.1.2.1.4.22.1.2). Der
Index ist `<ifIndex>.<ip>`, der Wert die MAC als Hex-Bytes.

**Keine billige Vorabfrage.** Fuer eine ARP-Tabelle gibt es kein Gegenstueck zu
`ifLastChange`: es gibt keinen Zaehler, der sich aendert, wenn ein Eintrag dazu
kommt. Der Walk ist die Abfrage. Das ist vertretbar — es ist EIN Geraet, 218
Eintraege, ein `snmpbulkwalk` mit grosser Wiederholungszahl braucht dafuer eine
Handvoll Pakete. Die Sparsamkeit holt sich das Werkzeug an anderer Stelle:
seltener fragen (alle 5 Minuten, Plan Block 25.1) und nur schreiben, was sich
geaendert hat.
"""
from __future__ import annotations

import re
import subprocess

from .modell import Beobachtung, Beziehung, Quelle, adress_schluessel

# 1.3.6.1.2.1.4.22.1.2 — ipNetToMediaPhysAddress
OID_ARP = "1.3.6.1.2.1.4.22.1.2"

# Dieselbe OID sieht je nach net-snmp-Installation anders aus. Am 16.09.2026
# beide Formen im selben Netz gemessen:
#
#   auf host-9d56      .1.3.6…11.172.16.0.2 = "80 F6 0F 9A BA DD "   Hex mit Leerzeichen
#   im Container   .1.3.6…11.172.16.0.2 = 80:f6:f:9a:ba:dd       Doppelpunkte, ungepolstert
#
# Ein Sammler darf sich nicht darauf verlassen, welches Werkzeug zufaellig
# installiert ist. Das Muster nimmt deshalb beide Formen.
_ZEILE = re.compile(
    r"^\." + OID_ARP.replace(".", r"\.") +
    r"\.(?P<ifindex>\d+)\.(?P<ip>\d+\.\d+\.\d+\.\d+)\s*=\s*\"?(?P<mac>[0-9A-Fa-f: ]+?)\"?\s*$")


def mac_lesbar(roh: str) -> str:
    """Beide Darstellungen werden zu `80:f6:0f:9a:ba:dd`.

    Eine Schreibweise, ueberall dieselbe — kleingeschrieben und auf zwei
    Stellen gepolstert. Sonst steht dasselbe Geraet dreimal im Bestand, weil
    drei Quellen drei Schreibweisen liefern.
    """
    roh = roh.strip()
    teile = roh.split(":") if ":" in roh else roh.split()
    teile = [t for t in teile if t]
    return ":".join(t.lower().zfill(2) for t in teile)


def zufalls_mac(mac: str) -> bool:
    """Wuerfelt das Geraet seine MAC? (lokal verwaltetes Bit im ersten Byte)

    Handys tun das je Netz. Solche Adressen gehoeren gekennzeichnet und aus
    „neues Geraet" herausgehalten — sonst besteht der Veraenderungsbericht bald
    nur noch aus ihnen.
    """
    try:
        return bool(int(mac.split(":")[0], 16) & 0b10)
    except (ValueError, IndexError):
        return False


def auswerten(ausgabe: str) -> list[Beobachtung]:
    """Aus der Ausgabe von `snmpbulkwalk` werden Beobachtungen.

    Unverstaendliche Zeilen werden uebergangen, nicht geworfen: eine einzige
    kaputte Zeile darf nicht die ganze Tabelle kosten.
    """
    aus: list[Beobachtung] = []
    for zeile in ausgabe.splitlines():
        t = _ZEILE.match(zeile.strip())
        if not t:
            continue
        mac = mac_lesbar(t.group("mac"))
        if len(mac) != 17:                       # keine vollstaendige MAC
            continue
        # Der Objektschluessel ist die MAC, nicht die IP: IPs wandern, MACs
        # meist nicht. Die MAC bleibt dabei ein *Identitaetsbeleg* — welches
        # Geraet dahintersteckt, entscheidet spaeter die Identitaetsaufloesung.
        # `0.0.0.0` ist keine Adresse, sondern ein Geraet mitten im DHCP. Im
        # ersten Dienstlauf am 16.09.2026 als "172.16.11.232 -> 0.0.0.0" im
        # Veraenderungsbericht gelandet — dieselbe Falle wie beim WLAN-Sammler.
        if t.group("ip") == "0.0.0.0":
            continue
        aus.append(Beobachtung(Beziehung.ADRESSE, mac, adress_schluessel(t.group("ip")),
                               t.group("ip"),
                               fluechtig=zufalls_mac(mac)))
    return aus


class SophosArp:
    """Liest die ARP-Tabelle der Sophos per SNMP."""

    def __init__(self, ziel: str, gemeinschaft: str, name: str = "sophos-arp",
                 zeitlimit: int = 8, aufrufer=None) -> None:
        self.ziel = ziel
        self.gemeinschaft = gemeinschaft
        self.zeitlimit = zeitlimit
        self._aufrufen = aufrufer or self._snmpbulkwalk
        self.quelle = Quelle(
            name=name,
            # Eng gefasst: diese Quelle sieht Adressen, sonst nichts. Ihr
            # Schweigen darf keinen Switch-Anschluss beenden.
            zustaendig_fuer=frozenset({Beziehung.ADRESSE}),
            # Ein ARP-Eintrag altert in Minuten aus. Zweimal nicht gesehen ist
            # ein belastbares Signal, einmal nicht.
            # ARP-Eintraege altern wie MAC-Tabellen nach Minuten aus; ein Geraet,
            # das eine Stunde lang kein Paket schickt, ist erst dann weg.
            fehlt_schwelle=12,
        )

    def _snmpbulkwalk(self) -> str:
        """`-Cr40` holt 40 Werte je Paket statt eines — das spart bei 218
        Eintraegen rund fuenfzig Paketumlaeufe gegenueber `snmpwalk`."""
        ergebnis = subprocess.run(
            ["snmpbulkwalk", "-v2c", "-c", self.gemeinschaft, "-Cr40", "-OQn",
             "-t", str(self.zeitlimit), "-r", "1", self.ziel, OID_ARP],
            capture_output=True, text=True, timeout=self.zeitlimit * 4)
        if ergebnis.returncode != 0 or not ergebnis.stdout.strip():
            raise RuntimeError(
                f"snmpbulkwalk gegen {self.ziel} fehlgeschlagen: "
                f"{(ergebnis.stderr or 'keine Ausgabe').strip()[:200]}")
        return ergebnis.stdout

    def vorab_unveraendert(self) -> bool:
        """Es gibt keine. Siehe Kopf dieser Datei."""
        return False

    def sammeln(self) -> list[Beobachtung]:
        beobachtungen = auswerten(self._aufrufen())
        if not beobachtungen:
            # Eine leere ARP-Tabelle gibt es an einer aktiven Firewall nicht.
            # Das ist ein Fehler, keine Beobachtung — und als Fehler laesst er
            # nach Regel 1 nichts verschwinden.
            raise RuntimeError(f"{self.ziel}: ARP-Tabelle leer, das kann nicht sein")
        return beobachtungen
