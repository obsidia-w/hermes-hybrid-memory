-- table chunks
CREATE TABLE chunks (
            id integer primary key autoincrement,
            doc_path text not null references documents(path) on delete cascade,
            heading text not null,
            level integer not null,
            content text not null,
            tags text not null,
            tokens_est integer not null,
            hash text not null unique,
            importance real not null default 1.0,
            created_at text not null,
            updated_at text not null
        );

-- table chunks_fts
CREATE VIRTUAL TABLE chunks_fts using fts5(
            heading, content, tags, doc_path unindexed, content='chunks', content_rowid='id', tokenize='unicode61'
        );

-- table chunks_fts_config
CREATE TABLE 'chunks_fts_config'(k PRIMARY KEY, v) WITHOUT ROWID;

-- table chunks_fts_data
CREATE TABLE 'chunks_fts_data'(id INTEGER PRIMARY KEY, block BLOB);

-- table chunks_fts_docsize
CREATE TABLE 'chunks_fts_docsize'(id INTEGER PRIMARY KEY, sz BLOB);

-- table chunks_fts_idx
CREATE TABLE 'chunks_fts_idx'(segid, term, pgno, PRIMARY KEY(segid, term)) WITHOUT ROWID;

-- table documents
CREATE TABLE documents (
            path text primary key,
            title text,
            mtime real not null,
            size integer not null,
            sha256 text not null,
            updated_at text not null
        );

-- table runs
CREATE TABLE runs (
            id integer primary key autoincrement,
            ran_at text not null,
            root text not null,
            documents integer not null,
            chunks integer not null
        );

-- trigger chunks_ad
CREATE TRIGGER chunks_ad after delete on chunks begin
          insert into chunks_fts(chunks_fts, rowid, heading, content, tags, doc_path)
          values('delete', old.id, old.heading, old.content, old.tags, old.doc_path);
        end;

-- trigger chunks_ai
CREATE TRIGGER chunks_ai after insert on chunks begin
          insert into chunks_fts(rowid, heading, content, tags, doc_path)
          values (new.id, new.heading, new.content, new.tags, new.doc_path);
        end;

-- trigger chunks_au
CREATE TRIGGER chunks_au after update on chunks begin
          insert into chunks_fts(chunks_fts, rowid, heading, content, tags, doc_path)
          values('delete', old.id, old.heading, old.content, old.tags, old.doc_path);
          insert into chunks_fts(rowid, heading, content, tags, doc_path)
          values (new.id, new.heading, new.content, new.tags, new.doc_path);
        end;
