# -*- coding: utf-8 -*-
"""Die Switch-Sammler, gegen eine echte Probe von c3po (16.09.2026)."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from daedalus.modell import Beziehung
from daedalus.sammler import durchlauf
from daedalus.sammler_switch import (OID, SwitchMacs, SwitchNachbarn, ist_kanal,
                                     mac_aus_index, vlan_aus_index, zerlegen)

ROH = (Path(__file__).parent / "proben" / "switch-c3po.txt").read_text(encoding="utf-8")
T0 = datetime(2026, 9, 16, 8, 0, tzinfo=timezone.utc)
def t(n): return T0 + timedelta(minutes=5 * n)

# Die Probe ist nach Abschnitten gegliedert; der Aufrufer bedient sich daraus.
ABSCHNITT = {"fdb": "### FDB", "bridgeport": "### BRIDGEPORT", "ifname": "### IFNAME",
             "lldpname": "### LLDPNAME", "lldpport": "### LLDPPORT",
             "lldplocal": "### LLDPLOCAL", "lastchange": "### IFLASTCHANGE"}


def teil(name: str) -> str:
    marke = ABSCHNITT[name]
    rest = ROH.split(marke, 1)[1]
    return rest.split("###", 1)[0]


def aufrufer(aenderungen=None, wirft=None):
    """Bedient jede OID aus der Probe. `aenderungen` ersetzt einzelne Abschnitte."""
    aenderungen = aenderungen or {}
    umkehr = {v: k for k, v in OID.items()}
    def hole(oid):
        if wirft:
            raise wirft
        name = umkehr[oid]
        return aenderungen.get(name, teil(name))
    return hole


def nachbarn(**kw):
    return SwitchNachbarn("c3po", "172.16.0.3", "x", aufrufer=aufrufer(**kw))
def macs(uplinks=(), **kw):
    return SwitchMacs("c3po", "172.16.0.3", "x", uplinks=uplinks, aufrufer=aufrufer(**kw))


# --------------------------------------------------------------- Zerlegen
def test_mac_aus_dem_fdb_index():
    assert mac_aus_index("1.60.222.152.72.101.20") == "3c:de:98:48:65:14"
    assert vlan_aus_index("1.60.222.152.72.101.20") == "1"


def test_kurzer_index_ergibt_keine_mac():
    assert mac_aus_index("1.2.3") == ""


def test_zerlegen_nimmt_nur_die_gefragte_oid():
    assert all(k.isdigit() or "." in k for k in zerlegen(ROH, OID["ifname"]))
    assert zerlegen(ROH, OID["ifname"])["49"] == "gi1"


# --------------------------------------------------------------- LLDP
def test_nachbarn_werden_erkannt():
    n = nachbarn().nachbarports()
    assert n["gi25"] == ("bb8", "gi1")
    assert "r2d2" in {x[0] for x in n.values()}


def test_nachbar_ohne_namen_faellt_weg():
    """Ein LLDP-Eintrag ohne SysName ist keine Nachbarschaft, sondern Rauschen."""
    n = nachbarn().nachbarports()
    assert all(nam for nam, _ in n.values())


def test_lldp_laeuft_durch(bestand):
    e = durchlauf(bestand, nachbarn(), t(0))
    assert e.erfolgreich and e.beobachtungen >= 2
    assert bestand.offen_fuer(Beziehung.VERBINDUNG, "c3po:gi25", "verbindung").wert == "bb8:gi1"


# --------------------------------------------------- die billige Vorabfrage
def test_vorabfrage_greift_beim_zweiten_mal():
    s = nachbarn()
    assert s.vorab_unveraendert() is False, "beim ersten Mal gibt es nichts zu vergleichen"
    assert s.vorab_unveraendert() is True,  "unveraenderte Zeitstempel -> nichts zu tun"


def test_vorabfrage_schlaegt_an_wenn_ein_port_flattert():
    s = nachbarn()
    s.vorab_unveraendert()
    s._aufrufen = aufrufer(aenderungen={"lastchange":
        teil("lastchange").replace("6:10:51:35.78", "0:0:00:04.11")})
    assert s.vorab_unveraendert() is False


def test_vorabfrage_ohne_antwort_sammelt_lieber():
    s = nachbarn()
    s._aufrufen = aufrufer(aenderungen={"lastchange": ""})
    assert s.vorab_unveraendert() is False


def test_uebersprungene_laeufe_lassen_nachbarn_stehen(bestand):
    s = nachbarn()
    durchlauf(bestand, s, t(0))
    for n in (1, 2, 3, 4, 5):
        assert durchlauf(bestand, s, t(n)).uebersprungen
    assert bestand.offen_fuer(Beziehung.VERBINDUNG, "c3po:gi25", "verbindung") is not None


# --------------------------------------------------------------- MAC-Tabelle
def test_an_c3po_reicht_die_kanalregel_schon(bestand):
    """Gemessen: an c3po liegen ALLE Durchgangs-MACs auf den Kanaelen Po1/Po2,
    keine direkt auf gi25-gi28. Die LLDP-Liste aendert deshalb hier nichts mehr
    — sie bleibt trotzdem noetig, siehe naechster Test."""
    ohne = macs().sammeln()
    mit = macs(uplinks={"gi25", "gi26", "gi27", "gi28"}).sammeln()
    assert len(ohne) == len(mit)


def test_ohne_kanal_braucht_es_die_lldp_liste(bestand):
    """Nicht jeder Uplink ist gebuendelt. Sitzt eine fremde MAC direkt auf
    gi25, faengt sie nur die Nachbarliste ab."""
    fremd = teil("fdb") + chr(10) + ".1.3.6.1.2.1.17.7.1.2.2.1.2.1.240.30.107.238.228.218 = 73" + chr(10)
    ohne = macs(aenderungen={"fdb": fremd}).sammeln()
    mit = macs(uplinks={"gi25"}, aenderungen={"fdb": fremd}).sammeln()
    assert len(ohne) == len(mit) + 1
    assert any(x.wert == "c3po:gi25" for x in ohne)
    assert all(x.wert != "c3po:gi25" for x in mit)


def test_macs_auf_uplinks_werden_verworfen(bestand):
    b = macs(uplinks={"gi25", "gi26", "gi27", "gi28"}).sammeln()
    assert all(not x.wert.endswith((":gi25", ":gi26", ":gi27", ":gi28")) for x in b)


def test_ein_geraet_landet_an_seinem_zugangsport(bestand):
    e = durchlauf(bestand, macs(uplinks={"gi25", "gi26"}), t(0))
    assert e.erfolgreich and e.beobachtungen >= 1
    einer = bestand.offene(Beziehung.ANSCHLUSS)[0]
    assert einer.wert.startswith("c3po:gi")


def test_mac_tabelle_hat_keine_billige_vorabfrage():
    """Eine MAC wandert, ohne dass sich ein Link aendert. Wer hier
    `ifLastChange` abfragt, verliert genau die Umzuege."""
    assert macs().vorab_unveraendert() is False


def test_leere_mac_tabelle_gilt_als_fehler(bestand):
    durchlauf(bestand, macs(uplinks={"gi25"}), t(0))
    vorher = len(bestand.offene(Beziehung.ANSCHLUSS))
    for n in (1, 2, 3):
        assert not durchlauf(bestand, macs(uplinks={"gi25"},
                             aenderungen={"fdb": ""}), t(n)).erfolgreich
    assert len(bestand.offene(Beziehung.ANSCHLUSS)) == vorher


def test_ausgefallener_switch_laesst_nichts_verschwinden(bestand):
    durchlauf(bestand, macs(uplinks={"gi25"}), t(0))
    vorher = len(bestand.offene(Beziehung.ANSCHLUSS))
    for n in (1, 2, 3, 4):
        assert not durchlauf(bestand, macs(uplinks={"gi25"},
                             wirft=TimeoutError("keine Antwort")), t(n)).erfolgreich
    assert len(bestand.offene(Beziehung.ANSCHLUSS)) == vorher


def test_die_quellen_sind_eng_gefasst():
    assert nachbarn().quelle.zustaendig_fuer == frozenset({Beziehung.VERBINDUNG})
    assert macs().quelle.zustaendig_fuer == frozenset({Beziehung.ANSCHLUSS})
    assert nachbarn().quelle.name != macs().quelle.name


# ---------------------------------------------------------------------------
# Der Befund aus den echten Daten: 52 von 60 MACs sitzen auf einem PORTKANAL.
# ---------------------------------------------------------------------------
def test_portkanal_wird_erkannt():
    assert ist_kanal("Po1") and ist_kanal("po12")
    assert not ist_kanal("gi1") and not ist_kanal("Port") and not ist_kanal("")


def test_der_portkanal_gilt_als_uplink_auch_ohne_lldp_eintrag():
    """LLDP meldet die MITGLIEDER (gi25, gi26), die MAC-Tabelle nennt den KANAL
    (Po1). Wer nur der LLDP-Liste folgt, haengt 52 Geraete an einen Stecker."""
    b = macs(uplinks=set()).sammeln()          # bewusst OHNE Uplinkwissen
    assert all(not x.wert.lower().endswith(":po1") for x in b)


def test_ohne_kanalregel_waere_es_falsch():
    """Gegenprobe an den echten Zahlen: der Kanal traegt die Mehrheit."""
    from daedalus.sammler_switch import OID, zerlegen
    fdb = zerlegen(teil("fdb"), OID["fdb"])
    bp = zerlegen(teil("bridgeport"), OID["bridgeport"])
    nm = zerlegen(teil("ifname"), OID["ifname"])
    auf_kanal = sum(1 for v in fdb.values() if ist_kanal(nm.get(bp.get(v, v), "")))
    assert auf_kanal > len(fdb) / 2, "die Mehrheit der MACs sitzt auf dem Kanal"


# ---------------------------------------------------------------------------
# Der Fund aus dem ersten echten Lauf: lldpLocPortId meldet bei
# Access-Point-Ports eine MAC statt eines Portnamens.
# ---------------------------------------------------------------------------
ROH_R2D2 = (Path(__file__).parent / "proben" / "switch-r2d2.txt").read_text(encoding="utf-8")


def teil_r2d2(name: str) -> str:
    marke = ABSCHNITT[name]
    return ROH_R2D2.split(marke, 1)[1].split("###", 1)[0]


def r2d2_nachbarn():
    umkehr = {v: k for k, v in OID.items()}
    return SwitchNachbarn("r2d2", "172.16.0.2", "x",
                          aufrufer=lambda oid: teil_r2d2(umkehr[oid]))


def test_ap_ports_werden_trotz_mac_kennung_aufgeloest():
    """An r2d2 melden vier Ports ihre lokale Kennung als MAC (Untertyp 3),
    sechs als Portnamen (Untertyp 5). Ueber ifName loesen beide auf."""
    n = r2d2_nachbarn().nachbarports()
    assert n["gi5"][0] == "Leia", "der AP-Port muss aufgeloest werden"
    assert n["gi25"][0] == "bb8", "der Switch-Port weiterhin auch"
    assert not any(" " in p for p in n), "keine MAC darf als Portname durchgehen"


def test_ap_ports_gelten_als_uplink():
    """Ein AP-Port ist ein Uplink wie jeder andere: dahinter liegen Geraete,
    sie haengen nicht dort. Ohne diese Regel meldete der erste echte Lauf 131
    WLAN-Clients als von ap:Leia nach r2d2:gi5 umgezogen."""
    up = r2d2_nachbarn().uplinkports()
    assert {"gi5", "gi12", "gi15", "gi24"} <= up, "die vier AP-Ports"
    assert {"gi25", "gi26", "gi27", "gi28", "gi7", "gi10"} <= up, "die Switch-Ports"


# ---------------------------------------------------------------------------
# Im Dienst laufen LLDP und MAC-Tabelle in verschiedenen Takten (30 Min gegen
# 5 Min). Die Uplinkliste muss deshalb zwischen ihnen wandern.
# ---------------------------------------------------------------------------
def test_mac_sammler_liest_die_uplinks_vom_nachbarsammler():
    nb = nachbarn()
    mc = SwitchMacs("c3po", "172.16.0.3", "x", nachbarn=nb, aufrufer=aufrufer())
    nb.sammeln()                                  # LLDP lief
    assert "gi25" in mc.uplinks


def test_ohne_vorherigen_lldp_lauf_meldet_die_mac_tabelle_nichts(bestand):
    """Lieber gar nichts als jedes Geraet hinter dem Uplink an den Uplink."""
    nb = nachbarn()
    mc = SwitchMacs("c3po", "172.16.0.3", "x", nachbarn=nb, aufrufer=aufrufer())
    e = durchlauf(bestand, mc, t(0))
    assert not e.erfolgreich and "Uplinks noch unbekannt" in e.fehler
    assert bestand.offene(Beziehung.ANSCHLUSS) == []


def test_und_danach_meldet_sie_wieder(bestand):
    nb = nachbarn()
    mc = SwitchMacs("c3po", "172.16.0.3", "x", nachbarn=nb, aufrufer=aufrufer())
    durchlauf(bestand, mc, t(0))                  # scheitert, wie es soll
    durchlauf(bestand, nb, t(1))                  # LLDP laeuft
    e = durchlauf(bestand, mc, t(2))
    assert e.erfolgreich and e.beobachtungen > 0
