-- cli-tools/archive/1 -- the shared archive schema (spec section 8.2).
--
-- One database per tool. Every row carries `identity_id`: an archive synced
-- as a personal account and one synced as a bot see different things, and
-- mixing them would misreport coverage. Search spans identities by default.
--
-- Keys follow section 8.2. Three of them are wider than the table there
-- lists, because the listed key cannot hold the row it describes:
--   * `tags` adds `label`, since a message carries more than one tag;
--   * `remaps` adds `source_rid`, since one apply remaps many rids;
--   * `checkpoints`, `coverage` keep the (rid, identity_id) pair the section
--     already gives them.
-- `scopes`, `messages` and `authors` keep the identity out of the key on
-- purpose: the object is the same object whoever saw it, and `identity_id`
-- records who put the row there.
--
-- Forward-only. This file never changes once it has shipped; a correction is
-- a new numbered file (section 8.6).

CREATE TABLE schema_version (
  source       TEXT    NOT NULL,          -- '' for the shared core, the tool's table prefix for its own files
  version      INTEGER NOT NULL,
  name         TEXT    NOT NULL,
  applied_at   TEXT    NOT NULL,
  core_version TEXT    NOT NULL DEFAULT '',
  tool_version TEXT    NOT NULL DEFAULT '',
  PRIMARY KEY (source, version)
);

CREATE TABLE identities (
  identity_id TEXT PRIMARY KEY,           -- the identity's rid
  label       TEXT NOT NULL,
  platform    TEXT NOT NULL,
  mode        TEXT NOT NULL,
  first_seen  TEXT NOT NULL
);

CREATE TABLE scopes (
  rid           TEXT PRIMARY KEY,
  identity_id   TEXT NOT NULL,
  kind          TEXT NOT NULL,
  title         TEXT NOT NULL DEFAULT '',
  path          TEXT NOT NULL DEFAULT '[]',   -- json array, the display trail
  parent_rid    TEXT,
  platform_json TEXT
);
CREATE INDEX scopes_by_identity ON scopes (identity_id);
CREATE INDEX scopes_by_parent ON scopes (parent_rid);

CREATE TABLE checkpoints (
  rid         TEXT NOT NULL,
  identity_id TEXT NOT NULL,
  newest_id   TEXT,
  oldest_id   TEXT,
  cursor      TEXT,
  last_sync   TEXT,
  last_status TEXT,
  error       TEXT,
  PRIMARY KEY (rid, identity_id)
);

CREATE TABLE coverage (
  rid            TEXT NOT NULL,
  identity_id    TEXT NOT NULL,
  visible        INTEGER NOT NULL DEFAULT 1,
  synced_from    TEXT,
  synced_to      TEXT,
  skipped_reason TEXT,
  PRIMARY KEY (rid, identity_id)
);

CREATE TABLE messages (
  rid           TEXT NOT NULL,
  message_id    TEXT NOT NULL,
  identity_id   TEXT NOT NULL,
  author_rid    TEXT,
  date          TEXT,
  text          TEXT NOT NULL DEFAULT '',
  reply_to      TEXT,
  edited        TEXT,
  deleted_at    TEXT,                        -- a deletion seen on resync, never a removed row
  platform_json TEXT,
  PRIMARY KEY (rid, message_id)
);
CREATE INDEX messages_by_scope_date ON messages (rid, date);
CREATE INDEX messages_by_identity ON messages (identity_id);
CREATE INDEX messages_by_author ON messages (author_rid);

CREATE VIRTUAL TABLE messages_fts USING fts5(
  text,
  content='messages',
  content_rowid='rowid'
);

CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages BEGIN
  INSERT INTO messages_fts (rowid, text) VALUES (new.rowid, new.text);
END;

CREATE TRIGGER messages_fts_delete AFTER DELETE ON messages BEGIN
  INSERT INTO messages_fts (messages_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
END;

CREATE TRIGGER messages_fts_update AFTER UPDATE ON messages BEGIN
  INSERT INTO messages_fts (messages_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
  INSERT INTO messages_fts (rowid, text) VALUES (new.rowid, new.text);
END;

CREATE TABLE authors (
  rid           TEXT PRIMARY KEY,
  identity_id   TEXT NOT NULL,
  label         TEXT NOT NULL DEFAULT '',
  username      TEXT,
  is_bot        INTEGER NOT NULL DEFAULT 0,
  platform_json TEXT
);

CREATE TABLE manifests (
  manifest_id       TEXT PRIMARY KEY,
  identity_id       TEXT NOT NULL,
  kind              TEXT NOT NULL,           -- media | link
  source_rid        TEXT NOT NULL,
  source_message_id TEXT NOT NULL,
  sender_rid        TEXT,
  claimed_type      TEXT,
  claimed_size      INTEGER,
  url               TEXT,
  redirect_chain    TEXT,                    -- json array
  final_url         TEXT,
  sha256            TEXT,
  verdict           TEXT,
  storage_path      TEXT,
  state             TEXT NOT NULL,
  created_at        TEXT NOT NULL
);
CREATE INDEX manifests_by_source ON manifests (source_rid, source_message_id);
CREATE INDEX manifests_by_sha ON manifests (sha256);

CREATE TABLE downloads (
  download_id   TEXT PRIMARY KEY,
  manifest_id   TEXT NOT NULL,
  identity_id   TEXT NOT NULL,
  state         TEXT NOT NULL,
  approved_at   TEXT,
  approved_by   TEXT,                        -- the approving identity's rid
  bytes_fetched INTEGER NOT NULL DEFAULT 0,
  resumable     INTEGER NOT NULL DEFAULT 0,
  last_error    TEXT
);
CREATE INDEX downloads_by_manifest ON downloads (manifest_id);

CREATE TABLE tags (
  rid         TEXT NOT NULL,
  message_id  TEXT NOT NULL,
  label       TEXT NOT NULL,
  identity_id TEXT NOT NULL,
  created     TEXT NOT NULL,
  source      TEXT NOT NULL,                 -- a rule name, or 'manual'
  PRIMARY KEY (rid, message_id, label)
);

CREATE TABLE bookmarks (
  rid         TEXT NOT NULL,
  message_id  TEXT NOT NULL,
  identity_id TEXT NOT NULL,
  label       TEXT NOT NULL DEFAULT '',
  created     TEXT NOT NULL,
  source      TEXT NOT NULL,
  PRIMARY KEY (rid, message_id)
);

CREATE TABLE remaps (
  apply_id       TEXT NOT NULL,
  source_rid     TEXT NOT NULL,
  target_rid     TEXT NOT NULL,
  identity_id    TEXT NOT NULL,
  created        TEXT NOT NULL,
  blueprint_hash TEXT NOT NULL,
  PRIMARY KEY (apply_id, source_rid)
);

CREATE TABLE rules_state (
  rule_name      TEXT PRIMARY KEY,
  identity_id    TEXT NOT NULL,
  last_evaluated TEXT,
  fire_count     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE rule_fires (
  rule_name   TEXT NOT NULL,
  event_key   TEXT NOT NULL,
  identity_id TEXT NOT NULL,
  fired_at    TEXT NOT NULL,
  actions     TEXT,                          -- json array of action names
  destination TEXT,
  PRIMARY KEY (rule_name, event_key)
);

CREATE TABLE runner_state (
  key         TEXT PRIMARY KEY,
  identity_id TEXT NOT NULL DEFAULT '',
  value       TEXT,
  updated_at  TEXT NOT NULL
);
