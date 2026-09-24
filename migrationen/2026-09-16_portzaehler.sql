-- Zaehler und Messwerte der Switchports: Fehler, verworfene Pakete, PoE-Leistung.
-- Kurzlebig (48 Stunden, der Sammler raeumt selbst auf) — Zustaende wie Link und
-- VLAN stehen als Intervalle in `zuordnung`, hier liegen nur Werte, die sich bei
-- jedem Lauf aendern und aus denen die Oberflaeche Raten rechnet.
-- Idempotent.
SET search_path TO daedalus;

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
