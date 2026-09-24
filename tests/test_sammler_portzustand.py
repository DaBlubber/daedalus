# -*- coding: utf-8 -*-
"""Portzustand an echten Walks von r2d2 (mit PoE) und bb8 (ohne), 16.09.2026."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from daedalus.sammler_portzustand import (OID, SwitchPortzustand, auswerten,
                                          bitfeld_ports, vlan_text)
from daedalus.speicher import gesundheit_rechnen

PROBEN = Path(__file__).parent / "proben"


def _roh(name):
    text = (PROBEN / f"portzustand-{name}.txt").read_text(encoding="utf-8")
    return {k: text for k in OID}          # zerlegen filtert je OID selbst


def _merkmale(beob):
    return {(b.objekt, b.schluessel): b.wert for b in beob}


def test_r2d2_liefert_link_vlan_und_poe():
    beob, zaehler = auswerten("r2d2", _roh("r2d2"), uplinks={"gi5", "gi25"})
    m = _merkmale(beob)
    # gi5 ist Leias Port: Uplink (per LLDP), PoE liefert
    assert m[("r2d2:gi5", "link")] == "up"
    assert m[("r2d2:gi5", "poe")] == "liefert"
    assert ("r2d2:gi5", "link_zugang") not in m
    assert m[("r2d2:gi5", "speed")] == "1000"
    assert m[("r2d2:gi5", "duplex")] == "voll"
    assert m[("r2d2:gi5", "vlans")]            # irgendeine Mitgliedschaft
    # Budget des Switches
    assert m[("r2d2", "poe_budget")] == "180"
    leia = next(z for z in zaehler if z.port == "r2d2:gi5")
    assert leia.poe_mw and 1000 < leia.poe_mw < 30000
    assert next(z for z in zaehler if z.port == "r2d2").poe_budget_w == 180


def test_zugangsport_meldet_link_still():
    beob, _ = auswerten("r2d2", _roh("r2d2"), uplinks=set())
    schluessel = {b.schluessel for b in beob if b.objekt == "r2d2:gi1"}
    assert "link_zugang" in schluessel and "link" not in schluessel


def test_bb8_ohne_poe_und_ohne_fremdzeilen():
    beob, zaehler = auswerten("bb8", _roh("bb8"), uplinks=set())
    assert not any(b.schluessel in ("poe", "poe_budget") for b in beob)
    ports = {b.objekt for b in beob}
    assert "bb8:gi1" in ports and all(p.startswith("bb8:") for p in ports)
    # nur echte Ports: keine VLAN-Schnittstellen o.ae.
    assert all(p.split(":")[1][:2] in ("gi", "fa", "te", "Po", "po") for p in ports)


def test_fehlende_tabelle_wird_einmal_geprueft_und_dann_ausgelassen():
    gewalkt = []
    probe = (PROBEN / "portzustand-bb8.txt").read_text(encoding="utf-8")

    def walk(oid):
        gewalkt.append(oid)
        return probe

    def pruefer(oid):
        # bb8 hat kein PoE: GETNEXT landet ausserhalb der Tabelle
        return ".1.3.6.1.2.1.105.2.1.0" if oid.startswith("1.3.6.1.2.1.105") or \
            oid.startswith("1.3.6.1.4.1.9.6.1.101.108") else "." + oid + ".1"

    s = SwitchPortzustand("bb8", "172.16.0.4", "x", aufrufer=walk, pruefer=pruefer)
    s.sammeln()
    s.sammeln()
    assert OID["poe_status"] not in gewalkt and OID["poe_mw"] not in gewalkt
    assert gewalkt.count(OID["oper"]) == 2


def test_bitfeld_und_vlantext():
    assert bitfeld_ports("80 01") == {1, 16}
    assert vlan_text([1], [20, 5, 10]) == "u:1 t:5,10,20"


def test_raten_mit_zaehlerneustart():
    t0 = datetime(2026, 9, 16, 10, 0, tzinfo=timezone.utc)
    zeilen = [("r2d2:gi24", t0 - timedelta(hours=23), 100, 0, 5, 0, None, None),
              ("r2d2:gi24", t0 - timedelta(minutes=50), 110, 0, 5, 0, None, None),
              ("r2d2:gi24", t0, 121, 2, 7, 0, 3300, None),
              # Neustart: Zaehler faengt bei null an
              ("c3po:gi3", t0 - timedelta(hours=2), 500, 0, 0, 0, None, None),
              ("c3po:gi3", t0, 4, 0, 0, 0, None, None)]
    g = gesundheit_rechnen(zeilen, t0)
    assert g["r2d2:gi24"]["fehler_1h"] == 13
    assert g["r2d2:gi24"]["fehler_24h"] == 23
    assert g["r2d2:gi24"]["verworfen_24h"] == 2
    assert g["r2d2:gi24"]["poe_w"] == 3.3
    assert g["c3po:gi3"]["fehler_24h"] == 4
