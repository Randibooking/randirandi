-- SPDX-License-Identifier: AGPL-3.0-only
-- The database's two users.
--
--   randirandi_owner  owns the tables. Migrations run as this user.
--   randirandi_app    reads and writes rows. It cannot create, change or
--                     drop a table. The app and the job runner run as it.
--
-- Run at every start of the laptop's stack, over the socket, as the
-- superuser. It is safe to run again: it makes what is missing and sets each
-- password to what the environment holds, so a password is changed by
-- changing it in the environment and starting the stack again.
--
-- This is the part only a superuser can do. At the platform it is not run:
-- there the existing two users are adopted by OpenTofu. What the
-- owner of the tables gives the app's user is the same in both places and is
-- not here: python manage.py grants, run before the migrations.

\set ON_ERROR_STOP on
\getenv owner_password RANDIRANDI_DB_OWNER_PASSWORD
\getenv app_password RANDIRANDI_DB_APP_PASSWORD

SELECT 'CREATE ROLE randirandi_owner'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'randirandi_owner') \gexec
SELECT 'CREATE ROLE randirandi_app'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'randirandi_app') \gexec

ALTER ROLE randirandi_owner
    LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
    PASSWORD :'owner_password';
ALTER ROLE randirandi_app
    LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
    PASSWORD :'app_password';

-- Nobody connects unless it is said here.
REVOKE ALL ON DATABASE postgres FROM PUBLIC;
REVOKE ALL ON DATABASE randirandi FROM PUBLIC;
ALTER DATABASE randirandi OWNER TO randirandi_owner;
GRANT CONNECT ON DATABASE randirandi TO randirandi_app;

\connect randirandi

ALTER SCHEMA public OWNER TO randirandi_owner;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
