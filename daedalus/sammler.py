# -*- coding: utf-8 -*-
"""Das Geruest fuer Sammler.

Ein Sammler ist absichtlich duenn: er beantwortet zwei Fragen und liefert eine
Liste von Beobachtungen. Alles Schwierige — wann etwas als verschwunden gilt,
was ein Fehlen bedeutet, wie Historie entsteht — steht in `abgleich.py` und geht
ihn nichts an.

**Die Delta-Logik steckt in `vorab_unveraendert()`** (Plan Block 25). Vor der
teuren Abfrage steht eine billige: hat sich seit dem letzten Lauf ueberhaupt
etwas geruehrt? `sysUpTime` und `ifLastChange` sind je EIN OID; ein HTTP-Kopf
mit `If-None-Match` kostet fast nichts. Sagt die Vorabfrage „unveraendert",
faellt der teure Teil komplett aus.

**Zwei Regeln fuer jede Vorabfrage:**

1. Sie muss **vorsichtig** sein. Im Zweifel `False` zurueckgeben — dann wird
   eben gesammelt. Ein zu Unrecht ausgelassener Lauf verliert eine Aenderung,
   ein zu Unrecht ausgefuehrter kostet nur ein paar Pakete.
2. „Unveraendert" ist **nicht** dasselbe wie „nichts gesehen". Ein ausgelassener
   Lauf darf den Fehlt-Zaehler nicht hochsetzen — sonst laesst ausgerechnet die
   Sparsamkeit Geraete verschwinden. Deshalb wird er gar nicht erst eingespielt.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

from .abgleich import einspielen
from .modell import Aenderung, Beobachtung, Lauf, Quelle
from .zeit import jetzt


class Sammler(Protocol):
    """Was ein Sammler koennen muss. Mehr nicht."""

    quelle: Quelle

    def vorab_unveraendert(self) -> bool:
        """Billige Frage: kann sich seit dem letzten Lauf nichts geaendert haben?

        Im Zweifel `False`. Wer keine billige Frage hat, gibt immer `False`
        zurueck — fuer die MAC-Tabelle gibt es zum Beispiel keine.
        """

    def sammeln(self) -> list[Beobachtung]:
        """Die teure Abfrage. Wirft, wenn die Quelle nicht antwortet."""


@dataclass
class Ergebnis:
    """Was ein Lauf gebracht hat."""
    quelle: str
    erfolgreich: bool
    uebersprungen: bool = False
    beobachtungen: int = 0
    aenderungen: tuple[Aenderung, ...] = ()
    dauer_ms: int = 0
    fehler: str = ""

    def __str__(self) -> str:
        if self.uebersprungen:
            return f"{self.quelle}: uebersprungen (Vorabfrage: unveraendert)"
        if not self.erfolgreich:
            return f"{self.quelle}: FEHLER nach {self.dauer_ms} ms — {self.fehler}"
        a = len(self.aenderungen)
        return (f"{self.quelle}: {self.beobachtungen} Beobachtungen, "
                f"{a if a else 'keine'} Aenderung{'en' if a != 1 else ''}, "
                f"{self.dauer_ms} ms")


def durchlauf(bestand, sammler: Sammler, zeitpunkt=None) -> Ergebnis:
    """Einen Sammler einmal laufen lassen und das Ergebnis einarbeiten."""
    q = sammler.quelle
    zeitpunkt = zeitpunkt or jetzt()
    begonnen = time.monotonic()

    # --- billige Frage zuerst ------------------------------------------------
    try:
        if sammler.vorab_unveraendert():
            # KEIN Lauf. „Unveraendert" heisst nicht „nichts gesehen" — ein
            # eingespielter Leerlauf wuerde den Fehlt-Zaehler hochsetzen.
            return Ergebnis(q.name, True, uebersprungen=True,
                            dauer_ms=int((time.monotonic() - begonnen) * 1000))
    except Exception:                                    # noqa: BLE001
        pass          # Vorabfrage kaputt? Dann eben der teure Weg.

    # --- teure Abfrage -------------------------------------------------------
    try:
        beobachtungen = sammler.sammeln()
    except Exception as e:                               # noqa: BLE001
        ms = int((time.monotonic() - begonnen) * 1000)
        # Regel 1: ein gescheiterter Lauf wird festgehalten, aendert aber nichts.
        # Genau das verhindert, dass ein stiller Switch 193 Geraete verschwinden
        # laesst.
        einspielen(bestand, Lauf(q.name, zeitpunkt, erfolgreich=False), q, [])
        return Ergebnis(q.name, False, dauer_ms=ms, fehler=f"{type(e).__name__}: {e}")

    aenderungen = einspielen(bestand, Lauf(q.name, zeitpunkt), q, beobachtungen)
    return Ergebnis(q.name, True, beobachtungen=len(beobachtungen),
                    aenderungen=tuple(aenderungen),
                    dauer_ms=int((time.monotonic() - begonnen) * 1000))


def runde(bestand, sammler: list[Sammler], zeitpunkt=None) -> list[Ergebnis]:
    """Mehrere Sammler nacheinander. Einer, der faellt, reisst keinen mit.

    Nacheinander und nicht gleichzeitig ist Absicht: fuenf Switches auf einmal
    abzufragen erzeugt genau die Lastspitze, die vermieden werden soll.
    """
    return [durchlauf(bestand, s, zeitpunkt) for s in sammler]
