-- Waitlist 1.0: watches on more than one provider (docs/waitlist/design.md §3).
-- A `trips` row is a watch on any provider: "trip" stays its name in SQL and in the agent protocol.
-- Expand only, except that the new column's default is dropped at once (design.md §3.4): every row before this
-- migration is a BC Ferries trip, and no binary older than this one ever runs against a Waitlist database.

-- ---- 1. Which provider each watch is on, fixed for its life

ALTER TABLE trips ADD COLUMN provider text NOT NULL DEFAULT 'bcferries';
-- From now on every insert says which provider: a forgotten one fails instead of becoming a BC Ferries trip.
ALTER TABLE trips ALTER COLUMN provider DROP DEFAULT;
-- The ids in waitlist.providers.PROVIDER_IDS (a test holds the two together). A new provider widens this list.
ALTER TABLE trips ADD CONSTRAINT trips_provider_check CHECK (provider IN ('bcferries', 'buntzen'));
CREATE INDEX trips_provider_live ON trips (provider, user_id) WHERE archived_at IS NULL;

CREATE FUNCTION trips_provider_is_fixed() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.provider <> OLD.provider THEN
        RAISE EXCEPTION 'a watch''s provider never changes';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER trips_provider_is_fixed BEFORE UPDATE OF provider ON trips
    FOR EACH ROW EXECUTE FUNCTION trips_provider_is_fixed();

COMMENT ON TABLE trips IS 'One watch per row, on any provider (trips.provider). Named for BC Ferries, the first.';

-- ---- 2. What only BC Ferries trips may have, and what only the others may

-- Browser pods, cart claims and checkout tickets exist for BC Ferries trips only, whatever a bug in
-- waitlist-control or waitlist-web asks for. (They fire on INSERT and on a change of trip_id: rows already there
-- aren't re-checked otherwise.)
CREATE FUNCTION bcferries_trip_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM trips WHERE id = NEW.trip_id AND provider = 'bcferries') THEN
        RAISE EXCEPTION '% rows are for BC Ferries trips only', TG_TABLE_NAME;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER browser_pods_bcferries_only BEFORE INSERT OR UPDATE OF trip_id ON browser_pods
    FOR EACH ROW EXECUTE FUNCTION bcferries_trip_only();
CREATE TRIGGER hold_claims_bcferries_only BEFORE INSERT OR UPDATE OF trip_id ON hold_claims
    FOR EACH ROW EXECUTE FUNCTION bcferries_trip_only();
CREATE TRIGGER vnc_mints_bcferries_only BEFORE INSERT OR UPDATE OF trip_id ON vnc_mints
    FOR EACH ROW EXECUTE FUNCTION bcferries_trip_only();

CREATE FUNCTION http_trip_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM trips WHERE id = NEW.trip_id AND provider <> 'bcferries') THEN
        RAISE EXCEPTION '% rows are for HTTP-checked watches only', TG_TABLE_NAME;
    END IF;
    RETURN NEW;
END;
$$;

-- ---- 3. What an HTTP check last found (waitlist-control writes it; waitlist-web reads it)

CREATE TABLE check_results (
    trip_id char(16) PRIMARY KEY REFERENCES trips (id) ON DELETE CASCADE,
    -- base.search_hash of the spec this result is for: waitlist-web shows it only while the spec still matches.
    search_hash text NOT NULL CHECK (search_hash ~ '^[a-f0-9]{16}$'),
    outcome text NOT NULL DEFAULT 'pending' CHECK (outcome IN ('pending', 'ok', 'no_inventory', 'error')),
    -- waitlist.providers.base.Option, as msgspec writes them: at most 20, at most 16 KiB.
    options jsonb NOT NULL DEFAULT '[]'
        CHECK (jsonb_typeof(options) = 'array' AND jsonb_array_length(options) <= 20
               AND octet_length(options::text) <= 16384),
    attempted_at timestamptz,  -- the last check that reached the site, whatever came of it
    checked_at timestamptz,  -- the last check in which at least one of the watch's queries read the site
    error_class text CHECK (error_class ~ '^[a-z_]{1,32}$'),  -- base.CheckErrorKind of the last failed check
    consecutive_errors integer NOT NULL DEFAULT 0 CHECK (consecutive_errors >= 0),
    error_since timestamptz,  -- the first failure of the current streak (NULL while checks succeed)
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER check_results_http_only BEFORE INSERT OR UPDATE OF trip_id ON check_results
    FOR EACH ROW EXECUTE FUNCTION http_trip_only();

-- ---- 4. Two more fleet_state keys: the admin's per-provider pause, and the check loop's last report

ALTER TABLE fleet_state
    DROP CONSTRAINT fleet_state_key_check,
    ADD CONSTRAINT fleet_state_key_check CHECK (key IN (
        'brake', 'breaker', 'maintenance', 'pause_carting', 'control_alive_at', 'fleet_report',
        'paused_providers', 'check_report'
    ));
-- Seeded, so waitlist-web (which may only update it) can flip it: a JSON array of provider ids, sorted.
INSERT INTO fleet_state (key, value) VALUES ('paused_providers', '[]') ON CONFLICT (key) DO NOTHING;
