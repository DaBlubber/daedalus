-- Daedalus - data model with a time axis
-- PostgreSQL 15+. Applied automatically by `run.py` on an empty database
-- (daedalus/db.py: ensure_schema); later changes come as files in migrations/.
--
-- Principle: intervals, not snapshots. As long as a state stays the same, its
-- interval stays open. Storage grows with CHANGES, not with TIME. There is no full
-- snapshot every 15 minutes.

BEGIN;

CREATE SCHEMA IF NOT EXISTS daedalus;
SET search_path = daedalus, public;

-- ---------------------------------------------------------------- sources
-- `responsible_for` is the most important column of the whole schema: only a
-- responsible source may end an interval. The Kea collector sees no switch
-- ports; its silence must not end an attachment.
CREATE TABLE source (
    name              text PRIMARY KEY,
    responsible_for   text[]  NOT NULL,
    missing_threshold smallint NOT NULL DEFAULT 2
                      CHECK (missing_threshold >= 1),
    interval_seconds  integer NOT NULL,
    last_success      timestamptz,
    remark            text
);
COMMENT ON COLUMN source.missing_threshold IS
  'Number of SUCCESSFUL runs without a sighting before something counts as gone. '
  'A MAC table ages out after minutes - not seen once means nothing.';

-- ---------------------------------------------------------------- runs
CREATE TABLE run (
    id            bigserial PRIMARY KEY,
    source        text        NOT NULL REFERENCES source(name),
    timestamp     timestamptz NOT NULL,
    duration_ms   integer,
    successful    boolean     NOT NULL,
    error         text,
    -- Rule 3: the same run imported twice must have no effect.
    UNIQUE (source, timestamp)
);
CREATE INDEX run_source_time ON run (source, timestamp DESC);

-- ---------------------------------------------------------------- objects
-- A MAC is an IDENTITY PROOF, not a device identifier. Devices get their own
-- stable identifier, so merging and later splitting stay possible without losing
-- history.
CREATE TABLE object (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind          text NOT NULL CHECK (kind IN
                  ('device','interface','network','access_point')),
    -- The key under which the source names this thing (a MAC, 'c3po:gi12'), as
    -- long as no identity resolution has happened. It is NOT the identity: that
    -- lives in `identity_proof`, and two `external` keys can later converge on the
    -- same device.
    external      text UNIQUE,
    display_name  text,
    note          text,
    owner         text,
    first_seen    timestamptz NOT NULL,
    last_seen     timestamptz NOT NULL
);
CREATE INDEX object_kind ON object (kind);

CREATE TABLE identity_proof (
    object        uuid NOT NULL REFERENCES object(id) ON DELETE CASCADE,
    kind          text NOT NULL CHECK (kind IN
                  ('mac','serial_number','lldp_id','proxmox_id','dhcp_id','manual')),
    value         text NOT NULL,
    source        text NOT NULL REFERENCES source(name),
    confidence    smallint NOT NULL DEFAULT 100 CHECK (confidence BETWEEN 0 AND 100),
    since         timestamptz NOT NULL,
    until         timestamptz,
    PRIMARY KEY (object, kind, value, since)
);
-- The same proof must not belong to two objects at the same time.
CREATE UNIQUE INDEX identity_proof_unique
    ON identity_proof (kind, value) WHERE until IS NULL;

-- ---------------------------------------------------------------- intervals
-- Addresses, attachments, connections and changing attributes are all the same:
-- an assignment with a validity period. One table, one rule set.
CREATE TABLE assignment (
    id            bigserial PRIMARY KEY,
    relation      text NOT NULL CHECK (relation IN
                  ('address','attachment','connection','attribute')),
    object        uuid NOT NULL REFERENCES object(id) ON DELETE CASCADE,
    key           text NOT NULL,          -- what stays the same
    value         text NOT NULL,          -- what hangs off it
    since         timestamptz NOT NULL,
    until         timestamptz,            -- NULL = still valid
    source        text NOT NULL REFERENCES source(name),
    missing_since smallint NOT NULL DEFAULT 0,
    -- Volatile: the object is tracked, but its appearing and disappearing is not
    -- news. Phones randomise their MAC per network; without this flag the change
    -- report would soon consist of nothing else.
    volatile      boolean NOT NULL DEFAULT false,
    CHECK (until IS NULL OR until >= since)
);
-- Only ONE interval may be open per object and key.
CREATE UNIQUE INDEX assignment_one_open
    ON assignment (relation, object, key) WHERE until IS NULL;
CREATE INDEX assignment_open    ON assignment (relation, value) WHERE until IS NULL;
CREATE INDEX assignment_history ON assignment (object, since DESC);
-- "Who was plugged into this port before?" - the second most frequent question of
-- all, and with this index a single query.
CREATE INDEX assignment_backwards ON assignment (value, since DESC)
    WHERE relation = 'attachment';

-- ---------------------------------------------------------------- changes
CREATE TABLE change (
    id            bigserial PRIMARY KEY,
    kind          text NOT NULL CHECK (kind IN
                  ('first_seen','disappeared','back',
                   'address_added','address_removed','moved','disconnected',
                   'attribute_changed')),
    object        uuid NOT NULL REFERENCES object(id) ON DELETE CASCADE,
    timestamp     timestamptz NOT NULL,
    before        text,
    after         text,
    source        text NOT NULL REFERENCES source(name),
    key           text,          -- which attribute: name, vlan, dhcp_lease ...
    -- A change never concerns only one object: if a device moves from port 15 to
    -- port 16, the device AND both ports are affected. The UI marks exactly those.
    affects       uuid[] NOT NULL DEFAULT '{}',
    run           bigint REFERENCES run(id)
);
CREATE INDEX change_time    ON change (timestamp DESC);
CREATE INDEX change_affects ON change USING gin (affects);

-- Without acknowledgement the change list becomes an unread stream within weeks.
CREATE TABLE acknowledgement (
    change        bigint PRIMARY KEY REFERENCES change(id) ON DELETE CASCADE,
    state         text NOT NULL CHECK (state IN ('seen','expected','ignored')),
    until         timestamptz,            -- only for 'expected'
    note          text,
    acked_by      text,
    acked_at      timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- annotations
-- Manually maintained details of an object: the wall outlet a port leads to, the
-- room, a note, an owner.
--
-- Deliberately a SEPARATE table and not a few columns on `object`. This data is
-- the opposite of everything else in the schema:
--   * no collector ever writes it - it only comes from a person,
--   * it has no validity period, it applies until someone changes it,
--   * it survives a device disappearing and coming back.
-- Putting both into one table means deleting it by accident sooner or later.
CREATE TABLE annotation (
    object        uuid PRIMARY KEY REFERENCES object(id) ON DELETE CASCADE,
    -- The outlet in the wall the port leads to: "B2-14", "office left". This is the
    -- detail that makes the difference in an emergency, because it is the only one
    -- that points from the map into the room.
    outlet        text,
    room          text,
    note          text,
    owner         text,
    -- A name given by hand beats every other name source.
    name          text,
    -- Why a finding on this object is fine. Set = confirmed.
    expected      text,
    changed_at    timestamptz NOT NULL DEFAULT now(),
    changed_by    text,
    CONSTRAINT annotation_check CHECK (outlet IS NOT NULL OR room IS NOT NULL
           OR note IS NOT NULL OR owner IS NOT NULL OR name IS NOT NULL
           OR expected IS NOT NULL)
);
CREATE INDEX annotation_outlet ON annotation (outlet) WHERE outlet IS NOT NULL;
CREATE INDEX annotation_room   ON annotation (room)   WHERE room   IS NOT NULL;
-- Full-text search over everything maintained - "where was that outlet in the
-- basement again?" ('simple': no language-specific stemming)
CREATE INDEX annotation_search ON annotation USING gin (
    to_tsvector('simple',
        coalesce(outlet,'')||' '||coalesce(room,'')||' '||
        coalesce(note,'')||' '||coalesce(name,'')));

-- ---------------------------------------------------------------- probes
-- Complete history, not just the last result. That is diagnostic history, not
-- bureaucracy: "did it already fail yesterday at this time?"
CREATE TABLE probe (
    id            bigserial PRIMARY KEY,
    object        uuid REFERENCES object(id) ON DELETE SET NULL,
    kind          text NOT NULL CHECK (kind IN
                  ('ping','traceroute','portscan','dns','wol','reachability')),
    timestamp     timestamptz NOT NULL,
    parameters    jsonb NOT NULL DEFAULT '{}',
    result        jsonb,
    successful    boolean,
    duration_ms   integer,
    triggered_by  text
);
CREATE INDEX probe_object_time ON probe (object, timestamp DESC);

-- ---------------------------------------------------------------- port counters
-- Counters and measurements of the switch ports: errors, discarded packets, PoE
-- power. Short-lived (48 hours, the collector cleans up itself) - states such as
-- link and VLAN are intervals in `assignment`; here are only values that change on
-- every run and from which the UI computes rates.
CREATE TABLE port_counter (
    port           text        NOT NULL,     -- "r2d2:gi5", or "r2d2" for the switch
    timestamp      timestamptz NOT NULL,
    in_errors      bigint,
    out_errors     bigint,
    in_discards    bigint,
    out_discards   bigint,
    poe_mw         integer,
    poe_budget_w   integer,
    PRIMARY KEY (port, timestamp)
);
CREATE INDEX port_counter_time ON port_counter (timestamp);

-- ---------------------------------------------------------------- projection
-- The canvas should not have to assemble history tables when clicking.
CREATE VIEW current AS
SELECT a.relation, a.object, a.key, a.value, a.since, a.source,
       o.kind AS object_kind, coalesce(n.name, o.display_name) AS display_name,
       n.outlet, n.room, n.note, n.owner
  FROM assignment a
  JOIN object     o ON o.id = a.object
  LEFT JOIN annotation n ON n.object = o.id
 WHERE a.until IS NULL;

COMMENT ON VIEW current IS 'The current state: all open assignments.';

-- Delta to the browser: everything that changed since a version.
CREATE VIEW since_version AS
SELECT c.id AS version, c.kind, c.object, c.timestamp, c.before, c.after, c.affects
  FROM change c
 ORDER BY c.id;

COMMENT ON VIEW since_version IS
  'GET /api/graph?since=<version> reads here - the page patches its model instead of '
  'loading a full snapshot.';

-- ---------------------------------------------------------------- time
-- Stored is `timestamptz`, an absolute point in time - UTC internally. The session
-- time zone is set per connection (daedalus/db.py) to the configured zone, so a
-- manual look via psql shows the clock time of the person next to it. That does
-- not change the stored value.
--
-- Why not store local time right away: in the night of the daylight-saving change
-- 02:30 exists TWICE in zones like Europe/Berlin. An interval ending there would no
-- longer be unambiguous, and the order of two events could not be decided.

COMMIT;
