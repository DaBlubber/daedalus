# Daedalus — Phase 1a

Schema und Abgleichregeln für das Netzwerk-Übersichtswerkzeug.
*[English version](README.md)*

**Hier ist noch kein Sammler.** Dieser Schritt baut absichtlich zuerst die Regeln,
nach denen aus Beobachtungen Historie wird — denn genau daran scheitern solche
Werkzeuge: sie melden 193 verschwundene Geräte, weil ein Switch kurz nicht
antwortete, und danach glaubt ihnen niemand mehr.

## Inhalt

| Datei | Was |
|---|---|
| `schema.sql` | PostgreSQL-Schema, 8 Tabellen, 10 Indizes, 2 Sichten |
| `daedalus/modell.py` | Datentypen: Quelle, Lauf, Beobachtung, Intervall, Änderung |
| `daedalus/abgleich.py` | Die Regeln — die einzige Stelle, die über neu/umgezogen/verschwunden entscheidet |
| `daedalus/speicher.py` | Wo der Bestand liegt: im Arbeitsspeicher **oder** in PostgreSQL |
| `daedalus/zeit.py` | Gerechnet in UTC, angezeigt in Europe/Berlin |
| `daedalus/pflege.py` | Von Hand Eingetragenes: Wanddose, Raum, Notiz, Betreuer, Name |
| `daedalus/sammler.py` | Das Gerüst: Vorabfrage, Fehlerbehandlung, eine Runde |
| `daedalus/sammler_arp.py` | ARP-Tabelle der Sophos (IP ↔ MAC) |
| `daedalus/sammler_prometheus.py` | Ansible-Inventar und UniFi-WLAN-Clients |
| `daedalus/sammler_switch.py` | MAC-Tabelle und LLDP-Nachbarn eines Switches |
| `daedalus/kette.py` | „Von wo nach wo" — die Kernfunktion |
| `daedalus/dienst.py` | Taktung: jede Quelle in ihrem eigenen Rhythmus |
| `lauf.py` | Einen Sammellauf ausführen |
| `tests/test_abgleich.py` | 15 künstliche Sammelläufe — **jeder läuft gegen beide Speicher** |

## Der Grundsatz

**Intervalle statt Abzüge.** Bleibt ein Zustand gleich, bleibt sein Intervall
offen. Erst bei einer Änderung wird das alte geschlossen und ein neues begonnen.
Der Bestand wächst damit mit den *Änderungen*, nicht mit der *Zeit* — und jedes
Änderungsereignis fällt dabei von selbst an, statt hinterher aus zwei Vollbildern
errechnet zu werden.

Das ist zugleich die Voraussetzung für den Delta-Import (Block 25): es gibt kein
Vollbild, das übertragen werden müsste.

## Die vier Regeln, die nicht verhandelbar sind

1. **Ein Fehlen beendet ein Intervall nur, wenn die zuständige Quelle
   erfolgreich war.** Ein stiller Switch darf nichts verschwinden lassen.
2. **Nur eine zuständige Quelle darf urteilen.** Der Kea-Sammler sieht keine
   Switch-Ports; sein Schweigen darf keinen Anschluss beenden.
3. **Der Import ist wiederholbar.** Derselbe Lauf zweimal eingespielt erzeugt
   keine zweite Beobachtung und kein zweites Ereignis.
4. **Der erste Lauf markiert alles als Bestand, nicht als neu** — auch der erste
   Lauf jeder *neu angeschlossenen* Quelle.

Dazu eine Schwelle je Quelle: eine MAC-Tabelle altert nach Minuten aus, ein Gerät
ist also nicht weg, nur weil es einmal fehlt.

## Was die Tests beweisen

```
206 passed
```

Jeder Fall läuft zweimal: einmal im Arbeitsspeicher und einmal gegen das echte
PostgreSQL. Damit ist nicht nur bewiesen, dass die Regeln stimmen, sondern auch,
dass **Schema und Regeln zusammenpassen** — der häufigste Ort, an dem so etwas
auseinanderläuft.

- der erste Lauf meldet nichts als neu, auch bei einer später dazukommenden Quelle
- elf unveränderte Läufe erzeugen **keine einzige** neue Zeile
- ein Umzug markiert Gerät **und beide** Ports
- eine gescheiterte Quelle lässt nichts verschwinden
- einmal nicht gesehen reicht nicht, zweimal schon — und Wiedersehen setzt zurück
- eine fremde Quelle beendet keinen Anschluss, auch wenn sie mehr liefert als sie beurteilen kann
- derselbe Lauf zweimal eingespielt ist folgenlos
- ein Adresswechsel schließt das alte Intervall lückenlos
- die Marke am Knoten ist eine Ableitung mit Zeitpunkt, keine gespeicherte Farbe

Ein Fall hat beim ersten Lauf einen echten Entwurfsfehler aufgedeckt: ein neu
entdecktes Gerät bekam sowohl „erstmals gesehen" als auch „umgezogen". **Was noch
nie irgendwo war, kann nirgendwohin gezogen sein** — bei einem ganz neuen Objekt
gibt es jetzt genau ein Ereignis.

## Der Dienst

**Ein laufender Dienst, kein wiederkehrender Stapellauf** — und das ist eine
Entscheidung, keine Bequemlichkeit:

1. **Die Quellen altern verschieden.** Ein gemeinsamer Takt ist die teuerste
   aller Varianten: entweder fragt man die MAC-Tabelle zu selten oder die
   Nachbarschaft zu oft.
2. **Die billige Vorabfrage braucht ein Gedächtnis.** `ifLastChange` wirkt nur
   im Vergleich mit dem letzten Stand. Ein Stapellauf startet jedes Mal frisch —
   der Vergleich liefe ins Leere und die Ersparnis wäre genau null. Wer sparen
   will, muss sich erinnern können.

Dazu drei Zurückhaltungen, jede als Test: Sammler laufen **nacheinander** (fünf
Switches gleichzeitig zu fragen erzeugt genau die Lastspitze, die vermieden
werden soll), der Start ist **gestreut**, und nach Fehlern wird der Takt
**verdoppelt** bis höchstens zum Achtfachen — ein Switch, der nicht antwortet,
wird durch häufigeres Fragen nicht gesprächiger.

Und der Dienst **schweigt**, wenn nichts war. Wer alle fünf Minuten „nichts
geändert" protokolliert, wird nach einer Woche nicht mehr gelesen.

## Die Kette

Die Frage, die heute niemand beantworten kann:

```
bb8 Port 1 → c3po Port 12 → 64:28:aa:9e:1c:b2
```

Sie entsteht aus drei Quellen, die **einzeln nichts taugen**:

| Quelle | sagt | allein wertlos, weil |
|---|---|---|
| ARP | IP ↔ MAC | kennt keinen Port |
| MAC-Tabelle | MAC ↔ Port | kennt keine Richtung |
| LLDP | Port ↔ Nachbarport | kennt keine Endgeräte |

**Wo ist oben?** LLDP liefert einen ungerichteten Graphen — c3po sagt „an gi25
hängt bb8", bb8 sagt „an gi1 hängt c3po". Keiner sagt, wo oben ist. Oben wird
deshalb festgelegt (die Wurzel ist der Switch an der Firewall), und von dort
bekommt jeder Switch per Breitensuche seinen Abstand.

**Das echte Netz ist kein Baum.** Gibt es mehrere gleich kurze Wege, sagt die
Kette das (`eindeutig=False`), statt sich still für einen zu entscheiden. Eine
Karte, die schweigend rät, ist schlimmer als eine, die zugibt, es nicht zu wissen.
Dasselbe bei einem unbekannten Switch: „hängt an `fremd` Port 3, Weg dorthin
unbekannt" ist eine ehrliche Teilauskunft.

**Nachbarschaft gilt in beide Richtungen.** LLDP wird oft nur von einer Seite
gemeldet; ein Switch, dessen SNMP gerade schweigt, wäre sonst vom Netz
abgeschnitten, obwohl sein Nachbar ihn sieht.

### „Wer hing hier vorher?"

Nach „wo hängt das Gerät?" die zweithäufigste Frage — und mit dem
Intervallmodell fällt sie gratis ab.

Eine Eigenschaft, die dabei auffällt und gewollt ist: die Portgeschichte zeigt
eine **Überlappung von genau einem Sammeltakt**. Wechselt ein Gerät den Port,
gilt das alte Intervall noch einen Lauf lang weiter, weil einmal Fehlen kein
Beweis ist. Ein Intervall auf Verdacht zu schließen wäre die unehrlichere Variante.

## Sammler

Ein Sammler beantwortet zwei Fragen und liefert Beobachtungen. Alles Schwierige
— wann etwas als verschwunden gilt, was ein Fehlen bedeutet — steht im Abgleich.

**Die Delta-Logik steckt in `vorab_unveraendert()`.** Vor der teuren Abfrage
steht eine billige. Gemessen:

| Sammler | volle Abfrage | Vorabfrage | gespart |
|---|---|---|---|
| Inventar | 37 475 B / 49 ms | 1 733 B / 23 ms | **95 % Nutzdaten** |
| WLAN | 72 280 B / 40 ms | 26 833 B / 42 ms | 63 % Nutzdaten, keine Zeit |
| Sophos-ARP | — | **keine** | — |
| LLDP je Switch | 4 Walks | `ifLastChange`, 1 Walk | Nachbarschaft ändert sich nur beim Umstecken |
| MAC-Tabelle | — | **keine** | eine MAC wandert ohne Linkwechsel |

Beim WLAN ist die Vorabfrage serverseitig sogar minimal langsamer: die sichere
Gruppierung kostet Rechenzeit. Ein bloßes `count(...)` wäre schneller, würde
aber einen AP- oder Adresswechsel bei gleicher Clientzahl übersehen.

**Für die ARP-Tabelle gibt es keine billige Vorabfrage** — es existiert kein
Zähler, der sich ändert, wenn ein Eintrag dazukommt. Der Sammler gibt ehrlich
`False` zurück und sammelt, statt eine Vorabfrage vorzutäuschen. Gespart wird
dort über den Takt und darüber, dass nur Änderungen geschrieben werden.

### Zugangsport oder Uplink — die wichtigste Ableitung

**Eine MAC auf einem Zugangsport hängt dort. Eine MAC auf einem Uplink hängt
weiter hinten.** Ein Uplink sieht alles, was dahinter liegt; wer das nicht
trennt, behauptet, an einem Stecker hingen 52 Geräte.

Woran ein Uplink erkannt wird — und hier hat die echte Messung den Entwurf
korrigiert:

1. **Er hat einen LLDP-Nachbarn.** Die verlässlichste Auskunft, sie kommt vom
   Nachbargerät selbst.
2. **Er ist ein Portkanal** (`Po1`). Das war nicht geplant und kam erst mit
   echten Daten heraus: an c3po sitzen **52 von 60 MACs auf `Po1`** — die
   MAC-Tabelle nennt bei einem Bündel den **Kanal**, LLDP dagegen die
   **Mitglieder** (gi25, gi26). Wer nur der LLDP-Liste folgt, hält `Po1` für
   einen Zugangsport.

*Bekannte Grenze:* ein Portkanal gilt immer als Uplink. Wer einen Server mit
zwei Leitungen bündelt, wird dadurch nicht gefunden. Im Haus-Netz kein Fall;
käme er vor, müsste `dot3adAggPortListPorts` dazugelesen werden.

### Flüchtige Objekte

Handys würfeln ihre MAC je Netz. Ohne Gegenmaßnahme wäre jedes von ihnen täglich
ein „neues Gerät", und der Veränderungsbericht bestünde bald nur noch aus ihnen.

Solche Beobachtungen tragen deshalb `fluechtig=True`: das Gerät wird **verfolgt**,
sein Auftauchen und Verschwinden aber **nicht gemeldet**. Umzüge und
Adresswechsel bleiben sichtbar — ein Client, der den Access Point wechselt, ist
eine echte Aussage, auch wenn seine MAC gewürfelt ist.

## Im echten Netz gemessen

Zwei Läufe, fünf Minuten auseinander (16.09.2026):

```
Lauf 1:  1103 Beobachtungen,  0 Änderungen, 0 Ausfälle
Lauf 2:  1114 Beobachtungen, 11 Änderungen, 0 Ausfälle
```

Der erste meldet **nichts als neu** — Regel 4. Der zweite fand, was wirklich
passiert war: zwei IoT-Geräte hatten sich ins WLAN eingebucht.

```
erstmals_gesehen   e4:78:af:22:98:fd  →  172.16.11.239
wieder_da          e4:78:af:22:98:fd  →  ap:Luke
```

Und Ketten aus echten Daten:

```
bb8 gi25 → r2d2 gi15 → Luke → 04:c5:81:9f:c2:ea    172.16.11.224   lwip0
bb8 gi1  → c3po gi7  → 5c:29:f4:c2:03:f2           172.16.10.30
```

## Gepflegte Angaben

Die Karte zeigt, dass an `C3PO Port 12` ein Laptop hängt. Sie zeigt **nicht**, wo
dieser Port an der Wand herauskommt — und keine Quelle der Welt liefert das.
Genau diese eine Angabe entscheidet um 23 Uhr, ob man das Kabel findet.

Deshalb eine eigene Tabelle `pflege`: Wanddose, Raum, Notiz, Betreuer und ein
von Hand vergebener Name, an **jedem** Objekt — Port, Netz, Switch, Gerät.

Sie ist bewusst vom Rest getrennt, weil sie dessen Gegenteil ist:

| gesammelt | gepflegt |
|---|---|
| kommt von einem Sammler | kommt nur vom Menschen |
| hat einen Gültigkeitszeitraum | gilt, bis jemand sie ändert |
| endet, wenn etwas verschwindet | **überlebt, dass ein Gerät verschwindet und wiederkommt** |

Wer beides in eine Tabelle legt, löscht das Gepflegte irgendwann versehentlich
mit. Ein Test hält jede dieser drei Zeilen fest.

Eine Dose kann eingetragen werden, **bevor** je etwas daran gesehen wurde — man
verkabelt zuerst und steckt später etwas ein.

## Datenbank

Angelegt am 15.09.2026 auf dem Patroni-Cluster:

```
Datenbank   daedalus
Rolle       daedalus  (Eigentümer von Schema und allen Objekten)
Zugang      172.16.1.5:5432  →  HAProxy  →  PgBouncer 6432  →  Patroni-Leader
PostgreSQL  17.0.10
```

PgBouncer schlägt Passwörter zur Laufzeit in `pg_shadow` nach (`auth_query`) —
für eine neue Rolle ist **kein Eintrag in `userlist.txt` nötig**.

Das Passwort gehört in **Vault**; es steht bewusst nicht in diesem Repository.

## Ausführen

```bash
python -m pytest tests -q
```

Ohne `DAEDALUS_DSN` läuft nur der Speicher-Durchgang, der Datenbankteil wird
übersprungen statt rot zu werden. Mit Datenbank:

```bash
DAEDALUS_TEST_DSN=postgresql://daedalus:<passwort>@172.16.1.5:5432/daedalus_test python -m pytest tests -q
```

> **Die Tests räumen die Datenbank leer.** Sie lesen deshalb `DAEDALUS_TEST_DSN`
> (nicht `DAEDALUS_DSN`) **und** weigern sich, wenn der Datenbankname nicht auf
> `_test` endet. Zwei Sperren, weil eine nicht gereicht hat: am 16.09.2026 lief
> die Vorrichtung einmal gegen die produktive Datenbank und hat einen
> vollständigen Sammellauf gelöscht.

## Zwei Befunde aus dem Bau

**Ein neu entdecktes Gerät bekam „erstmals gesehen" *und* „umgezogen".** Weil
„umgezogen" schwerer wiegt, hätte die Karte jedes neue Gerät als Umzug markiert.
Was noch nie irgendwo war, kann nirgendwohin gezogen sein — bei einem ganz neuen
Objekt gibt es jetzt genau ein Ereignis.

**Gerechnet wird in UTC, angezeigt in Europe/Berlin.** Das ist kein
Widerspruch, sondern die Trennung, an der solche Werkzeuge sonst scheitern: in
der Nacht der Zeitumstellung gibt es 02:30 in Europe/Berlin **zweimal**. Ein
Intervall, das dort endet, wäre ortszeitlich nicht mehr eindeutig, und die
Reihenfolge zweier Ereignisse nicht entscheidbar. Ein Test hält genau das fest.

**Und PgBouncer überschreibt die Zeitzone des Servers.** Gemessen:

```
ALTER DATABASE daedalus SET timezone='Europe/Berlin'
  direkt am Leader   →  Europe/Berlin   2026-09-16 06:56:00+02
  über PgBouncer     →  Etc/UTC
```

Der Pool baut seine Serververbindungen mit eigenen Startparametern auf. Der
verlässliche Weg ist der Startparameter in der Verbindungszeichenfolge —
`zeit.mit_zeitzone()` setzt ihn, und PgBouncer schlüsselt seine Pools danach.

## Offen

- Sammler (Phase 1b) — mit Delta-Logik nach Block 25 von Anfang an
- Zusammenführen mehrerer Identitätsbelege zu einem Gerät (Block 3.2):
  `objekt.extern` ist der Schlüssel der Quelle, **nicht** die Identität
- Passwort nach Vault

## Betrieb

Der Nomad-Job liegt **nicht hier**, sondern wie alle Lab-Dienste in
`admin/Nomad-Services` unter `standard/daedalus.nomad`. Ein Push dort rollt ihn
ueber Jenkins aus (nur die Dateien des letzten Commits). Das Abbild
`registry.example.com/daedalus:<version>` wird auf host-59fa gebaut; eine neue
Version heisst: hier taggen und bauen, dort den Tag im Job anheben.
