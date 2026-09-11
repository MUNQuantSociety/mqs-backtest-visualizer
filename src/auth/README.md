# JWT Authentication

Initial JWT authentication setup for the FastAPI server.

The goal for now is to keep the implementation simple and work from the authentication flow downward before adding SQLAlchemy/database logic.

## Dependencies

### JWT

Using the `PyJWT` package:

```bash
pip install "pyjwt[crypto]"
```

### Password Hashing

Using `pwdlib` with Argon2:

```bash
pip install "pwdlib[argon2]"
```

## Current JWT Flow

### Access Token

`create_access_token(user_id)`

Creates a short-lived JWT containing:

```python
{
    "sub": user_id,
    "type": "access",
    "exp": expiration_time,
}
```

Current access-token lifetime:

```text
5 minutes
```

### Refresh Token

`create_refresh_token(user_id)`

Creates a longer-lived JWT containing:

```python
{
    "sub": user_id,
    "type": "refresh",
    "exp": expiration_time,
}
```

### Token Pair

`obtain_token_pair(user_id)`

Returns:

```python
{
    "access": access_token,
    "refresh": refresh_token,
}
```

## Login Flow

Current planned flow:

```text
email + password
       ↓
get_user_via_email()
       ↓
verify_password()
       ↓
obtain_token_pair(user_id)
       ↓
access + refresh token
```

User lookup is currently mocked. SQLAlchemy/database integration will be added later.

## Password Hashing

Password hashing is separate from JWT signing.

```text
Password
   ↓
Argon2
   ↓
Stored password hash
```

JWT tokens use their own `SECRET_KEY` and algorithm:

```text
JWT payload
   ↓
SECRET_KEY + HS256
   ↓
Signed JWT
```

Do not use the JWT secret key for password hashing.

## Current Status

* JWT access-token creation working
* JWT refresh-token creation working
* Token-pair creation working
* Password hashing dependency added
* Basic login flow being implemented
* Dummy user data being used for now
* `ruff check` passes

## Next

Continue with the login endpoint, then:

```text
decode / verify token
        ↓
refresh endpoint
        ↓
refresh-token rotation
        ↓
logout / revocation
        ↓
SQLAlchemy integration
```
