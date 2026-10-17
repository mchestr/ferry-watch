-- Ferry Watch 0.7.0: each user's checkout viewer. 'classic' is KasmVNC (everyone, by default); 'stream' is the
-- streamed viewer beta. A 0.6.4 ferry-web ignores the column; its inserts get the default.
-- Expand only: nothing reads it but a 0.7.0 ferry-web, and a user without a row is 'classic' too.
ALTER TABLE user_settings ADD COLUMN checkout_viewer text NOT NULL DEFAULT 'classic'
    CHECK (checkout_viewer IN ('classic', 'stream'));
