-- Ferry Watch, multi-tenant: everyone has quiet hours, 10 pm to 5 am Pacific unless they change them. The
-- ferries sail from about 5 am to 10 pm, so that's when trips are watched.
-- Expand only: the times stay nullable for a release, because a 0.6.3 ferry-web still running during the
-- rollout (or after a rollback) stores nulls when someone turns quiet hours off. Nulls read as the default
-- (quiet_hours() in polling.py). A later release fills any nulls again and makes the times NOT NULL.

-- Whoever had turned them off in 0.6.3 gets the default. A user without a row has the default too
-- (DEFAULT_QUIET_HOURS in polling.py, the same times), so nobody needs a row added.
UPDATE user_settings SET quiet_start = '22:00', quiet_end = '05:00' WHERE quiet_start IS NULL OR quiet_end IS NULL;

ALTER TABLE user_settings
    ALTER COLUMN quiet_start SET DEFAULT '22:00',
    ALTER COLUMN quiet_end SET DEFAULT '05:00';
