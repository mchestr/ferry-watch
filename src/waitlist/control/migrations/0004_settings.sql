-- Ferry Watch, multi-tenant: each user's quiet hours, pausing per trip, and versions on the admin page.
-- Expand only: the old ferry-web and ferry-control ignore the new table and columns.

-- Each user's own settings. No row, or no times: no quiet hours (the default). In their quiet hours (Pacific,
-- like the ferries; the end may be past midnight) a user's trips aren't checked at all.
CREATE TABLE user_settings (
    user_id text PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
    quiet_start time,
    quiet_end time,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((quiet_start IS NULL) = (quiet_end IS NULL)),
    CHECK (quiet_start <> quiet_end)
);

-- A manual check lifts the user's quiet hours for this trip until then ("Hold it for me" does too, through
-- hold_requested_until). ferry-web sets it; ferry-control reads it.
ALTER TABLE trip_runtime ADD COLUMN quiet_lifted_until timestamptz;

-- The agent version each pod said hello with, for the admin page.
ALTER TABLE browser_pods ADD COLUMN agent_version text CHECK (length(agent_version) <= 64);

-- "Pause all my trips" is gone: trips are paused one at a time. Whoever had paused everything gets each live
-- trip paused instead, so nothing starts checking again by itself. users.paused is dropped a release later.
UPDATE trips SET enabled = false, spec = jsonb_set(spec, '{enabled}', 'false'), rev = rev + 1, updated_at = now()
    WHERE archived_at IS NULL AND enabled AND user_id IN (SELECT id FROM users WHERE paused);
UPDATE users SET paused = false WHERE paused;
