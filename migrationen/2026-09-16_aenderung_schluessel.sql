-- Aenderungen tragen, WELCHE Angabe sich geaendert hat, und kennen "getrennt".
-- Idempotent: darf auf einer schon migrierten Datenbank nochmal laufen.
SET search_path TO daedalus;

ALTER TABLE aenderung ADD COLUMN IF NOT EXISTS schluessel text;

ALTER TABLE aenderung DROP CONSTRAINT IF EXISTS aenderung_art_check;
ALTER TABLE aenderung ADD CONSTRAINT aenderung_art_check CHECK (art IN
    ('erstmals_gesehen','verschwunden','wieder_da',
     'adresse_dazu','adresse_weg','umgezogen','getrennt','merkmal_geaendert'));
