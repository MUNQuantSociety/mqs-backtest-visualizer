"""Verify Cognito access tokens using only the configured pool's trusted JWKS."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

import jwt
from jwt import PyJWKClient


class InvalidAccessToken(Exception):
    """The supplied credential cannot authenticate a caller."""


class AuthenticationUnavailable(Exception):
    """Authentication configuration or the trusted key endpoint is unavailable."""


@dataclass(frozen=True)
class CognitoIdentity:
    issuer: str
    subject: str


class CognitoVerifier:
    def __init__(self, issuer: str, client_id: str):
        if not re.fullmatch(
            r"https://cognito-idp\.[a-z0-9-]+\.amazonaws\.com(?:\.cn)?/[a-z0-9-]+_[A-Za-z0-9]+",
            issuer,
        ) or not re.fullmatch(r"[a-zA-Z0-9]{1,128}", client_id):
            raise AuthenticationUnavailable("Cognito authentication is not configured.")
        self.issuer = issuer
        self.client_id = client_id
        # PyJWT serializes key lookups and limits unknown-kid refreshes. Do not
        # use cache_keys: a separate unbounded-age key cache defeats JWKS expiry.
        self.jwks = PyJWKClient(
            issuer + "/.well-known/jwks.json", cache_keys=False,
            cache_jwk_set=True, lifespan=300, timeout=5, cooldown_duration=30,
        )

    def verify(self, token: str) -> CognitoIdentity:
        if not token or len(token) > 16_384:
            raise InvalidAccessToken()
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
                raise InvalidAccessToken()
            if not 1 <= len(header["kid"]) <= 256:
                raise InvalidAccessToken()
            # Never read jku/x5u or choose the issuer/JWKS URL from token claims.
            key = self.jwks.get_signing_key(header["kid"])
            claims = jwt.decode(
                token, key.key, algorithms=["RS256"], issuer=self.issuer,
                leeway=30,
                options={
                    "require": ["iss", "sub", "exp", "iat", "client_id", "token_use"],
                    # Cognito access tokens identify the app with client_id;
                    # aud is optional resource binding, not the ID-token aud.
                    "verify_aud": False,
                },
            )
            if claims["token_use"] != "access" or claims["client_id"] != self.client_id:
                raise InvalidAccessToken()
            if not isinstance(claims["sub"], str) or not 1 <= len(claims["sub"]) <= 2048:
                raise InvalidAccessToken()
            if any(type(claims[name]) is not int for name in ("iat", "exp")):
                raise InvalidAccessToken()
            if claims["iat"] >= claims["exp"]:
                raise InvalidAccessToken()
        except jwt.PyJWKClientConnectionError as exc:
            raise AuthenticationUnavailable("The trusted signing keys are unavailable.") from exc
        except (jwt.InvalidTokenError, jwt.PyJWKClientError, ValueError, TypeError) as exc:
            raise InvalidAccessToken() from exc
        return CognitoIdentity(issuer=self.issuer, subject=claims["sub"])


@lru_cache(maxsize=4)
def get_verifier(issuer: str, client_id: str) -> CognitoVerifier:
    return CognitoVerifier(issuer, client_id)
