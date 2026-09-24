-- Daedalus — Datenmodell mit Zeitachse (Plan Block 24)
-- PostgreSQL 15+, gedacht fuer den Patroni-Cluster auf 172.16.1.5:5432
--
-- Grundsatz: Intervalle statt Abzuege. Bleibt ein Zustand gleich, bleibt sein
-- Intervall offen. Der Bestand waechst mit den AENDERUNGEN, nicht mit der ZEIT.
-- Ein Vollbild alle 15 Minuten gibt es nicht.

BEGIN;

CREATE SCHEMA IF NOT EXISTS daedalus;
SET search_path = daedalus, public;

-- ---------------------------------------------------------------- Quellen
-- `zustaendig_fuer` ist die wichtigste Spalte im ganzen Schema: nur eine
-- zustaendige Quelle darf ein Intervall beenden. Der Kea-Sammler sieht keine
-- Switch-Ports; sein Schweigen darf keinen Anschluss beenden.
CREATE TABLE quelle (
    name             text PRIMARY KEY,
    zustaendig_fuer  text[]  NOT NULL,
    fehlt_schwelle   smallint NOT NULL DEFAULT 2
                     CHECK (fehlt_schwelle >= 1),
    takt_sekunden    integer NOT NULL,
    letzter_erfolg   timestamptz,
    bemerkung        text
);
COMMENT ON COLUMN quelle.fehlt_schwelle IS
  'Zahl ERFOLGREICHER Laeufe ohne Sichtung, bevor etwas als verschwunden gilt. '
  'Eine MAC-Tabelle altert nach Minuten aus — einmal nicht gesehen heisst nichts.';

-- ---------------------------------------------------------------- Laeufe
CREATE TABLE lauf (
    id            bigserial PRIMARY KEY,
    quelle        text        NOT NULL REFERENCES quelle(name),
    zeitpunkt     timestamptz NOT NULL,
    dauer_ms      integer,
    erfolgreich   boolean     NOT NULL,
    fehler        text,
    -- Regel 3: derselbe Lauf zweimal eingespielt darf nichts bewirken.
    UNIQUE (quelle, zeitpunkt)
);
CREATE INDEX lauf_quelle_zeit ON lauf (quelle, zeitpunkt DESC);

-- ---------------------------------------------------------------- Objekte
-- Eine MAC ist ein IDENTITAETSBELEG, keine Geraete-Kennung. Geraete bekommen
-- eine eigene stabile Kennung, damit Zusammenfuehren und spaeteres Trennen
-- moeglich bleibt, ohne dass Historie verlorengeht.
CREATE TABLE objekt (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    art           text NOT NULL CHECK (art IN
                  ('geraet','schnittstelle','netz','access_point')),
    -- Der Schluessel, unter dem die Quelle dieses Ding nennt (eine MAC,
    -- 'c3po:gi12'), solange keine Identitaetsaufloesung stattgefunden hat.
    -- Er ist NICHT die Identitaet: die steckt in `identitaetsbeleg`, und
    -- zwei `extern` koennen spaeter auf dasselbe Geraet zusammenlaufen.
    extern        text UNIQUE,
    anzeigename   text,
    notiz         text,
    betreuer      text,
    erste_sicht   timestamptz NOT NULL,
    letzte_sicht  timestamptz NOT NULL
);
CREATE INDEX objekt_art ON objekt (art);

CREATE TABLE identitaetsbeleg (
    objekt        uuid NOT NULL REFERENCES objekt(id) ON DELETE CASCADE,
    art           text NOT NULL CHECK (art IN
                  ('mac','seriennummer','lldp_id','proxmox_id','dhcp_id','manuell')),
    wert          text NOT NULL,
    quelle        text NOT NULL REFERENCES quelle(name),
    sicherheit    smallint NOT NULL DEFAULT 100 CHECK (sicherheit BETWEEN 0 AND 100),
    ab            timestamptz NOT NULL,
    bis           timestamptz,
    PRIMARY KEY (objekt, art, wert, ab)
);
-- Derselbe Beleg darf nicht gleichzeitig zwei Objekten gehoeren.
CREATE UNIQUE INDEX identitaetsbeleg_eindeutig
    ON identitaetsbeleg (art, wert) WHERE bis IS NULL;

-- ---------------------------------------------------------------- Intervalle
-- Adressen, Anschluesse, Verbindungen und wechselnde Merkmale sind alle
-- dasselbe: eine Zuordnung mit Gueltigkeitszeitraum. Eine Tabelle, ein Regelsatz.
CREATE TABLE zuordnung (
    id            bigserial PRIMARY KEY,
    beziehung     text NOT NULL CHECK (beziehung IN
                  ('adresse','anschluss','verbindung','merkmal')),
    objekt        uuid NOT NULL REFERENCES objekt(id) ON DELETE CASCADE,
    schluessel    text NOT NULL,          -- was gleich bleibt
    wert          text NOT NULL,          -- was daran haengt
    ab            timestamptz NOT NULL,
    bis           timestamptz,            -- NULL = gilt noch
    quelle        text NOT NULL REFERENCES quelle(name),
    fehlt_seit    smallint NOT NULL DEFAULT 0,
    -- Fluechtig: das Objekt wird verfolgt, sein Auftauchen und Verschwinden ist
    -- aber keine Nachricht. Handys wuerfeln ihre MAC je Netz; ohne dieses
    -- Kennzeichen bestuende der Veraenderungsbericht bald nur noch aus ihnen.
    fluechtig     boolean NOT NULL DEFAULT false,
    CHECK (bis IS NULL OR bis >= ab)
);
-- Je Objekt und Schluessel darf nur EIN Intervall offen sein.
CREATE UNIQUE INDEX zuordnung_eine_offen
    ON zuordnung (beziehung, objekt, schluessel) WHERE bis IS NULL;
CREATE INDEX zuordnung_offen   ON zuordnung (beziehung, wert) WHERE bis IS NULL;
CREATE INDEX zuordnung_verlauf ON zuordnung (objekt, ab DESC);
-- „Wer hing frueher an diesem Port?" — die zweithaeufigste Frage ueberhaupt,
-- und mit diesem Index eine einzige Abfrage.
CREATE INDEX zuordnung_rueckwaerts ON zuordnung (wert, ab DESC)
    WHERE beziehung = 'anschluss';

-- ---------------------------------------------------------------- Aenderungen
CREATE TABLE aenderung (
    id            bigserial PRIMARY KEY,
    art           text NOT NULL CHECK (art IN
                  ('erstmals_gesehen','verschwunden','wieder_da',
                   'adresse_dazu','adresse_weg','umgezogen','getrennt',
                   'merkmal_geaendert')),
    objekt        uuid NOT NULL REFERENCES objekt(id) ON DELETE CASCADE,
    zeitpunkt     timestamptz NOT NULL,
    vorher        text,
    nachher       text,
    quelle        text NOT NULL REFERENCES quelle(name),
    schluessel    text,          -- welche Angabe: name, vlan, dhcp_lease ...
    -- Eine Aenderung betrifft nie nur ein Objekt: wandert ein Geraet von Port 15
    -- auf Port 16, sind Geraet UND beide Ports betroffen. Genau danach markiert
    -- die Oberflaeche.
    betrifft      uuid[] NOT NULL DEFAULT '{}',
    lauf          bigint REFERENCES lauf(id)
);
CREATE INDEX aenderung_zeit     ON aenderung (zeitpunkt DESC);
CREATE INDEX aenderung_betrifft ON aenderung USING gin (betrifft);

-- Ohne Quittierung wird die Aenderungsliste binnen Wochen ein ungelesener Strom.
CREATE TABLE quittung (
    aenderung     bigint PRIMARY KEY REFERENCES aenderung(id) ON DELETE CASCADE,
    stand         text NOT NULL CHECK (stand IN ('gesehen','erwartet','ignoriert')),
    bis           timestamptz,            -- nur bei 'erwartet'
    notiz         text,
    wer           text,
    wann          timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- Pflege
-- Von Hand gepflegte Angaben zu einem Objekt: die Wanddose, an der ein Port
-- haengt, der Raum, eine Notiz, ein Betreuer.
--
-- Bewusst eine EIGENE Tabelle und nicht ein paar Spalten an `objekt`. Diese
-- Daten sind das Gegenteil von allem anderen im Schema:
--   * kein Sammler schreibt sie jemals — sie kommen nur vom Menschen,
--   * sie haben keinen Gueltigkeitszeitraum, sie gelten bis jemand sie aendert,
--   * sie ueberleben, dass ein Geraet verschwindet und wiederkommt.
-- Wer beides in eine Tabelle legt, loescht sie irgendwann versehentlich mit.
CREATE TABLE pflege (
    objekt        uuid PRIMARY KEY REFERENCES objekt(id) ON DELETE CASCADE,
    -- Die Dose an der Wand, auf die der Port geht: "B2-14", "Buero links".
    -- Das ist die Angabe, die im Ernstfall den Unterschied macht, weil sie
    -- als einzige aus der Karte in den Raum zeigt.
    dose          text,
    raum          text,
    notiz         text,
    betreuer      text,
    -- Ein von Hand vergebener Name schlaegt jede Namensquelle (Block 3.3).
    name          text,
    -- Warum ein Fund an diesem Objekt so in Ordnung ist. Gesetzt = bestaetigt.
    erwartet      text,
    geaendert     timestamptz NOT NULL DEFAULT now(),
    von           text,
    CONSTRAINT pflege_check CHECK (dose IS NOT NULL OR raum IS NOT NULL
           OR notiz IS NOT NULL OR betreuer IS NOT NULL OR name IS NOT NULL
           OR erwartet IS NOT NULL)
);
CREATE INDEX pflege_dose  ON pflege (dose)  WHERE dose  IS NOT NULL;
CREATE INDEX pflege_raum  ON pflege (raum)  WHERE raum  IS NOT NULL;
-- Freitextsuche ueber alles Gepflegte — „wo war nochmal die Dose im Keller?"
CREATE INDEX pflege_suche ON pflege USING gin (
    to_tsvector('german',
        coalesce(dose,'')||' '||coalesce(raum,'')||' '||
        coalesce(notiz,'')||' '||coalesce(name,'')));

-- ---------------------------------------------------------------- Pruefungen
-- Vollstaendige Historie, nicht nur das letzte Ergebnis. Das ist
-- Diagnosehistorie, keine Buerokratie: „ging es gestern um die Zeit schon nicht?"
CREATE TABLE pruefung (
    id            bigserial PRIMARY KEY,
    objekt        uuid REFERENCES objekt(id) ON DELETE SET NULL,
    art           text NOT NULL CHECK (art IN
                  ('ping','traceroute','portscan','dns','wol','erreichbarkeit')),
    zeitpunkt     timestamptz NOT NULL,
    parameter     jsonb NOT NULL DEFAULT '{}',
    ergebnis      jsonb,
    erfolgreich   boolean,
    dauer_ms      integer,
    ausgeloest_von text
);
CREATE INDEX pruefung_objekt_zeit ON pruefung (objekt, zeitpunkt DESC);

-- ---------------------------------------------------------------- Projektion
-- Die Leinwand soll beim Klicken keine Historientabellen zusammensetzen muessen.
CREATE VIEW jetzt AS
SELECT z.beziehung, z.objekt, z.schluessel, z.wert, z.ab, z.quelle,
       o.art AS objekt_art, coalesce(p.name, o.anzeigename) AS anzeigename,
       p.dose, p.raum, p.notiz, p.betreuer
  FROM zuordnung z
  JOIN objekt   o ON o.id = z.objekt
  LEFT JOIN pflege p ON p.objekt = o.id
 WHERE z.bis IS NULL;

COMMENT ON VIEW jetzt IS 'Der Ist-Zustand: alle offenen Zuordnungen.';

-- Delta zum Browser: alles, was sich seit einem Stand geaendert hat.
CREATE VIEW seit_stand AS
SELECT a.id AS version, a.art, a.objekt, a.zeitpunkt, a.vorher, a.nachher, a.betrifft
  FROM aenderung a
 ORDER BY a.id;

COMMENT ON VIEW seit_stand IS
  'GET /api/graph?seit=<version> liest hier — die Seite flickt ihr Modell, '
  'statt ein Vollbild zu laden.';

-- ---------------------------------------------------------------- Zeit
-- Gespeichert wird `timestamptz`, also ein absoluter Zeitpunkt — intern UTC.
-- Die Sitzungszeitzone steht auf Europe/Berlin, damit ein Blick von Hand per
-- psql gleich die Uhrzeit zeigt, auf die der Mensch daneben schaut. An der
-- gespeicherten Groesse aendert das nichts.
--
-- Warum nicht gleich ortszeitlich speichern: in der Nacht der Zeitumstellung
-- gibt es 02:30 in Europe/Berlin ZWEIMAL. Ein Intervall, das dort endet, waere
-- nicht mehr eindeutig, und die Reihenfolge zweier Ereignisse nicht entscheidbar.
ALTER DATABASE daedalus SET timezone = 'Europe/Berlin';

COMMIT;

-- Portzaehler (siehe migrationen/2026-09-16_portzaehler.sql)
CREATE TABLE IF NOT EXISTS portzaehler (
    port           text        NOT NULL,     -- "r2d2:gi5" oder "r2d2" fuer den Switch
    zeitpunkt      timestamptz NOT NULL,
    in_fehler      bigint,
    out_fehler     bigint,
    in_verworfen   bigint,
    out_verworfen  bigint,
    poe_mw         integer,
    poe_budget_w   integer,
    PRIMARY KEY (port, zeitpunkt)
);
CREATE INDEX IF NOT EXISTS portzaehler_zeit ON portzaehler (zeitpunkt);
