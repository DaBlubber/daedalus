# -*- coding: utf-8 -*-
"""Die Kette: „von wo nach wo" (Plan Block 4).

Das ist die Kernfunktion des ganzen Werkzeugs — die Frage, die heute niemand
beantworten kann:

    YODA → BB8 Port 3+4 → C3PO Port 12 → device-08ef

Sie entsteht aus drei Quellen, die einzeln nichts taugen und zusammen alles:

| Quelle | sagt | allein wertlos, weil |
|---|---|---|
| ARP (Sophos) | IP ↔ MAC | sie kennt keinen Port |
| MAC-Tabelle (Switch) | MAC ↔ Port | sie kennt keine Richtung |
| LLDP (Switch) | Port ↔ Nachbarport | sie kennt keine Endgeraete |

## Die Richtung

LLDP liefert einen **ungerichteten** Graphen: c3po sagt „an gi25 haengt bb8",
bb8 sagt „an gi1 haengt c3po". Keiner von beiden sagt, wo *oben* ist.

Oben wird deshalb **festgelegt**: die Wurzel ist der Switch an der Firewall.
Von dort aus bekommt jeder Switch seinen Abstand (Breitensuche), und die Kette
eines Geraets laeuft immer von grossem zu kleinem Abstand.

**Das echte Netz ist kein Baum.** c3po haengt an bb8 *und* an r2d2 — gemessen
am 16.09.2026. Die Breitensuche waehlt den kuerzesten Weg; gibt es mehrere
gleich kurze, sagt die Kette das (`eindeutig=False`), statt sich still fuer
einen zu entscheiden. Eine Karte, die schweigend raet, ist schlimmer als eine,
die zugibt, es nicht zu wissen.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .modell import Beziehung


@dataclass(frozen=True)
class Glied:
    """Ein Schritt in der Kette."""
    art: str          # 'switch' | 'geraet'
    name: str         # 'c3po' oder die MAC
    port: str = ""    # der Port, ueber den es weitergeht
    gemessen: bool = True   # False = erschlossen, nicht beobachtet

    def __str__(self) -> str:
        return f"{self.name} {self.port}".strip()


@dataclass
class Kette:
    glieder: tuple[Glied, ...]
    eindeutig: bool = True
    vollstaendig: bool = True     # False = reicht nicht bis zur Wurzel

    def __str__(self) -> str:
        return " → ".join(str(g) for g in self.glieder)

    def __bool__(self) -> bool:
        return bool(self.glieder)


# Zwei Schluesselformen teilen sich denselben Doppelpunkt:
#   `c3po:gi12`  Switch und Port
#   `ap:Luke`    ein Access Point
# Wer blind am ersten Doppelpunkt trennt, macht aus `ap:Luke` den Knoten „ap"
# mit Port „Luke" — und die Kette eines WLAN-Clients endet im Nichts. Beim
# ersten echten Lauf am 16.09.2026 genau so passiert.
def _knoten(anschluss: str) -> str:
    """Das Geraet, an dem etwas haengt: `c3po:gi12` -> `c3po`, `ap:Luke` -> `Luke`"""
    if anschluss.startswith("ap:"):
        return anschluss[3:]
    return anschluss.split(":", 1)[0]


def _port(anschluss: str) -> str:
    """Der Port, an dem es haengt. Ein Access Point hat keinen."""
    if anschluss.startswith("ap:"):
        return ""
    return anschluss.split(":", 1)[1] if ":" in anschluss else ""


_switch = _knoten          # alter Name, gleiche Bedeutung


def nachbarschaft(bestand) -> dict[str, list[tuple[str, str, str]]]:
    """Der Switch-Graph aus den offenen LLDP-Beobachtungen.

    `{switch: [(nachbar, eigener Port, Nachbarport), ...]}` — und zwar in
    **beide** Richtungen. LLDP wird oft nur von einer Seite gemeldet: ein
    Switch, dessen SNMP gerade schweigt, waere sonst vom Netz abgeschnitten,
    obwohl sein Nachbar ihn sieht.
    """
    aus: dict[str, list[tuple[str, str, str]]] = {}
    verbindungen = bestand.offene(Beziehung.VERBINDUNG)

    # Ein Switch meldet sich selbst unter dem Namen, den wir ihm geben (`l337`),
    # seine Nachbarn melden ihn per LLDP unter seinem Systemnamen (`L337`).
    # Ohne Angleich werden daraus zwei Knoten, und l337 haengt zweimal im Netz
    # — gemessen am 16.09.2026. Angeglichen wird nur, was als eigener Switch
    # bekannt ist; Access Points behalten ihre Schreibweise (`ap:Luke`).
    eigene = {_switch(i.objekt).lower(): _switch(i.objekt) for i in verbindungen}

    def angleichen(name: str) -> str:
        return eigene.get(name.lower(), name)

    for i in verbindungen:
        hier, dort = angleichen(_switch(i.objekt)), angleichen(_switch(i.wert))
        p_hier, p_dort = _port(i.objekt), _port(i.wert)
        if not hier or not dort or hier == dort:
            continue
        aus.setdefault(hier, []).append((dort, p_hier, p_dort))
        gegen = (hier, p_dort, p_hier)
        if gegen not in aus.setdefault(dort, []):
            aus[dort].append(gegen)
    return aus


def abstaende(graph: dict, wurzel: str) -> tuple[dict[str, int], dict[str, list]]:
    """Breitensuche von der Wurzel. Liefert Abstand und Vorgaenger je Switch.

    Der Vorgaenger ist eine **Liste**: gibt es zwei gleich kurze Wege, stehen
    beide drin. Genau daran erkennt die Kette, dass sie nicht eindeutig ist.
    """
    abstand = {wurzel: 0}
    vorher: dict[str, list] = {wurzel: []}
    warteschlange = deque([wurzel])
    while warteschlange:
        hier = warteschlange.popleft()
        for nachbar, p_hier, p_dort in graph.get(hier, []):
            neu = abstand[hier] + 1
            if nachbar not in abstand:
                abstand[nachbar] = neu
                vorher[nachbar] = [(hier, p_dort, p_hier)]
                warteschlange.append(nachbar)
            elif abstand[nachbar] == neu:
                eintrag = (hier, p_dort, p_hier)
                if eintrag not in vorher[nachbar]:
                    vorher[nachbar].append(eintrag)
    return abstand, vorher


def kette(bestand, objekt: str, wurzel: str) -> Kette:
    """Der Weg von der Wurzel bis zu einem Geraet.

    `objekt` ist eine MAC (oder ein Switchname, dann endet die Kette dort).
    """
    graph = nachbarschaft(bestand)
    abstand, vorher = abstaende(graph, wurzel)

    # Wo haengt das Geraet? Ein Switchname braucht keinen Anschluss.
    if objekt in graph or objekt == wurzel:
        start_switch, letzter_port, geraet = objekt, "", None
    else:
        anschluss = bestand.offen_fuer(Beziehung.ANSCHLUSS, objekt, "anschluss")
        if anschluss is None:
            return Kette((), vollstaendig=False)
        start_switch = _switch(anschluss.wert)
        letzter_port = _port(anschluss.wert)
        geraet = objekt

    if start_switch not in abstand:
        # Der Switch haengt in keiner bekannten Nachbarschaft. Das ist eine
        # ehrliche Teilauskunft: „haengt an c3po Port 12, Weg dorthin unbekannt".
        glieder = [Glied("switch", start_switch, letzter_port)]
        if geraet:
            glieder.append(Glied("geraet", geraet))
        return Kette(tuple(glieder), vollstaendig=False)

    # Von der Wurzel her aufbauen
    rueckwaerts: list[Glied] = []
    eindeutig = True
    hier, port_nach_unten = start_switch, letzter_port
    while True:
        rueckwaerts.append(Glied("switch", hier, port_nach_unten))
        eltern = vorher.get(hier) or []
        if not eltern:
            break
        if len(eltern) > 1:
            eindeutig = False
        # Der Eintrag ist (Elternteil, Port am KIND, Port am ELTERNTEIL). Am
        # Elternteil steht sein eigener Port — der, ueber den es das Kind
        # erreicht. Der Port am Kind waere die Sicht von unten und gehoert
        # nicht hierher; bei einem Access Point ist er sogar eine MAC.
        eltern_name, _port_am_kind, port_am_elternteil = eltern[0]
        hier, port_nach_unten = eltern_name, port_am_elternteil

    glieder = list(reversed(rueckwaerts))
    if geraet:
        glieder.append(Glied("geraet", geraet))
    return Kette(tuple(glieder), eindeutig=eindeutig)


def wer_hing_hier(bestand, anschluss: str) -> list[tuple[str, object, object]]:
    """„Wer haengt an diesem Port — und wer hing vorher hier?"

    Nach „wo haengt das Geraet?" die zweithaeufigste Frage ueberhaupt, und mit
    dem Intervallmodell faellt sie gratis ab: alle Zuordnungen, deren *Wert*
    dieser Port ist, juengste zuerst.
    """
    treffer = []
    for i in bestand.alle_mit_wert(Beziehung.ANSCHLUSS, anschluss):
        treffer.append((i.objekt, i.ab, i.bis))
    return sorted(treffer, key=lambda x: x[1], reverse=True)
