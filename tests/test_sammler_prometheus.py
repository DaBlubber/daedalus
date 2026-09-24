# -*- coding: utf-8 -*-
"""Prometheus-Sammler gegen echte, am 16.09.2026 abgenommene Ausschnitte."""
from __future__ import annotations

import copy
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from daedalus.modell import Beziehung, Ereignis
from daedalus.sammler import durchlauf
from daedalus.sammler_prometheus import (
    ABFRAGE_INVENTAR_VORAB,
    ABFRAGE_WLAN,
    ABFRAGE_WLAN_VORAB,
    PrometheusInventar,
    PrometheusWlan,
    inventar_auswerten,
    wlan_auswerten,
)

PROBEN = Path(__file__).parent / "proben"
WLAN = json.loads((PROBEN / "prometheus-wlan.json").read_text(encoding="utf-8"))
ZIELE = json.loads((PROBEN / "prometheus-targets.json").read_text(encoding="utf-8"))
T0 = datetime(2026, 9, 16, 8, 0, tzinfo=timezone.utc)


def t(n):
    return T0 + timedelta(minutes=5 * n)


def vorab_aus_wlan(antwort=WLAN):
    reihen = []
    for r in antwort["data"]["result"]:
        felder = ("mac", "ip", "name", "vlan", "essid", "ap_name", "oui")
        reihen.append({"metric": {f: r["metric"][f] for f in felder if f in r["metric"]},
                       "value": [r["value"][0], "1"]})
    return {"status": "success", "data": {"resultType": "vector", "result": reihen}}


def vorab_aus_zielen(antwort=ZIELE):
    paare = sorted({(z["labels"].get("host"), z["labels"].get("instance"))
                    for z in antwort["data"]["activeTargets"]
                    if z.get("labels", {}).get("host")})
    return {"status": "success", "data": {"resultType": "vector", "result": [
        {"metric": {"host": h, "instance": i}, "value": [1789539972.0, "1"]}
        for h, i in paare
    ]}}


class Antworten:
    def __init__(self, wlan=WLAN, ziele=ZIELE, wirft=None):
        self.wlan = wlan
        self.ziele = ziele
        self.wirft = wirft
        self.aufrufe = []

    def __call__(self, pfad, parameter):
        self.aufrufe.append((pfad, parameter))
        if self.wirft:
            raise self.wirft
        if pfad == "/api/v1/targets":
            return self.ziele
        ausdruck = parameter["query"]
        if ausdruck == ABFRAGE_WLAN:
            return self.wlan
        if ausdruck == ABFRAGE_WLAN_VORAB:
            return vorab_aus_wlan(self.wlan)
        if ausdruck == ABFRAGE_INVENTAR_VORAB:
            return vorab_aus_zielen(self.ziele)
        raise AssertionError(f"unerwartete Abfrage: {ausdruck}")


def test_echte_wlan_probe_hat_alle_beziehungsarten():
    b = wlan_auswerten(WLAN)
    assert {x.beziehung for x in b} == {
        Beziehung.ADRESSE, Beziehung.ANSCHLUSS, Beziehung.MERKMAL,
    }
    assert any(x.objekt == "00:85:07:12:dd:fd" and x.wert == "ap:Anakin" for x in b)
    assert any(x.schluessel == "oui" and x.wert == "Espressif Inc." for x in b)


def test_inventar_dedupliziert_hosts_und_ist_eng_gefasst():
    b = inventar_auswerten(ZIELE)
    assert [(x.objekt, x.schluessel, x.wert) for x in b] == [
        ("host-9d56", "name", "host-9d56"), ("sophos", "name", "sophos"),
    ]
    s = PrometheusInventar("http://prometheus", aufrufer=Antworten())
    assert s.quelle.zustaendig_fuer == frozenset({Beziehung.MERKMAL})


def test_zufalls_mac_wird_am_client_gekennzeichnet():
    b = wlan_auswerten(WLAN)
    marken = [x for x in b if x.schluessel == "zufalls_mac"]
    assert [(x.objekt, x.wert) for x in marken] == [("2a:70:66:44:08:90", "ja")]


def test_fehlende_und_unerwartete_felder_werfen_nichts_um():
    probe = copy.deepcopy(WLAN)
    probe["data"]["result"][0]["metric"].pop("ip")
    probe["data"]["result"][0]["metric"]["neu_und_unbekannt"] = {"kein": "String"}
    probe["data"]["result"].append({"metric": {"mac": "kaputt", "ip": 17}})
    b = wlan_auswerten(probe)
    assert not any(x.objekt == "00:85:07:12:dd:fd" and
                   x.beziehung is Beziehung.ADRESSE for x in b)
    assert any(x.objekt == "00:85:07:12:dd:fd" and
               x.beziehung is Beziehung.ANSCHLUSS for x in b)


def test_unveraenderter_lauf_spart_die_teure_wlan_abfrage(bestand):
    antworten = Antworten()
    s = PrometheusWlan("http://prometheus", aufrufer=antworten)
    assert durchlauf(bestand, s, t(0)).erfolgreich
    vorher = bestand.anzahl_intervalle()
    antworten.aufrufe.clear()
    e = durchlauf(bestand, s, t(1))
    assert e.uebersprungen and bestand.anzahl_intervalle() == vorher
    assert antworten.aufrufe == [("/api/v1/query", {"query": ABFRAGE_WLAN_VORAB})]


def test_unveraenderter_lauf_spart_die_teure_target_abfrage(bestand):
    antworten = Antworten()
    s = PrometheusInventar("http://prometheus", aufrufer=antworten)
    durchlauf(bestand, s, t(0))
    antworten.aufrufe.clear()
    assert durchlauf(bestand, s, t(1)).uebersprungen
    assert antworten.aufrufe == [
        ("/api/v1/query", {"query": ABFRAGE_INVENTAR_VORAB})]


def test_vorabfehler_fuehrt_zur_vollabfrage(bestand):
    class EinmalKaputt(Antworten):
        def __call__(self, pfad, parameter):
            if parameter and parameter.get("query") == ABFRAGE_WLAN_VORAB:
                self.aufrufe.append((pfad, parameter))
                raise TimeoutError("Vorabfrage ausgefallen")
            return super().__call__(pfad, parameter)

    antworten = EinmalKaputt()
    s = PrometheusWlan("http://prometheus", aufrufer=antworten)
    e = durchlauf(bestand, s, t(0))
    assert e.erfolgreich
    assert any(p == {"query": ABFRAGE_WLAN} for _, p in antworten.aufrufe)


def test_ausgefallene_quelle_laesst_nichts_verschwinden(bestand):
    s = PrometheusWlan("http://prometheus", aufrufer=Antworten())
    durchlauf(bestand, s, t(0))
    s._aufrufer = Antworten(wirft=TimeoutError("Prometheus weg"))
    for n in (1, 2, 3):
        assert not durchlauf(bestand, s, t(n)).erfolgreich
    assert bestand.offen_fuer(
        Beziehung.ANSCHLUSS, "00:85:07:12:dd:fd", "anschluss") is not None


def test_leere_wlan_antwort_ist_ein_fehler(bestand):
    leer = {"status": "success", "data": {"resultType": "vector", "result": []}}
    s = PrometheusWlan("http://prometheus", aufrufer=Antworten(wlan=leer))
    assert not durchlauf(bestand, s, t(0)).erfolgreich


def test_ap_wechsel_wird_verfolgt_aber_nicht_gemeldet(bestand):
    antworten = Antworten()
    s = PrometheusWlan("http://prometheus", aufrufer=antworten)
    durchlauf(bestand, s, t(0))
    gewechselt = copy.deepcopy(WLAN)
    gewechselt["data"]["result"][0]["metric"]["ap_name"] = "Leia"
    antworten.wlan = gewechselt
    e = durchlauf(bestand, s, t(1))
    assert [a for a in e.aenderungen if a.objekt == "00:85:07:12:dd:fd"] == []
    offen = bestand.offen_fuer(Beziehung.ANSCHLUSS, "00:85:07:12:dd:fd", "anschluss")
    assert offen.wert == "ap:Leia"


def test_neue_nichtzufalls_mac_wird_normal_als_neu_gemeldet(bestand):
    erste = copy.deepcopy(WLAN)
    neue_reihe = erste["data"]["result"].pop(0)
    antworten = Antworten(wlan=erste)
    s = PrometheusWlan("http://prometheus", aufrufer=antworten)
    durchlauf(bestand, s, t(0))
    erste["data"]["result"].append(neue_reihe)
    e = durchlauf(bestand, s, t(1))
    assert any(a.art is Ereignis.ERSTMALS_GESEHEN and
               a.objekt == "00:85:07:12:dd:fd" for a in e.aenderungen)


def test_zufalls_macs_werden_als_fluechtig_gekennzeichnet():
    """Nicht nur als Merkmal: der Abgleich muss sie gar nicht erst melden."""
    from daedalus.sammler_prometheus import wlan_auswerten
    antwort = {"status": "success", "data": {"result": [
        {"metric": {"mac": "96:0D:91:1F:48:D9", "ip": "172.16.10.91",
                    "ap_name": "AP FLUR", "name": "handy"}},
        {"metric": {"mac": "80:F6:0F:9A:BA:DD", "ip": "172.16.10.92",
                    "ap_name": "AP FLUR", "name": "laptop"}},
    ]}}
    b = wlan_auswerten(antwort)
    gewuerfelt = [x for x in b if x.objekt == "96:0d:91:1f:48:d9"]
    fest = [x for x in b if x.objekt == "80:f6:0f:9a:ba:dd"]
    assert gewuerfelt and all(x.fluechtig for x in gewuerfelt)
    assert fest and not any(x.fluechtig for x in fest)


def test_null_adresse_ist_keine_adresse():
    """Ein Client, der sich angemeldet hat, aber noch keine Adresse per DHCP
    hat, meldet 0.0.0.0. Im echten Lauf erzeugte das die Aenderung
    "0.0.0.0 -> 172.16.11.232" - ein Zwischenzustand, keine Nachricht."""
    from daedalus.sammler_prometheus import wlan_auswerten
    from daedalus.modell import Beziehung
    antwort = {"status": "success", "data": {"result": [
        {"metric": {"mac": "8A:BF:AB:4D:27:5C", "ip": "0.0.0.0", "ap_name": "Luke"}},
        {"metric": {"mac": "E2:05:40:AF:C7:DC", "ip": "172.16.11.5", "ap_name": "Luke"}},
    ]}}
    b = wlan_auswerten(antwort)
    adressen = [x for x in b if x.beziehung is Beziehung.ADRESSE]
    assert [x.wert for x in adressen] == ["172.16.11.5"]
    # der Anschluss bleibt trotzdem erhalten - das Geraet IST ja da
    assert any(x.objekt == "8a:bf:ab:4d:27:5c" and x.beziehung is Beziehung.ANSCHLUSS
               for x in b)


def test_inventar_liefert_eindeutige_netzadresse_je_host():
    ziele = {"status": "success", "data": {"activeTargets": [
        {"labels": {"host": "host-59a8", "instance": "172.16.1.6:9100"}},
        {"labels": {"host": "host-59a8", "instance": "172.16.1.6:8080"}},
        {"labels": {"host": "host-9d56", "instance": "127.0.0.1:9100"}},
        {"labels": {"host": "zwei", "instance": "172.16.1.30:9100"}},
        {"labels": {"host": "zwei", "instance": "172.16.10.30:9100"}},
    ]}}
    adressen = {(b.objekt, b.wert) for b in inventar_auswerten(ziele)
                if b.schluessel == "inventar_adresse"}
    assert adressen == {("host-59a8", "172.16.1.6")}
