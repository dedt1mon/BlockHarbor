-- Public cosmetic data only. Owner credentials remain on the player's computer;
-- the service stores their digest and a salted network rate-limit identity.
CREATE TABLE IF NOT EXISTS skin_profiles (
    username TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    owner_hash TEXT NOT NULL UNIQUE,
    publisher TEXT NOT NULL,
    model TEXT NOT NULL CHECK (model IN ('classic', 'slim')),
    sha256 TEXT NOT NULL,
    png BLOB NOT NULL CHECK (length(png) <= 24576),
    updated_at INTEGER NOT NULL
);
--> statement-breakpoint
CREATE INDEX IF NOT EXISTS skin_profiles_texture ON skin_profiles(sha256, updated_at);
--> statement-breakpoint
CREATE INDEX IF NOT EXISTS skin_profiles_publisher ON skin_profiles(publisher);
--> statement-breakpoint
CREATE TABLE IF NOT EXISTS skin_rate (
    subject TEXT PRIMARY KEY,
    window_start INTEGER NOT NULL,
    attempts INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
--> statement-breakpoint
CREATE INDEX IF NOT EXISTS skin_rate_cleanup ON skin_rate(updated_at);
