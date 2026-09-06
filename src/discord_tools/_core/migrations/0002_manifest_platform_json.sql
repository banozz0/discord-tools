-- cli-tools/archive/1, migration 0002: `manifests` gains `platform_json`.
--
-- Section 8.2 says platform_json columns carry extras without a schema
-- change, and 0001 gave one to scopes, messages and authors but not to
-- manifests. The review queue needs it: a media candidate carries the
-- platform's own file key so the platform fetcher can find the bytes after a
-- human approves, and a link may carry a source-supplied checksum. Neither is
-- a column section 8.2 lists, so they ride here as a JSON object.
--
-- Additive: a version 1 database migrates forward and every existing row
-- reads NULL. Forward-only: this file never changes once it has shipped.

ALTER TABLE manifests ADD COLUMN platform_json TEXT;
