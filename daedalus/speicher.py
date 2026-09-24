# -*- coding: utf-8 -*-
"""Wo der Bestand liegt.

`abgleich.py` kennt keine Datenbank. Es kennt nur die Handvoll Fragen und
Aenderungen, die in diesem Modul stehen. Das hat zwei Gruende: die Regeln lassen
sich ohne laufenden Server pruefen, und dieselben Tests laufen anschliessend
unveraendert gegen das echte PostgreSQL — womit bewiesen ist, dass Schema und
Regeln zusammenpassen und nicht nur je fuer sich funktionieren.
"""
from __future__ import annotations

from datetime import datetime
from typing import Protocol

from .modell import Aenderung, Beziehung, Intervall, Lauf


class Speicher(Protocol):
    """Die einzige Schnittstelle, die der Abgleich braucht."""

    def lauf_gesehen(self, quelle: str, zeitpunkt: datetime) -> bool: ...
    def lauf_merken(self, lauf: Lauf) -> None: ...
    def hatte_erfolgreichen_lauf(self, quelle: str, vor: datetime) -> bool: ...

    def offen_fuer(self, beziehung: Beziehung, objekt: str,
                   schluessel: str) -> Intervall | None: ...
    def offene_von(self, quelle: str,
                   beziehungen: frozenset[Beziehung]) -> list[Intervall]: ...
    def hat_offene(self, objekt: str) -> bool: ...
    def offene(self, beziehung: Beziehung) -> list[Intervall]: ...
    def anzahl_intervalle(self) -> int: ...
    def verlauf(self, objekt: str) -> list[Intervall]: ...
    def alle_mit_wert(self, beziehung: Beziehung, wert: str) -> list[Intervall]: ...

    def oeffnen(self, i: Intervall) -> None: ...
    def schliessen(self, i: Intervall, bis: datetime) -> None: ...
    def fehlt_setzen(self, i: Intervall, wert: int) -> None: ...

    def objekt_bekannt(self, objekt: str) -> bool: ...
    def objekt_merken(self, objekt: str, zeitpunkt: datetime) -> None: ...
    def gesehen_merken(self, objekte: set[str], zeitpunkt: datetime) -> None: ...
    def jemals(self, schluessel: str) -> set[str]: ...

    def aenderung_merken(self, a: Aenderung) -> None: ...
    def aenderungen_seit(self, seit: datetime) -> list[Aenderung]: ...

    def zaehler_merken(self, zeitpunkt: datetime, staende: list) -> None: ...
    def portgesundheit(self, jetzt: datetime) -> dict[str, dict]: ...
    def wechsel_seit(self, schluessel: str, seit: datetime) -> dict[str, int]: ...


# ===========================================================================
class ImSpeicher:
    """Im Arbeitsspeicher. Fuer die Regelpruefung und fuer Trockenlaeufe."""

    def __init__(self) -> None:
        self.intervalle: list[Intervall] = []
        self.aenderungen: list[Aenderung] = []
        self.laeufe: list[Lauf] = []
        self.bekannt: set[str] = set()
        self.zuletzt: dict[str, datetime] = {}   # Objekt -> letzte Sichtung
        self.zaehler: list[tuple] = []          # (zeitpunkt, Zaehlerstand)

    # --- Laeufe ---
    def lauf_gesehen(self, quelle, zeitpunkt):
        return any(l.quelle == quelle and l.zeitpunkt == zeitpunkt for l in self.laeufe)

    def lauf_merken(self, lauf):
        self.laeufe.append(lauf)

    def hatte_erfolgreichen_lauf(self, quelle, vor):
        return any(l.quelle == quelle and l.erfolgreich and l.zeitpunkt < vor
                   for l in self.laeufe)

    # --- Intervalle ---
    def offen_fuer(self, beziehung, objekt, schluessel):
        for i in self.intervalle:
            if (i.offen and i.beziehung is beziehung and i.objekt == objekt
                    and i.schluessel == schluessel):
                return i
        return None

    def offene_von(self, quelle, beziehungen):
        return [i for i in self.intervalle
                if i.offen and i.quelle == quelle and i.beziehung in beziehungen]

    def hat_offene(self, objekt):
        return any(i.offen and i.objekt == objekt for i in self.intervalle)

    def offene(self, beziehung):
        return [i for i in self.intervalle if i.offen and i.beziehung is beziehung]

    def anzahl_intervalle(self):
        return len(self.intervalle)

    def verlauf(self, objekt):
        """Alles, was je fuer dieses Objekt galt — juengstes zuerst."""
        return sorted([i for i in self.intervalle if i.objekt == objekt],
                      key=lambda i: i.ab, reverse=True)

    def alle_mit_wert(self, beziehung, wert):
        """Andersherum gefragt: was zeigte je auf diesen Wert?
        Das ist „wer hing frueher an diesem Port?"""
        return [i for i in self.intervalle
                if i.beziehung is beziehung and i.wert == wert]

    def oeffnen(self, i):
        self.intervalle.append(i)

    def schliessen(self, i, bis):
        i.bis = bis

    def fehlt_setzen(self, i, wert):
        i.fehlt_seit = wert

    # --- Objekte ---
    def objekt_bekannt(self, objekt):
        return objekt in self.bekannt

    def objekt_merken(self, objekt, zeitpunkt):
        self.bekannt.add(objekt)

    def gesehen_merken(self, objekte, zeitpunkt):
        for o in objekte:
            if o in self.bekannt and (o not in self.zuletzt or self.zuletzt[o] < zeitpunkt):
                self.zuletzt[o] = zeitpunkt

    def sichtungen(self):
        return {o: (None, t) for o, t in self.zuletzt.items()}

    def jemals(self, schluessel):
        """Welche Objekte hatten je eine Angabe unter diesem Schluessel?"""
        return {i.objekt for i in self.intervalle if i.schluessel == schluessel}

    # --- Aenderungen ---
    def aenderung_merken(self, a):
        self.aenderungen.append(a)

    def aenderungen_seit(self, seit):
        return [a for a in self.aenderungen if a.zeitpunkt >= seit]

    # --- Portzaehler ---
    def zaehler_merken(self, zeitpunkt, staende):
        self.zaehler.extend((zeitpunkt, z) for z in staende)

    def portgesundheit(self, jetzt):
        return gesundheit_rechnen(
            [(z.port, t, z.in_fehler, z.out_fehler, z.in_verworfen, z.out_verworfen,
              z.poe_mw, z.poe_budget_w) for t, z in self.zaehler], jetzt)

    def wechsel_seit(self, schluessel, seit):
        aus: dict[str, int] = {}
        for i in self.intervalle:
            if i.schluessel == schluessel and i.bis is not None and i.bis >= seit:
                aus[i.objekt] = aus.get(i.objekt, 0) + 1
        return aus


# ===========================================================================
class PgSpeicher:
    """PostgreSQL nach `schema.sql`.

    Die Aussenwelt spricht in Quellenschluesseln (eine MAC, `c3po:gi12`), die
    Datenbank in stabilen Objekt-Kennungen. Die Uebersetzung passiert hier und
    nur hier — damit bleibt der Weg zur spaeteren Identitaetsaufloesung offen,
    ohne dass die Regeln etwas davon mitbekommen.
    """

    def __init__(self, verbindung) -> None:
        self.db = verbindung
        self._ids: dict[str, str] = {}
        self._externe: dict[str, str] = {}   # Gegenrichtung zu _ids

    @property
    def pflege(self):
        """Die von Hand gepflegten Angaben zu denselben Objekten."""
        from .pflege import PflegeVerwaltung
        if not hasattr(self, '_pflege'):
            self._pflege = PflegeVerwaltung(self.db, self._id)
        return self._pflege

    # --- Uebersetzung Quellenschluessel <-> Objekt-Kennung ---
    def _id(self, extern: str, zeitpunkt: datetime | None = None,
            pflicht: bool = False) -> str:
        """Quellenschluessel zu Objekt-Kennung. `pflicht` legt das Objekt an,
        falls es noch keins gibt — das braucht die Pflege: eine Wanddose kann
        man eintragen, bevor je ein Geraet daran gesehen wurde."""
        if extern in self._ids:
            return self._ids[extern]
        art = 'schnittstelle' if ':' in extern else 'geraet'
        if pflicht and zeitpunkt is None:
            from .zeit import jetzt
            zeitpunkt = jetzt()
        with self.db.cursor() as c:
            c.execute("SELECT id FROM objekt WHERE extern = %s", (extern,))
            zeile = c.fetchone()
            if zeile is None:
                if zeitpunkt is None:
                    return ""          # gibt es nicht und soll nicht entstehen
                c.execute("""INSERT INTO objekt (art, extern, erste_sicht, letzte_sicht)
                             VALUES (%s, %s, %s, %s) RETURNING id""",
                          (art, extern, zeitpunkt, zeitpunkt))
                zeile = c.fetchone()
        self._ids[extern] = str(zeile[0])
        self._externe[str(zeile[0])] = extern
        return self._ids[extern]

    def sichtungen(self) -> dict[str, tuple]:
        """{Quellenschluessel: (erste_sicht, letzte_sicht)} — fuer die Anzeige."""
        with self.db.cursor() as c:
            c.execute("SELECT extern, erste_sicht, letzte_sicht FROM objekt "
                      "WHERE extern IS NOT NULL")
            return {z[0]: (z[1], z[2]) for z in c.fetchall()}

    def _extern(self, kennung: str) -> str:
        if kennung in self._externe:
            return self._externe[kennung]
        with self.db.cursor() as c:
            c.execute("SELECT extern FROM objekt WHERE id = %s", (kennung,))
            z = c.fetchone()
        if not z:
            return kennung
        self._externe[kennung] = z[0]
        self._ids.setdefault(z[0], kennung)
        return z[0]

    # --- Laeufe ---
    def lauf_gesehen(self, quelle, zeitpunkt):
        with self.db.cursor() as c:
            c.execute("SELECT 1 FROM lauf WHERE quelle=%s AND zeitpunkt=%s",
                      (quelle, zeitpunkt))
            return c.fetchone() is not None

    def lauf_merken(self, lauf):
        with self.db.cursor() as c:
            c.execute("""INSERT INTO lauf (quelle, zeitpunkt, erfolgreich)
                         VALUES (%s,%s,%s) ON CONFLICT DO NOTHING""",
                      (lauf.quelle, lauf.zeitpunkt, lauf.erfolgreich))

    def hatte_erfolgreichen_lauf(self, quelle, vor):
        with self.db.cursor() as c:
            c.execute("""SELECT 1 FROM lauf
                          WHERE quelle=%s AND erfolgreich AND zeitpunkt < %s LIMIT 1""",
                      (quelle, vor))
            return c.fetchone() is not None

    # --- Intervalle ---
    def _zu_intervall(self, z) -> Intervall:
        # Die letzte Spalte ist der Quellenschluessel, per Unterabfrage gleich
        # mitgeliefert. Vorher wurde er je Zeile einzeln nachgeschlagen — beim
        # Bau der Karte waren das 700 Abfragen und vier Sekunden.
        kennung, extern = str(z[2]), z[10]
        if extern:
            self._ids.setdefault(extern, kennung)
            self._externe[kennung] = extern
        i = Intervall(beziehung=Beziehung(z[1]), objekt=extern or self._extern(kennung),
                      schluessel=z[3], wert=z[4], ab=z[5], bis=z[6],
                      quelle=z[7], fehlt_seit=z[8], fluechtig=bool(z[9]))
        i.db_id = z[0]                       # type: ignore[attr-defined]
        return i

    _SPALTEN = ("id, beziehung, objekt, schluessel, wert, ab, bis, quelle, "
                "fehlt_seit, fluechtig, "
                "(SELECT o.extern FROM objekt o WHERE o.id = zuordnung.objekt)")

    def offen_fuer(self, beziehung, objekt, schluessel):
        kennung = self._id(objekt)
        if not kennung:
            return None
        with self.db.cursor() as c:
            c.execute(f"""SELECT {self._SPALTEN} FROM zuordnung
                           WHERE bis IS NULL AND beziehung=%s AND objekt=%s AND schluessel=%s""",
                      (beziehung.value, kennung, schluessel))
            z = c.fetchone()
        return self._zu_intervall(z) if z else None

    def offene_von(self, quelle, beziehungen):
        with self.db.cursor() as c:
            c.execute(f"""SELECT {self._SPALTEN} FROM zuordnung
                           WHERE bis IS NULL AND quelle=%s AND beziehung = ANY(%s)""",
                      (quelle, [b.value for b in beziehungen]))
            return [self._zu_intervall(z) for z in c.fetchall()]

    def hat_offene(self, objekt):
        kennung = self._id(objekt)
        with self.db.cursor() as c:
            c.execute("SELECT 1 FROM zuordnung WHERE bis IS NULL AND objekt=%s LIMIT 1",
                      (kennung,))
            return c.fetchone() is not None

    def offene(self, beziehung):
        with self.db.cursor() as c:
            c.execute(f"""SELECT {self._SPALTEN} FROM zuordnung
                           WHERE bis IS NULL AND beziehung=%s""", (beziehung.value,))
            return [self._zu_intervall(z) for z in c.fetchall()]

    def anzahl_intervalle(self):
        with self.db.cursor() as c:
            c.execute("SELECT count(*) FROM zuordnung")
            return c.fetchone()[0]

    def gesehen_merken(self, objekte, zeitpunkt):
        """Die letzte Sichtung fortschreiben — bei JEDEM Lauf, der das Objekt meldet.

        Bis 19.09.2026 wurde `letzte_sicht` nur beim Oeffnen eines Intervalls
        gesetzt. Ein Geraet, an dem sich nichts aenderte, stand dadurch als
        „zuletzt Do 06:29" da, obwohl es gerade eben gemeldet hatte (host-59a8 und
        160 andere). Ein einziges UPDATE je Lauf, nicht eines je Objekt.
        """
        if not objekte:
            return
        with self.db.cursor() as c:
            c.execute("""UPDATE objekt SET letzte_sicht = %s
                          WHERE extern = ANY(%s)
                            AND (letzte_sicht IS NULL OR letzte_sicht < %s)""",
                      (zeitpunkt, sorted(objekte), zeitpunkt))

    def jemals(self, schluessel):
        """Welche Objekte hatten je eine Angabe unter diesem Schluessel — offen
        oder laengst beendet: hat sich dieses Geraet je per DHCP gemeldet?"""
        with self.db.cursor() as c:
            c.execute("""SELECT DISTINCT o.extern FROM zuordnung z
                            JOIN objekt o ON o.id = z.objekt
                           WHERE z.schluessel = %s""", (schluessel,))
            return {z[0] for z in c.fetchall()}

    def verlauf(self, objekt):
        kennung = self._id(objekt)
        if not kennung:
            return []
        with self.db.cursor() as c:
            c.execute(f"""SELECT {self._SPALTEN} FROM zuordnung
                           WHERE objekt=%s ORDER BY ab DESC""", (kennung,))
            return [self._zu_intervall(z) for z in c.fetchall()]

    def alle_mit_wert(self, beziehung, wert):
        with self.db.cursor() as c:
            c.execute(f"""SELECT {self._SPALTEN} FROM zuordnung
                           WHERE beziehung=%s AND wert=%s ORDER BY ab DESC""",
                      (beziehung.value, wert))
            return [self._zu_intervall(z) for z in c.fetchall()]

    def oeffnen(self, i):
        kennung = self._id(i.objekt, i.ab)
        with self.db.cursor() as c:
            c.execute("""INSERT INTO zuordnung
                         (beziehung, objekt, schluessel, wert, ab, quelle,
                          fehlt_seit, fluechtig)
                         VALUES (%s,%s,%s,%s,%s,%s,0,%s) RETURNING id""",
                      (i.beziehung.value, kennung, i.schluessel, i.wert, i.ab,
                       i.quelle, i.fluechtig))
            i.db_id = c.fetchone()[0]        # type: ignore[attr-defined]
            c.execute("UPDATE objekt SET letzte_sicht=%s WHERE id=%s", (i.ab, kennung))

    def schliessen(self, i, bis):
        i.bis = bis
        with self.db.cursor() as c:
            c.execute("UPDATE zuordnung SET bis=%s WHERE id=%s", (bis, i.db_id))

    def fehlt_setzen(self, i, wert):
        i.fehlt_seit = wert
        with self.db.cursor() as c:
            c.execute("UPDATE zuordnung SET fehlt_seit=%s WHERE id=%s", (wert, i.db_id))

    # --- Objekte ---
    def objekt_bekannt(self, objekt):
        return bool(self._id(objekt))

    def objekt_merken(self, objekt, zeitpunkt):
        self._id(objekt, zeitpunkt)

    # --- Aenderungen ---
    def aenderung_merken(self, a):
        with self.db.cursor() as c:
            c.execute("""INSERT INTO aenderung
                         (art, objekt, zeitpunkt, vorher, nachher, quelle, schluessel, betrifft)
                         VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                      (a.art.value, self._id(a.objekt, a.zeitpunkt), a.zeitpunkt,
                       a.vorher, a.nachher, a.quelle, a.schluessel or None,
                       [self._id(x, a.zeitpunkt) for x in (a.betrifft or (a.objekt,))]))

    # --- Portzaehler ---
    def zaehler_merken(self, zeitpunkt, staende):
        """Zaehlerstaende ablegen und alles aelter als 48 Stunden verwerfen."""
        with self.db.cursor() as c:
            c.executemany(
                """INSERT INTO portzaehler (port, zeitpunkt, in_fehler, out_fehler,
                          in_verworfen, out_verworfen, poe_mw, poe_budget_w)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                [(z.port, zeitpunkt, z.in_fehler, z.out_fehler, z.in_verworfen,
                  z.out_verworfen, z.poe_mw, z.poe_budget_w) for z in staende])
            c.execute("DELETE FROM portzaehler WHERE zeitpunkt < %s - interval '48 hours'",
                      (zeitpunkt,))

    def portgesundheit(self, jetzt):
        from datetime import timedelta
        with self.db.cursor() as c:
            c.execute("""SELECT port, zeitpunkt, in_fehler, out_fehler, in_verworfen,
                                out_verworfen, poe_mw, poe_budget_w
                           FROM portzaehler WHERE zeitpunkt >= %s""",
                      (jetzt - timedelta(hours=25),))
            return gesundheit_rechnen(c.fetchall(), jetzt)

    def wechsel_seit(self, schluessel, seit):
        """Wie oft hat eine Angabe seit `seit` gewechselt? Je Objekt.

        Gezaehlt werden Intervalle, die im Fenster ENDETEN — jedes Ende ist ein
        Wechsel. Wer die begonnenen zaehlt, zaehlt auch den ersten Lauf mit."""
        with self.db.cursor() as c:
            c.execute("""SELECT o.extern, count(*) FROM zuordnung z
                           JOIN objekt o ON o.id = z.objekt
                          WHERE z.schluessel = %s AND z.bis >= %s
                          GROUP BY o.extern""", (schluessel, seit))
            return {z[0]: z[1] for z in c.fetchall()}

    def aenderungen_seit(self, seit):
        from .modell import Ereignis
        with self.db.cursor() as c:
            c.execute("""SELECT a.art, o.extern, a.zeitpunkt, a.vorher, a.nachher, a.quelle,
                                a.schluessel,
                                ARRAY(SELECT b.extern FROM objekt b
                                       WHERE b.id = ANY(a.betrifft))
                           FROM aenderung a JOIN objekt o ON o.id = a.objekt
                          WHERE a.zeitpunkt >= %s ORDER BY a.id""", (seit,))
            # Quellenschluessel per Verbund statt je Zeile nachgeschlagen —
            # dieselbe Falle wie bei den Intervallen (4 s beim Kartenbau).
            return [Aenderung(art=Ereignis(z[0]), objekt=z[1], zeitpunkt=z[2],
                              vorher=z[3], nachher=z[4], quelle=z[5],
                              schluessel=z[6] or "", betrifft=tuple(z[7] or ()))
                    for z in c.fetchall()]


def gesundheit_rechnen(zeilen, jetzt) -> dict[str, dict]:
    """Raten aus Zaehlerstaenden: Fehler in der letzten Stunde und den letzten
    24 Stunden, dazu die juengste PoE-Leistung.

    Ein Zaehler, der kleiner wird, ist nach einem Neustart des Switches wieder
    bei null angefangen. Dann gilt der juengste Stand selbst als Zuwachs — lieber
    etwas zu viel Fehler zeigen als einen echten Fehler verschlucken.
    """
    from datetime import timedelta
    je_port: dict[str, list] = {}
    for z in zeilen:
        je_port.setdefault(z[0], []).append(z)
    aus: dict[str, dict] = {}
    for port, reihe in je_port.items():
        reihe.sort(key=lambda z: z[1])
        neu = reihe[-1]

        def zuwachs(spalte: int, fenster: timedelta) -> int | None:
            alt = next((z for z in reihe if z[1] >= jetzt - fenster), None)
            if alt is None or neu[spalte] is None or alt[spalte] is None or alt is neu:
                return None
            d = neu[spalte] - alt[spalte]
            return neu[spalte] if d < 0 else d

        stunde, tag = timedelta(hours=1), timedelta(hours=24)
        werte = {
            "fehler_1h": _summe(zuwachs(2, stunde), zuwachs(3, stunde)),
            "fehler_24h": _summe(zuwachs(2, tag), zuwachs(3, tag)),
            "verworfen_24h": _summe(zuwachs(4, tag), zuwachs(5, tag)),
            "poe_w": round(neu[6] / 1000, 1) if neu[6] else None,
            "poe_budget_w": neu[7],
            "gemessen_seit": reihe[0][1],
        }
        aus[port] = {k: v for k, v in werte.items() if v is not None}
    return aus


def _summe(*werte):
    da = [w for w in werte if w is not None]
    return sum(da) if da else None
