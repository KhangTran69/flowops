"""Authentication boundary for protected automation actions."""

from dataclasses import dataclass
import json
import os
import secrets
import time
from typing import Any, Optional

import jwt
import requests


@dataclass(frozen=True)
class Actor:
    subject: str
    roles: tuple[str, ...]
    source: str = "local"  # local | bearer | session


class AuthenticationError(ValueError):
    """Raised when a protected action has no valid bearer token."""


def _roles(claims: dict[str, Any]) -> tuple[str, ...]:
    values = claims.get("roles", claims.get("scp", ""))
    if isinstance(values, str):
        return tuple(values.split())
    return tuple(str(value) for value in values or ())


def _authenticate_jwt(token: str, values: dict[str, str]) -> Actor:
    issuer = values.get("FLOWOPS_ENTRA_ISSUER")
    audience = values.get("FLOWOPS_ENTRA_AUDIENCE")
    jwks_url = values.get("FLOWOPS_ENTRA_JWKS_URL")
    required_role = values.get("FLOWOPS_ENTRA_REQUIRED_ROLE", "automation.operator")
    if not issuer or not audience or not jwks_url:
        raise AuthenticationError("Entra issuer, audience, and JWKS URL are required.")

    cache = _authenticate_jwt.__dict__.setdefault("cache", {})
    now = time.time()
    keys, expires = cache.get(jwks_url, (None, 0))
    if keys is None or expires <= now:
        try:
            response = requests.get(jwks_url, timeout=5)
            response.raise_for_status()
            keys = response.json()["keys"]
            cache[jwks_url] = (keys, now + 300)
        except (requests.RequestException, KeyError, TypeError, ValueError) as error:
            raise AuthenticationError("Unable to retrieve Entra signing keys.") from error

    try:
        header = jwt.get_unverified_header(token)
        key_data = next(item for item in keys if item.get("kid") == header.get("kid"))
        key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(key_data))
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=audience,
            issuer=issuer,
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except (jwt.PyJWTError, StopIteration, TypeError, ValueError) as error:
        raise AuthenticationError("Entra bearer token was rejected.") from error

    roles = _roles(claims)
    if required_role and required_role not in roles:
        raise AuthenticationError("Bearer token lacks the required automation role.")
    return Actor(subject=claims["sub"], roles=roles)


def validate_auth_configuration(
    environ: Optional[dict[str, str]] = None,
) -> None:
    """Fail fast when protected mode lacks a usable credential source."""
    values = environ if environ is not None else os.environ
    if values.get("FLOWOPS_AUTH_REQUIRED", "false").lower() != "true":
        return
    if values.get("FLOWOPS_ENTRA_ISSUER"):
        if not values.get("FLOWOPS_ENTRA_AUDIENCE") or not values.get(
            "FLOWOPS_ENTRA_JWKS_URL"
        ):
            raise AuthenticationError(
                "Entra issuer, audience, and JWKS URL must all be configured."
            )
        return
    environment = values.get("FLOWOPS_ENVIRONMENT", "development").lower()
    if environment == "development" and values.get("FLOWOPS_DEV_BEARER_TOKEN"):
        return
    raise AuthenticationError(
        "Protected mode requires Entra configuration or a development bearer token."
    )


def authenticate_bearer(
    authorization: Optional[str], *, environ: Optional[dict[str, str]] = None
) -> Actor:
    """Validate Entra JWTs in protected mode, with an explicit local fallback."""
    values = environ if environ is not None else os.environ
    environment = values.get("FLOWOPS_ENVIRONMENT", "development").lower()
    if values.get("FLOWOPS_AUTH_REQUIRED", "false").lower() != "true":
        return Actor(
            subject="local-development",
            roles=("automation.operator", "automation.admin"),
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise AuthenticationError("Bearer authentication is required.")
    token = authorization.removeprefix("Bearer ")
    if values.get("FLOWOPS_ENTRA_ISSUER"):
        return _authenticate_jwt(token, values)
    expected = values.get("FLOWOPS_DEV_BEARER_TOKEN")
    if (
        environment == "development"
        and expected
        and secrets.compare_digest(token, expected)
    ):
        return Actor(subject="configured-bearer", roles=("automation.operator",))
    raise AuthenticationError("Entra authentication is required in protected mode.")
