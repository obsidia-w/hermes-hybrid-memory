-- table events
CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event TEXT NOT NULL,
                object_id TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

-- table jobs
CREATE TABLE jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                source_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(kind, source_id)
            );

-- table memories
CREATE TABLE memories (
                id TEXT PRIMARY KEY,
                normalized_key TEXT NOT NULL UNIQUE,
                path TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                title TEXT NOT NULL,
                statement TEXT NOT NULL,
                subject_id TEXT NOT NULL,
                predicate TEXT NOT NULL,
                value TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                confidence REAL NOT NULL,
                importance REAL NOT NULL,
                stability TEXT NOT NULL,
                sensitivity TEXT NOT NULL,
                evidence_count INTEGER NOT NULL DEFAULT 1,
                source_ids TEXT NOT NULL,
                superseded_by TEXT,
                related_ids TEXT NOT NULL DEFAULT '[]',
                promotion_target TEXT,
                promotion_score REAL NOT NULL DEFAULT 0,
                direct INTEGER NOT NULL DEFAULT 0,
                recall_count INTEGER NOT NULL DEFAULT 0,
                last_recalled_at TEXT
            );

-- table memory_fts
CREATE VIRTUAL TABLE memory_fts USING fts5(
                memory_id UNINDEXED,
                title,
                statement,
                kind,
                tokenize='unicode61'
            );

-- table memory_fts_config
CREATE TABLE 'memory_fts_config'(k PRIMARY KEY, v) WITHOUT ROWID;

-- table memory_fts_content
CREATE TABLE 'memory_fts_content'(id INTEGER PRIMARY KEY, c0, c1, c2, c3);

-- table memory_fts_data
CREATE TABLE 'memory_fts_data'(id INTEGER PRIMARY KEY, block BLOB);

-- table memory_fts_docsize
CREATE TABLE 'memory_fts_docsize'(id INTEGER PRIMARY KEY, sz BLOB);

-- table memory_fts_idx
CREATE TABLE 'memory_fts_idx'(segid, term, pgno, PRIMARY KEY(segid, term)) WITHOUT ROWID;

-- table sources
CREATE TABLE sources (
                id TEXT PRIMARY KEY,
                path TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL,
                session_id TEXT NOT NULL DEFAULT '',
                turn_number INTEGER NOT NULL DEFAULT 0,
                captured_at TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                user_content TEXT NOT NULL DEFAULT '',
                assistant_content TEXT NOT NULL DEFAULT '',
                sensitivity TEXT NOT NULL DEFAULT 'private'
            );
