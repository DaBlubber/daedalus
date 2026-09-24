# -*- coding: utf-8 -*-
"""Der Stand fuer die Leinwand — an den Fallen, die der erste echte Bau zeigte.

Jeder Test hier steht fuer etwas, das am 16.09.2026 an echten Daten schiefging
oder schiefgegangen waere.
"""
from datetime import datetime, timezone

from daedalus.modell import Aenderung, Beziehung, Ereignis, Intervall
from daedalus.speicher import ImSpeicher
from daedalus.stand import OHNE_NETZ, SONSTIGE, bauen, netz_von, port_nummer

T = datetime(2026, 9, 16, 10, 0, tzinfo=timezone.utc)


def _offen(b, beziehung, objekt, wert, schluessel=None):
    b.oeffnen(Intervall(beziehung, objekt, schluessel or beziehung.value, wert, ab=T))


def _netz(b):
    """bb8 ist Wurzel; r2d2 haengt mit zwei Leitungen daran, l337 an r2d2.
    l337 wird von r2d2 so gemeldet, wie LLDP ihn nennt: `L337`."""
    for hier, dort in [("bb8:gi25", "r2d2:gi25"), ("bb8:gi26", "r2d2:gi26"),
                       ("r2d2:gi25", "bb8:gi25"), ("r2d2:gi26", "bb8:gi26"),
                       ("r2d2:gi10", "L337:gi25"), ("l337:gi25", "r2d2:gi10"),
                       ("r2d2:gi15", "Luke:44 2B 62 63 B3 51")]:
        _offen(b, Beziehung.VERBINDUNG, hier, dort)


def test_switch_in_zwei_schreibweisen_ist_ein_knoten():
    b = ImSpeicher()
    _netz(b)
    s = bauen(b, zeitpunkt=T)
    assert "L337" not in s["PORTS"]
    r2d2 = {p["p"]: p for p in s["PORTS"]["r2d2"]}
    assert r2d2["gi10"]["to"] == "sw:l337"
    assert [p["to"] for p in s["PORTS"]["l337"]] == ["up"]


def test_access_point_haengt_am_switchport():
    b = ImSpeicher()
    _netz(b)
    _offen(b, Beziehung.ANSCHLUSS, "c6:3c:d0:13:86:00", "ap:Luke")
    s = bauen(b, zeitpunkt=T)
    r2d2 = {p["p"]: p for p in s["PORTS"]["r2d2"]}
    assert r2d2["gi15"]["to"] == "ap:Luke"
    assert {"id": "ap:Luke", "label": "LUKE", "ip": "", "port": "r2d2:gi15"} in s["APS"]
    # Die MAC aus der LLDP-Port-Kennung benennt das Geraet und liefert die Adresse
    _offen(b, Beziehung.ADRESSE, "44:2b:62:63:b3:51", "172.16.1.101")
    s = bauen(b, zeitpunkt=T)
    luke = {d["id"]: d for d in s["DEV"]}["44:2b:62:63:b3:51"]
    assert (luke["label"], luke["namensquelle"]) == ("Luke", "LLDP")
    assert next(a for a in s["APS"] if a["id"] == "ap:Luke")["ip"] == "172.16.1.101"
    # Ein AP bekommt keine eigene Portleiste
    assert "Luke" not in s["PORTS"]


def test_zwei_leitungen_zum_selben_switch_sind_ein_buendel():
    b = ImSpeicher()
    _netz(b)
    s = bauen(b, zeitpunkt=T)
    zu_r2d2 = [p for p in s["PORTS"]["bb8"] if p["to"] == "sw:r2d2"]
    assert len(zu_r2d2) == 1
    assert zu_r2d2[0]["p"] == "gi25+gi26"
    assert zu_r2d2[0]["bundle"] == 2
    assert zu_r2d2[0]["fiber"] is True
    # Auf der Kindseite sind es Uplinks, kein zweiter Weg
    assert {p["to"] for p in s["PORTS"]["r2d2"] if p["p"] in ("gi25", "gi26")} == {"up"}


def test_geraet_setzt_sich_aus_allen_beziehungen_zusammen():
    b = ImSpeicher()
    _netz(b)
    mac = "64:28:aa:9e:1c:b2"
    _offen(b, Beziehung.ADRESSE, mac, "172.16.10.87")
    _offen(b, Beziehung.ADRESSE, mac, "172.30.32.1")
    _offen(b, Beziehung.ANSCHLUSS, mac, "l337:gi4")
    _offen(b, Beziehung.MERKMAL, mac, "Intel", schluessel="oui")
    s = bauen(b, zeitpunkt=T, pflege={mac: {"name": "device-08ef"}})
    g = next(d for d in s["DEV"] if d["id"] == mac)
    assert g["label"] == "device-08ef"          # Gepflegtes schlaegt Gemessenes
    assert g["ip"] == "172.16.10.87"            # bekanntes Netz vor Fremdadresse
    assert g["net"] == "netz:172.16.10.0/24"
    assert g["hersteller"] == "Intel"
    assert {p["p"]: p["to"] for p in s["PORTS"]["l337"]}["gi4"] == "dev"


def test_adressgruppen_bleiben_getrennt():
    assert netz_von("172.16.13.14") == "netz:172.16.13.0/24"
    assert netz_von("172.16.14.20") == SONSTIGE
    assert netz_von("") == OHNE_NETZ
    b = ImSpeicher()
    _offen(b, Beziehung.MERKMAL, "26:ad:17:a2:e9:42", "x", schluessel="name")
    s = bauen(b, zeitpunkt=T)
    ids = [n["id"] for n in s["NETS"]]
    assert OHNE_NETZ in ids and SONSTIGE not in ids


def test_portnummern():
    assert port_nummer("gi12") == 12
    assert port_nummer("Po1") is None


def test_aenderung_markiert_das_buendel_nicht_den_einzelport():
    b = ImSpeicher()
    _netz(b)
    mac = "0e:a9:d0:bf:9d:8c"
    _offen(b, Beziehung.ANSCHLUSS, mac, "r2d2:gi3")
    b.aenderung_merken(Aenderung(Ereignis.ERSTMALS_GESEHEN, mac, T,
                                 betrifft=(mac, "bb8:gi25")))
    s = bauen(b, zeitpunkt=T)
    marke, objekt, _wann, was, betrifft, kat = s["CHANGES"][0]
    assert kat == "port"
    assert (marke, objekt, was) == ("neu", mac, "erstmals gesehen")
    assert "bb8:gi25+gi26" in betrifft and "bb8:gi25" not in betrifft
    assert "r2d2:gi3" in betrifft


def test_namen_nach_vorrang_und_mit_herkunft():
    b = ImSpeicher()
    server, laptop, handy = "6c:44:8c:7e:4f:ed", "44:4f:91:f9:32:cc", "2a:70:66:44:08:90"
    _offen(b, Beziehung.ADRESSE, server, "172.16.1.6")
    _offen(b, Beziehung.MERKMAL, "host-59a8", "172.16.1.6", schluessel="inventar_adresse")
    _offen(b, Beziehung.MERKMAL, server, "server-dhcp", schluessel="dhcp_name")
    _offen(b, Beziehung.ADRESSE, laptop, "172.16.10.13")
    _offen(b, Beziehung.MERKMAL, laptop, "device-08ef", schluessel="dhcp_name")
    # UniFi meldet einen namenlosen Client mit seiner MAC als Namen
    _offen(b, Beziehung.MERKMAL, handy, handy, schluessel="name")
    s = bauen(b, zeitpunkt=T)
    g = {d["id"]: d for d in s["DEV"]}
    assert (g[server]["label"], g[server]["namensquelle"]) == ("host-59a8", "Inventar")
    assert g[server]["namen"] == {"Inventar": "host-59a8", "DHCP": "server-dhcp"}
    assert (g[laptop]["label"], g[laptop]["namensquelle"]) == ("device-08ef", "DHCP")
    assert (g[handy]["label"], g[handy]["namensquelle"]) == (handy, "")
    s = bauen(b, zeitpunkt=T, pflege={laptop: {"name": "Alexs Laptop"}})
    assert {d["id"]: d["label"] for d in s["DEV"]}[laptop] == "Alexs Laptop"


def test_dhcp_uebersicht_mit_funden():
    b = ImSpeicher()
    M = Beziehung.MERKMAL
    _offen(b, M, "netz:172.16.10.0/24", "172.16.10.150-172.16.10.248", schluessel="dhcp_pools")
    # Groesse weicht ab: Kea fuehrt GAST als /25, die Tabelle als /24
    _offen(b, M, "netz:172.16.12.0/25", "172.16.12.50-172.16.12.120", schluessel="dhcp_pools")
    handy, laptop, fest, weg = ("12:30:bd:6a:36:d2", "44:4f:91:f9:32:cc",
                                "4a:52:01:1f:df:2d", "66:60:fb:d0:45:8b")
    # Reserviert auf .33, gesehen aber mit .34
    _offen(b, M, handy, "172.16.10.33", schluessel="dhcp_reservierung")
    _offen(b, M, handy, "device-6807", schluessel="reservierung_name")
    _offen(b, Beziehung.ADRESSE, handy, "172.16.10.34")
    # Normaler Lease im Pool
    _offen(b, M, laptop, "172.16.10.160", schluessel="dhcp_lease")
    _offen(b, Beziehung.ADRESSE, laptop, "172.16.10.160")
    # Feste Adresse mitten im Pool, ohne Lease
    _offen(b, Beziehung.ADRESSE, fest, "172.16.10.200")
    # Reservierung fuer ein Geraet, das nie gesehen wurde: kein Geraet auf der Karte
    _offen(b, M, weg, "172.16.10.40", schluessel="dhcp_reservierung")

    s = bauen(b, zeitpunkt=T)
    assert weg not in {d["id"] for d in s["DEV"]}
    assert {d["id"]: d["label"] for d in s["DEV"]}[handy] == "device-6807"
    netz = {n["cidr"]: n for n in s["DHCP"]}
    intern = netz["172.16.10.0/24"]
    assert intern["label"] == "INTERN"
    assert intern["pools"] == [{"von": "172.16.10.150", "bis": "172.16.10.248",
                                "groesse": 99, "belegt": 1, "frei": 98}]
    arten = sorted(f["art"] for f in intern["funde"])
    assert arten == ["fest_im_pool", "reservierung_weicht_ab"]
    assert [e["mac"] for e in intern["eintraege"]] == [handy, weg, laptop]   # nach IP
    assert [f["art"] for f in netz["172.16.12.0/25"]["funde"]] == ["netzgroesse_weicht_ab"]


def test_aenderungen_kategorisiert_lesbar_und_ohne_altlast():
    from daedalus.stand import _beschreibung, _ohne_altlast, kategorie
    A = Aenderung
    E = Ereignis
    port = A(E.UMGEZOGEN, "m", T, "c3po:gi12", "c3po:gi13", quelle="fdb-c3po")
    ap = A(E.GETRENNT, "m", T, "ap:Luke", None, quelle="wlan")
    lease = A(E.MERKMAL_GEAENDERT, "m", T, "172.16.10.160", "172.16.10.161",
              quelle="kea", schluessel="dhcp_lease")
    ip = A(E.ADRESSE_DAZU, "m", T, None, "172.16.10.5", quelle="wlan")
    assert [kategorie(x) for x in (port, ap, lease, ip)] == ["port", "wlan", "dhcp", "ip"]
    assert _beschreibung(port) == "umgezogen: C3PO Port 12 → C3PO Port 13"
    assert _beschreibung(ap) == "getrennt von LUKE"
    assert _beschreibung(lease) == "DHCP-Lease: 172.16.10.160 → 172.16.10.161"

    t2 = datetime(2026, 9, 16, 11, 0, tzinfo=timezone.utc)
    altlast = [A(E.MERKMAL_GEAENDERT, "x", t2, "wlan0", None, quelle="wlan"),
               A(E.MERKMAL_GEAENDERT, "x", t2, None, "20", quelle="wlan"),
               A(E.UMGEZOGEN, "x", t2, "ap:Luke", None, quelle="wlan"),
               A(E.ADRESSE_WEG, "x", t2, "172.16.11.239", None, quelle="wlan"),
               A(E.VERSCHWUNDEN, "x", t2, "20", None, quelle="wlan")]
    assert [a.art for a in _ohne_altlast(altlast)] == [E.VERSCHWUNDEN]


def test_portzustand_switchnamen_und_funde():
    b = ImSpeicher()
    _netz(b)
    M = Beziehung.MERKMAL
    # r2d2 als Geraet unter seiner Management-Adresse
    _offen(b, Beziehung.ADRESSE, "80:f6:0f:9a:ba:dd", "172.16.0.2")
    # Buendel bb8 -> r2d2: eine Leitung down
    _offen(b, M, "bb8:gi25", "up", schluessel="link")
    _offen(b, M, "bb8:gi26", "down", schluessel="link")
    _offen(b, M, "bb8:gi25", "1000", schluessel="speed")
    # freier Port, der nur durch den Portzustand bekannt ist
    _offen(b, M, "bb8:gi9", "down", schluessel="link_zugang")
    # Doppelte Adresse und APIPA
    _offen(b, Beziehung.ADRESSE, "4a:52:01:1f:df:2d", "172.16.10.50")
    _offen(b, Beziehung.ADRESSE, "66:60:fb:d0:45:8b", "172.16.10.50")
    _offen(b, Beziehung.ADRESSE, "6e:eb:ed:58:76:e8", "169.254.78.106")

    s = bauen(b, zeitpunkt=T,
              gesundheit={"bb8:gi25": {"fehler_24h": 7}},
              wechsel={"bb8:gi26": 5})
    g = {d["id"]: d for d in s["DEV"]}
    assert (g["80:f6:0f:9a:ba:dd"]["label"], g["80:f6:0f:9a:ba:dd"]["namensquelle"]) \
        == ("R2D2", "Switch")
    bb8 = {p["p"]: p for p in s["PORTS"]["bb8"]}
    assert bb8["gi9"]["link"] == "down" and bb8["gi9"]["to"] is None
    buendel = bb8["gi25+gi26"]
    assert buendel["link"] == "teilweise"
    assert buendel["fehler_24h"] == 7 and buendel["wechsel_24h"] == 5
    assert [m["p"] for m in buendel["mitglieder"]] == ["gi25", "gi26"]
    texte = " | ".join(f["text"] for f in s["FUNDE"])
    assert "halb" in texte and "7 Fehler" in texte and "5-mal" in texte
    assert "172.16.10.50 wird von 2 Geraeten" in texte
    assert "169.254.78.106" in texte
    assert all(f["kategorie"] in ("ip", "port", "wlan", "dhcp") for f in s["FUNDE"])


def test_flattern_aus_der_zeit_vor_der_schwelle_wird_nicht_angezeigt():
    from datetime import timedelta
    from daedalus.stand import _ohne_altlast
    A, E = Aenderung, Ereignis
    t0 = datetime(2026, 9, 17, 1, 0, tzinfo=timezone.utc)
    reihe = [A(E.GETRENNT, "jetkvm", t0, "k2so:gi11", None, quelle="fdb-k2so"),
             A(E.WIEDER_DA, "jetkvm", t0 + timedelta(minutes=15), None, "k2so:gi11",
               quelle="fdb-k2so"),
             # echt abgezogen: kommt erst nach drei Stunden wieder
             A(E.GETRENNT, "drucker", t0, "c3po:gi14", None, quelle="fdb-c3po"),
             A(E.WIEDER_DA, "drucker", t0 + timedelta(hours=3), None, "c3po:gi14",
               quelle="fdb-c3po")]
    uebrig = [(a.objekt, a.art) for a in _ohne_altlast(reihe)]
    assert uebrig == [("drucker", E.GETRENNT), ("drucker", E.WIEDER_DA)]


def test_gleiche_adresse_unter_zwei_schluesseln_zaehlt_einmal():
    b = ImSpeicher()
    _offen(b, Beziehung.ADRESSE, "16:ed:ae:b0:fe:f3", "172.16.10.9", schluessel="adresse")
    _offen(b, Beziehung.ADRESSE, "16:ed:ae:b0:fe:f3", "172.16.10.9", schluessel="ip:172.16.10.9")
    s = bauen(b, zeitpunkt=T)
    assert {d["id"]: d["ips"] for d in s["DEV"]}["16:ed:ae:b0:fe:f3"] == ["172.16.10.9"]
    assert not any("172.16.10.9" in f["text"] for f in s["FUNDE"])


def test_link_local_neben_echter_adresse_ist_kein_fund():
    b = ImSpeicher()
    _offen(b, Beziehung.ADRESSE, "0c:85:db:07:df:09", "172.16.11.92", schluessel="ip:172.16.11.92")
    _offen(b, Beziehung.ADRESSE, "0c:85:db:07:df:09", "169.254.78.106",
           schluessel="ip:169.254.78.106")
    _offen(b, Beziehung.ADRESSE, "ce:3c:9f:0d:54:1c", "169.254.1.2", schluessel="ip:169.254.1.2")
    texte = [f["text"] for f in bauen(b, zeitpunkt=T)["FUNDE"]]
    assert not any("169.254.78.106" in t for t in texte)
    assert any("169.254.1.2" in t for t in texte)


def _pool(b):
    _offen(b, Beziehung.MERKMAL, "netz:172.16.11.0/24", "172.16.11.200-172.16.11.250",
           schluessel="dhcp_pools")


def test_pool_adresse_mit_frueherem_lease_ist_arp_rest_kein_fund():
    """19.09.2026: ein Amazon-Geraet mit vier alten Adressen aus dem Sophos-ARP —
    alle vier hatte Kea ihm frueher gegeben. Nur wer sich NIE bei Kea meldete,
    hat eine feste Adresse im Pool (das Tuya-Geraet auf .222)."""
    b = ImSpeicher()
    _pool(b)
    amazon, tuya = "fc:01:d2:73:29:6e", "9c:5a:b2:30:c3:fd"
    b.oeffnen(Intervall(Beziehung.MERKMAL, amazon, "dhcp_lease", "172.16.11.229",
                        ab=T, bis=T))
    _offen(b, Beziehung.ADRESSE, amazon, "172.16.11.229", schluessel="ip:172.16.11.229")
    _offen(b, Beziehung.ADRESSE, amazon, "172.16.11.247", schluessel="ip:172.16.11.247")
    _offen(b, Beziehung.ADRESSE, tuya, "172.16.11.222")
    s = bauen(b, zeitpunkt=T)
    texte = [f["text"] for f in s["FUNDE"]]
    assert not any(amazon in t for t in texte)
    assert any("172.16.11.222" in t for t in texte)
    assert {d["id"]: d["dhcp"]["jemals"] for d in s["DEV"]} == {amazon: True, tuya: False}


def test_link_local_mit_kea_lease_ist_kein_fund():
    """gisinotebook, 19.09.2026: Lease von Kea, aber die Sophos fuehrte noch die
    Link-Local-Adresse von vor drei Tagen."""
    b = ImSpeicher()
    _offen(b, Beziehung.MERKMAL, "14:9a:07:77:ea:ee", "172.16.10.217", schluessel="dhcp_lease")
    _offen(b, Beziehung.ADRESSE, "14:9a:07:77:ea:ee", "169.254.212.204",
           schluessel="ip:169.254.212.204")
    assert not bauen(b, zeitpunkt=T)["FUNDE"]


def test_als_erwartet_markierter_fund_steht_hinten_und_traegt_den_grund():
    """Die Nintendo Switch 2 an C3PO Port 15 flattert im Standby stuendlich."""
    from daedalus.stand import funde_sammeln
    ports = {"c3po": [{"p": "gi15", "bundle": 1, "wechsel_24h": 67},
                      {"p": "gi16", "bundle": 1, "wechsel_24h": 40}]}
    funde = funde_sammeln(ImSpeicher(), {}, ports, [], [], [],
                          {"c3po:gi15": {"erwartet": "Switch 2 im Standby"}})
    assert [(f["objekt"], f.get("erwartet")) for f in funde] ==         [("c3po:gi16", None), ("c3po:gi15", "Switch 2 im Standby")]
