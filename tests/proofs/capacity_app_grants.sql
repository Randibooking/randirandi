-- SPDX-License-Identifier: AGPL-3.0-only
-- Part of the capacity proof, applied by the database owner after table grants.
-- {app} is a quoted role identifier supplied by the proof installer.
-- The copies follow their asset by cascade, which runs as the tables' owner;
-- the application changes only what it may.
REVOKE UPDATE ON ALL TABLES IN SCHEMA public FROM {app};
GRANT UPDATE (space_id, whole_space) ON asset TO {app};
GRANT UPDATE (state, mode, quantity, starts_at, ends_at) ON taken_time TO {app};

-- No UPDATE on allocation: a claim's spots are replaced, never changed.
-- SELECT/INSERT/DELETE remain granted by the installer. Deleting a whole
-- reservation with its allocations is an explicit lifecycle operation; this
-- proof is not an append-only audit log.
