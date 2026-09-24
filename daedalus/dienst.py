# -*- coding: utf-8 -*-
"""Der Dienst: Sammler nach ihrem eigenen Takt laufen lassen.

**Ein laufender Dienst, kein wiederkehrender Stapellauf** — und das ist eine
Entscheidung, keine Bequemlichkeit. Zwei Gruende, beide aus Plan Block 25:

1. **Die Quellen altern verschieden.** Eine MAC-Tabelle altert in Minuten aus,
   eine LLDP-Nachbarschaft aendert sich beim Umstecken, eine Portkonfiguration
   nie. Ein gemeinsamer Takt ist die teuerste aller Varianten: entweder fragt
   man die MAC-Tabelle zu selten oder die Nachbarschaft zu oft.

2. **Die billige Vorabfrage braucht ein Gedaechtnis.** `ifLastChange` wirkt nur
   im Vergleich mit dem letzten Stand. Ein Stapellauf startet jedes Mal einen
   frischen Prozess — der Vergleich liefe ins Leere, und die Ersparnis waere
   genau null. Wer sparen will, muss sich erinnern koennen.

Die Sammler laufen **nacheinander**, nie gleichzeitig: fuenf Switches auf einmal
abzufragen erzeugt genau die Lastspitze, die vermieden werden soll.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field

from .sammler import Ergebnis, durchlauf
from .zeit import anzeige, jetzt


@dataclass
class Auftrag:
    """Ein Sammler mit seinem Takt."""
    sammler: object
    takt: float                      # Sekunden zwischen zwei Laeufen
    faellig_ab: float = 0.0          # monotone Uhr
    fehler_in_folge: int = 0

    @property
    def name(self) -> str:
        return self.sammler.quelle.name

    def naechster_takt(self) -> float:
        """Nach Fehlern wird zurueckhaltender gefragt.

        Ein Switch, der nicht antwortet, wird durch haeufigeres Fragen nicht
        gespraechiger — man erzeugt nur Last und fuellt Protokolle. Verdoppeln
        bis hoechstens das Achtfache, dann bleibt es dabei.
        """
        if not self.fehler_in_folge:
            return self.takt
        return self.takt * min(8, 2 ** self.fehler_in_folge)


class Dienst:
    """Fuehrt Auftraege aus, wenn sie faellig sind."""

    def __init__(self, auftraege: list[Auftrag], *, streuung: float = 0.1,
                 uhr=time.monotonic, schlafen=time.sleep, zeitgeber=jetzt) -> None:
        self.auftraege = auftraege
        self.streuung = streuung
        self._uhr = uhr              # monoton, fuer die Taktung
        self._zeitgeber = zeitgeber  # Wanduhr, fuer den Zeitstempel des Laufs
        self._schlafen = schlafen
        self.laeuft = True
        jetzt_ = self._uhr()
        # Nicht alle gleichzeitig starten lassen: sonst treffen sich beim
        # ersten Durchgang alle Abfragen im selben Augenblick.
        for i, a in enumerate(auftraege):
            a.faellig_ab = jetzt_ + i * 0.5

    def faellig(self) -> list[Auftrag]:
        h = self._uhr()
        return [a for a in self.auftraege if a.faellig_ab <= h]

    def wartezeit(self) -> float:
        if not self.auftraege:
            return 1.0
        return max(0.0, min(a.faellig_ab for a in self.auftraege) - self._uhr())

    def einmal(self, bestand, melden=None) -> list[Ergebnis]:
        """Alles Faellige einmal abarbeiten. Nacheinander, nie gleichzeitig."""
        ergebnisse = []
        for auftrag in self.faellig():
            e = durchlauf(bestand, auftrag.sammler, self._zeitgeber())
            ergebnisse.append(e)

            auftrag.fehler_in_folge = 0 if e.erfolgreich else auftrag.fehler_in_folge + 1
            takt = auftrag.naechster_takt()
            # Etwas Streuung, damit sich die Takte nicht einschwingen und die
            # Abfragen fuer immer im Gleichschritt laufen.
            takt *= 1 + random.uniform(-self.streuung, self.streuung)
            auftrag.faellig_ab = self._uhr() + takt

            if melden:
                melden(e, auftrag)
        return ergebnisse

    def laufen(self, bestand, melden=None, hoechstens: int | None = None) -> None:
        """Bis jemand stoppt. `hoechstens` begrenzt die Durchgaenge (Tests)."""
        durchgaenge = 0
        while self.laeuft:
            self.einmal(bestand, melden)
            durchgaenge += 1
            if hoechstens is not None and durchgaenge >= hoechstens:
                return
            self._schlafen(min(self.wartezeit(), 5.0))


def bericht(e: Ergebnis, auftrag: Auftrag) -> str:
    """Eine Zeile je Lauf — aber nur, wenn es etwas zu sagen gibt.

    Ein Dienst, der alle fuenf Minuten „nichts geaendert" protokolliert, wird
    nach einer Woche nicht mehr gelesen. Uebersprungene und unveraenderte
    Laeufe schweigen deshalb.
    """
    if e.uebersprungen or (e.erfolgreich and not e.aenderungen):
        return ""
    kopf = f"{anzeige(jetzt())}  {e.quelle}"
    if not e.erfolgreich:
        return f"{kopf}: FEHLER ({auftrag.fehler_in_folge}. in Folge) — {e.fehler}"
    zeilen = [f"{kopf}: {len(e.aenderungen)} Aenderung"
              f"{'en' if len(e.aenderungen) != 1 else ''}"]
    for a in e.aenderungen[:10]:
        zeilen.append(f"    {a.art.value:<18} {a.objekt}  "
                      f"{a.vorher or '—'} -> {a.nachher or '—'}")
    if len(e.aenderungen) > 10:
        zeilen.append(f"    … und {len(e.aenderungen) - 10} weitere")
    return "\n".join(zeilen)
