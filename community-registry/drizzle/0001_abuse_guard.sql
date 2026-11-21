-- Apply once to the existing D1 database before deploying rate-limited Worker.
-- Only salted/HMAC anonymized publisher identifiers are stored, never raw IPs.
CREATE TABLE IF NOT EXISTS publisher_rate (
    subject TEXT PRIMARY KEY,
    window_start INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    blocked_until INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL
);
--> statement-breakpoint
CREATE INDEX IF NOT EXISTS publisher_rate_cleanup ON publisher_rate(updated_at, blocked_until);
