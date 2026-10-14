-- Ferry Watch, multi-tenant: the first schema (docs/multi-tenant/design-reference.md §6.4).
-- Expand/contract only: later migrations add, then (a release later) remove.

-- ---- people

CREATE TABLE users (
    id text PRIMARY KEY CHECK (id ~ '^[a-z0-9]{20}$'),
    state text NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'suspended')),
    username text NOT NULL,
    email text,
    display text,
    is_admin boolean NOT NULL DEFAULT false,
    paused boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz
);

CREATE TABLE identities (
    iss text NOT NULL,
    sub text NOT NULL,
    uid text NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    username text,
    email text,
    last_groups text[] NOT NULL DEFAULT '{}',
    first_seen timestamptz NOT NULL DEFAULT now(),
    last_seen timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (iss, sub)
);

CREATE TABLE unmatched_identities (
    iss text NOT NULL,
    sub text NOT NULL,
    preferred_username text,
    email text,
    first_seen timestamptz NOT NULL DEFAULT now(),
    last_seen timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (iss, sub)
);

CREATE TABLE invites (
    id text PRIMARY KEY,
    match_claim text NOT NULL CHECK (match_claim IN ('email', 'preferred_username')),
    match_value text NOT NULL,
    email text,
    limits jsonb NOT NULL DEFAULT '{}',
    reason text,
    created_by text REFERENCES users (id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    used_by_uid text REFERENCES users (id) ON DELETE SET NULL,
    used_at timestamptz
);

CREATE TABLE sessions (
    token_hash bytea PRIMARY KEY CHECK (length(token_hash) = 32),
    user_id text NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    iss text NOT NULL,
    sub text NOT NULL,
    client_label text,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_used_at timestamptz NOT NULL DEFAULT now(),
    verified_at timestamptz NOT NULL DEFAULT now(),
    idle_expires_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    revoked_reason text
);
CREATE INDEX sessions_user ON sessions (user_id);

CREATE TABLE user_limits (
    user_id text NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    key text NOT NULL,
    value integer NOT NULL CHECK (value >= 0),
    set_by text,
    reason text,
    set_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, key)
);

-- ---- trips

CREATE TABLE trips (
    id char(16) PRIMARY KEY CHECK (id ~ '^[a-f0-9]{16}$'),
    user_id text NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    rev bigint NOT NULL DEFAULT 1,
    spec jsonb NOT NULL,
    enabled boolean NOT NULL DEFAULT true,
    paused_reason text,
    checkout_mode text NOT NULL DEFAULT 'guest' CHECK (checkout_mode IN ('guest')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    archived_at timestamptz
);
CREATE INDEX trips_user ON trips (user_id) WHERE archived_at IS NULL;

CREATE FUNCTION trips_owner_is_fixed() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.user_id <> OLD.user_id THEN
        RAISE EXCEPTION 'a trip''s owner never changes';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER trips_owner_is_fixed BEFORE UPDATE ON trips FOR EACH ROW EXECUTE FUNCTION trips_owner_is_fixed();

CREATE TABLE trip_runtime (
    trip_id char(16) PRIMARY KEY REFERENCES trips (id) ON DELETE CASCADE,
    phase text NOT NULL DEFAULT 'off',
    mode text CHECK (mode IN ('warm', 'short_check', 'off')),
    reason text,
    queue_position integer,
    queued_since timestamptz,
    next_check_at timestamptz,
    last_check_at timestamptz,
    gen integer NOT NULL DEFAULT 0,
    failures integer NOT NULL DEFAULT 0,
    backoff_until timestamptz,
    needs_attention boolean NOT NULL DEFAULT false,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE alert_memory (
    trip_id char(16) PRIMARY KEY REFERENCES trips (id) ON DELETE CASCADE,
    key_hash text NOT NULL,
    alerted text[] NOT NULL DEFAULT '{}',
    offered text[] NOT NULL DEFAULT '{}',
    held text
);

-- ---- browser pods (rows outlive their trips: they are the pod history)

CREATE TABLE browser_pods (
    name text PRIMARY KEY CHECK (name ~ '^fw-[a-f0-9]{16}-g[0-9]{1,6}$'),
    trip_id char(16) NOT NULL,
    uid text UNIQUE,
    user_id text NOT NULL,
    gen integer NOT NULL CHECK (gen >= 1),
    mode text NOT NULL CHECK (mode IN ('warm', 'short_check')),
    phase text NOT NULL DEFAULT 'creating'
        CHECK (phase IN ('creating', 'starting', 'watching', 'holding', 'draining', 'failed', 'gone')),
    egress text NOT NULL DEFAULT 'allowlist' CHECK (egress IN ('allowlist', 'hold')),
    node text,
    image_digest text,
    created_at timestamptz NOT NULL DEFAULT now(),
    deadline_at timestamptz NOT NULL,
    ended_at timestamptz,
    end_reason text
);
CREATE UNIQUE INDEX browser_pods_one_per_trip ON browser_pods (trip_id) WHERE ended_at IS NULL;

CREATE TABLE hold_claims (
    user_id text PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,  -- one hold per user at a time
    trip_id char(16) NOT NULL,
    pod text NOT NULL,
    claimed_at timestamptz NOT NULL DEFAULT now(),
    cart_id text
);

CREATE TABLE trip_state (
    trip_id char(16) PRIMARY KEY REFERENCES trips (id) ON DELETE CASCADE,
    pod text,
    boot text,
    seq bigint,
    agent_phase text,
    checked_at timestamptz,
    next_due timestamptz,
    effective_interval integer,
    sailings jsonb NOT NULL DEFAULT '[]',
    error_class text,
    consecutive_errors integer NOT NULL DEFAULT 0,
    cart jsonb,
    hold_ready_at timestamptz,
    first_viewer_at timestamptz,
    viewer_connected boolean NOT NULL DEFAULT false,
    heartbeat_at timestamptz,
    lockdown_ok boolean NOT NULL DEFAULT false,
    working_set_mb integer,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE agent_commands (
    id bigserial PRIMARY KEY,
    trip_id char(16) NOT NULL,
    op text NOT NULL CHECK (op IN ('check_now', 'release', 'dismiss')),
    cart_id text,
    requested_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    consumed_at timestamptz
);
CREATE INDEX agent_commands_pending ON agent_commands (trip_id) WHERE consumed_at IS NULL;

-- ---- what users hear and see

CREATE TABLE events (
    id bigserial PRIMARY KEY,
    user_id text NOT NULL,
    trip_id char(16),
    ts timestamptz NOT NULL DEFAULT now(),
    kind text NOT NULL,
    code text NOT NULL,
    params jsonb NOT NULL DEFAULT '{}',
    dedup_key text UNIQUE  -- pod:boot:seq for agent events, so a replay after a reconnect isn't counted twice
);
CREATE INDEX events_user_ts ON events (user_id, ts DESC);

CREATE TABLE notifications (
    id bigserial PRIMARY KEY,
    user_id text NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    trip_id char(16),
    kind text NOT NULL,
    params jsonb NOT NULL DEFAULT '{}',
    dedup_key text UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now(),
    sent_at timestamptz,
    attempts integer NOT NULL DEFAULT 0,
    last_error text
);
CREATE INDEX notifications_unsent ON notifications (created_at) WHERE sent_at IS NULL;

CREATE TABLE push_subscriptions (
    id bigserial PRIMARY KEY,
    user_id text NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    endpoint text NOT NULL UNIQUE,
    p256dh text NOT NULL,
    auth text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_ok timestamptz,
    failures integer NOT NULL DEFAULT 0
);

CREATE TABLE vnc_mints (
    jti text PRIMARY KEY,
    user_id text NOT NULL,
    trip_id char(16) NOT NULL,
    pod text NOT NULL,
    aud text NOT NULL,
    cart_id text,  -- only the checkout audience has one
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE hold_outcomes (
    id bigserial PRIMARY KEY,
    user_id text NOT NULL,
    trip_id char(16) NOT NULL,
    day date NOT NULL,
    outcome text NOT NULL,
    answer_reason text,
    fare text CHECK (length(fare) <= 32),
    vehicle_type text,
    login_seen text NOT NULL DEFAULT 'none' CHECK (login_seen IN ('none', 'visit', 'forced')),
    release_check text,
    checkout_seconds integer,
    pod text,
    created_at timestamptz NOT NULL DEFAULT now(),
    dedup_key text UNIQUE  -- pod:boot:seq of the message it came from
);

CREATE TABLE usage_ledger (
    user_id text NOT NULL,
    day date NOT NULL,  -- Pacific
    kind text NOT NULL
        CHECK (kind IN ('holds', 'cart_attempts', 'vnc_seconds', 'manual_checks', 'test_alerts', 'mints', 'pod_starts')),
    amount bigint NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day, kind)
);

-- ---- the fleet and the record

CREATE TABLE fleet_state (
    key text PRIMARY KEY CHECK (key IN ('brake', 'breaker', 'maintenance', 'pause_carting', 'control_alive_at')),
    value jsonb NOT NULL
);
-- The admin's two switches exist from the start, so ferry-web (which may only update them) can flip them.
INSERT INTO fleet_state (key, value) VALUES ('maintenance', 'false'), ('pause_carting', 'false');

CREATE TABLE audit_log (
    id bigserial PRIMARY KEY,
    ts timestamptz NOT NULL DEFAULT now(),
    actor_kind text NOT NULL CHECK (actor_kind IN ('user', 'admin', 'system')),
    actor text NOT NULL,
    action text NOT NULL,
    target_kind text,
    target_id text,
    before jsonb,
    after jsonb,
    reason text,
    request_id text
);
