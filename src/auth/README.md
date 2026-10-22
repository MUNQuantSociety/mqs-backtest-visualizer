# Authentication

Auth is self-contained in this directory. It uses psycopg2 and parameterized
SQL, without importing SQLAlchemy or the application's database/settings modules.

- `config.py`: loads the repository-root `.env`; environment variables take precedence.
- `db.py`: opens a connection per operation, commits on success, rolls back on
  exceptions, and always closes it. Route transactions commit before responses are sent.
- `repository.py`: all database queries; change table/column names here if needed.
- `schemas.py`: request models.
- `passwords.py`: Argon2 password hashing and verification.
- `jwt.py`: token signing and validation.
- `router.py`: login, registration, and refresh endpoints.
- `services/discord.py`: Discord API calls and role mapping lookup.

## Configuration

Database credentials come from `POSTGRES_HOST`, `POSTGRES_PORT` (default 25060),
`POSTGRES_DB`, `POSTGRES_USER`, and `POSTGRES_PASSWORD` in `.env`.
Optional: `POSTGRES_SSLMODE` (default prefer) and
`DB_CONNECT_TIMEOUT_SECONDS` (default 3).

Existing token settings are still required: `SECRET_KEY`, `ALGORITHM`,
`ACCESS_TOKEN_EXPIRE_MINUTES`, `TEMP_TOKEN_EXPIRE_MINUTES`, and
`REFRESH_TOKEN_EXPIRE_DAYS`. Discord uses `DISCORD_BASE_URL`,
`DISCORD_REDIRECT_URI`, `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET`, and
`DISCORD_GUILD_ID`.

## Expected existing schema

Share [`database_schema.py`](database_schema.py) with the database team. It
contains the complete proposed PostgreSQL DDL: fields, types, keys, foreign
keys, indexes, and provisioning notes. It uses no ORM or project dependencies.
Running `python src/auth/database_schema.py` only prints SQL for review; it
never connects to a database. The SQL describes a fresh schema, not a migration.


These names are the assumed database contract, not a verified live schema.
No tables, schema, seed data, or migrations are created by this code.

| Table | Columns used | Required constraints |
| --- | --- | --- |
| `auth.users` | `user_id` UUID, `email` text, `hashed_password` text | Primary key `user_id`; unique `email` |
| `auth.roles` | `role_id`, `name` text | Primary key `role_id`; unique `name` |
| `auth.user_roles` | `user_id`, `role_id` | Unique pair; foreign keys to users and roles |
| `auth.third_party_accounts` | `user_id`, `provider` text, `provider_user_id` text | Unique `(provider, provider_user_id)`; user foreign key |
| `auth.discord_role_mappings` | `discord_role_id` text, `role_id` | Unique pair; role foreign key |
| `auth.revoked_refresh_tokens` | `jti` UUID, `expires_at` timestamptz | Primary key or unique `jti` |

Role IDs must have matching types across referencing tables. Store emails in
lowercase; auth normalizes them before lookup/insertion. Roles and Discord role
mappings must already be populated. There is no built-in test account.

Registration inserts the user, Discord account, and roles in one transaction.
Database uniqueness constraints also protect concurrent registrations.
Refresh rotation inserts the consumed token ID using `ON CONFLICT DO NOTHING`;
only the first caller receives a replacement pair. Revocations survive restarts
and are shared across workers. Expired revocation rows can be deleted once
`expires_at < now()`; there is no automatic cleanup job.

Missing tables, incorrect columns, and connection failures raise database errors;
they are never treated as missing users or successful authentication.

The existing Discord callback still needs OAuth state validation coordinated with
the frontend before production use.

## Local checks

Run `python -m pytest -q src/auth/tests` and `ruff check src/auth`.
The tests use fake connections and do not contact or modify the live database.
