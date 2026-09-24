# -*- coding: utf-8 -*-
"""Zeit: gerechnet in UTC, angezeigt in Europe/Berlin.

Das ist kein Widerspruch, sondern die Trennung, an der solche Werkzeuge sonst
scheitern:

* **Gespeichert und gerechnet wird in UTC.** Ein Intervall, das um 02:30 endet,
  muss eindeutig sein. In der Nacht der Zeitumstellung gibt es 02:30 in
  Europe/Berlin aber **zweimal** — einmal in Sommerzeit, eine Stunde spaeter
  noch einmal in Winterzeit. Wer dort ortszeitlich rechnet, verliert genau in
  dieser Stunde Historie oder erzeugt Intervalle, die rueckwaerts laufen.
* **Angezeigt wird in Europe/Berlin.** „vor 3 Minuten" und „gestern 18:51"
  sollen zu der Uhr passen, auf die der Mensch daneben schaut.

Die Datenbank speichert `timestamptz`, also einen absoluten Zeitpunkt. Ihre
Sitzungszeitzone steht auf `Europe/Berlin`, damit ein `psql`-Blick von Hand
gleich die richtige Uhrzeit zeigt — an der gespeicherten Groesse aendert das
nichts.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")
UTC = timezone.utc

_WOCHENTAG = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")


def jetzt() -> datetime:
    """Der aktuelle Zeitpunkt — immer zeitzonenbehaftet, immer UTC."""
    return datetime.now(UTC)


def nach_utc(dt: datetime) -> datetime:
    """Alles, was hereinkommt, auf UTC bringen.

    Eine nackte Zeitangabe wird als Ortszeit gelesen — so kommt sie aus
    Geraetequellen, die keine Zeitzone mitliefern (SNMP, Kea, Switch-Logs).
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=BERLIN)
    return dt.astimezone(UTC)


def anzeige(dt: datetime, mit_datum: bool = True) -> str:
    """Wie ein Zeitpunkt auf dem Bildschirm steht — in Europe/Berlin."""
    o = dt.astimezone(BERLIN)
    return o.strftime("%d.%m.%Y %H:%M") if mit_datum else o.strftime("%H:%M")


def kurz(dt: datetime, bezug: datetime | None = None) -> str:
    """Menschliche Kurzform: „vor 3 Min", „heute 18:51", „gestern 06:31".

    Genau die Form, die auf den Karten der Leinwand steht.
    """
    bezug = bezug or jetzt()
    o, b = dt.astimezone(BERLIN), bezug.astimezone(BERLIN)
    d = b - o

    if d < timedelta(0):
        return anzeige(dt)
    if d < timedelta(minutes=1):
        return "gerade eben"
    if d < timedelta(hours=1):
        return f"vor {int(d.total_seconds() // 60)} Min"
    if o.date() == b.date():
        return f"heute {o:%H:%M}"
    if (b.date() - o.date()).days == 1:
        return f"gestern {o:%H:%M}"
    if (b.date() - o.date()).days < 7:
        return f"{_WOCHENTAG[o.weekday()]} {o:%H:%M}"
    return anzeige(dt)


def dauer(von: datetime, bis: datetime | None = None) -> str:
    """Wie lange ein Intervall schon gilt — „seit 4 Tagen", „seit 2 Std"."""
    d = (bis or jetzt()) - von
    tage = d.days
    if tage >= 365:
        return f"seit {tage // 365} Jahr" + ("en" if tage // 365 > 1 else "")
    if tage >= 1:
        return f"seit {tage} Tag" + ("en" if tage > 1 else "")
    std = int(d.total_seconds() // 3600)
    if std >= 1:
        return f"seit {std} Std"
    return f"seit {max(1, int(d.total_seconds() // 60))} Min"


# ---------------------------------------------------------------------------
# Datenbankverbindung
# ---------------------------------------------------------------------------
# `ALTER DATABASE daedalus SET timezone='Europe/Berlin'` greift am Server —
# aber NICHT durch PgBouncer hindurch: der Pool baut seine Serververbindungen
# mit eigenen Startparametern auf und setzt Etc/UTC. Gemessen am 16.09.2026:
#
#     direkt am Leader   -> Europe/Berlin   2026-09-16 06:56:00+02
#     ueber PgBouncer    -> Etc/UTC
#
# Der verlaessliche Weg ist der Startparameter in der Verbindungszeichenfolge.
# PgBouncer schluesselt seine Pools nach Startparametern, die Einstellung haelt
# damit ueber alle Verbindungen des Pools.
_OPTION = "-c TimeZone=Europe/Berlin"


def mit_zeitzone(dsn: str) -> str:
    """Stellt sicher, dass die Verbindung in Europe/Berlin anzeigt."""
    from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
    teile = urlsplit(dsn)
    p = parse_qs(teile.query, keep_blank_values=True)
    if "options" not in p:
        p["options"] = [_OPTION]
    elif "TimeZone" not in p["options"][0]:
        p["options"] = [p["options"][0] + " " + _OPTION]
    return urlunsplit(teile._replace(query=urlencode(p, doseq=True)))
