-- Durable, non-refilling publication burst. Do not delete these counters when
-- an upload, archive or owner receipt is removed: that would restore the burst.
-- Only salted publisher/build digests are stored, never IPs or local profile IDs.
CREATE TABLE publication_rate (
    subject TEXT PRIMARY KEY,
    attempts INTEGER NOT NULL CHECK(attempts BETWEEN 1 AND 3),
    next_allowed INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL
);
--> statement-breakpoint
-- A public scheduler endpoint is safe to call without an administrative key.
-- This single durable row throttles duplicate invocations across Worker isolates.
CREATE TABLE maintenance_runs (
    name TEXT PRIMARY KEY CHECK(name = 'prune'),
    next_allowed INTEGER NOT NULL
);
