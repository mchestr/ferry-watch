-- Ferry Watch, multi-tenant: what the admin pages need (docs/multi-tenant/design-reference.md §9.4).
-- Expand only: each check below accepts everything the old one did.

-- An admin can recycle a trip's pod: the hub drains it, and the fleet starts the next generation.
ALTER TABLE agent_commands
    DROP CONSTRAINT agent_commands_op_check,
    ADD CONSTRAINT agent_commands_op_check CHECK (op IN ('check_now', 'release', 'dismiss', 'recycle'));

-- What the fleet loop knew at the end of its last pass, for the Fleet page.
ALTER TABLE fleet_state
    DROP CONSTRAINT fleet_state_key_check,
    ADD CONSTRAINT fleet_state_key_check
        CHECK (key IN ('brake', 'breaker', 'maintenance', 'pause_carting', 'control_alive_at', 'fleet_report'));
