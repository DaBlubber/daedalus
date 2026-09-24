# -*- coding: utf-8 -*-
"""Kea: Namen und Adressen, wie die Geraete sie melden, und die Konfiguration."""
from pathlib import Path

import pytest

from daedalus.modell import Beziehung
from daedalus.sammler_kea import (KeaKonfig, KeaLeases, hostname_sauber,
                                  konfig_auswerten, leases_auswerten)

# Form wie die echte Antwort vom 16.09.2026 (lease4-get-all auf exekutor-3)
ANTWORT = [{"result": 0, "text": "4 IPv4 lease(s) found.", "arguments": {"leases": [
    {"hw-address": "44:4f:91:f9:32:cc", "ip-address": "172.16.10.13",
     "hostname": "device-08ef.example.com.", "state": 0},
    {"hw-address": "5c:29:f4:c2:03:f2", "ip-address": "172.16.10.30",
     "hostname": "device-d22d.corp.example.net.", "state": 0},
    {"hw-address": "00:c6:ea:cc:84:6d", "ip-address": "172.16.10.10",
     "hostname": "", "state": 0},
    {"hw-address": "aa:bb:cc:dd:ee:ff", "ip-address": "172.16.10.11",
     "hostname": "abgelaufen", "state": 2},
]}}]

# Echte, ausgerollte Konfiguration aus admin/dhcpsetting (Stand 16.09.2026)
KONFIG = (Path(__file__).parent / "proben" / "kea-dhcp4.conf").read_text(encoding="utf-8")


def test_namen_werden_gekuerzt_fremde_domain_bleibt():
    assert hostname_sauber("device-08ef.example.com.") == "device-08ef"
    assert hostname_sauber("device-d22d.corp.example.net.") == "device-d22d.corp.example.net"
    assert hostname_sauber("device-a6c1") == "device-a6c1"


def test_nur_gueltige_leases_mit_name_und_adresse():
    b = leases_auswerten(ANTWORT)
    assert sorted((x.objekt, x.schluessel, x.wert) for x in b) == sorted([
        ("5c:29:f4:c2:03:f2", "dhcp_lease", "172.16.10.30"),
        ("5c:29:f4:c2:03:f2", "dhcp_name", "device-d22d.corp.example.net"),
        ("44:4f:91:f9:32:cc", "dhcp_lease", "172.16.10.13"),
        ("44:4f:91:f9:32:cc", "dhcp_name", "device-08ef"),
        ("00:c6:ea:cc:84:6d", "dhcp_lease", "172.16.10.10"),
    ])
    assert {x.beziehung for x in b} == {Beziehung.MERKMAL}


def test_leere_tabelle_ist_ausfall_nicht_alle_weg():
    with pytest.raises(RuntimeError):
        leases_auswerten([{"result": 3, "arguments": {"leases": []}}])


def test_standby_springt_ein():
    gefragt = []

    def aufrufer(url):
        gefragt.append(url)
        if "253" in url:
            raise OSError("primary weg")
        return ANTWORT

    s = KeaLeases(aufrufer=aufrufer)
    assert len(s.sammeln()) == 5
    assert gefragt == ["http://172.16.10.253:8000/", "http://172.16.10.251:8000/"]
    assert s.quelle.zustaendig_fuer == frozenset({Beziehung.MERKMAL})


def test_echte_konfiguration_mit_includes_und_kommentaren():
    b = konfig_auswerten(KONFIG)
    pools = {x.objekt: x.wert for x in b if x.schluessel == "dhcp_pools"}
    assert pools["netz:172.16.10.0/24"] == "172.16.10.150-172.16.10.248"
    assert len(pools) == 6
    assert sum(1 for x in b if x.schluessel == "dhcp_reservierung") == 40
    namen = {x.objekt: x.wert for x in b if x.schluessel == "reservierung_name"}
    assert namen["12:30:bd:6a:36:d2"] == "device-6807"


def test_konfig_spart_bei_unveraendertem_etag():
    aufrufe = []

    def abrufer(etag):
        aufrufe.append(etag)
        return (304, etag, "") if etag else (200, '"abc"', KONFIG)

    s = KeaKonfig(abrufer=abrufer)
    assert s.vorab_unveraendert() is False          # noch nie gelesen
    assert s.sammeln()
    assert s.vorab_unveraendert() is True
    assert aufrufe == ["", '"abc"']


def test_zufalls_mac_ist_fluechtig():
    antwort = [{"result": 0, "arguments": {"leases": [
        {"hw-address": "9e:3e:bc:71:64:d6", "ip-address": "172.16.10.195",
         "hostname": "", "state": 0}]}}]
    assert [b.fluechtig for b in leases_auswerten(antwort)] == [True]
