-- Funde lassen sich als erwartet bestaetigen: eigenes Pflegefeld mit Begruendung.
-- Idempotent: darf auf einer schon migrierten Datenbank nochmal laufen.
SET search_path TO daedalus;

ALTER TABLE pflege ADD COLUMN IF NOT EXISTS erwartet text;

-- Die Pruefung „irgendetwas ist gepflegt" muss das neue Feld kennen, sonst
-- laesst sich eine Bestaetigung ohne Dose/Raum/Notiz nicht speichern.
ALTER TABLE pflege DROP CONSTRAINT IF EXISTS pflege_check;
ALTER TABLE pflege ADD CONSTRAINT pflege_check CHECK (dose IS NOT NULL OR raum IS NOT NULL
    OR notiz IS NOT NULL OR betreuer IS NOT NULL OR name IS NOT NULL OR erwartet IS NOT NULL);
