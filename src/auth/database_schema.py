"""PostgreSQL schema contract to share with the database team.

This file needs only Python's standard library. It does not connect to a
database, load credentials, or create tables. Running it prints the proposed
SQL for review: ``python src/auth/database_schema.py``.

Names and types below match repository.py. This describes a fresh schema;
it is not a migration for existing tables. The team should review ownership
and grants before applying it through their normal database workflow.

Application-generated user IDs and refresh-token IDs are UUIDs. Role IDs are
stable text identifiers, supplied when roles are provisioned. Discord IDs are
text so they match Discord API responses and the role lookup's text array.
"""

SCHEMA_SQL = """\
BEGIN;

CREATE SCHEMA auth;

CREATE TABLE auth.users (
    user_id UUID PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    hashed_password TEXT NOT NULL,
    CONSTRAINT users_email_lowercase CHECK (email = lower(email))
);
COMMENT ON COLUMN auth.users.hashed_password IS
    'Argon2 password hash produced by pwdlib; never a plaintext password.';

CREATE TABLE auth.roles (
    role_id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE auth.user_roles (
    user_id UUID NOT NULL REFERENCES auth.users(user_id) ON DELETE CASCADE,
    role_id TEXT NOT NULL REFERENCES auth.roles(role_id),
    PRIMARY KEY (user_id, role_id)
);

CREATE TABLE auth.third_party_accounts (
    user_id UUID NOT NULL REFERENCES auth.users(user_id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    provider_user_id TEXT NOT NULL,
    PRIMARY KEY (provider, provider_user_id)
);
CREATE INDEX third_party_accounts_user_id_idx
    ON auth.third_party_accounts (user_id);

CREATE TABLE auth.discord_role_mappings (
    discord_role_id TEXT NOT NULL,
    role_id TEXT NOT NULL REFERENCES auth.roles(role_id),
    PRIMARY KEY (discord_role_id, role_id)
);

CREATE TABLE auth.revoked_refresh_tokens (
    jti UUID PRIMARY KEY,
    expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX revoked_refresh_tokens_expires_at_idx
    ON auth.revoked_refresh_tokens (expires_at);
COMMENT ON TABLE auth.revoked_refresh_tokens IS
    'Consumed refresh token IDs. Unique jti prevents concurrent token reuse.';

COMMIT;
"""

PROVISIONING_NOTES = """\
-- Provision roles and Discord mappings separately using real team-approved values.
-- Suggested role rows (IDs are stable text identifiers, not Discord role IDs):
-- INSERT INTO auth.roles (role_id, name) VALUES
--     ('role-member', 'member'),
--     ('role-developer', 'developer'),
--     ('role-admin', 'admin');
-- INSERT INTO auth.discord_role_mappings (discord_role_id, role_id)
-- VALUES ('<actual-discord-role-id>', 'role-member');
-- Each Discord role may map to multiple application roles.
-- No users or passwords are seeded.
--
-- Runtime database role needs USAGE on schema auth, SELECT on users, roles,
-- third_party_accounts, and discord_role_mappings; INSERT on users,
-- third_party_accounts, user_roles, and revoked_refresh_tokens; and SELECT
-- on revoked_refresh_tokens for INSERT ... RETURNING.
-- Provisioning and maintenance permissions are managed separately.
--
-- Optional maintenance, after token expiry (never delete unexpired rows):
-- DELETE FROM auth.revoked_refresh_tokens WHERE expires_at < now();
"""

if __name__ == "__main__":
    print(SCHEMA_SQL)
    print(PROVISIONING_NOTES)
