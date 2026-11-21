-- Immutable staged revisions; existing published IDs remain stable.
ALTER TABLE builds ADD COLUMN object_key TEXT;
--> statement-breakpoint
CREATE TABLE revisions (
    upload TEXT PRIMARY KEY, id TEXT NOT NULL REFERENCES builds(id) ON DELETE CASCADE,
    multipart TEXT NOT NULL, object_key TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK(state IN ('uploading','ready')),
    name TEXT NOT NULL, search_name TEXT NOT NULL, category TEXT NOT NULL,
    minecraft TEXT NOT NULL, loader TEXT NOT NULL,
    size INTEGER NOT NULL, sha256 TEXT NOT NULL, base_sha TEXT NOT NULL,
    created INTEGER NOT NULL
);
--> statement-breakpoint
CREATE UNIQUE INDEX revisions_pending ON revisions(id) WHERE state='uploading';
--> statement-breakpoint
CREATE TABLE revision_parts (
    upload TEXT NOT NULL REFERENCES revisions(upload) ON DELETE CASCADE,
    number INTEGER NOT NULL, etag TEXT NOT NULL, PRIMARY KEY(upload,number)
);
--> statement-breakpoint
CREATE TABLE retired_objects (
    object_key TEXT PRIMARY KEY, id TEXT NOT NULL, created INTEGER NOT NULL
);
--> statement-breakpoint
CREATE INDEX retired_by_time ON retired_objects(created);
