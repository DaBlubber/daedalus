# -*- coding: utf-8 -*-
"""Einen Sammellauf ausfuehren.

    python lauf.py            # alle Quellen, einmal
    python lauf.py --dienst   # dauerhaft, jede Quelle in ihrem Takt
    python lauf.py --trocken  # nur fragen, nichts schreiben
    python lauf.py --nur arp  # eine Quelle

Zugangsdaten kommen aus der Umgebung, nie aus dem Quelltext:

    DAEDALUS_DSN          postgresql://daedalus:...@172.16.1.5:5432/daedalus
    DAEDALUS_SNMP_SOPHOS  Gemeinschaft fuer 172.16.1.1
    DAEDALUS_SNMP_SWITCH  Gemeinschaft fuer die Switches
    DAEDALUS_SNMP_UEBER   optional: Knoten, ueber den SNMP laeuft (Entwicklung)
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

from daedalus import migration
from daedalus.dienst import Auftrag, Dienst, bericht
from daedalus.kette import kette, nachbarschaft
from daedalus.modell import Beziehung
from daedalus.sammler import runde
from daedalus.sammler_arp import SophosArp
from daedalus.sammler_kea import KeaKonfig, KeaLeases
from daedalus.sammler_portzustand import SwitchPortzustand
from daedalus.sammler_prometheus import PrometheusInventar, PrometheusWlan
from daedalus.sammler_switch import SwitchMacs, SwitchNachbarn
from daedalus.speicher import ImSpeicher, PgSpeicher
from daedalus.zeit import anzeige, jetzt, mit_zeitzone

SOPHOS = "172.16.1.1"
PROMETHEUS = "http://172.16.1.10:9090"
SWITCHES = {"bb8": "172.16.0.4", "r2d2": "172.16.0.2", "c3po": "172.16.0.3",
            "l337": "172.16.0.5", "k2so": "172.16.0.6"}
WURZEL = "bb8"          # der Switch an der Firewall

# Takt je Quelle in Sekunden (Plan Block 25.1). Die Quellen altern verschieden
# schnell — ein gemeinsamer Takt waere die teuerste aller Varianten.
TAKT = {
    "sophos-arp": 300,      # ARP altert in Minuten aus
    "inventar":   900,      # die Hostliste aendert sich selten
    "kea":        900,      # Leases leben Stunden, Namen noch laenger
    "dhcp-konfig": 900,     # bedingter GET, kostet unveraendert null Bytes
    "wlan":       300,      # Clients kommen und gehen
    "fdb-":       300,      # MAC-Tabelle, altert nach ~5 Minuten aus
    "lldp-":     1800,      # Nachbarschaft aendert sich nur beim Umstecken
    "port-":      300,      # Link, VLAN, PoE, Fehlerzaehler
}


def takt_fuer(name: str) -> int:
    for schluessel, sekunden in TAKT.items():
        if name == schluessel or name.startswith(schluessel):
            return sekunden
    return 600


def snmp_ueber_knoten(knoten: str):
    """SNMP ueber einen Lab-Knoten laufen lassen — **nur fuer die Entwicklung**.

    Auf den Lab-Knoten ist kein net-snmp installiert, und das soll auch so
    bleiben: im Betrieb bringt der Nomad-Job seine Werkzeuge im Abbild mit.
    Fuer einen Lauf von der Werkbank aus geht es ueber einen Wegwerf-Container.
    Langsam (jeder Aufruf startet einen Container), aber es aendert nichts am Wirt.
    """
    def aufrufen(ziel: str, gemeinschaft: str, oid: str) -> str:
        befehl = (f"docker run --rm --net=host alpine:3.20 sh -c "
                  f"'apk add --no-cache net-snmp-tools >/dev/null 2>&1; "
                  f"snmpbulkwalk -v2c -c \"{gemeinschaft}\" -Cr40 -OQn -t 6 -r 1 "
                  f"{ziel} {oid}'")
        e = subprocess.run(["ssh", "-o", "ConnectTimeout=20", "-o", "BatchMode=yes",
                            f"root@{knoten}", befehl],
                           capture_output=True, text=True, timeout=180)
        if e.returncode != 0:
            raise RuntimeError(f"{ziel}: {(e.stderr or 'keine Ausgabe').strip()[:160]}")
        return e.stdout
    return aufrufen


def sammler_bauen(args) -> list:
    sophos_gem = os.environ.get("DAEDALUS_SNMP_SOPHOS", "")
    switch_gem = os.environ.get("DAEDALUS_SNMP_SWITCH", "")
    ueber = os.environ.get("DAEDALUS_SNMP_UEBER", "")
    snmp = snmp_ueber_knoten(ueber) if ueber else None

    alle = []

    if sophos_gem:
        arp = SophosArp(SOPHOS, sophos_gem)
        if snmp:
            arp._aufrufen = lambda: snmp(SOPHOS, sophos_gem,
                                         "1.3.6.1.2.1.4.22.1.2")
        alle.append(arp)

    alle.append(PrometheusInventar(PROMETHEUS))
    alle.append(PrometheusWlan(PROMETHEUS))
    alle.append(KeaLeases())
    alle.append(KeaKonfig())

    if switch_gem:
        for name, ip in SWITCHES.items():
            nb = SwitchNachbarn(name, ip, switch_gem)
            # Die MAC-Tabelle braucht die Uplinkliste — sonst haengt an einem
            # Uplink das halbe Netz. Sie liest sie beim Nachbarsammler ab;
            # deshalb steht LLDP in der Liste immer VOR seiner MAC-Tabelle.
            mc = SwitchMacs(name, ip, switch_gem, nachbarn=nb)
            if snmp:
                nb._aufrufen = lambda oid, i=ip: snmp(i, switch_gem, oid)
                mc._aufrufen = lambda oid, i=ip: snmp(i, switch_gem, oid)
            # Portzustand nach der Nachbarschaft: er fragt, welche Ports Uplinks
            # sind, damit nur deren Linkwechsel gemeldet werden.
            pz = SwitchPortzustand(name, ip, switch_gem, nachbarn=nb)
            alle.append(nb)
            alle.append(mc)
            alle.append(pz)

    if args.nur:
        alle = [s for s in alle if args.nur in s.quelle.name]
    return alle


def quellen_eintragen(db, sammler) -> None:
    """Die Quellen muessen in der Datenbank stehen — `lauf` haengt per
    Fremdschluessel daran. Das ist Absicht: eine Beobachtung ohne bekannte
    Herkunft darf es nicht geben."""
    with db.cursor() as c:
        for s in sammler:
            q = s.quelle
            c.execute("""INSERT INTO quelle (name, zustaendig_fuer, fehlt_schwelle,
                                             takt_sekunden)
                         VALUES (%s,%s,%s,%s)
                         ON CONFLICT (name) DO UPDATE
                            SET zustaendig_fuer = EXCLUDED.zustaendig_fuer,
                                fehlt_schwelle  = EXCLUDED.fehlt_schwelle""",
                      (q.name, [b.value for b in q.zustaendig_fuer],
                       q.fehlt_schwelle, 300))


def als_dienst(bestand, sammler, db) -> int:
    """Dauerhaft laufen. Jede Quelle in ihrem Takt, nacheinander."""
    auftraege = [Auftrag(s, takt=takt_fuer(s.quelle.name)) for s in sammler]
    print(f"Dienst gestartet, {len(auftraege)} Quellen")
    for a in sorted(auftraege, key=lambda x: (x.takt, x.name)):
        print(f"  {a.name:<12} alle {int(a.takt // 60)} Minuten")
    print("\nAb jetzt wird nur noch gemeldet, was sich aendert.\n", flush=True)

    def melden(e, auftrag):
        zeile = bericht(e, auftrag)
        if zeile:
            print(zeile, flush=True)

    try:
        Dienst(auftraege).laufen(bestand, melden)
    except KeyboardInterrupt:
        print("\nDienst beendet.")
    finally:
        if db:
            db.close()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Einen Sammellauf ausfuehren")
    p.add_argument("--dienst", action="store_true",
                   help="dauerhaft laufen, jede Quelle in ihrem Takt")
    p.add_argument("--trocken", action="store_true",
                   help="nur fragen, nichts schreiben (Arbeitsspeicher)")
    p.add_argument("--nur", default="", help="nur Quellen, deren Name das enthaelt")
    args = p.parse_args()

    sammler = sammler_bauen(args)
    if not sammler:
        print("Keine Sammler — fehlen die Zugangsdaten in der Umgebung?")
        return 2

    dsn = os.environ.get("DAEDALUS_DSN", "")
    if args.trocken or not dsn:
        bestand, db = ImSpeicher(), None
        print("TROCKENLAUF — es wird nichts geschrieben\n")
    else:
        import psycopg
        db = psycopg.connect(mit_zeitzone(dsn), connect_timeout=8, autocommit=True)
        neu = migration.einspielen(db)
        if neu:
            print(f"Migrationen eingespielt: {', '.join(neu)}", flush=True)
        quellen_eintragen(db, sammler)
        bestand = PgSpeicher(db)

    # Zaehlerstaende gehen am Abgleich vorbei direkt in ihre Tabelle. Erst hier
    # verdrahtet, weil der Bestand erst jetzt feststeht (Datenbank oder Speicher).
    for s in sammler:
        if isinstance(s, SwitchPortzustand):
            s.zaehler_ablage = lambda staende, b=bestand: b.zaehler_merken(jetzt(), staende)

    if args.dienst:
        return als_dienst(bestand, sammler, db)

    zeitpunkt = jetzt()
    print(f"Sammellauf {anzeige(zeitpunkt)}  —  {len(sammler)} Quellen\n")

    # LLDP steht in der Liste direkt vor seiner MAC-Tabelle und laeuft deshalb
    # zuerst; ohne das kennt die MAC-Tabelle keine Uplinks und meldet lieber nichts.
    ergebnisse = runde(bestand, sammler, zeitpunkt)

    breit = max(len(e.quelle) for e in ergebnisse)
    for e in ergebnisse:
        print(f"  {e.quelle:<{breit}}  {str(e).split(': ', 1)[1]}")

    aenderungen = [a for e in ergebnisse for a in e.aenderungen]
    print(f"\n{sum(e.beobachtungen for e in ergebnisse)} Beobachtungen, "
          f"{len(aenderungen)} Aenderungen, "
          f"{sum(1 for e in ergebnisse if not e.erfolgreich)} Ausfaelle")

    for a in aenderungen[:15]:
        print(f"  {a.art.value:<18} {a.objekt}  {a.vorher or ''} -> {a.nachher or ''}")
    if len(aenderungen) > 15:
        print(f"  ... und {len(aenderungen) - 15} weitere")

    # Was wissen wir jetzt ueber die Verkabelung?
    graph = nachbarschaft(bestand)
    if graph:
        print("\nNachbarschaft:")
        for sw in sorted(graph):
            nachbarn = sorted({n for n, _, _ in graph[sw]})
            print(f"  {sw:<6} -> {', '.join(nachbarn)}")

    if db:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
