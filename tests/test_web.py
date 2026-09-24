# -*- coding: utf-8 -*-
"""HTTP-Vertrag der Leinwand, ohne eine laufende Datenbank."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from daedalus.modell import Beziehung, Intervall
from daedalus.speicher import ImSpeicher
from daedalus.web import app_bauen


JETZT = datetime(2026, 9, 16, 10, 0, tzinfo=timezone.utc)


class PflegeDouble:
    def __init__(self) -> None:
        self.daten = {"c3po:gi12": {"raum": "Buero"}}
        self.schreibzugriffe = []

    def alle(self):
        return self.daten.copy()

    def lesen(self, schluessel):
        return self.daten.get(schluessel, {})

    def schreiben(self, schluessel, von="", **felder):
        self.schreibzugriffe.append((schluessel, von, felder))
        self.daten[schluessel] = {k: v for k, v in felder.items() if v}
        return self.daten[schluessel]


class BestandDouble(ImSpeicher):
    def __init__(self) -> None:
        super().__init__()
        self.pflege = PflegeDouble()

    def sichtungen(self):
        return {"aa:bb:cc:dd:ee:ff": (JETZT, JETZT)}


def _bestand() -> BestandDouble:
    bestand = BestandDouble()
    bestand.intervalle.extend([
        Intervall(Beziehung.ADRESSE, "aa:bb:cc:dd:ee:ff", "adresse",
                  "172.16.10.44", JETZT),
        Intervall(Beziehung.ANSCHLUSS, "aa:bb:cc:dd:ee:ff", "anschluss",
                  "c3po:gi12", JETZT),
        # Die Zeichenfolge beweist, dass Nutzdaten den JSON-script-Block nicht
        # beenden koennen und nach JSON.parse trotzdem unveraendert ankommen.
        Intervall(Beziehung.MERKMAL, "aa:bb:cc:dd:ee:ff", "name",
                  "Sensor </script> Labor", JETZT),
    ])
    return bestand


def _client():
    bestand = _bestand()
    app = app_bauen(lambda: bestand, speicher_bauen=lambda db: db)
    return TestClient(app), bestand


def test_startseite_bettet_parsebaren_und_sicheren_stand_ein():
    client, _ = _client()
    antwort = client.get("/")

    assert antwort.status_code == 200
    treffer = re.search(
        r'<script id="stand" type="application/json">(.*?)</script>',
        antwort.text,
        re.DOTALL,
    )
    assert treffer
    assert "<\\/script>" in treffer.group(1)
    stand = json.loads(treffer.group(1))
    assert stand["DEV"][0]["label"] == "Sensor </script> Labor"


def test_api_stand_hat_die_erwartete_form():
    client, _ = _client()
    stand = client.get("/api/stand").json()

    assert stand["wurzel"] == "bb8"
    assert stand["DEV"][0]["id"] == "aa:bb:cc:dd:ee:ff"
    assert {"NETS", "APS", "SWITCHES", "PORTS", "CHANGES", "PFLEGE"} <= set(stand)


def test_pflege_portschluessel_und_benutzer_kommen_korrekt_an():
    client, bestand = _client()
    antwort = client.put(
        "/api/pflege/c3po:gi12",
        json={"dose": "B2-14"},
        headers={"X-Forwarded-Preferred-Username": "franz"},
    )

    assert antwort.status_code == 200
    assert bestand.pflege.schreibzugriffe[-1] == (
        "c3po:gi12", "franz", {"dose": "B2-14"},
    )


def test_pflege_netzschluessel_mit_schraegstrich_kommt_korrekt_an():
    client, bestand = _client()
    antwort = client.put(
        "/api/pflege/netz:172.16.10.0/24",
        json={"notiz": "Intern"},
        headers={"X-Forwarded-User": "proxy-nutzer"},
    )

    assert antwort.status_code == 200
    assert bestand.pflege.schreibzugriffe[-1] == (
        "netz:172.16.10.0/24", "proxy-nutzer", {"notiz": "Intern"},
    )


def test_pflege_unbekanntes_feld_ist_ungueltig():
    client, _ = _client()
    antwort = client.put("/api/pflege/c3po:gi12", json={"farbe": "blau"})
    assert antwort.status_code == 422


def test_gesund_braucht_keine_datenbank():
    app = app_bauen(lambda: (_ for _ in ()).throw(RuntimeError("keine DB")))
    antwort = TestClient(app).get("/gesund")
    assert antwort.status_code == 200
    assert antwort.json() == {"ok": True}
