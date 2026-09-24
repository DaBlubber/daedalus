# -*- coding: utf-8 -*-
"""Sammler: Zustand und Gesundheit der Switchports.

Die Topologie sagt, WAS wo steckt. Dieser Sammler sagt, WIE es dort geht: Link,
Geschwindigkeit, Duplex, VLANs, Spanning Tree, PoE — und die Fehlerzaehler.
Ein Port, der alle zwei Minuten flattert, erklaert mehr als jede Karte.

Am 16.09.2026 an r2d2 gemessen, was die Cisco-SB-Switches liefern:

| Angabe | OID | Beispiel |
|---|---|---|
| Link | ifOperStatus | `up` |
| Geschwindigkeit | ifHighSpeed | `1000` |
| Duplex | dot3StatsDuplexStatus | `3` (voll) |
| letzter Linkwechsel | ifLastChange | `18:11:22:45.72` |
| PVID | dot1qPvid | `1` |
| VLAN-Mitgliedschaft | dot1qVlanStaticEgress/UntaggedPorts | Bitfeld je VLAN |
| Spanning Tree | dot1dStpPortState | `5` (forwarding) |
| PoE-Zustand | pethPsePortDetectionStatus | `3` (liefert) |
| PoE-Leistung | CISCOSB rlPethPsePort Spalte 5 | `7700` mW (Leia an gi5) |
| PoE-Budget | pethMainPsePower / ConsumptionPower | `180` W / `20` W |

## Zwei Arten von Werten, zwei Ablagen

**Zustaende** (Link, VLAN, PoE an/aus) aendern sich selten und bedeuten etwas,
wenn sie es tun. Sie werden Beobachtungen wie alles andere — damit entsteht ihre
Historie von selbst, und ein Uplink, der wegfaellt, steht in der Veraenderungsliste.

**Zaehler und Messwerte** (Fehler, verworfene Pakete, Watt) aendern sich bei
jedem Lauf. Als Intervall waere jeder Lauf eine "Aenderung". Sie gehen in eine
eigene, kurzlebige Tabelle (`portzaehler`, 48 Stunden), aus der die Oberflaeche
Raten rechnet. Zeitreihen ueber Tage gehoeren nach Prometheus (Plan Block 26) —
dort gibt es fuer die Switches aber noch keine, und fuer "flattert der Port
gerade?" reichen zwei Tage.

## Was still bleibt

Ein Linkwechsel an einem **Zugangsport** ist keine Nachricht: ein PC, der abends
ausgeht, waere sonst jeden Tag zwei Zeilen. An einem **Uplink oder AP-Port**
(einem Port mit LLDP-Nachbarn) ist er eine. Deshalb zwei Schluessel: `link`
wird gemeldet, `link_zugang` nur verfolgt. Der rohe Linkwechsel-Zeitstempel
(`link_wechsel`) ist immer still — er zaehlt das Flattern, meldet es aber nicht.
"""
from __future__ import annotations

from dataclasses import dataclass

from .modell import STILLE_SCHLUESSEL, Beobachtung, Beziehung, Quelle  # noqa: F401
from .sammler_switch import OID as SWITCH_OID, _Switch, zerlegen

OID = {
    "ifname":      SWITCH_OID["ifname"],
    "bridgeport":  SWITCH_OID["bridgeport"],
    "oper":        "1.3.6.1.2.1.2.2.1.8",           # ifOperStatus
    "speed":       "1.3.6.1.2.1.31.1.1.1.15",       # ifHighSpeed (Mbit/s)
    "lastchange":  "1.3.6.1.2.1.2.2.1.9",           # ifLastChange
    "duplex":      "1.3.6.1.2.1.10.7.2.1.19",       # dot3StatsDuplexStatus
    "pvid":        "1.3.6.1.2.1.17.7.1.4.5.1.1",    # dot1qPvid (Index: Bridgeport)
    "egress":      "1.3.6.1.2.1.17.7.1.4.3.1.2",    # dot1qVlanStaticEgressPorts
    "untagged":    "1.3.6.1.2.1.17.7.1.4.3.1.4",    # dot1qVlanStaticUntaggedPorts
    "stp":         "1.3.6.1.2.1.17.2.15.1.3",       # dot1dStpPortState (Bridgeport)
    "in_fehler":   "1.3.6.1.2.1.2.2.1.14",          # ifInErrors
    "out_fehler":  "1.3.6.1.2.1.2.2.1.20",          # ifOutErrors
    "in_verworfen":  "1.3.6.1.2.1.2.2.1.13",        # ifInDiscards
    "out_verworfen": "1.3.6.1.2.1.2.2.1.19",        # ifOutDiscards
    "poe_status":  "1.3.6.1.2.1.105.1.1.1.6",       # pethPsePortDetectionStatus
    "poe_mw":      "1.3.6.1.4.1.9.6.1.101.108.1.1.5",  # CISCOSB rlPethPsePort Leistung
    "poe_haupt":   "1.3.6.1.2.1.105.1.3.1.1",       # pethMainPseTable
}

_OPER = {"1": "up", "2": "down", "3": "testing", "5": "dormant", "6": "notPresent",
         "7": "lowerLayerDown"}
_DUPLEX = {"2": "halb", "3": "voll"}
_STP = {"1": "aus", "2": "blockiert", "3": "lauscht", "4": "lernt",
        "5": "leitet weiter", "6": "defekt"}
_POE = {"1": "aus", "2": "sucht", "3": "liefert", "4": "Fehler", "5": "Test",
        "6": "Fehler"}


def _text(wert: str, tabelle: dict[str, str]) -> str:
    """net-snmp liefert je nach geladener MIB `up` oder `1` — beides verstehen."""
    w = wert.strip()
    if w in tabelle:
        return tabelle[w]
    # "up(1)" oder "up"
    return w.split("(")[0] if w else ""


def ist_physischer_port(name: str) -> bool:
    n = name.strip().lower()
    return (n.startswith(("gi", "fa", "te")) and n[2:].isdigit()) or \
           (n.startswith("po") and n[2:].isdigit())


def bitfeld_ports(hexwert: str) -> set[int]:
    """`"00 00 09 12"` -> Bridgeports, deren Bit gesetzt ist (MSB = Port 1)."""
    ports: set[int] = set()
    try:
        bytes_ = [int(b, 16) for b in hexwert.replace('"', "").split()]
    except ValueError:
        return ports
    for i, byte in enumerate(bytes_):
        for bit in range(8):
            if byte & (0x80 >> bit):
                ports.add(i * 8 + bit + 1)
    return ports


def vlan_text(untagged: list[int], tagged: list[int]) -> str:
    """`u:1 t:5,10,20` — kurz, vergleichbar, und lesbar genug fuer die Historie."""
    teile = []
    if untagged:
        teile.append("u:" + ",".join(str(v) for v in sorted(untagged)))
    if tagged:
        teile.append("t:" + ",".join(str(v) for v in sorted(tagged)))
    return " ".join(teile)


@dataclass
class Zaehlerstand:
    port: str                  # "r2d2:gi5" oder "r2d2" fuer den ganzen Switch
    in_fehler: int | None = None
    out_fehler: int | None = None
    in_verworfen: int | None = None
    out_verworfen: int | None = None
    poe_mw: int | None = None
    poe_budget_w: int | None = None


def auswerten(switch: str, roh: dict[str, str], uplinks: set[str] | None
              ) -> tuple[list[Beobachtung], list[Zaehlerstand]]:
    """Aus den Walks werden Beobachtungen und Zaehlerstaende."""
    z = {k: zerlegen(v, OID[k]) for k, v in roh.items()}
    ifname = z.get("ifname", {})
    if not ifname:
        raise RuntimeError(f"{switch}: keine Portnamen")
    # Bridgeport -> ifIndex. Auf dieser Hardware sind sie gleich, aber darauf
    # verlassen waere geraten; die Tabelle kostet einen Walk.
    bp_zu_if = {bp: ifi for bp, ifi in z.get("bridgeport", {}).items()}
    if_zu_bp = {ifi: bp for bp, ifi in bp_zu_if.items()}

    # VLAN-Mitgliedschaft je Bridgeport
    vlans_u: dict[int, list[int]] = {}
    vlans_t: dict[int, list[int]] = {}
    for vlan, bits in z.get("egress", {}).items():
        if not vlan.isdigit():
            continue
        unt = bitfeld_ports(z.get("untagged", {}).get(vlan, ""))
        for bp in bitfeld_ports(bits):
            (vlans_u if bp in unt else vlans_t).setdefault(bp, []).append(int(vlan))

    uplinks = uplinks or set()
    beob: list[Beobachtung] = []
    zaehler: list[Zaehlerstand] = []

    def merk(port: str, schluessel: str, wert: str) -> None:
        if wert != "":
            beob.append(Beobachtung(Beziehung.MERKMAL, f"{switch}:{port}", schluessel, wert))

    for ifi, name in sorted(ifname.items(), key=lambda x: int(x[0]) if x[0].isdigit() else 0):
        if not ist_physischer_port(name):
            continue
        link = _text(z.get("oper", {}).get(ifi, ""), _OPER)
        merk(name, "link" if name in uplinks else "link_zugang", link)
        merk(name, "link_wechsel", z.get("lastchange", {}).get(ifi, ""))
        if link == "up":
            # Heruntergefahrene Ports melden 10 Mbit/s oder 0 — keine Aussage.
            merk(name, "speed", z.get("speed", {}).get(ifi, ""))
            merk(name, "duplex", _text(z.get("duplex", {}).get(ifi, ""), _DUPLEX))
            bp = if_zu_bp.get(ifi, "")
            merk(name, "stp", _text(z.get("stp", {}).get(bp, ""), _STP))
        bp = if_zu_bp.get(ifi, "")
        if bp.isdigit():
            merk(name, "pvid", z.get("pvid", {}).get(bp, ""))
            merk(name, "vlans", vlan_text(vlans_u.get(int(bp), []), vlans_t.get(int(bp), [])))
        poe = z.get("poe_status", {}).get(f"1.{ifi}")
        if poe is not None:
            merk(name, "poe", _text(poe, _POE))

        def zahl(schluessel: str, index: str = ifi) -> int | None:
            w = z.get(schluessel, {}).get(index)
            try:
                return int(w) if w is not None else None
            except ValueError:
                return None

        zaehler.append(Zaehlerstand(
            port=f"{switch}:{name}",
            in_fehler=zahl("in_fehler"), out_fehler=zahl("out_fehler"),
            in_verworfen=zahl("in_verworfen"), out_verworfen=zahl("out_verworfen"),
            poe_mw=zahl("poe_mw", f"1.{ifi}")))

    haupt = z.get("poe_haupt", {})
    if haupt.get("2.1"):
        # pethMainPseTable: Spalte 2 Budget (W), Spalte 4 Verbrauch (W)
        beob.append(Beobachtung(Beziehung.MERKMAL, switch, "poe_budget", haupt["2.1"]))
        try:
            zaehler.append(Zaehlerstand(port=switch, poe_mw=int(haupt.get("4.1", "0")) * 1000,
                                        poe_budget_w=int(haupt["2.1"])))
        except ValueError:
            pass
    return beob, zaehler


class SwitchPortzustand(_Switch):
    """Link, VLAN, STP, PoE und Fehlerzaehler aller Ports eines Switches."""

    PFLICHT = ("ifname", "bridgeport", "oper", "lastchange")
    # Nicht jeder Switch kennt jede Tabelle. bb8 (SG200-26) hat kein PoE — und
    # liefert auf einen Walk nach einer fehlenden Tabelle nicht "leer", sondern
    # laeuft ueber das Ende hinaus weiter: am 16.09.2026 gut 3500 fremde Zeilen.
    # Deshalb wird jede optionale Tabelle einmal mit EINER Anfrage geprueft und
    # danach nie wieder gefragt, wenn es sie nicht gibt.
    OPTIONAL = ("poe_status", "poe_mw", "poe_haupt", "duplex", "stp", "pvid",
                "egress", "untagged")

    def __init__(self, *a, nachbarn=None, zaehler_ablage=None, pruefer=None, **k) -> None:
        super().__init__(*a, **k)
        self.nachbarn = nachbarn
        self.zaehler_ablage = zaehler_ablage
        self._pruefen = pruefer or self._getnext
        self._fehlt: set[str] | None = None     # erst beim ersten Lauf bestimmt
        self.quelle = Quelle(
            name=f"port-{self.switch}",
            zustaendig_fuer=frozenset({Beziehung.MERKMAL}),
            fehlt_schwelle=2,
        )

    def _getnext(self, oid: str) -> str:
        """Die OID, die ein GETNEXT auf `oid` liefert — ein Paket hin, eins zurueck."""
        import subprocess
        e = subprocess.run(["snmpgetnext", "-v2c", "-c", self.gemeinschaft, "-OQn",
                            "-t", str(self.zeitlimit), "-r", "1", self.ziel, oid],
                           capture_output=True, text=True, timeout=self.zeitlimit * 3)
        if e.returncode != 0:
            raise RuntimeError(f"{self.switch}: {e.stderr.strip()[:120]}")
        return e.stdout.split("=", 1)[0].strip()

    def vorhandene_bestimmen(self) -> set[str]:
        fehlt: set[str] = set()
        for schluessel in self.OPTIONAL:
            oid = OID[schluessel]
            try:
                naechste = self._pruefen(oid)
            except Exception:
                continue                     # im Zweifel fragen, nicht auslassen
            if not naechste.startswith("." + oid + "."):
                fehlt.add(schluessel)
        return fehlt

    def vorab_unveraendert(self) -> bool:
        # Zaehler laufen immer weiter — es gibt nichts Billigeres als sie zu lesen.
        return False

    def sammeln(self) -> list[Beobachtung]:
        if self._fehlt is None:
            self._fehlt = self.vorhandene_bestimmen()
        roh: dict[str, str] = {}
        for schluessel, oid in OID.items():
            if schluessel in self._fehlt:
                roh[schluessel] = ""
                continue
            try:
                roh[schluessel] = self._aufrufen(oid)
            except Exception:
                # PoE gibt es nicht auf jedem Switch (bb8 ist ein SG200-26 ohne);
                # fehlt dagegen eine Pflichtangabe, ist der Lauf gescheitert.
                if schluessel in self.PFLICHT:
                    raise
                roh[schluessel] = ""
        uplinks = getattr(self.nachbarn, "letzte_uplinks", None) if self.nachbarn else None
        beob, zaehler = auswerten(self.switch, roh, uplinks)
        if self.zaehler_ablage and zaehler:
            self.zaehler_ablage(zaehler)
        return beob
