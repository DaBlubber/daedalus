# -*- coding: utf-8 -*-
"""Der Stand fuer die Leinwand: aus Intervallen wird ein Bild.

Die Oberflaeche ist der Prototyp aus Phase P — bewiesen, abgenommen, und
datengetrieben ueber eine Handvoll Tabellen (NETS, APS, SWITCHES, PORTS, DEV,
CHANGES, PFLEGE). Statt die Leinwand fuer echte Daten neu zu bauen, liefert
dieses Modul **genau diese Tabellen** aus dem Bestand. Damit bleibt die
Oberflaeche, die sich richtig anfuehlt, dieselbe; nur ihre Herkunft wechselt.

Was hier NICHT passiert: raten. Ein Geraet ohne Anschluss haengt nirgends in
der Topologie, sondern nur in seinem Netz. Ein Port ohne Beobachtung wird nicht
gezeichnet — die Karte zeigt, was gemessen ist, nicht was es am Geraet gibt.
"""
from __future__ import annotations

import ipaddress
from datetime import datetime, timedelta

from .kette import _knoten, _port, abstaende, nachbarschaft
from .modell import Beziehung, Ereignis, meldenswert
from .sammler_arp import mac_lesbar
from .zeit import jetzt, kurz

# --- Was keine Quelle liefert --------------------------------------------------
# Netze und VLANs nach `Netzwerk/IP-Uebersicht.md` (Stand 14.09.2026). Das
# VLAN des Management-Netzes steht dort nicht — deshalb `None` statt einer
# plausiblen Zahl. Die Netzgroessen sind an der Sophos gemessen
# (ipAdEntNetMask, 17.09.2026): alle /24 — auch VPN, das die Uebersicht als
# /28 fuehrte; Kea hatte recht.
NETZE = (
    ("172.16.0.0/24",  None, "MANAGEMENT"),
    ("172.16.1.0/24",  5,    "SERVER"),
    ("172.16.10.0/24", 10,   "INTERN"),
    ("172.16.11.0/24", 20,   "SMART"),
    ("172.16.12.0/24", 30,   "GAST"),
    ("172.16.13.0/24", 40,   "VPN"),
)
OHNE_NETZ = "netz:ohne"            # keine Adresse bekannt
SONSTIGE = "netz:sonstige"         # Adresse, aber in keinem gepflegten Netz

# Die Switches mit Modell (Plan Block 19). Die Portzahl folgt dem Modell:
# SG200-26 hat 24 Kupferports und 2 Kombiports, SG300-28 hat 24 und 4.
SWITCHES = {
    "bb8":  ("172.16.0.4", "SG200-26"),
    "r2d2": ("172.16.0.2", "SG300-28PP"),
    "c3po": ("172.16.0.3", "SG300-28"),
    "l337": ("172.16.0.5", "SG200-26"),
    "k2so": ("172.16.0.6", "SG200-26"),
}
KUPFER = 24

AENDERUNG_TAGE = 7

# Was der Portzustand-Sammler je Port liefert und die Karte zeigt
PORT_ANGABEN = frozenset({"link", "link_zugang", "speed", "duplex", "vlans", "pvid",
                          "stp", "poe"})
FLATTERN_AB = 4             # Linkwechsel in 24 Stunden, ab denen ein Port auffaellt

_MARKE = {
    Ereignis.ERSTMALS_GESEHEN: "neu",
    Ereignis.VERSCHWUNDEN: "weg",
}


def netz_von(ip: str) -> str:
    """Die Netzkennung einer Adresse, `netz:sonstige` oder `netz:ohne`."""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return OHNE_NETZ
    for cidr, _vlan, _name in NETZE:
        if a in ipaddress.ip_network(cidr):
            return f"netz:{cidr}"
    return SONSTIGE


def port_nummer(name: str) -> int | None:
    """`gi12` -> 12, `Po1` -> None (ein Buendel hat keine Frontblende)."""
    ziffern = "".join(ch for ch in name if ch.isdigit())
    if not ziffern or not name[:2].lower() in ("gi", "fa", "te"):
        return None
    return int(ziffern)


def _port_sortierung(p: dict) -> tuple:
    erste = str(p["p"]).split("+")[0]
    return (port_nummer(erste) or 10_000, erste)


def _berlin(t: datetime | None) -> str:
    return kurz(t) if t else ""


def bauen(bestand, *, wurzel: str = "bb8", pflege: dict | None = None,
          sichtungen: dict | None = None, zeitpunkt: datetime | None = None,
          gesundheit: dict | None = None, wechsel: dict | None = None) -> dict:
    """Den Stand aus dem Bestand bauen.

    `pflege`      {Schluessel: {dose, raum, notiz, ...}} — von Hand gepflegt
    `sichtungen`  {Schluessel: (erste_sicht, letzte_sicht)}
    `gesundheit`  {Port: {fehler_1h, fehler_24h, verworfen_24h, poe_w}} aus den Zaehlern
    `wechsel`     {Port: Linkwechsel in 24 Stunden}
    """
    zeitpunkt = zeitpunkt or jetzt()
    pflege = pflege or {}
    sichtungen = sichtungen or {}
    gesundheit = gesundheit or {}
    wechsel = wechsel or {}

    adressen = bestand.offene(Beziehung.ADRESSE)
    anschluesse = bestand.offene(Beziehung.ANSCHLUSS)
    merkmale = bestand.offene(Beziehung.MERKMAL)

    # --- Geraete: alles, was eine MAC ist -----------------------------------
    geraete: dict[str, dict] = {}

    def geraet(mac: str) -> dict:
        if mac not in geraete:
            erst, letzt = sichtungen.get(mac, (None, None))
            geraete[mac] = {
                "id": mac, "mac": mac, "label": mac, "typ": "", "hersteller": "",
                "ip": "", "ips": [], "net": OHNE_NETZ, "port": None, "ap": None,
                "state": "seen", "erst": _berlin(erst), "letzt": _berlin(letzt),
                "verkehr": "—", "vlan": "", "essid": "", "zufall": False,
                "fluechtig": False,
            }
        return geraete[mac]

    def ist_mac(s: str) -> bool:
        return s.count(":") == 5 and len(s) == 17

    for i in adressen:
        if not ist_mac(i.objekt):
            continue
        g = geraet(i.objekt)
        # Dieselbe Adresse kann unter zwei Schluesseln offen sein (zwei Quellen,
        # oder der Umstieg auf "ip:<adresse>"). Doppelt gezaehlt erzeugte das am
        # 17.09.2026 ueber 200 falsche Funde "von 2 Geraeten gemeldet".
        if i.wert not in g["ips"]:
            g["ips"].append(i.wert)
        if i.fehlt_seit:
            g["state"] = "quiet"
        g["fluechtig"] = g["fluechtig"] or i.fluechtig

    for i in anschluesse:
        if not ist_mac(i.objekt):
            continue
        g = geraet(i.objekt)
        if i.wert.startswith("ap:"):
            g["ap"] = i.wert
        else:
            g["port"] = i.wert
        if i.fehlt_seit:
            g["state"] = "quiet"

    # Namen aus dem Inventar haengen nicht an einer MAC, sondern an einer
    # Adresse: 172.16.1.6 ist host-59a8. Zugeordnet wird unten ueber die IP.
    inventar_namen = {i.wert: i.objekt for i in merkmale
                      if i.schluessel == "inventar_adresse"}
    namen: dict[str, dict[str, str]] = {}      # mac -> {quelle: name}

    # DHCP-Angaben erzeugen KEIN Geraet: eine Reservierung fuer ein Geraet, das
    # seit Monaten niemand gesehen hat, gehoert in die DHCP-Uebersicht, nicht
    # als "gesehen" auf die Karte.
    dhcp: dict[str, dict[str, str]] = {}       # mac -> {lease, reservierung, ...}
    netz_dhcp: dict[str, dict[str, str]] = {}  # netz:cidr -> {pools, router}
    for i in merkmale:
        if i.schluessel in ("dhcp_lease", "dhcp_reservierung", "reservierung_name",
                            "dhcp_name") and ist_mac(i.objekt):
            dhcp.setdefault(i.objekt, {})[i.schluessel] = i.wert
        elif i.schluessel in ("dhcp_pools", "dhcp_router"):
            netz_dhcp.setdefault(i.objekt, {})[i.schluessel] = i.wert

    # Wer sich je bei Kea gemeldet hat, spricht DHCP — auch wenn der Lease
    # gerade abgelaufen ist und die Sophos noch alte ARP-Eintraege fuehrt.
    lease_jemals = bestand.jemals("dhcp_lease")

    for mac, angaben in dhcp.items():
        if "reservierung_name" in angaben:
            namen.setdefault(mac, {})["Reservierung"] = angaben["reservierung_name"]
        if "dhcp_name" in angaben:
            namen.setdefault(mac, {})["DHCP"] = angaben["dhcp_name"]

    for i in merkmale:
        if not ist_mac(i.objekt) or i.schluessel in (
                "dhcp_lease", "dhcp_reservierung", "reservierung_name", "dhcp_name"):
            continue
        g = geraet(i.objekt)
        if i.schluessel == "name":
            namen.setdefault(i.objekt, {})["WLAN"] = i.wert
        elif i.schluessel == "oui":
            g["hersteller"] = i.wert
        elif i.schluessel in ("vlan", "essid"):
            g[i.schluessel] = i.wert
        elif i.schluessel == "zufalls_mac":
            g["zufall"] = True

    # Access Points melden sich per LLDP mit ihrer MAC als Port-Kennung
    # (`Luke:44 2B 62 63 B3 51`). Dieselbe MAC steht in der ARP-Tabelle mit der
    # Adresse des APs — so bekommen die APs Namen und Adresse, ohne dass eine
    # weitere Quelle gefragt werden muss.
    graph = nachbarschaft(bestand)
    ap_macs: dict[str, str] = {}                 # mac -> AP-Name
    for sw, nachbarn in graph.items():
        if sw not in SWITCHES:
            continue
        for nachbar, _p_hier, p_dort in nachbarn:
            mac = mac_lesbar(p_dort)
            if nachbar not in SWITCHES and len(mac) == 17:
                ap_macs[mac] = nachbar

    for g in geraete.values():
        # Mehrere Adressen: die aus einem bekannten Netz zuerst, dann sortiert.
        # Eine Adresse ausserhalb aller Netze ist meist ein zweites Bein.
        g["ips"].sort(key=lambda ip: (netz_von(ip) == SONSTIGE, ip))
        if g["ips"]:
            g["ip"] = g["ips"][0]
            g["net"] = netz_von(g["ip"])
        g["typ"] = "WLAN-Client" if g["ap"] else ("kabelgebunden" if g["port"] else "")

        # Wer benennt ein Geraet? Von Hand Gepflegtes schlaegt alles; danach das
        # Inventar, weil es fuer Server die verbindliche Liste ist; dann die
        # DHCP-Reservierung (ebenfalls von Hand gepflegt, nur anderswo); danach,
        # was das Geraet selbst meldet (UniFi, dann DHCP). DNS kommt nicht vor:
        # der Pi-hole kennt keine PTR-Eintraege (gemessen am 16.09.2026).
        # Ein Switch ist per SNMP unter seiner Adresse bekannt — dieselbe
        # Adresse steht in der ARP-Tabelle mit seiner MAC. Ohne diese Zuordnung
        # hiess 172.16.0.2 in der IP-Linse nur "80:f6:0f:9a:ba:dd" (16.09.2026).
        switch_hier = next((sw for sw, (sw_ip, _m) in SWITCHES.items()
                            if sw_ip in g["ips"]), None)
        if switch_hier:
            g["switch"] = switch_hier
            g["typ"] = "Switch"
        kandidaten = [("gepflegt", (pflege.get(g["id"]) or {}).get("name")),
                      ("Switch", switch_hier.upper() if switch_hier else None),
                      ("Inventar", next((inventar_namen[ip] for ip in g["ips"]
                                         if ip in inventar_namen), None)),
                      ("LLDP", ap_macs.get(g["id"])),
                      ("Reservierung", namen.get(g["id"], {}).get("Reservierung")),
                      ("WLAN", namen.get(g["id"], {}).get("WLAN")),
                      ("DHCP", namen.get(g["id"], {}).get("DHCP"))]
        g["namen"] = {q: n for q, n in kandidaten if n}
        g["namensquelle"] = ""
        for quelle, name in kandidaten:
            # UniFi meldet fuer namenlose Clients die MAC als Namen — das ist
            # keiner.
            if name and name != g["id"]:
                g["label"], g["namensquelle"] = name, quelle
                break
        angaben = dhcp.get(g["id"], {})
        g["dhcp"] = {"lease": angaben.get("dhcp_lease"),
                     "reservierung": angaben.get("dhcp_reservierung"),
                     # Hatte je einen Kea-Lease, auch einen laengst abgelaufenen.
                     "jemals": g["id"] in lease_jemals}

    # --- Netze ----------------------------------------------------------------
    belegt: dict[str, int] = {}
    for g in geraete.values():
        belegt[g["net"]] = belegt.get(g["net"], 0) + 1
    netze = []
    for cidr, vlan, name in NETZE:
        nid = f"netz:{cidr}"
        groesse = ipaddress.ip_network(cidr).num_addresses - 2
        netze.append({"id": nid, "vlan": vlan, "label": name, "cidr": cidr,
                      "frei": max(groesse - belegt.get(nid, 0), 0)})
    if belegt.get(SONSTIGE):
        netze.append({"id": SONSTIGE, "vlan": None, "label": "SONSTIGE",
                      "cidr": "", "frei": None})
    if belegt.get(OHNE_NETZ):
        netze.append({"id": OHNE_NETZ, "vlan": None, "label": "OHNE ADRESSE",
                      "cidr": "", "frei": None})

    dhcp_uebersicht = _dhcp(netz_dhcp, dhcp, geraete, netze)

    # --- Switches und Ports -----------------------------------------------------
    abstand, vorher = abstaende(graph, wurzel)

    # Wer ist Kind von wem? Nur der Baum der Breitensuche zeichnet eine Kante.
    # Jede weitere Verbindung (c3po haengt an bb8 UND an r2d2) ist real, wird
    # aber als Querverbindung am Port vermerkt statt als zweiter Weg gezeichnet.
    kind_ports: dict[str, dict[str, set]] = {}     # switch -> kind -> {ports}
    zum_elternteil: dict[str, set] = {}            # switch -> {eigene ports}
    for sw, eltern in vorher.items():
        if not eltern:
            continue
        el, port_am_kind, _p_el = eltern[0]
        zum_elternteil.setdefault(sw, set())
        for nachbar, p_hier, p_dort in graph.get(sw, []):
            if nachbar == el:
                zum_elternteil[sw].add(p_hier)
                kind_ports.setdefault(el, {}).setdefault(sw, set()).add(p_dort)

    ports: dict[str, dict[str, dict]] = {sw: {} for sw in SWITCHES}
    aps_am_kabel: dict[str, str] = {}              # ap:Luke -> r2d2:gi15

    def port(sw: str, name: str) -> dict:
        tabelle = ports.setdefault(sw, {})
        if name not in tabelle:
            nr = port_nummer(name)
            glas = nr is not None and nr > KUPFER
            tabelle[name] = {"p": name, "n": (nr - KUPFER) if glas else (nr if nr is not None else name),
                             "fiber": glas, "to": None, "bundle": 1, "speed": 1000}
            # "speed" kommt, wenn gemessen, als Text vom Sammler; hier als Zahl.
        return tabelle[name]

    for sw, nachbarn in graph.items():
        if sw not in SWITCHES:
            continue            # ein Access Point hat keine Portleiste
        for nachbar, p_hier, _p_dort in nachbarn:
            if not p_hier:
                continue
            eintrag = port(sw, p_hier)
            if nachbar not in SWITCHES:
                # Kein Switch, aber per LLDP gemeldet: ein Access Point. Damit
                # haengt das WLAN endlich an seinem Kabel — r2d2 Port 15 → Luke.
                eintrag["to"] = f"ap:{nachbar}"
                aps_am_kabel[f"ap:{nachbar}"] = f"{sw}:{p_hier}"
            elif p_hier in kind_ports.get(sw, {}).get(nachbar, set()):
                eintrag["to"] = f"sw:{nachbar}"
            elif p_hier in zum_elternteil.get(sw, set()):
                eintrag["to"] = "up"
            else:
                eintrag["to"] = "up"
                eintrag["quer"] = nachbar

    for g in geraete.values():
        if g["port"]:
            sw, pn = _knoten(g["port"]), _port(g["port"])
            eintrag = port(sw, pn)
            if eintrag["to"] is None:
                eintrag["to"] = "dev"

    # Zustand und Gesundheit je Port (sammler_portzustand). Damit erscheinen
    # auch die freien Ports — die Karte zeigt die ganze Blende, nicht nur, was
    # zufaellig eine MAC gemeldet hat.
    for i in merkmale:
        if i.schluessel not in PORT_ANGABEN or ":" not in i.objekt:
            continue
        sw, pn = i.objekt.split(":", 1)
        if sw not in SWITCHES or pn.lower().startswith("po"):
            continue
        eintrag = port(sw, pn)
        feld = "link" if i.schluessel == "link_zugang" else i.schluessel
        eintrag[feld] = i.wert
    for sw, tabelle in ports.items():
        for pn, eintrag in tabelle.items():
            werte = gesundheit.get(f"{sw}:{pn}", {})
            for feld in ("fehler_1h", "fehler_24h", "verworfen_24h", "poe_w"):
                if feld in werte:
                    eintrag[feld] = werte[feld]
            if f"{sw}:{pn}" in wechsel:
                eintrag["wechsel_24h"] = wechsel[f"{sw}:{pn}"]

    # Buendel: zwei Ports desselben Switches zum selben Kind werden EIN Eintrag,
    # sonst zeichnet die Karte zwei Wege, wo einer ist (Plan Block 19).
    port_liste: dict[str, list] = {}
    umbenannt: dict[str, str] = {}
    for sw, tabelle in ports.items():
        nach_ziel: dict[str, list] = {}
        rest = []
        for eintrag in tabelle.values():
            if str(eintrag["to"]).startswith("sw:"):
                nach_ziel.setdefault(eintrag["to"], []).append(eintrag)
            else:
                rest.append(eintrag)
        for ziel, gruppe in nach_ziel.items():
            if len(gruppe) == 1:
                rest.append(gruppe[0])
                continue
            gruppe.sort(key=_port_sortierung)
            p = "+".join(e["p"] for e in gruppe)
            for e in gruppe:
                umbenannt[f"{sw}:{e['p']}"] = f"{sw}:{p}"
            buendel = {"p": p, "n": "+".join(str(e["n"]) for e in gruppe),
                       "fiber": all(e["fiber"] for e in gruppe), "to": ziel,
                       "bundle": len(gruppe),
                       "speed": sum(_zahl(e.get("speed"), 1000) for e in gruppe),
                       "mitglieder": [dict(e) for e in gruppe]}
            links = {e.get("link") for e in gruppe if e.get("link")}
            if links:
                # Ein halbes Buendel ist der gefaehrlichste Zustand: es laeuft,
                # aber ohne Reserve — und niemand merkt es.
                buendel["link"] = "up" if links == {"up"} else (
                    "down" if "up" not in links else "teilweise")
            for feld in ("fehler_1h", "fehler_24h", "verworfen_24h", "wechsel_24h"):
                summe = [e[feld] for e in gruppe if feld in e]
                if summe:
                    buendel[feld] = sum(summe)
            rest.append(buendel)
        for e in rest:
            if e["bundle"] == 1:
                e["speed"] = _zahl(e.get("speed"), 1000)
        rest.sort(key=lambda e: (e["fiber"], _port_sortierung(e)))
        port_liste[sw] = rest

    # --- Access Points ----------------------------------------------------------
    aps = sorted({g["ap"] for g in geraete.values() if g["ap"]} | set(aps_am_kabel))
    ap_adressen = {f"ap:{name}": geraete[mac]["ip"]
                   for mac, name in ap_macs.items() if mac in geraete}
    for mac, name in ap_macs.items():
        # Der AP selbst als Geraet: sein Ort ist der Switchport aus LLDP. Vorher
        # stand in seiner Detailkarte "Ort unbekannt", obwohl die Karte ihn zeigte.
        if mac in geraete:
            kabel = aps_am_kabel.get(f"ap:{name}")
            geraete[mac]["ap_kabel"] = umbenannt.get(kabel, kabel)
            geraete[mac]["typ"] = "Access Point"
    ap_liste = [{"id": a, "label": a[3:].upper(), "ip": ap_adressen.get(a, ""),
                 "port": umbenannt.get(aps_am_kabel.get(a, ""), aps_am_kabel.get(a))}
                for a in aps]

    switch_liste = []
    for sw, (ip, modell) in SWITCHES.items():
        werte = gesundheit.get(sw, {})
        switch_liste.append({"id": sw, "label": sw.upper(), "modell": modell, "ip": ip,
                             "ports": 28 if modell.startswith("SG300") else 26,
                             "erreichbar": sw in abstand,
                             "poe_budget_w": werte.get("poe_budget_w"),
                             "poe_w": werte.get("poe_w")})

    # --- Aenderungen ------------------------------------------------------------
    aenderungen = []
    for a in reversed(_ohne_altlast(
            bestand.aenderungen_seit(zeitpunkt - timedelta(days=AENDERUNG_TAGE)))):
        if a.objekt not in geraete and not any(x in geraete for x in a.betrifft):
            continue
        # Was vor der Regel gespeichert wurde, bekommt sie beim Anzeigen.
        g_a = geraete.get(a.objekt)
        if a.art not in (Ereignis.ERSTMALS_GESEHEN, Ereignis.VERSCHWUNDEN) and \
                not meldenswert(a.art, a.vorher, a.nachher,
                                bool(g_a and (g_a["zufall"] or g_a["fluechtig"]
                                              or _zufall(a.objekt)))):
            continue
        marke = _MARKE.get(a.art, "um")
        betrifft = []
        for x in a.betrifft:
            if x == a.objekt:
                continue
            betrifft.append(umbenannt.get(x, x))
        g = geraete.get(a.objekt)
        if g:
            if g["port"]:
                betrifft.append(umbenannt.get(g["port"], g["port"]))
            if g["ap"]:
                betrifft.append(g["ap"])
            betrifft.append(g["net"])
        aenderungen.append([marke, a.objekt, kurz(a.zeitpunkt), _beschreibung(a),
                            sorted(set(betrifft)), kategorie(a)])

    return {
        "stand": zeitpunkt.isoformat(),
        "angezeigt": kurz(zeitpunkt),
        "wurzel": wurzel,
        "NETS": netze,
        "APS": ap_liste,
        "SWITCHES": switch_liste,
        "PORTS": port_liste,
        "DEV": sorted(geraete.values(), key=lambda g: (g["label"].lower(), g["id"])),
        "CHANGES": aenderungen,
        "PFLEGE": {umbenannt.get(k, k): v for k, v in pflege.items()},
        "DHCP": dhcp_uebersicht,
        "FUNDE": funde_sammeln(bestand, geraete, port_liste, switch_liste, netze,
                               dhcp_uebersicht, pflege),
    }


def _ip_zahl(ip: str) -> int:
    try:
        return int(ipaddress.ip_address(ip))
    except ValueError:
        return -1


def _dhcp(netz_dhcp: dict, dhcp: dict, geraete: dict, netze: list) -> list[dict]:
    """Die DHCP-Uebersicht je Netz — und das, was nicht zusammenpasst.

    Die Funde sind der eigentliche Wert (Plan Block 26, Widerspruchserkennung):
    eine Reservierung, deren Geraet mit einer anderen Adresse gesehen wird; eine
    feste Adresse mitten im Pool, die Kea jederzeit ein zweites Mal vergeben
    kann; ein Netz, dessen Groesse in Kea anders steht als in der Uebersicht.
    """
    bekannte_netze = {n["id"]: n for n in netze}
    aus = []
    for nid in sorted(netz_dhcp, key=lambda n: _ip_zahl(n[5:].split("/")[0])):
        cidr = nid[5:]
        try:
            netz = ipaddress.ip_network(cidr)
        except ValueError:
            continue
        pools = []
        for teil in (netz_dhcp[nid].get("dhcp_pools") or "").split(","):
            if "-" not in teil:
                continue
            von, bis = teil.split("-", 1)
            pools.append((_ip_zahl(von), _ip_zahl(bis), von, bis))

        def im_pool(ip: str) -> bool:
            z = _ip_zahl(ip)
            return any(a <= z <= b for a, b, _, _ in pools)

        def im_netz(ip: str) -> bool:
            try:
                return ipaddress.ip_address(ip) in netz
            except ValueError:
                return False

        eintraege, funde = [], []
        for mac, angaben in dhcp.items():
            lease, res = angaben.get("dhcp_lease"), angaben.get("dhcp_reservierung")
            if not (lease and im_netz(lease)) and not (res and im_netz(res)):
                continue
            g = geraete.get(mac)
            gesehen = list(g["ips"]) if g else []
            e = {"mac": mac, "label": g["label"] if g else
                 (angaben.get("reservierung_name") or angaben.get("dhcp_name") or mac),
                 "lease": lease, "reservierung": res, "gesehen": gesehen,
                 "bekannt": bool(g), "art": "reserviert" if res else "pool"}
            eintraege.append(e)
            if res and gesehen and res not in gesehen:
                funde.append({"art": "reservierung_weicht_ab", "mac": mac,
                              "text": f"{e['label']}: reserviert {res}, gesehen {', '.join(gesehen)}"})
            if lease and not res and pools and not im_pool(lease):
                funde.append({"art": "lease_ausserhalb_pool", "mac": mac,
                              "text": f"{e['label']}: Lease {lease} liegt ausserhalb des Pools"})

        # Feste Adressen im Pool: gesehen, im Pool, aber weder Lease noch Reservierung
        statisch = []
        mit_dhcp = {e["mac"] for e in eintraege}
        for g in geraete.values():
            # Hatte das Geraet je einen Kea-Lease, ist eine Pool-Adresse ohne
            # gueltigen Lease ein ARP-Rest der Sophos, keine feste Adresse
            # (19.09.2026: 32:48:d6… und ein Amazon-Geraet mit vier alten Adressen).
            if (g.get("dhcp") or {}).get("jemals"):
                continue
            for ip in g["ips"]:
                if not im_netz(ip) or g["id"] in mit_dhcp:
                    continue
                statisch.append({"mac": g["id"], "label": g["label"], "ip": ip,
                                 "im_pool": im_pool(ip)})
                if im_pool(ip):
                    # Vorsichtig formuliert: gemessen ist nur "im Pool, ohne
                    # Lease". Ob die Adresse fest eingetragen ist oder der Lease
                    # nur auf dem Primaerknoten fehlt, sagt keine Quelle.
                    funde.append({"art": "fest_im_pool", "mac": g["id"],
                                  "text": f"{g['label']}: {ip} liegt im Pool, Kea kennt dafuer keinen Lease"})

        pool_liste = []
        for a, b, von, bis in pools:
            belegt = sum(1 for e in eintraege if e["lease"] and a <= _ip_zahl(e["lease"]) <= b)
            pool_liste.append({"von": von, "bis": bis, "groesse": b - a + 1,
                               "belegt": belegt, "frei": max(b - a + 1 - belegt, 0)})

        uebersicht = bekannte_netze.get(nid)
        if uebersicht is None:
            # Gleiche Netzadresse, andere Groesse? Dann steht es in der
            # IP-Uebersicht anders als in Kea — sagen, nicht still waehlen.
            for n in netze:
                if n["cidr"] and n["cidr"].split("/")[0] == cidr.split("/")[0]:
                    funde.append({"art": "netzgroesse_weicht_ab", "mac": "",
                                  "text": f"Kea fuehrt {cidr}, die IP-Uebersicht {n['cidr']}"})
                    uebersicht = n

        eintraege.sort(key=lambda e: _ip_zahl(e["lease"] or e["reservierung"] or ""))
        statisch.sort(key=lambda s: _ip_zahl(s["ip"]))
        aus.append({"id": f"dhcp:{cidr}", "netz": uebersicht["id"] if uebersicht else nid,
                    "cidr": cidr, "label": uebersicht["label"] if uebersicht else cidr,
                    "vlan": uebersicht["vlan"] if uebersicht else None,
                    "router": netz_dhcp[nid].get("dhcp_router"),
                    "pools": pool_liste, "eintraege": eintraege, "statisch": statisch,
                    "funde": funde})
    return aus


# --- Aenderungen lesbar machen -----------------------------------------------

KATEGORIEN = {
    "port": "Port",          # MAC-Tabelle und LLDP: wo etwas steckt
    "ip": "IP",              # ARP: welche Adresse
    "wlan": "WLAN",          # UniFi: an welchem Access Point
    "dhcp": "DHCP",          # Kea: Leases, Reservierungen, Pools
    "inventar": "Inventar",  # Prometheus-Targets
}

_ANGABE = {
    "name": "Name", "vlan": "VLAN", "essid": "ssid-feef", "oui": "Hersteller",
    "dhcp_name": "DHCP-Name", "dhcp_lease": "DHCP-Lease",
    "dhcp_reservierung": "Reservierung", "reservierung_name": "Reservierungsname",
    "dhcp_pools": "Pool", "dhcp_router": "Router",
    "inventar_adresse": "Inventaradresse", "zufalls_mac": "Zufalls-MAC",
}


def kategorie(a) -> str:
    """Wovon handelt eine Aenderung? Abgeleitet aus Quelle und Art.

    Keine gespeicherte Spalte: die Quelle steht ohnehin an jeder Aenderung, und
    so gilt die Einteilung auch fuer alles, was vor ihrer Einfuehrung entstand.
    """
    q = a.quelle or ""
    if q in ("kea", "dhcp-konfig"):
        return "dhcp"
    if a.art in (Ereignis.ADRESSE_DAZU, Ereignis.ADRESSE_WEG) or q == "sophos-arp":
        return "ip"
    if q == "wlan":
        return "wlan"
    if q == "inventar":
        return "inventar"
    return "port"


def _ort(wert: str | None) -> str:
    """`c3po:gi12` -> `C3PO Port 12`, `ap:Luke` -> `LUKE`."""
    if not wert:
        return "?"
    if wert.startswith("ap:"):
        return wert[3:].upper()
    if ":" not in wert:
        return wert
    sw, port = wert.split(":", 1)
    nr = port_nummer(port)
    if nr is None:
        return f"{sw.upper()} {port}"
    if nr > KUPFER:
        return f"{sw.upper()} Fiber {nr - KUPFER}"
    return f"{sw.upper()} Port {nr}"


def _ohne_altlast(aenderungen: list) -> list:
    """Was vor dem Aufraeumen des Abgleichs (16.09.2026) gespeichert wurde,
    nicht noch einmal zeigen.

    Damals entstand fuer jede wegfallende oder neue Angabe eine eigene Zeile
    ("merkmal_geaendert — -> 20"), und ein Geraet, das ging, brachte neben
    "verschwunden" noch "Adresse weg" und "umgezogen x -> —" mit. Die Zeilen
    bleiben gespeichert, sie werden nur nicht mehr angezeigt — geloescht wird
    Historie nie.
    """
    weg = {(a.objekt, a.zeitpunkt) for a in aenderungen if a.art is Ereignis.VERSCHWUNDEN}
    aus = []
    for a in aenderungen:
        if a.art is Ereignis.MERKMAL_GEAENDERT and (a.vorher is None or a.nachher is None):
            continue
        if (a.objekt, a.zeitpunkt) in weg and a.art in (
                Ereignis.ADRESSE_WEG, Ereignis.UMGEZOGEN, Ereignis.GETRENNT):
            continue
        aus.append(a)
    return _ohne_flattern(aus)


GEHEN = (Ereignis.GETRENNT, Ereignis.ADRESSE_WEG, Ereignis.VERSCHWUNDEN, Ereignis.UMGEZOGEN)
KOMMEN = (Ereignis.WIEDER_DA, Ereignis.ADRESSE_DAZU)
FLATTERFENSTER = timedelta(hours=1)


def _ohne_flattern(aenderungen: list) -> list:
    """Weg und nach weniger als einer Stunde wieder da: kein Befund, sondern Aging.

    Bis zum 17.09.2026 galt ein Geraet nach zehn Minuten Stille als getrennt; die
    MAC-Tabelle vergisst stille Geraete aber schon nach fuenf. So entstanden ueber
    Nacht Hunderte Paare "getrennt / wieder da". Der Abgleich wartet inzwischen
    eine Stunde. Fuer das, was vorher gespeichert wurde, faellt hier jedes Paar
    weg, dessen Rueckkehr innerhalb der Stunde lag.
    """
    je_objekt: dict[str, list] = {}
    for a in aenderungen:
        je_objekt.setdefault(a.objekt, []).append(a)
    verworfen: set[int] = set()
    for reihe in je_objekt.values():
        reihe.sort(key=lambda a: a.zeitpunkt)
        for i, gehen in enumerate(reihe):
            if gehen.art not in GEHEN or gehen.art is Ereignis.UMGEZOGEN and gehen.nachher:
                continue
            rueckkehr = [k for k in reihe[i + 1:]
                         if k.art in KOMMEN and k.zeitpunkt - gehen.zeitpunkt <= FLATTERFENSTER]
            if rueckkehr:
                verworfen.add(id(gehen))
                # alles, was im selben Lauf zurueckkam (Adresse UND Anschluss)
                verworfen.update(id(k) for k in rueckkehr
                                 if k.zeitpunkt == rueckkehr[0].zeitpunkt)
        # Eine Adresse, die dieses Geraet schon einmal hatte, ist keine neue —
        # so sah das Hin und Her zwischen mehreren ARP-Eintraegen einer MAC aus.
        bekannt: set[str] = set()
        for a in reihe:
            if a.art is Ereignis.ADRESSE_DAZU and a.nachher in bekannt:
                verworfen.add(id(a))
            bekannt.update(x for x in (a.vorher, a.nachher) if x)
    return [a for a in aenderungen if id(a) not in verworfen]


def _beschreibung(a) -> str:
    """Eine Zeile fuer die Aenderungsliste, in der Sprache der Oberflaeche."""
    if a.art == Ereignis.ERSTMALS_GESEHEN:
        wo = _ort(a.nachher) if a.nachher and ":" in a.nachher else a.nachher
        return f"erstmals gesehen · {wo}" if wo else "erstmals gesehen"
    if a.art == Ereignis.VERSCHWUNDEN:
        wo = _ort(a.vorher) if a.vorher and ":" in a.vorher else a.vorher
        zuletzt = f" · zuletzt {wo}" if wo else ""
        return f"verschwunden{zuletzt} · Quelle war erreichbar"
    if a.art == Ereignis.WIEDER_DA:
        return f"wieder da · {_ort(a.nachher)}"
    if a.art == Ereignis.UMGEZOGEN:
        if a.nachher is None:                  # Altlast: "umgezogen x -> —"
            return f"getrennt von {_ort(a.vorher)}"
        return f"umgezogen: {_ort(a.vorher)} → {_ort(a.nachher)}"
    if a.art == Ereignis.GETRENNT:
        return f"getrennt von {_ort(a.vorher)}"
    if a.art == Ereignis.ADRESSE_DAZU:
        return f"neue Adresse {a.nachher or ''}".strip()
    if a.art == Ereignis.ADRESSE_WEG:
        return f"Adresse weg: {a.vorher or ''}".strip()
    angabe = _ANGABE.get(getattr(a, "schluessel", ""), "")
    if a.vorher and ":" in a.vorher and a.nachher and ":" in a.nachher and not angabe:
        return f"Verbindung: {_ort(a.vorher)} → {_ort(a.nachher)}"
    vorne = f"{angabe}: " if angabe else ""
    return f"{vorne}{a.vorher or '—'} → {a.nachher or '—'}"


def _zufall(mac: str) -> bool:
    try:
        return bool(int(mac.split(":")[0], 16) & 0b10) and mac.count(":") == 5
    except ValueError:
        return False


def _zahl(wert, ersatz: int) -> int:
    try:
        return int(wert)
    except (TypeError, ValueError):
        return ersatz


# --- Widersprueche (Plan Block 26) -----------------------------------------------

def funde_sammeln(bestand, geraete: dict, port_liste: dict, switch_liste: list,
                  netze: list, dhcp: list, pflege: dict | None = None) -> list[dict]:
    """Was nicht zusammenpasst — die eigentlichen Funde des Werkzeugs.

    Jeder Fund: {kategorie, schwere ('warn'|'info'), text, objekt, betrifft}.
    Die Kategorien sind dieselben wie bei den Veraenderungen, damit die
    Oberflaeche beide gleich filtern kann.
    """
    funde: list[dict] = []

    def fund(kategorie, schwere, text, objekt="", betrifft=()):
        funde.append({"kategorie": kategorie, "schwere": schwere, "text": text,
                      "objekt": objekt, "betrifft": sorted(set(betrifft) | ({objekt} - {""}))})

    # Doppelte Adressen
    je_ip: dict[str, list[str]] = {}
    for g in geraete.values():
        for ip in g["ips"]:
            je_ip.setdefault(ip, []).append(g["id"])
    for ip, macs in sorted(je_ip.items()):
        if len(macs) > 1:
            namen = {geraete[m]["label"] for m in macs}
            if len(namen) == 1 and not all(n == m for n, m in zip(namen, macs)):
                # Ein Host mit zwei Schnittstellen unter einer Adresse (host-59a8,
                # 16.09.2026) — erwaehnenswert, aber kein Konflikt.
                fund("ip", "info", f"{ip}: {next(iter(namen))} meldet sich mit "
                     f"{len(macs)} MAC-Adressen", macs[0], macs)
            else:
                fund("ip", "warn", f"{ip} wird von {len(macs)} Geraeten gemeldet: "
                     f"{', '.join(sorted(namen))}", macs[0], macs)

    for g in geraete.values():
        verbindlich = [ip for ip in g["ips"] if not ip.startswith("169.254.")]
        kea = g.get("dhcp") or {}
        for ip in g["ips"]:
            if not ip.startswith("169.254."):
                continue
            if verbindlich or kea.get("lease") or kea.get("jemals"):
                # device-9661 (17.09.2026) hatte seine reservierte 172.16.11.92 per DHCP
                # UND daneben eine Link-Local-Adresse — viele Gateways fuehren
                # die zusaetzlich. Und gisinotebook (19.09.2026) hatte einen
                # Kea-Lease, waehrend die Sophos die alte Link-Local-Adresse noch
                # tagelang im ARP fuehrte. In beiden Faellen waere „hat keine
                # DHCP-Adresse bekommen" schlicht falsch.
                continue
            fund("ip", "warn", f"{g['label']}: {ip} — hat keine DHCP-Adresse bekommen",
                 g["id"])
        # WLAN-VLAN gegen das Netz der Adresse
        if g["vlan"] and g["ip"]:
            netz = next((n for n in netze if n["id"] == g["net"]), None)
            if netz and netz["vlan"] is not None and str(netz["vlan"]) != str(g["vlan"]):
                fund("wlan", "warn",
                     f"{g['label']}: WLAN meldet VLAN {g['vlan']}, die Adresse {g['ip']} "
                     f"liegt aber in {netz['label']} (VLAN {netz['vlan']})", g["id"])

    # Ports: Fehler, Flattern, halbe Buendel, langsame Uplinks
    for sw, ports in port_liste.items():
        for e in ports:
            pid = f"{sw}:{e['p']}"
            ort = _ort(f"{sw}:{e['p'].split('+')[0]}") + ("+" if e["bundle"] > 1 else "")
            weiter = str(e.get("to") or "")
            if e.get("fehler_24h"):
                fund("port", "warn", f"{ort}: {e['fehler_24h']} Fehler in 24 Stunden", pid)
            if e.get("wechsel_24h", 0) >= FLATTERN_AB:
                fund("port", "warn", f"{ort}: Link hat {e['wechsel_24h']}-mal gewechselt (24 h)",
                     pid)
            if e.get("link") == "teilweise":
                fund("port", "warn", f"{ort}: Buendel laeuft nur halb — eine Leitung ist down",
                     pid)
            if weiter.startswith(("sw:", "ap:")) or weiter == "up":
                if e.get("duplex") == "halb":
                    fund("port", "warn", f"{ort}: Uplink im Halbduplex", pid)
                if e["bundle"] == 1 and str(e.get("speed")).isdigit() and \
                        e.get("link") == "up" and int(e["speed"]) < 1000:
                    fund("port", "warn", f"{ort}: Uplink nur mit {e['speed']} Mbit/s", pid)
                if e.get("stp") == "blockiert":
                    fund("port", "info",
                         f"{ort}: Spanning Tree blockiert — erwartet bei einem redundanten Weg",
                         pid)

    for sw in switch_liste:
        if sw.get("poe_budget_w") and sw.get("poe_w") is not None:
            anteil = sw["poe_w"] / sw["poe_budget_w"]
            if anteil >= 0.8:
                fund("port", "warn",
                     f"{sw['label']}: PoE bei {round(anteil * 100)} % "
                     f"({sw['poe_w']} von {sw['poe_budget_w']} W)", sw["id"])

    # Einseitiges LLDP: nur ein Switch sieht den anderen
    gemeldet = {(i.objekt, i.wert) for i in bestand.offene(Beziehung.VERBINDUNG)}
    for hier, dort in sorted(gemeldet):
        sw_h, sw_d = hier.split(":", 1)[0], dort.split(":", 1)[0].lower()
        if sw_h in SWITCHES and sw_d in SWITCHES and \
                not any(a.split(":", 1)[0] == sw_d and b.split(":", 1)[0].lower() == sw_h
                        for a, b in gemeldet):
            fund("port", "info", f"{_ort(hier)} sieht {sw_d.upper()}, aber nicht umgekehrt",
                 hier)

    # DHCP-Funde einsortieren
    for netz in dhcp:
        for f in netz.get("funde", []):
            fund("dhcp", "warn", f["text"], f.get("mac", ""))

    # Von Hand als erwartet markiert (Pflegefeld `erwartet`): der Fund bleibt
    # sichtbar, zaehlt aber nicht mehr und rutscht ans Ende — die Nintendo
    # Switch 2 an C3PO Port 15 flattert im Standby jede Stunde, und das ist so.
    pflege = pflege or {}
    for f in funde:
        grund = next(((pflege.get(o) or {}).get("erwartet") for o in [f["objekt"], *f["betrifft"]]
                      if (pflege.get(o) or {}).get("erwartet")), None)
        if grund:
            f["erwartet"] = grund

    rang = {"warn": 0, "info": 1}
    funde.sort(key=lambda f: ("erwartet" in f, rang[f["schwere"]], f["kategorie"], f["text"]))
    return funde
