"""Parameterized SQL against existing auth tables. No schema creation."""


def get_user_via_email(connection, email):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT user_id, email, hashed_password FROM auth.users WHERE email = %s",
            (email.lower(),),
        )
        return cursor.fetchone()


def get_third_party_user(connection, provider, provider_user_id):
    with connection.cursor() as cursor:
        cursor.execute(
            """SELECT user_id FROM auth.third_party_accounts
               WHERE provider = %s AND provider_user_id = %s""",
            (provider, provider_user_id),
        )
        return cursor.fetchone()


def get_role_via_name(connection, name):
    with connection.cursor() as cursor:
        cursor.execute("SELECT role_id, name FROM auth.roles WHERE name = %s", (name,))
        return cursor.fetchone()


def create_user(connection, user_id, email, hashed_password, discord_user_id, roles):
    with connection.cursor() as cursor:
        cursor.execute(
            """INSERT INTO auth.users (user_id, email, hashed_password)
               VALUES (%s, %s, %s)""",
            (user_id, email, hashed_password),
        )
        cursor.execute(
            """INSERT INTO auth.third_party_accounts
               (user_id, provider, provider_user_id) VALUES (%s, %s, %s)""",
            (user_id, "discord", discord_user_id),
        )
        cursor.executemany(
            "INSERT INTO auth.user_roles (user_id, role_id) VALUES (%s, %s)",
            [(user_id, role["role_id"]) for role in roles],
        )


def get_mapped_roles(connection, discord_role_ids):
    with connection.cursor() as cursor:
        cursor.execute(
            """SELECT DISTINCT r.name FROM auth.discord_role_mappings AS m
               JOIN auth.roles AS r ON r.role_id = m.role_id
               WHERE m.discord_role_id = ANY(%s)""",
            (discord_role_ids,),
        )
        return [row["name"] for row in cursor.fetchall()]


def consume_refresh_token(connection, jti, expires_at):
    """A unique key makes concurrent reuse fail atomically."""
    with connection.cursor() as cursor:
        cursor.execute(
            """INSERT INTO auth.revoked_refresh_tokens (jti, expires_at)
               VALUES (%s, %s) ON CONFLICT (jti) DO NOTHING RETURNING jti""",
            (jti, expires_at),
        )
        return cursor.fetchone() is not None
