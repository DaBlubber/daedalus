# -*- coding: utf-8 -*-
"""HTTP-Oberflaeche fuer den aktuellen Daedalus-Stand.

Die App bleibt absichtlich schmal: sie uebersetzt die vorhandenen Fachobjekte
in JSON und ueberlaesst Aufbau und Bewegung weiterhin dem abgenommenen HTML.
"""
from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Callable, Iterator

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from . import aktionen
from .kette import kette, wer_hing_hier
from .speicher import PgSpeicher
from .stand import bauen
from .zeit import anzeige, mit_zeitzone


HTML_PFAD = Path(__file__).with_name("web") / "index.html"
_STAND_RE = re.compile(
    r'(<script\s+id="stand"\s+type="application/json">).*?(</script>)',
    re.DOTALL,
)


class PflegeEingabe(BaseModel):
    """Nur die fachlich erlaubten, begrenzten Pflegefelder."""

    model_config = ConfigDict(extra="forbid")

    dose: str | None = Field(default=None, max_length=120)
    raum: str | None = Field(default=None, max_length=120)
    notiz: str | None = Field(default=None, max_length=2000)
    betreuer: str | None = Field(default=None, max_length=120)
    name: str | None = Field(default=None, max_length=120)
    erwartet: str | None = Field(default=None, max_length=300)


class AktionZiel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ziel: str = Field(max_length=15)


class AktionPort(AktionZiel):
    port: int


class AktionWol(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mac: str = Field(max_length=17)
    ip: str | None = Field(default=None, max_length=15)


def _json_wert(wert):
    """Fachobjekte fuer die Detailantwort verlustarm JSON-faehig machen."""
    if isinstance(wert, datetime):
        return anzeige(wert)
    if isinstance(wert, Enum):
        return wert.value
    if is_dataclass(wert):
        return {k: _json_wert(v) for k, v in asdict(wert).items()}
    if isinstance(wert, dict):
        return {k: _json_wert(v) for k, v in wert.items()}
    if isinstance(wert, (list, tuple)):
        return [_json_wert(v) for v in wert]
    return wert


def _pflege_wert(pflege) -> dict:
    if pflege is None:
        return {}
    if isinstance(pflege, dict):
        return _json_wert(pflege)
    return _json_wert(pflege)


def stand_einbetten(html: str, stand: dict) -> str:
    """Den Stand als Daten, nicht als ausfuehrbares JavaScript, einbetten.

    Das Escaping von ``</`` verhindert, dass Nutzdaten den script-Block vorzeitig
    schliessen. JSON-Sonderzeichen bleiben dabei unveraendert parsebar.
    """
    daten = json.dumps(stand, ensure_ascii=False, separators=(",", ":"))
    daten = daten.replace("</", "<\\/")
    # Eine Funktion als Ersatz ist wichtig: re.sub wuerde Backslashes aus dem
    # JSON sonst selbst als Gruppen- oder Escape-Syntax auswerten.
    ersetzt, anzahl = _STAND_RE.subn(
        lambda treffer: treffer.group(1) + daten + treffer.group(2), html, count=1
    )
    if anzahl != 1:
        raise RuntimeError("index.html enthaelt keinen eindeutigen Stand-Platzhalter")
    return ersetzt


def app_bauen(
    verbinden: Callable[[], object],
    speicher_bauen: Callable[[object], object] = PgSpeicher,
) -> FastAPI:
    """Eine testbare App mit injizierbarer Verbindung und Speicher bauen."""
    app = FastAPI(title="Daedalus")

    @contextmanager
    def speicher() -> Iterator[object]:
        # Eine Verbindung je Anfrage ist hier die robustere Wahl: nach einem
        # Proxy- oder DB-Abbruch kann keine tote Verbindung die naechste Anfrage
        # vergiften. Bei diesem kleinen internen Dienst ist der Verbindungsaufbau
        # billiger und durchschaubarer als ein eigener Pool mit Wiederbelebung.
        db = verbinden()
        try:
            yield speicher_bauen(db)
        finally:
            schliessen = getattr(db, "close", None)
            if callable(schliessen):
                schliessen()

    def aktuellen_stand(bestand) -> dict:
        from datetime import timedelta
        from .zeit import jetzt
        t = jetzt()
        return bauen(
            bestand,
            wurzel="bb8",
            pflege=bestand.pflege.alle(),
            sichtungen=bestand.sichtungen(),
            zeitpunkt=t,
            gesundheit=bestand.portgesundheit(t),
            wechsel=bestand.wechsel_seit("link_wechsel", t - timedelta(hours=24)),
        )

    @app.get("/", response_class=HTMLResponse)
    def startseite():
        with speicher() as bestand:
            inhalt = stand_einbetten(
                HTML_PFAD.read_text(encoding="utf-8"),
                aktuellen_stand(bestand),
            )
        return HTMLResponse(inhalt)

    @app.get("/api/stand")
    def api_stand():
        with speicher() as bestand:
            return aktuellen_stand(bestand)

    @app.get("/api/objekt/{schluessel:path}")
    def api_objekt(schluessel: str):
        with speicher() as bestand:
            weg = kette(bestand, schluessel, "bb8")
            glieder = [
                {
                    "art": glied.art,
                    "name": glied.name,
                    "port": glied.port,
                    "gemessen": glied.gemessen,
                }
                for glied in weg.glieder
            ]
            verlauf = [_json_wert(i) for i in bestand.verlauf(schluessel)]
            # MACs und Sonderkennungen enthalten ebenfalls Doppelpunkte. Nur
            # die Form "Knoten:Port" ist eine sinnvolle Rueckwaertsfrage.
            ist_port = schluessel.count(":") == 1 and not schluessel.startswith(
                ("ap:", "netz:")
            )
            hing_hier = []
            if ist_port:
                hing_hier = [
                    {"objekt": objekt, "ab": _json_wert(ab), "bis": _json_wert(bis)}
                    for objekt, ab, bis in wer_hing_hier(bestand, schluessel)
                ]
            return {
                "kette": glieder,
                "eindeutig": weg.eindeutig,
                "vollstaendig": weg.vollstaendig,
                "verlauf": verlauf,
                "hing_hier": hing_hier,
                "pflege": _pflege_wert(bestand.pflege.lesen(schluessel)),
            }

    @app.put("/api/pflege/{schluessel:path}")
    def api_pflege_schreiben(
        schluessel: str,
        eingabe: PflegeEingabe,
        bevorzugter_name: str | None = Header(
            default=None, alias="X-Forwarded-Preferred-Username"
        ),
        weitergeleiteter_name: str | None = Header(
            default=None, alias="X-Forwarded-User"
        ),
    ):
        with speicher() as bestand:
            pflege = bestand.pflege.schreiben(
                schluessel,
                von=bevorzugter_name or weitergeleiteter_name or "",
                **eingabe.model_dump(exclude_unset=True),
            )
            return _pflege_wert(pflege)

    # --- Aktionen ---------------------------------------------------------------
    # Nur POST mit JSON-Koerper: ein fremder Browser-Tab kann das nicht ohne
    # CORS-Vorabfrage absetzen, und die beantwortet dieser Dienst nicht.
    def _aktion(funktion, *args):
        try:
            return funktion(*args)
        except aktionen.Abgelehnt as e:
            raise HTTPException(status_code=422, detail=str(e)) from e

    @app.post("/api/aktion/ping")
    def aktion_ping(eingabe: AktionZiel):
        return _aktion(aktionen.ping, eingabe.ziel)

    @app.post("/api/aktion/traceroute")
    def aktion_traceroute(eingabe: AktionZiel):
        return _aktion(aktionen.traceroute, eingabe.ziel)

    @app.post("/api/aktion/port")
    def aktion_port(eingabe: AktionPort):
        return _aktion(aktionen.port_pruefen, eingabe.ziel, eingabe.port)

    @app.post("/api/aktion/wol")
    def aktion_wol(eingabe: AktionWol):
        return _aktion(aktionen.wake_on_lan, eingabe.mac, eingabe.ip)

    @app.post("/api/aktion/scan")
    def aktion_scan(eingabe: AktionZiel):
        return _aktion(aktionen.scan_starten, eingabe.ziel)

    @app.get("/api/aktion/scan/{kennung}")
    def aktion_scan_status(kennung: str):
        return _aktion(aktionen.scan_status, kennung)

    @app.delete("/api/aktion/scan/{kennung}")
    def aktion_scan_abbrechen(kennung: str):
        return _aktion(aktionen.scan_abbrechen, kennung)

    @app.get("/api/aktion/faehigkeiten")
    def aktion_faehigkeiten():
        # Die Oberflaeche zeigt den Scan-Knopf nur, wenn er auch darf.
        return {"scan": aktionen.scan_freigegeben()}

    @app.get("/gesund")
    def gesund():
        return {"ok": True}

    @app.get("/bereit")
    def bereit():
        try:
            db = verbinden()
            try:
                with db.cursor() as cursor:
                    cursor.execute("SELECT 1")
                    cursor.fetchone()
            finally:
                schliessen = getattr(db, "close", None)
                if callable(schliessen):
                    schliessen()
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Datenbank nicht bereit") from exc
        return {"ok": True}

    return app


def _verbinden():
    """Die Produktionsverbindung erst bei einer Anfrage aufbauen."""
    dsn = os.environ.get("DAEDALUS_DSN")
    if not dsn:
        raise RuntimeError("DAEDALUS_DSN ist nicht gesetzt")
    import psycopg

    return psycopg.connect(
        mit_zeitzone(dsn), autocommit=True, connect_timeout=8
    )


app = app_bauen(_verbinden)
