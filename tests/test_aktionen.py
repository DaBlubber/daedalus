# -*- coding: utf-8 -*-
"""Aktionen: was abgewiesen wird, bevor irgendein Paket das Haus verlaesst."""
import pytest

from daedalus import aktionen
from daedalus.aktionen import Abgelehnt, ziel_pruefen


@pytest.mark.parametrize("ziel", ["host-59a8", "172.16.1.6; rm -rf /", "8.8.8.8",
                                  "172.16.10.255", "", "::1"])
def test_unzulaessige_ziele(ziel):
    with pytest.raises(Abgelehnt):
        ziel_pruefen(ziel, "traceroute")


def test_kundennetz_nur_ping():
    assert str(ziel_pruefen("192.168.100.20", "ping")) == "192.168.100.20"
    with pytest.raises(Abgelehnt, match="nur Ping"):
        ziel_pruefen("192.168.100.20", "port")


def test_befehl_bekommt_die_adresse_als_eigenes_argument(monkeypatch):
    gesehen = {}

    def lauf(befehl, **_k):
        gesehen["befehl"] = befehl

        class E:
            stdout, stderr, returncode = "4 packets transmitted, 4 received, 0% packet loss\n" \
                                         "rtt min/avg/max/mdev = 0.3/0.4/0.5/0.1 ms", "", 0
        return E()

    monkeypatch.setattr(aktionen.subprocess, "run", lauf)
    e = aktionen.ping("172.16.1.6")
    assert gesehen["befehl"][-1] == "172.16.1.6"
    assert e["ok"] and "0% packet loss" in e["kurz"]


def test_wol_prueft_mac():
    with pytest.raises(Abgelehnt):
        aktionen.wake_on_lan("nicht-eine-mac")


def test_web_weist_unzulaessiges_ziel_mit_422_ab():
    from fastapi.testclient import TestClient
    from daedalus.web import app_bauen
    client = TestClient(app_bauen(lambda: None))
    r = client.post("/api/aktion/traceroute", json={"ziel": "8.8.8.8"})
    assert r.status_code == 422 and "eigenen Netzen" in r.json()["detail"]
    assert client.post("/api/aktion/ping", json={"ziel": "1", "x": 1}).status_code == 422


def test_wol_braucht_adresse_und_sendet_unicast_zuerst(monkeypatch):
    with pytest.raises(Abgelehnt, match="Adresse"):
        aktionen.wake_on_lan("68:74:37:d4:c0:0b")
    gesendet = []

    class Sock:
        def __init__(self, *a): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def setsockopt(self, *a): pass
        def sendto(self, paket, ziel):
            gesendet.append(ziel)
            assert paket[:6] == bytes([0xFF]) * 6 and len(paket) == 102

    monkeypatch.setattr(aktionen.socket, "socket", Sock)
    e = aktionen.wake_on_lan("68:74:37:d4:c0:0b", "172.16.10.101")
    assert gesendet == [("172.16.10.101", 9), ("172.16.10.255", 9)]
    assert e["kurz"] == "gesendet"


def test_scan_ist_ohne_freigabe_gesperrt(monkeypatch):
    monkeypatch.delenv("DAEDALUS_SCAN_FREIGABE", raising=False)
    with pytest.raises(Abgelehnt, match="Sophos"):
        aktionen.scan_starten("172.16.1.6")


def test_scan_nie_im_kundennetz_und_nie_bereiche(monkeypatch):
    monkeypatch.setenv("DAEDALUS_SCAN_FREIGABE", "ja")
    with pytest.raises(Abgelehnt, match="nur Ping"):
        aktionen.scan_starten("192.168.100.5")
    with pytest.raises(Abgelehnt):
        aktionen.scan_starten("172.16.1.0/24")
