-- Waitlist (formerly Ferry Watch): browser pods are now named wl-<trip>-g<gen>. The rows of pods that ran
-- before the rename, restored from the Ferry Watch database, keep their fw- names, so the check takes both.
ALTER TABLE browser_pods DROP CONSTRAINT browser_pods_name_check;
ALTER TABLE browser_pods ADD CONSTRAINT browser_pods_name_check
    CHECK (name ~ '^(fw|wl)-[a-f0-9]{16}-g[0-9]{1,6}$');
