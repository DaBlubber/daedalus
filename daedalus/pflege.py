# -*- coding: utf-8 -*-
"""Von Hand gepflegte Angaben: Dose, Raum, Notiz, Betreuer, Name.

Das ist der Gegenpol zu allem anderen in diesem Werkzeug. Alles andere wird
gesammelt, hat einen Gueltigkeitszeitraum und kann enden. Das hier kommt vom
Menschen, gilt bis jemand es aendert, und **ueberlebt, dass ein Geraet
verschwindet und wiederkommt**.

Warum es sich lohnt: die Karte zeigt, dass an `C3PO Port 12` ein Laptop haengt.
Sie zeigt nicht, wo dieser Port an der Wand herauskommt. Genau diese eine
Angabe entscheidet um 23 Uhr darueber, ob man das Kabel findet — und keine
Quelle der Welt liefert sie. Sie muss einmal eingetragen werden, und dann steht
sie fuer immer da.

`ifAlias` (die Portbeschreibung am Switch) kann dasselbe leisten und wird
spaeter als *Quelle* gelesen — aber sie ist gepflegt, nicht gemessen, und sie
steht nur dort, wo jemand sie am Geraet eingetragen hat. Beides nebeneinander
ist richtig: was hier steht, schlaegt im Zweifel den Switch.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime

FELDER = ("dose", "raum", "notiz", "betreuer", "name", "erwartet")


@dataclass
class Pflege:
    """Was ein Mensch zu einem Objekt hinterlegt hat."""
    objekt: str
    dose: str | None = None
    raum: str | None = None
    notiz: str | None = None
    betreuer: str | None = None
    name: str | None = None
    # Begruendung, warum ein Fund an diesem Objekt so in Ordnung ist
    # („Switch 2 im Standby wacht stuendlich auf"). Gesetzt = Fund bestaetigt.
    erwartet: str | None = None
    geaendert: datetime | None = None
    von: str | None = None

    @property
    def leer(self) -> bool:
        return not any(getattr(self, f) for f in FELDER)

    def zusammenfassung(self) -> str:
        """Eine Zeile fuer die Karte: „Dose B2-14 · Buero"."""
        teile = []
        if self.dose:
            teile.append(f"Dose {self.dose}")
        if self.raum:
            teile.append(self.raum)
        return " · ".join(teile)


def _sauber(wert: str | None) -> str | None:
    """Leereingaben werden zu NULL, nicht zu einer leeren Zeichenkette.

    Sonst steht spaeter in der Karte ein Feld „Raum:" ohne Inhalt, und man
    weiss nicht, ob es nie gepflegt oder absichtlich geleert wurde.
    """
    if wert is None:
        return None
    wert = wert.strip()
    return wert or None


class PflegeVerwaltung:
    """Lesen und Schreiben der gepflegten Angaben."""

    def __init__(self, verbindung, aufloesen) -> None:
        self.db = verbindung
        self._id = aufloesen          # Quellenschluessel -> Objekt-Kennung

    def lesen(self, objekt: str) -> Pflege:
        kennung = self._id(objekt)
        if not kennung:
            return Pflege(objekt=objekt)
        with self.db.cursor() as c:
            c.execute("""SELECT dose, raum, notiz, betreuer, name, erwartet, geaendert, von
                           FROM pflege WHERE objekt = %s""", (kennung,))
            z = c.fetchone()
        if z is None:
            return Pflege(objekt=objekt)
        return Pflege(objekt, *z)

    def schreiben(self, objekt: str, von: str = "", **werte) -> Pflege:
        """Angaben setzen. Nur uebergebene Felder werden angefasst.

        `dose=""` loescht die Dose, `dose` weglassen laesst sie unberuehrt —
        der Unterschied zwischen „soll weg" und „geht mich nichts an".
        """
        unbekannt = set(werte) - set(FELDER)
        if unbekannt:
            raise ValueError(f"unbekannte Felder: {sorted(unbekannt)}")

        kennung = self._id(objekt, pflicht=True)
        gesetzt = {k: _sauber(v) for k, v in werte.items()}

        # Erst ausrechnen, was danach dastehen wuerde, dann schreiben. Die
        # Datenbank verbietet eine Zeile, in der nichts mehr gepflegt ist — man
        # kann sie also nicht erst leerraeumen und danach aufraeumen.
        vorher = self.lesen(objekt)
        nachher = {f: gesetzt.get(f, getattr(vorher, f)) for f in FELDER}
        gibt_es = not vorher.leer

        with self.db.cursor() as c:
            if not any(nachher.values()):
                if gibt_es:
                    c.execute("DELETE FROM pflege WHERE objekt = %s", (kennung,))
            elif gibt_es:
                teile = ", ".join(f"{f} = %s" for f in FELDER)
                c.execute(f"""UPDATE pflege SET {teile}, geaendert = now(), von = %s
                               WHERE objekt = %s""",
                          (*[nachher[f] for f in FELDER], von or None, kennung))
            else:
                spalten = ", ".join(FELDER)
                platz = ", ".join(["%s"] * len(FELDER))
                c.execute(f"""INSERT INTO pflege (objekt, {spalten}, von)
                              VALUES (%s, {platz}, %s)""",
                          (kennung, *[nachher[f] for f in FELDER], von or None))

        return self.lesen(objekt)

    def alle(self) -> dict[str, dict]:
        """Alles Gepflegte auf einmal, nach Quellenschluessel — fuer die Karte."""
        with self.db.cursor() as c:
            c.execute(f"""SELECT o.extern, {", ".join("p." + f for f in FELDER)}
                            FROM pflege p JOIN objekt o ON o.id = p.objekt""")
            return {z[0]: {f: v for f, v in zip(FELDER, z[1:]) if v}
                    for z in c.fetchall()}

    def suchen(self, text: str) -> list[tuple[str, Pflege]]:
        """Volltext ueber alles Gepflegte — „wo war nochmal die Dose im Keller?" """
        with self.db.cursor() as c:
            c.execute("""SELECT o.extern, p.dose, p.raum, p.notiz, p.betreuer,
                                p.name, p.erwartet, p.geaendert, p.von
                           FROM pflege p JOIN objekt o ON o.id = p.objekt
                          WHERE to_tsvector('german',
                                  coalesce(p.dose,'')||' '||coalesce(p.raum,'')||' '||
                                  coalesce(p.notiz,'')||' '||coalesce(p.name,''))
                                @@ plainto_tsquery('german', %s)
                             OR p.dose ILIKE %s OR p.raum ILIKE %s
                          ORDER BY o.extern""",
                      (text, f"%{text}%", f"%{text}%"))
            return [(z[0], Pflege(z[0], *z[1:])) for z in c.fetchall()]
