# -*- coding: utf-8 -*-
"""DHCP: Leases und Konfiguration von Kea.

Warum Kea und nicht DNS: am 16.09.2026 gemessen, der Pi-hole kennt **keinen
einzigen** PTR-Eintrag, und nicht einmal `host-59a8.example.com` loest auf. Kea
dagegen kennt fuer 47 von 65 Leases den Namen, den das Geraet selbst beim
Anfordern der Adresse mitschickt (`device-08ef`, `jetkvm`, `device-ad34`),
und die Konfiguration fuehrt 40 Reservierungen mit gepflegtem Namen.

Zwei Sammler, weil die beiden Teile verschieden altern und verschieden billig
zu fragen sind:

| Sammler | Quelle | Vorabfrage |
|---|---|---|
| `KeaLeases` | HA-Listener `lease4-get-all`, ein POST, ~15 KB | keine |
| `KeaKonfig` | ausgerollte Datei aus dem Repo `dhcpsetting` | bedingter GET, 304 |

Geliefert werden **nur Merkmale**, keine Adressen. Ob ein Geraet gerade da ist,
beurteilt die ARP-Tabelle der Sophos; eine zweite zustaendige Quelle fuer
dieselbe Beziehung wuerde Intervalle gegeneinander auf- und zumachen, sobald ein
Lease laenger lebt als sein ARP-Eintrag.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .modell import Beobachtung, Beziehung, Quelle
from .sammler_arp import mac_lesbar, zufalls_mac

# exekutor-3 ist primary, exekutor-1 standby (Kea-Notiz vom 12.08.2026).
KEA_KNOTEN = ("http://172.16.10.253:8000/", "http://172.16.10.251:8000/")
EIGENE_DOMAIN = "example.com"

Aufrufer = Callable[[str], Any]


def hostname_sauber(roh: str) -> str:
    """`device-08ef.example.com.` -> `device-08ef`; fremde Domains bleiben stehen.

    `device-d22d.corp.example.net.` ist ein Firmenlaptop — dass er nicht zu uns
    gehoert, ist genau die Angabe, die man sehen will.
    """
    name = (roh or "").strip().rstrip(".")
    if name.lower().endswith("." + EIGENE_DOMAIN):
        name = name[: -len(EIGENE_DOMAIN) - 1]
    return name


# --- Leases ---------------------------------------------------------------------

def leases_auswerten(antwort: Any) -> list[Beobachtung]:
    """Aus der Kea-Antwort werden Name und vergebene Adresse je MAC."""
    eintrag = antwort[0] if isinstance(antwort, list) and antwort else antwort
    if not isinstance(eintrag, dict) or eintrag.get("result") not in (0, 3):
        # 3 heisst "leer" — bei einem Netz mit Dutzenden Geraeten ein Ausfall.
        raise RuntimeError(f"Kea antwortet nicht erfolgreich: {str(eintrag)[:120]}")
    leases = (eintrag.get("arguments") or {}).get("leases")
    if not isinstance(leases, list) or not leases:
        raise RuntimeError("Kea liefert keine Leases")

    aus: dict[tuple[str, str], Beobachtung] = {}
    for lease in leases:
        if not isinstance(lease, dict) or lease.get("state", 0) != 0:
            continue                      # 1 = abgelehnt, 2 = abgelaufen
        mac = mac_lesbar(str(lease.get("hw-address", "")))
        if len(mac) != 17:
            continue
        # Handys mit gewuerfelter MAC holen sich je Netz einen eigenen Lease. Ihr
        # Ablauf ist keine Nachricht — am 16.09.2026 kam genau so ein
        # "verschwunden 9e:3e:bc:71:64:d6". Gleiche Regel wie im WLAN-Sammler.
        fl = zufalls_mac(mac)
        name = hostname_sauber(str(lease.get("hostname", "")))
        if name:
            aus[(mac, "dhcp_name")] = Beobachtung(Beziehung.MERKMAL, mac, "dhcp_name", name,
                                                  fluechtig=fl)
        # Die vergebene Adresse als Merkmal: sie sagt "kam aus dem Pool", nicht
        # "ist gerade da". Die Ablaufzeit bleibt draussen — sie aendert sich bei
        # jeder Verlaengerung und machte jedes Mal ein neues Intervall auf.
        if lease.get("ip-address"):
            aus[(mac, "dhcp_lease")] = Beobachtung(Beziehung.MERKMAL, mac, "dhcp_lease",
                                                  str(lease["ip-address"]), fluechtig=fl)
    return [aus[k] for k in sorted(aus)]


class KeaLeases:
    """Liest die Lease-Tabelle von Kea."""

    def __init__(self, knoten: tuple[str, ...] = KEA_KNOTEN, name: str = "kea",
                 zeitlimit: int = 8, aufrufer: Aufrufer | None = None) -> None:
        self.knoten = knoten
        self.zeitlimit = zeitlimit
        self._aufrufer = aufrufer or self._abrufen
        self.quelle = Quelle(
            name=name,
            zustaendig_fuer=frozenset({Beziehung.MERKMAL}),
            # Leases leben Stunden; ein Name, der zweimal fehlt, ist wirklich weg.
            fehlt_schwelle=2,
        )

    def _abrufen(self, url: str) -> Any:
        koerper = json.dumps({"command": "lease4-get-all", "service": ["dhcp4"]}).encode()
        anfrage = Request(url, data=koerper, method="POST",
                          headers={"Content-Type": "application/json"})
        with urlopen(anfrage, timeout=self.zeitlimit) as antwort:  # noqa: S310
            return json.load(antwort)

    def vorab_unveraendert(self) -> bool:
        # Kea hat keine billige Frage — die ganze Tabelle ist schon die billige.
        return False

    def sammeln(self) -> list[Beobachtung]:
        fehler = []
        for url in self.knoten:
            try:
                return leases_auswerten(self._aufrufer(url))
            except Exception as e:  # noqa: BLE001
                fehler.append(f"{url}: {e}")
        raise RuntimeError("; ".join(fehler))


# --- Konfiguration: Netze, Pools, Reservierungen ----------------------------------
#
# Ueber den HA-Listener verweigert Kea `config-get` (403, gemessen am 16.09.2026)
# — dort sind nur HA- und Lease-Befehle freigegeben. Die Konfiguration ist aber
# ohnehin versioniert: Repo `admin/dhcpsetting`, ein Push rollt sie per Jenkins
# auf die drei Knoten aus. Gelesen wird deshalb die Datei, die ausgerollt wird.
#
# Das ETag der Datei ist die billigste denkbare Vorabfrage: ein bedingter GET
# liefert 304 und null Bytes, solange niemand die DHCP-Konfiguration anfasst.

KONFIG_URL = ("https://git.example.com/api/v1/repos/admin/dhcpsetting/raw/"
              "kea-dhcp4.conf")

_INCLUDE = re.compile(r"<\?include[^>]*\?>")
_ZEILENKOMMENTAR = re.compile(r"(?m)^\s*(#|//).*$")
_BLOCKKOMMENTAR = re.compile(r"/\*.*?\*/", re.S)


def konfig_lesen(text: str) -> dict:
    """Kea-JSON mit Kommentaren und Includes in ein dict.

    Includes verweisen auf Dateien, die nur auf den Knoten liegen (Schnittstellen,
    eigener Servername) — fuer die Uebersicht belanglos, sie werden zu `null`.
    """
    text = _BLOCKKOMMENTAR.sub("", _ZEILENKOMMENTAR.sub("", text))
    text = _INCLUDE.sub("null", text)
    daten = json.loads(text)
    if not isinstance(daten, dict) or not isinstance(daten.get("Dhcp4"), dict):
        raise RuntimeError("keine Dhcp4-Konfiguration")
    return daten["Dhcp4"]


def konfig_auswerten(text: str) -> list[Beobachtung]:
    """Netze mit Pools und Router, Reservierungen je MAC."""
    d = konfig_lesen(text)
    netze = list(d.get("subnet4") or [])
    for geteilt in d.get("shared-networks") or []:
        netze.extend(geteilt.get("subnet4") or [])
    if not netze:
        raise RuntimeError("Konfiguration ohne Netze")

    aus: list[Beobachtung] = []
    for netz in netze:
        kennung = f"netz:{netz['subnet']}"
        pools = ",".join(p["pool"].replace(" ", "") for p in netz.get("pools") or []
                         if isinstance(p, dict) and p.get("pool"))
        if pools:
            aus.append(Beobachtung(Beziehung.MERKMAL, kennung, "dhcp_pools", pools))
        for option in netz.get("option-data") or []:
            if option.get("name") == "routers" and option.get("data"):
                aus.append(Beobachtung(Beziehung.MERKMAL, kennung, "dhcp_router",
                                       str(option["data"])))
        for r in netz.get("reservations") or []:
            mac = mac_lesbar(str(r.get("hw-address", "")))
            if len(mac) != 17:
                continue
            if r.get("ip-address"):
                aus.append(Beobachtung(Beziehung.MERKMAL, mac, "dhcp_reservierung",
                                       str(r["ip-address"])))
            name = hostname_sauber(str(r.get("hostname", "")))
            if name:
                aus.append(Beobachtung(Beziehung.MERKMAL, mac, "reservierung_name", name))
    return aus


class KeaKonfig:
    """Liest die ausgerollte Kea-Konfiguration aus dem Git-Repo."""

    def __init__(self, url: str = KONFIG_URL, name: str = "dhcp-konfig",
                 zeitlimit: int = 8, abrufer=None) -> None:
        self.url = url
        self.zeitlimit = zeitlimit
        self._abrufer = abrufer or self._abrufen
        self._etag = ""
        self.quelle = Quelle(
            name=name,
            zustaendig_fuer=frozenset({Beziehung.MERKMAL}),
            # Eine Konfiguration altert nicht aus: was einmal fehlt, ist geloescht.
            fehlt_schwelle=1,
        )

    def _abrufen(self, etag: str) -> tuple[int, str, str]:
        """(Status, ETag, Text). 304 heisst: seit `etag` unveraendert."""
        kopf = {"If-None-Match": etag} if etag else {}
        try:
            with urlopen(Request(self.url, headers=kopf),  # noqa: S310
                         timeout=self.zeitlimit) as antwort:
                return (antwort.status, antwort.headers.get("ETag", ""),
                        antwort.read().decode("utf-8"))
        except HTTPError as e:
            if e.code == 304:
                return 304, etag, ""
            raise

    def vorab_unveraendert(self) -> bool:
        if not self._etag:
            return False
        try:
            status, _etag, _text = self._abrufer(self._etag)
            return status == 304
        except Exception:  # noqa: BLE001
            return False

    def sammeln(self) -> list[Beobachtung]:
        status, etag, text = self._abrufer("")
        if status != 200:
            raise RuntimeError(f"Konfiguration nicht lesbar: HTTP {status}")
        beobachtungen = konfig_auswerten(text)
        self._etag = etag
        return beobachtungen
