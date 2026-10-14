-- Ferry Watch, multi-tenant: asking for a hold, and counting checkout time once.
-- Expand only: the old ferry-web and ferry-control ignore both.

-- "Hold it for me": until then, ferry-control lets this trip's pod cart even in quiet hours or with the
-- trip's auto-cart off. Every other limit still applies. ferry-web sets it; ferry-control clears it once
-- the pod has held or failed to hold a cart.
ALTER TABLE trip_runtime ADD COLUMN hold_requested_until timestamptz;

-- The checkout seconds already counted for a pod's current boot. An agent reports its boot's running total,
-- so after a reconnect (or a ferry-control restart) only what's new is added to the user's day.
ALTER TABLE browser_pods
    ADD COLUMN vnc_boot text,
    ADD COLUMN vnc_seconds integer NOT NULL DEFAULT 0 CHECK (vnc_seconds >= 0);
-- Pods live now were counted by the ferry-control before this one: their next report sets the baseline.
UPDATE browser_pods SET vnc_boot = 'pre-0003' WHERE ended_at IS NULL;
