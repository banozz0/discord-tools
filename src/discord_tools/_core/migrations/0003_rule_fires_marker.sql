-- cli-tools/archive/1, migration 0003: `rule_fires` gains `marker`.
--
-- Section 10.3 says the origin marker a fired alert carried is also stored on
-- the fired row, and 0001 gave `rule_fires` its actions and destination but
-- no column for the marker. The rule engine writes it here: the marker names
-- the rule and the first eight hex digits of the event key, so a row can be
-- matched to the alert text a runner sees coming back.
--
-- Additive: a version 2 database migrates forward and every existing row
-- reads NULL. Forward-only: this file never changes once it has shipped.

ALTER TABLE rule_fires ADD COLUMN marker TEXT;
