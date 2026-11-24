-- Apply before the notification-aware Worker. No archives or existing rows are removed.
ALTER TABLE builds ADD COLUMN expired_at INTEGER;
--> statement-breakpoint
ALTER TABLE builds ADD COLUMN cleanup_after INTEGER NOT NULL DEFAULT 0;
--> statement-breakpoint
ALTER TABLE builds ADD COLUMN cleanup_attempts INTEGER NOT NULL DEFAULT 0;
--> statement-breakpoint
CREATE INDEX builds_cleanup ON builds(cleanup_after, state, created);
--> statement-breakpoint
-- A small owner-only receipt survives physical archive deletion and Worker restarts.
-- It contains neither the owner token nor archive/name/user contents. The owner
-- acknowledges it only after durably saving the notification on their device.
CREATE TABLE publication_events (
    id TEXT PRIMARY KEY,
    owner_hash TEXT NOT NULL,
    expired_at INTEGER NOT NULL,
    removed_at INTEGER NOT NULL
);
