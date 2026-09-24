# -*- coding: utf-8 -*-
"""Sammler fuer Prometheus-Inventar und UniFi-WLAN-Clients.

Beide Sammler lesen nur die HTTP-API von Prometheus. Die Vorabfragen bilden
jeweils genau den fachlichen Zustand ab, den der Sammler spaeter liefert. Ein
blosser ``count(...)`` waere fuer WLAN nicht sicher: ein Client kann bei gleich
bleibender Anzahl den Access Point oder seine Adresse wechseln.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .modell import Beobachtung, Beziehung, Quelle, adress_schluessel
from .sammler_arp import mac_lesbar, zufalls_mac

METRIK_WLAN = "unpoller_client_uptime_seconds"
ABFRAGE_WLAN = f'{METRIK_WLAN}{{wired="false"}}'
ABFRAGE_WLAN_VORAB = (
    "count by (mac, ip, name, vlan, essid, ap_name, oui) "
    f"({ABFRAGE_WLAN})"
)
ABFRAGE_INVENTAR_VORAB = 'count by (host, instance) (up{host!=""})'

_MAC = re.compile(r"^[0-9a-f]{2}(?::[0-9a-f]{2}){5}$")
Json = dict[str, Any]
Aufrufer = Callable[[str, dict[str, str] | None], Json]


def _text(wert: Any) -> str:
    """Prometheus-Labels sind Strings; Fremdtypen gelten als nicht gesehen."""
    return wert.strip() if isinstance(wert, str) else ""


def _mac(roh: Any) -> str:
    text = _text(roh).replace(":", " ")
    mac = mac_lesbar(text)
    return mac if _MAC.fullmatch(mac) else ""


def _ergebnis(antwort: Json) -> list[Json]:
    """Einen Prometheus-Vektor vorsichtig aus einer API-Antwort holen."""
    if not isinstance(antwort, dict) or antwort.get("status") != "success":
        raise RuntimeError("Prometheus meldet keinen erfolgreichen Abruf")
    daten = antwort.get("data")
    reihen = daten.get("result") if isinstance(daten, dict) else None
    if not isinstance(reihen, list):
        raise RuntimeError("Prometheus-Antwort enthaelt keinen Vektor")
    return [r for r in reihen if isinstance(r, dict)]


_IP_AM_ANFANG = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3})(?::\d+)?$")


def _inventar_paare(eintraege) -> list[Beobachtung]:
    """Aus (host, instance)-Paaren werden Name und Inventaradresse je Host.

    Die Adresse ist der eigentliche Gewinn: der Pi-hole kennt keine PTR-Eintraege
    (gemessen am 16.09.2026), das Inventar aber weiss, dass 172.16.1.6 host-59a8
    ist. `127.0.0.1` ist die Sicht des Exporters auf sich selbst und keine
    Adresse im Netz; ein Host mit mehreren Adressen bekommt keine — sonst
    entschiede die Reihenfolge der Targets, welche gilt.
    """
    hosts: set[str] = set()
    adressen: dict[str, set[str]] = {}
    for host, instanz in eintraege:
        if not host:
            continue
        hosts.add(host)
        t = _IP_AM_ANFANG.match(instanz)
        if t and not t.group(1).startswith("127."):
            adressen.setdefault(host, set()).add(t.group(1))
    aus = [Beobachtung(Beziehung.MERKMAL, host, "name", host) for host in sorted(hosts)]
    for host in sorted(adressen):
        if len(adressen[host]) == 1:
            aus.append(Beobachtung(Beziehung.MERKMAL, host, "inventar_adresse",
                                   next(iter(adressen[host]))))
    return aus


def inventar_auswerten(antwort: Json) -> list[Beobachtung]:
    """Aus aktiven Targets wird je eindeutigem Host ein Name und, wo eindeutig,
    seine Adresse."""
    if not isinstance(antwort, dict) or antwort.get("status") != "success":
        raise RuntimeError("Prometheus meldet keinen erfolgreichen Target-Abruf")
    daten = antwort.get("data")
    ziele = daten.get("activeTargets") if isinstance(daten, dict) else None
    if not isinstance(ziele, list):
        raise RuntimeError("Prometheus-Antwort enthaelt keine aktiven Targets")

    paare = []
    for ziel in ziele:
        labels = ziel.get("labels") if isinstance(ziel, dict) else None
        if isinstance(labels, dict):
            paare.append((_text(labels.get("host")), _text(labels.get("instance"))))
    return _inventar_paare(paare)


def _signatur(beobachtungen: list[Beobachtung]) -> tuple:
    return tuple((b.objekt, b.schluessel, b.wert) for b in beobachtungen)


def _wlan_labels(antwort: Json) -> list[dict[str, Any]]:
    labels: list[dict[str, Any]] = []
    for reihe in _ergebnis(antwort):
        metrik = reihe.get("metric")
        if isinstance(metrik, dict):
            labels.append(metrik)
    return labels


def wlan_signatur(antwort: Json) -> tuple[tuple[str, ...], ...]:
    """Der komplette fachliche WLAN-Zustand, ohne Messwert und Zeitstempel."""
    felder = ("ip", "name", "vlan", "essid", "ap_name", "oui")
    reihen = []
    for labels in _wlan_labels(antwort):
        mac = _mac(labels.get("mac"))
        if mac:
            reihen.append((mac, *(_text(labels.get(f)) for f in felder)))
    return tuple(sorted(set(reihen)))


def wlan_auswerten(antwort: Json) -> list[Beobachtung]:
    """UniFi-Labels werden zu Adresse, Anschluss und Merkmalen.

    Jede vorhandene Angabe wird einzeln genutzt. So kostet ein fehlendes ``oui``
    nicht auch Adresse und Anschluss desselben Clients. Zufalls-MACs bekommen
    ein explizites Merkmal: Nur so kann eine spaetere Anzeige sie aus der Liste
    "neues Geraet" filtern, ohne ihre technisch nuetzlichen Zuordnungen zu
    verlieren. Wichtiger noch: ihre Beobachtungen werden als *fluechtig*
    gekennzeichnet, womit der Abgleich sie gar nicht erst als Nachricht fuehrt.
    """
    aus: dict[tuple[Beziehung, str, str], Beobachtung] = {}
    for labels in _wlan_labels(antwort):
        mac = _mac(labels.get("mac"))
        if not mac:
            continue

        # Wuerfelt das Geraet seine MAC, wird jede seiner Beobachtungen als
        # fluechtig gekennzeichnet: verfolgt ja, gemeldet nein. Der Abgleich
        # unterdrueckt daraufhin Erstsichtung und Verschwinden, laesst Umzuege
        # und Adresswechsel aber sichtbar.
        fluechtig = zufalls_mac(mac)

        def merken(beziehung: Beziehung, schluessel: str, wert: str) -> None:
            if wert:
                b = Beobachtung(beziehung, mac, schluessel, wert, fluechtig=fluechtig)
                aus[(beziehung, mac, schluessel)] = b

        # `0.0.0.0` meldet ein Client, der sich angemeldet hat, aber noch keine
        # Adresse per DHCP bekommen hat. Das ist keine Adresse, sondern ein
        # Zwischenzustand — im echten Lauf am 16.09.2026 als "adresse_dazu
        # 0.0.0.0 -> 172.16.11.232" sichtbar geworden. Solche Zwischenschritte
        # gehoeren nicht in den Veraenderungsbericht.
        ip = _text(labels.get("ip"))
        if ip and ip not in ("0.0.0.0", "::"):
            merken(Beziehung.ADRESSE, adress_schluessel(ip), ip)
        ap = _text(labels.get("ap_name"))
        merken(Beziehung.ANSCHLUSS, "anschluss", f"ap:{ap}" if ap else "")
        for feld in ("name", "vlan", "essid", "oui"):
            merken(Beziehung.MERKMAL, feld, _text(labels.get(feld)))
        if fluechtig:
            # Zusaetzlich zum Kennzeichen an der Beobachtung auch als Merkmal,
            # damit die Oberflaeche danach filtern kann ("zufallsmac").
            merken(Beziehung.MERKMAL, "zufalls_mac", "ja")

    return [aus[k] for k in sorted(aus, key=lambda x: (x[1], x[0].value, x[2]))]


class _Prometheus:
    def __init__(self, basis_url: str, zeitlimit: int, aufrufer: Aufrufer | None) -> None:
        self.basis_url = basis_url.rstrip("/")
        self.zeitlimit = zeitlimit
        self._aufrufer = aufrufer or self._http_json

    def _http_json(self, pfad: str, parameter: dict[str, str] | None = None) -> Json:
        url = f"{self.basis_url}{pfad}"
        if parameter:
            url += "?" + urlencode(parameter)
        anfrage = Request(url, headers={"Accept": "application/json"})
        with urlopen(anfrage, timeout=self.zeitlimit) as antwort:  # noqa: S310
            if antwort.status != 200:
                raise RuntimeError(f"Prometheus antwortet mit HTTP {antwort.status}")
            try:
                daten = json.load(antwort)
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                raise RuntimeError("Prometheus liefert kein gueltiges JSON") from e
        if not isinstance(daten, dict):
            raise RuntimeError("Prometheus liefert kein JSON-Objekt")
        return daten

    def _query(self, ausdruck: str) -> Json:
        return self._aufrufer("/api/v1/query", {"query": ausdruck})


class PrometheusInventar(_Prometheus):
    """Verwaltete Hosts aus ``/api/v1/targets``."""

    def __init__(self, basis_url: str, name: str = "inventar", zeitlimit: int = 8,
                 aufrufer: Aufrufer | None = None) -> None:
        super().__init__(basis_url, zeitlimit, aufrufer)
        self._letzte_signatur: tuple | None = None
        self.quelle = Quelle(
            name=name,
            # Targets sagen nichts ueber WLAN-Anschluesse oder IP-Zuordnungen.
            zustaendig_fuer=frozenset({Beziehung.MERKMAL}),
            fehlt_schwelle=2,
        )

    def vorab_unveraendert(self) -> bool:
        try:
            paare = [(_text(r["metric"].get("host")), _text(r["metric"].get("instance")))
                     for r in _ergebnis(self._query(ABFRAGE_INVENTAR_VORAB))
                     if isinstance(r.get("metric"), dict)]
            signatur = _signatur(_inventar_paare(paare))
            return bool(signatur and self._letzte_signatur is not None
                        and signatur == self._letzte_signatur)
        except Exception:  # noqa: BLE001
            # Eine kaputte Sparabfrage darf niemals eine Vollabfrage verhindern.
            return False

    def sammeln(self) -> list[Beobachtung]:
        beobachtungen = inventar_auswerten(self._aufrufer("/api/v1/targets", None))
        if not beobachtungen:
            raise RuntimeError("Prometheus liefert keine Targets mit Hostnamen")
        self._letzte_signatur = _signatur(beobachtungen)
        return beobachtungen


class PrometheusWlan(_Prometheus):
    """WLAN-Clients aus den von unpoller gelieferten Reihen."""

    def __init__(self, basis_url: str, name: str = "wlan", zeitlimit: int = 8,
                 aufrufer: Aufrufer | None = None) -> None:
        super().__init__(basis_url, zeitlimit, aufrufer)
        self._letzte_signatur: tuple[tuple[str, ...], ...] | None = None
        self.quelle = Quelle(
            name=name,
            zustaendig_fuer=frozenset({
                Beziehung.ADRESSE, Beziehung.ANSCHLUSS, Beziehung.MERKMAL,
            }),
            fehlt_schwelle=2,
        )

    def vorab_unveraendert(self) -> bool:
        try:
            signatur = wlan_signatur(self._query(ABFRAGE_WLAN_VORAB))
            return bool(signatur and self._letzte_signatur is not None
                        and signatur == self._letzte_signatur)
        except Exception:  # noqa: BLE001
            return False

    def sammeln(self) -> list[Beobachtung]:
        antwort = self._query(ABFRAGE_WLAN)
        signatur = wlan_signatur(antwort)
        beobachtungen = wlan_auswerten(antwort)
        if not signatur or not beobachtungen:
            # Im gemessenen Netz sind weit ueber hundert Clients vorhanden. Eine
            # leere Reihe ist deshalb ein Ausfall, keine Aussage "alle weg".
            raise RuntimeError("Prometheus liefert keine verwertbaren WLAN-Clients")
        self._letzte_signatur = signatur
        return beobachtungen


# Kurze Namen fuer Konfigurationen, in denen der Quellenname bereits den
# Prometheus-Bezug ausdrueckt.
Inventar = PrometheusInventar
Wlan = PrometheusWlan
