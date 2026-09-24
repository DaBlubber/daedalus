# -*- coding: utf-8 -*-
"""Aktionen aus der Detailkarte: Ping, Traceroute, Port pruefen, Wake-on-LAN, Portscan.

Pruefen ohne Kommandozeile (Plan Phase 6). Drei Regeln, die jede Aktion einhaelt:

1. **Nur Adressen, keine Namen, kein freier Text.** Das Ziel wird als IPv4-Adresse
   geparst und als eigenes Argument uebergeben — es gibt keine Stelle, an der
   Nutzereingabe in eine Kommandozeile geklebt wird.
2. **Nur eigene Netze.** Alles ausserhalb von 172.16.0.0/16 wird abgewiesen.
   Die Kundennetze sind fuer alles ausser Ping gesperrt (Plan R4:
   "Ping ja, Scan nein") — sie liegen ohnehin ausserhalb, die Sperre steht hier
   trotzdem ausdruecklich, damit eine spaetere Erweiterung des Bereichs sie
   nicht still aufhebt.
3. **Wenig auf einmal.** Hoechstens zwei Aktionen gleichzeitig, jede mit festem
   Zeitlimit. Ein Doppelklick soll kein Netz fluten.

Der Portscan (nmap) steht unten und ist ausgeschaltet, bis jemand ihn freigibt.
"""
from __future__ import annotations

import ipaddress
import socket
import subprocess
import threading
import time

EIGENE = (ipaddress.ip_network("172.16.0.0/16"),)
GESPERRT_AUSSER_PING = tuple(ipaddress.ip_network(n) for n in
                             ("192.168.100.0/24", "10.13.0.0/24", "10.14.0.0/24"))

_GLEICHZEITIG = threading.BoundedSemaphore(2)


class Abgelehnt(ValueError):
    """Eine Aktion, die nicht ausgefuehrt werden darf — mit Grund fuer den Menschen."""


def ziel_pruefen(ziel: str, aktion: str) -> ipaddress.IPv4Address:
    try:
        ip = ipaddress.IPv4Address(ziel.strip())
    except (ipaddress.AddressValueError, AttributeError):
        raise Abgelehnt("Ziel muss eine IPv4-Adresse sein") from None
    if aktion != "ping" and any(ip in n for n in GESPERRT_AUSSER_PING):
        raise Abgelehnt("Kundennetz — hier ist nur Ping erlaubt")
    if not any(ip in n for n in EIGENE) and not any(ip in n for n in GESPERRT_AUSSER_PING):
        raise Abgelehnt("nur Adressen aus den eigenen Netzen (172.16.0.0/16)")
    if ip.is_multicast or ip.is_unspecified or str(ip).endswith(".255"):
        raise Abgelehnt("keine Broadcast- oder Sammeladresse")
    return ip


def _ausfuehren(befehl: list[str], zeitlimit: int) -> dict:
    if not _GLEICHZEITIG.acquire(blocking=False):
        raise Abgelehnt("es laufen schon zwei Aktionen — kurz warten")
    begonnen = time.monotonic()
    try:
        e = subprocess.run(befehl, capture_output=True, text=True, timeout=zeitlimit)
        ausgabe, code = (e.stdout + e.stderr).strip(), e.returncode
    except subprocess.TimeoutExpired as t:
        ausgabe = ((t.stdout or b"").decode(errors="replace") if isinstance(t.stdout, bytes)
                   else (t.stdout or "")) + "\n… Zeitlimit erreicht"
        code = -1
    except FileNotFoundError:
        raise Abgelehnt(f"{befehl[0]} ist im Abbild nicht vorhanden") from None
    finally:
        _GLEICHZEITIG.release()
    return {"ok": code == 0, "code": code, "ausgabe": ausgabe[-4000:],
            "dauer_ms": int((time.monotonic() - begonnen) * 1000)}


def ping(ziel: str) -> dict:
    ip = ziel_pruefen(ziel, "ping")
    ergebnis = _ausfuehren(["ping", "-c", "4", "-i", "0.3", "-W", "1", str(ip)], 10)
    # Zusammenfassung fuer die Karte: "4/4 · 0,4 ms"
    zeilen = ergebnis["ausgabe"].splitlines()
    verlust = next((z for z in zeilen if "packet loss" in z), "")
    rtt = next((z for z in zeilen if "min/avg/max" in z), "")
    ergebnis["kurz"] = " · ".join(x for x in (verlust.strip(), rtt.split("=")[-1].strip()) if x)
    return ergebnis


def traceroute(ziel: str) -> dict:
    ip = ziel_pruefen(ziel, "traceroute")
    # -I (ICMP statt UDP): aus dem Container heraus kamen die UDP-Antworten der
    # Zwischenstationen nicht durch das NAT der Docker-Bruecke zurueck — jede
    # Station war ein Stern (gemessen am 17.09.2026). Mit ICMP antworten sie.
    return _ausfuehren(["traceroute", "-I", "-n", "-w", "1", "-q", "1", "-m", "12", str(ip)], 20)


def port_pruefen(ziel: str, port: int) -> dict:
    ip = ziel_pruefen(ziel, "port")
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise Abgelehnt("Port muss zwischen 1 und 65535 liegen")
    if not _GLEICHZEITIG.acquire(blocking=False):
        raise Abgelehnt("es laufen schon zwei Aktionen — kurz warten")
    begonnen = time.monotonic()
    try:
        with socket.create_connection((str(ip), port), timeout=2):
            ok, text = True, f"{ip}:{port} ist offen"
    except socket.timeout:
        ok, text = False, f"{ip}:{port} antwortet nicht (Zeitlimit — gefiltert oder Geraet aus)"
    except ConnectionRefusedError:
        ok, text = False, f"{ip}:{port} ist geschlossen (Verbindung abgelehnt)"
    except OSError as e:
        ok, text = False, f"{ip}:{port} nicht erreichbar: {e.strerror or e}"
    finally:
        _GLEICHZEITIG.release()
    return {"ok": ok, "ausgabe": text, "kurz": text,
            "dauer_ms": int((time.monotonic() - begonnen) * 1000)}


def wake_on_lan(mac: str, ip: str | None = None) -> dict:
    """Magisches Paket an das Geraet — zuerst per Unicast an seine letzte Adresse.

    Gemessen am 17.09.2026 mit einer Dokumentations-MAC und tcpdump auf host-01b8
    (VLAN 10) und host-8739 (Servernetz): **Broadcasts kamen nirgends an**, nicht
    einmal im eigenen Netz — sie verlassen die Docker-Bruecke des Containers
    nicht, und ein gerichteter Broadcast in ein fremdes VLAN wuerde die Sophos
    ohnehin verwerfen. **Unicast an die IP kam an**, ueber die VLAN-Grenze hinweg.

    Ein schlafendes Geraet erreicht das Paket, solange die Sophos seine MAC noch
    in der ARP-Tabelle hat; der Switch flutet es dann, auch wenn der Port die MAC
    schon vergessen hat. Ist der ARP-Eintrag abgelaufen, kommt nichts an —
    deshalb meldet diese Aktion "gesendet", nie "aufgeweckt".
    """
    teile = mac.lower().replace("-", ":").split(":")
    if len(teile) != 6 or not all(len(t) == 2 and all(c in "0123456789abcdef" for c in t)
                                  for t in teile):
        raise Abgelehnt("MAC im Format aa:bb:cc:dd:ee:ff")
    if not ip:
        raise Abgelehnt("ohne bekannte Adresse kommt das Paket nicht aus dem Container")
    adresse = ziel_pruefen(ip, "wol")
    netz = ipaddress.ip_network(f"{adresse}/24", strict=False)
    ziele = [str(adresse), str(netz.broadcast_address)]
    paket = b"\xff" * 6 + bytes.fromhex("".join(teile)) * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for z in ziele:
            try:
                s.sendto(paket, (z, 9))
            except OSError:
                pass                      # der Broadcast darf scheitern, Unicast zaehlt
    text = (f"magisches Paket an {adresse} (Unicast) gesendet — kommt an, solange die "
            f"Sophos das Geraet noch in ihrer ARP-Tabelle fuehrt; ob es aufwacht, meldet niemand")
    return {"ok": True, "ausgabe": text, "kurz": "gesendet"}


# --- Portscan (nmap) ------------------------------------------------------------
#
# Der heikelste Baustein, deshalb mit mehr Riegeln als die anderen:
#
# - **Aus, bis jemand ihn einschaltet** (`DAEDALUS_SCAN_FREIGABE=ja`). Ein Scan
#   aus dem Container laeuft ueber die Sophos, und deren Angriffserkennung sperrt
#   den scannenden Host — das waere der Nomad-Knoten, auf dem der Dienst gerade
#   laeuft (Plan Risiko R2). Die Ausnahme dort muss VOR dem ersten Scan stehen.
# - Genau ein Ziel, genau ein Scan gleichzeitig, keine Bereiche.
# - `-sT` (Verbindungsscan): braucht keine Sonderrechte und ist der hoeflichste.
# - Die 100 haeufigsten Ports, Zeitlimit 90 Sekunden je Host, abbrechbar.
# - Kundennetze: gesperrt (ziel_pruefen erlaubt dort nur Ping).

import os
import re
import tempfile
import uuid
import xml.etree.ElementTree as ET

_SCANS: dict[str, dict] = {}
_SCAN_SPERRE = threading.Lock()


def scan_freigegeben() -> bool:
    return os.environ.get("DAEDALUS_SCAN_FREIGABE", "").strip().lower() == "ja"


def _scan_laeuft() -> dict | None:
    return next((s for s in _SCANS.values() if s["zustand"] == "laeuft"), None)


def scan_starten(ziel: str) -> dict:
    if not scan_freigegeben():
        raise Abgelehnt("Portscan ist gesperrt, bis an der Sophos eine Ausnahme fuer den "
                        "scannenden Knoten steht (DAEDALUS_SCAN_FREIGABE)")
    ip = ziel_pruefen(ziel, "scan")
    with _SCAN_SPERRE:
        if _scan_laeuft():
            raise Abgelehnt("es laeuft schon ein Scan — erst abwarten oder abbrechen")
        kennung = uuid.uuid4().hex[:12]
        xml = tempfile.NamedTemporaryFile(prefix="scan-", suffix=".xml", delete=False)
        xml.close()
        befehl = ["nmap", "-sT", "-Pn", "--top-ports", "100", "-T3",
                  "--host-timeout", "90s", "--stats-every", "2s", "-oX", xml.name, str(ip)]
        try:
            prozess = subprocess.Popen(befehl, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True)
        except FileNotFoundError:
            raise Abgelehnt("nmap ist im Abbild nicht vorhanden") from None
        eintrag = {"id": kennung, "ziel": str(ip), "zustand": "laeuft", "fortschritt": 0,
                   "begonnen": time.monotonic(), "prozess": prozess, "xml": xml.name,
                   "ports": [], "meldung": ""}
        _SCANS[kennung] = eintrag
    threading.Thread(target=_scan_verfolgen, args=(eintrag,), daemon=True).start()
    return scan_status(kennung)


def _scan_verfolgen(eintrag: dict) -> None:
    prozess = eintrag["prozess"]
    for zeile in prozess.stdout:
        t = re.search(r"About ([\d.]+)% done", zeile)
        if t:
            eintrag["fortschritt"] = min(99, int(float(t.group(1))))
    prozess.wait()
    if eintrag["zustand"] == "abgebrochen":
        return
    try:
        wurzel = ET.parse(eintrag["xml"]).getroot()
        for p in wurzel.iter("port"):
            status = p.find("state")
            dienst = p.find("service")
            if status is not None and status.get("state") == "open":
                eintrag["ports"].append({
                    "port": int(p.get("portid")), "protokoll": p.get("protocol"),
                    "dienst": dienst.get("name") if dienst is not None else ""})
        eintrag["zustand"] = "fertig" if prozess.returncode == 0 else "fehler"
        if prozess.returncode != 0:
            eintrag["meldung"] = f"nmap endete mit Code {prozess.returncode}"
    except (ET.ParseError, OSError) as e:
        eintrag["zustand"], eintrag["meldung"] = "fehler", f"Ergebnis nicht lesbar: {e}"
    finally:
        eintrag["fortschritt"] = 100
        try:
            os.unlink(eintrag["xml"])
        except OSError:
            pass


def scan_status(kennung: str) -> dict:
    s = _SCANS.get(kennung)
    if not s:
        raise Abgelehnt("unbekannter Scan")
    return {"id": s["id"], "ziel": s["ziel"], "zustand": s["zustand"],
            "fortschritt": s["fortschritt"], "ports": s["ports"], "meldung": s["meldung"],
            "sekunden": int(time.monotonic() - s["begonnen"])}


def scan_abbrechen(kennung: str) -> dict:
    s = _SCANS.get(kennung)
    if not s:
        raise Abgelehnt("unbekannter Scan")
    if s["zustand"] == "laeuft":
        s["zustand"] = "abgebrochen"
        s["prozess"].terminate()
        try:
            s["prozess"].wait(timeout=5)
        except subprocess.TimeoutExpired:
            s["prozess"].kill()
    return scan_status(kennung)
