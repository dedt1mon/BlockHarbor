CREATE TABLE builds (
    id TEXT PRIMARY KEY, upload TEXT NOT NULL UNIQUE, owner_hash TEXT NOT NULL,
    multipart TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('uploading','ready','removing')),
    name TEXT NOT NULL, search_name TEXT NOT NULL, category TEXT NOT NULL,
    minecraft TEXT NOT NULL, loader TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL,
    created INTEGER NOT NULL, publisher TEXT NOT NULL
);
--> statement-breakpoint
CREATE INDEX builds_published ON builds(state, created DESC, id);
--> statement-breakpoint
CREATE INDEX builds_publisher ON builds(publisher, created);
--> statement-breakpoint
CREATE TABLE parts (upload TEXT NOT NULL REFERENCES builds(upload) ON DELETE CASCADE,
    number INTEGER NOT NULL, etag TEXT NOT NULL, PRIMARY KEY(upload, number));
