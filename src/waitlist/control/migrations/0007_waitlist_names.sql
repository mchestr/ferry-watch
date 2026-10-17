-- Waitlist (formerly Ferry Watch): browser pods are now named wl-<trip>-g<gen>. 0001's check allowed only the
-- old fw- names; it still allows them, so a database created before the rename stays valid. On a new database
-- this only relaxes the check.
ALTER TABLE browser_pods DROP CONSTRAINT browser_pods_name_check;
ALTER TABLE browser_pods ADD CONSTRAINT browser_pods_name_check
    CHECK (name ~ '^(fw|wl)-[a-f0-9]{16}-g[0-9]{1,6}$');
