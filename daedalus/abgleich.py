# -*- coding: utf-8 -*-
"""Der Abgleich: aus Beobachtungen werden Intervalle und Aenderungen.

Das ist das Herzstueck und die einzige Stelle, an der entschieden wird, ob etwas
neu, umgezogen oder verschwunden ist. Vier Regeln, die nicht verhandelbar sind
(Plan Block 24.2):

1. **Ein Fehlen beendet ein Intervall nur, wenn die zustaendige Quelle
   erfolgreich war.** Sonst meldet ein stiller Switch 193 verschwundene Geraete
   und niemand glaubt dem Werkzeug mehr.
2. **Nur eine zustaendige Quelle darf urteilen.** Der Kea-Sammler sieht keine
   Switch-Ports; sein Schweigen darf keinen Anschluss beenden.
3. **Der Import ist wiederholbar.** Derselbe Lauf zweimal eingespielt erzeugt
   keine zweite Beobachtung und kein zweites Ereignis.
4. **Der erste Lauf markiert alles als Bestand, nicht als neu.** Sonst waere die
   Veraenderungsansicht von Anfang an wertlos. Gilt auch fuer jede neu
   angeschlossene Quelle.
"""
from __future__ import annotations

from .modell import (STILLE_SCHLUESSEL, Aenderung, Beobachtung, Beziehung, Ereignis,
                     Intervall, Lauf, Quelle, meldenswert)

# Welches Ereignis entsteht, wenn eine Zuordnung dieser Art neu entsteht bzw. endet
_DAZU = {
    Beziehung.ADRESSE: Ereignis.ADRESSE_DAZU,
    Beziehung.ANSCHLUSS: Ereignis.UMGEZOGEN,
    Beziehung.VERBINDUNG: Ereignis.MERKMAL_GEAENDERT,
    Beziehung.MERKMAL: Ereignis.MERKMAL_GEAENDERT,
}
_WEG = {
    Beziehung.ADRESSE: Ereignis.ADRESSE_WEG,
    Beziehung.ANSCHLUSS: Ereignis.GETRENNT,
    Beziehung.VERBINDUNG: Ereignis.MERKMAL_GEAENDERT,
    Beziehung.MERKMAL: Ereignis.MERKMAL_GEAENDERT,
}


def einspielen(bestand, lauf: Lauf, quelle: Quelle,
               beobachtungen: list[Beobachtung]) -> list[Aenderung]:
    """Einen Sammellauf einarbeiten. Gibt die entstandenen Aenderungen zurueck.

    Der Rueckgabewert ist leer, wenn sich nichts geaendert hat — das ist der
    Normalfall und genau der Grund, warum der Bestand nicht mit der Zeit waechst.
    """
    if quelle.name != lauf.quelle:
        raise ValueError("Lauf und Quelle passen nicht zusammen")

    # --- Regel 3: wiederholbar ---------------------------------------------
    if bestand.lauf_gesehen(lauf.quelle, lauf.zeitpunkt):
        return []
    bestand.lauf_merken(lauf)

    # --- Regel 1: eine gescheiterte Quelle sagt gar nichts -------------------
    if not lauf.erfolgreich:
        return []

    # Ist das der erste erfolgreiche Lauf DIESER Quelle? Dann ist alles Bestand.
    erstlauf = not bestand.hatte_erfolgreichen_lauf(lauf.quelle, lauf.zeitpunkt)

    neu: list[Aenderung] = []

    # --- Regel 2: nur zustaendige Beziehungen anfassen -----------------------
    beobachtungen = [b for b in beobachtungen if b.beziehung in quelle.zustaendig_fuer]
    gesehen = {(b.beziehung, b.objekt, b.schluessel): b for b in beobachtungen}

    # ---------- Was ist neu oder hat sich geaendert? ------------------------
    for (bez, objekt, schluessel), b in gesehen.items():
        offen = bestand.offen_fuer(bez, objekt, schluessel)
        objekt_neu = not bestand.objekt_bekannt(objekt)

        if offen is not None and offen.wert == b.wert:
            if offen.fehlt_seit:
                bestand.fehlt_setzen(offen, 0)   # weiterhin da
            continue

        if offen is not None and schluessel in STILLE_SCHLUESSEL:
            bestand.schliessen(offen, lauf.zeitpunkt)   # Historie ja, Meldung nein
        elif offen is not None:             # Wert hat sich geaendert
            bestand.schliessen(offen, lauf.zeitpunkt)
            neu.append(Aenderung(
                art=_DAZU[bez], objekt=objekt, zeitpunkt=lauf.zeitpunkt,
                vorher=offen.wert, nachher=b.wert, quelle=quelle.name,
                betrifft=_betroffen(bez, objekt, offen.wert, b.wert),
                schluessel=schluessel))
        elif bez is Beziehung.ADRESSE and _gleiche_adresse_offen(bestand, objekt, b.wert,
                                                                  schluessel):
            # Dieselbe Adresse steht schon unter einem anderen Schluessel da —
            # der Umstieg auf einen Schluessel je Adresse (17.09.2026), oder
            # eine zweite Quelle, die dasselbe sieht. Beides ist keine Aenderung.
            pass
        elif bez is Beziehung.MERKMAL:
            # Eine Angabe, die es an diesem Objekt vorher nicht gab, ist keine
            # Aenderung, sondern neues Wissen: ein WLAN-Client, der wiederkommt,
            # bringt Name, ssid-feef, VLAN und Hersteller mit — vier Zeilen, die alle
            # dasselbe sagen wie das eine "wieder da". Und eine Quelle, die eine
            # neue Angabe lernt (Inventaradressen am 16.09.2026), meldete sonst
            # jedes Objekt einzeln.
            pass
        elif not erstlauf and not objekt_neu:
            # Ein bekanntes Objekt bekommt eine Zuordnung, die es vorher nicht
            # hatte — etwa ein Geraet, das wieder angesteckt wurde.
            neu.append(Aenderung(
                art=Ereignis.WIEDER_DA if bez is Beziehung.ANSCHLUSS else _DAZU[bez],
                objekt=objekt, zeitpunkt=lauf.zeitpunkt,
                vorher=None, nachher=b.wert, quelle=quelle.name,
                betrifft=_betroffen(bez, objekt, None, b.wert),
                schluessel=schluessel))
        # Ist das Objekt ganz neu, sagt das eine Ereignis „erstmals gesehen"
        # unten alles. Ein zusaetzliches „umgezogen" waere schlicht falsch:
        # was noch nie irgendwo war, kann nirgendwohin gezogen sein.

        bestand.oeffnen(Intervall(
            beziehung=bez, objekt=objekt, schluessel=schluessel, wert=b.wert,
            ab=lauf.zeitpunkt, quelle=quelle.name, fluechtig=b.fluechtig))

        # Erstsichtung des Objekts selbst — nur ausserhalb des Erstlaufs (Regel 4)
        # und nicht bei fluechtigen Objekten: ein Handy mit gewuerfelter MAC ist
        # jeden Tag ein anderes Geraet, und keines davon ist eine Nachricht.
        if objekt_neu:
            bestand.objekt_merken(objekt, lauf.zeitpunkt)
            if not erstlauf and not b.fluechtig:
                neu.append(Aenderung(
                    art=Ereignis.ERSTMALS_GESEHEN, objekt=objekt,
                    zeitpunkt=lauf.zeitpunkt, nachher=b.wert, quelle=quelle.name,
                    betrifft=_betroffen(bez, objekt, None, b.wert),
                    schluessel=schluessel))

    # Jede Meldung ist eine Sichtung, auch wenn sich nichts geaendert hat.
    bestand.gesehen_merken({o for (_, o, _) in gesehen}, lauf.zeitpunkt)

    # ---------- Was fehlt? --------------------------------------------------
    # Nur Intervalle betrachten, die DIESE Quelle gesetzt hat und fuer die sie
    # zustaendig ist. Alles andere geht sie nichts an.
    for i in bestand.offene_von(quelle.name, quelle.zustaendig_fuer):
        if (i.beziehung, i.objekt, i.schluessel) in gesehen:
            continue

        bestand.fehlt_setzen(i, i.fehlt_seit + 1)
        if i.fehlt_seit < quelle.fehlt_schwelle:
            continue                         # noch nicht sicher genug

        bestand.schliessen(i, lauf.zeitpunkt)
        # Eine Angabe, die wegfaellt, ist aus demselben Grund still wie eine,
        # die hinzukommt (siehe oben). Ein geaenderter Wert bleibt sichtbar.
        # Eine Adresse, die unter anderem Schluessel weiter offen ist, ist nicht weg.
        if i.beziehung is Beziehung.ADRESSE and _gleiche_adresse_offen(
                bestand, i.objekt, i.wert, i.schluessel):
            pass
        elif i.beziehung is not Beziehung.MERKMAL:
            neu.append(Aenderung(
                art=_WEG[i.beziehung], objekt=i.objekt, zeitpunkt=lauf.zeitpunkt,
                vorher=i.wert, nachher=None, quelle=quelle.name,
                betrifft=_betroffen(i.beziehung, i.objekt, i.wert, None),
                schluessel=i.schluessel))

        # Hat das Objekt danach ueberhaupt keine offene Zuordnung mehr?
        # Dann ist es als Ganzes verschwunden, nicht nur eine Angabe daran.
        # Fluechtige Objekte ausgenommen — ihr Verschwinden ist so wenig eine
        # Nachricht wie ihr Auftauchen.
        if not i.fluechtig and not bestand.hat_offene(i.objekt):
            neu.append(Aenderung(
                art=Ereignis.VERSCHWUNDEN, objekt=i.objekt,
                zeitpunkt=lauf.zeitpunkt, vorher=i.wert, quelle=quelle.name,
                betrifft=_betroffen(i.beziehung, i.objekt, i.wert, None),
                schluessel=i.schluessel))

    fluechtig = {b.objekt for b in beobachtungen if b.fluechtig} | {
        i.objekt for i in bestand.offene_von(quelle.name, quelle.zustaendig_fuer)
        if i.fluechtig}
    neu = [a for a in zusammenfassen(neu)
           if a.art in (Ereignis.ERSTMALS_GESEHEN, Ereignis.VERSCHWUNDEN)
           or meldenswert(a.art, a.vorher, a.nachher, a.objekt in fluechtig)]
    for a in neu:
        bestand.aenderung_merken(a)
    return neu


def _gleiche_adresse_offen(bestand, objekt: str, wert: str, ausser: str) -> bool:
    return any(i.bis is None and i.beziehung is Beziehung.ADRESSE and i.wert == wert
               and i.schluessel != ausser for i in bestand.verlauf(objekt))


def zusammenfassen(aenderungen: list[Aenderung]) -> list[Aenderung]:
    """Ist ein Objekt verschwunden, sagt das eine Ereignis alles.

    Vorher kamen fuer einen WLAN-Client, der geht, drei Zeilen: Adresse weg,
    getrennt von Luke, verschwunden. Die ersten beiden sind im dritten enthalten.
    """
    weg = {a.objekt for a in aenderungen if a.art is Ereignis.VERSCHWUNDEN}
    return [a for a in aenderungen
            if a.objekt not in weg
            or a.art not in (Ereignis.ADRESSE_WEG, Ereignis.GETRENNT)]


def _betroffen(bez: Beziehung, objekt: str, vorher: str | None,
               nachher: str | None) -> tuple[str, ...]:
    """Welche Objekte markiert eine Aenderung?

    Wandert ein Geraet von Port 15 auf Port 16, sind **drei** Dinge betroffen:
    das Geraet und beide Ports. Genau das braucht die Oberflaeche, damit die
    Marke nicht nur am Geraet klebt.
    """
    aus = [objekt]
    if bez in (Beziehung.ANSCHLUSS, Beziehung.VERBINDUNG):
        aus += [x for x in (vorher, nachher) if x]
    return tuple(dict.fromkeys(aus))


def marken(bestand, seit) -> dict[str, Ereignis]:
    """Was die Oberflaeche an den Knoten markiert.

    Keine gespeicherte Farbe, sondern eine Ableitung: die gewichtigste noch
    nicht quittierte Aenderung eines Objekts seit einem Zeitpunkt.
    """
    rang = {Ereignis.VERSCHWUNDEN: 3, Ereignis.UMGEZOGEN: 2,
            Ereignis.ADRESSE_WEG: 2, Ereignis.ADRESSE_DAZU: 2, Ereignis.GETRENNT: 2,
            Ereignis.MERKMAL_GEAENDERT: 2, Ereignis.WIEDER_DA: 1,
            Ereignis.ERSTMALS_GESEHEN: 1}
    aus: dict[str, Ereignis] = {}
    for a in bestand.aenderungen_seit(seit):
        for obj in a.betrifft or (a.objekt,):
            vorhanden = aus.get(obj)
            if vorhanden is None or rang[a.art] > rang[vorhanden]:
                aus[obj] = a.art
    return aus
