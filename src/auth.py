"""Authentication boundary for protected automation actions."""

import json
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import jwt
import requests


@dataclass(frozen=True)
class Actor:
    subject: str
    roles: tuple[str, ...]
    source: str = "local"  # local | bearer | session
    tenant_key: str | None = None


class AuthenticationError(ValueError):
    """Raised when a protected action has no valid bearer token."""


def _roles(claims: dict[str, Any]) -> tuple[str, ...]:
    # Entra application roles are an array; delegated scopes are a string.
    # Never treat dictionary keys or coerced numbers as granted permissions.
    if "roles" in claims:
        values = claims["roles"]
        if not isinstance(values, list) or any(
            not isinstance(value, str) or not value or value != value.strip() for value in values
        ):
            raise AuthenticationError("Invalid bearer roles claim.")
        return tuple(values)
    scopes = claims.get("scp", "")
    if not isinstance(scopes, str):
        raise AuthenticationError("Invalid bearer scope claim.")
    return tuple(scopes.split())


def _identity_subject(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > 256:
        raise AuthenticationError("Invalid identity subject.")
    return value


def _identity_tenant(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > 64:
        raise AuthenticationError("Invalid identity tenant.")
    return value


def _validate_entra_urls(issuer: str, jwks_url: str) -> None:
    for label, value in (("issuer", issuer), ("JWKS URL", jwks_url)):
        try:
            parsed = urlsplit(value)
            valid = (parsed.scheme == "https" and bool(parsed.hostname)
                     and parsed.username is None and parsed.password is None
                     and not parsed.fragment and not parsed.query
                     and not any(char.isspace() for char in value))
        except ValueError:
            valid = False
        if not valid:
            raise AuthenticationError(f"Entra {label} must be a trusted HTTPS URL.")


def _authenticate_jwt(token: str, values: dict[str, str]) -> Actor:
    issuer = values.get("FLOWOPS_ENTRA_ISSUER")
    audience = values.get("FLOWOPS_ENTRA_AUDIENCE")
    jwks_url = values.get("FLOWOPS_ENTRA_JWKS_URL")
    required_role = values.get("FLOWOPS_ENTRA_REQUIRED_ROLE", "automation.operator")
    if not issuer or not audience or not jwks_url:
        raise AuthenticationError("Entra issuer, audience, and JWKS URL are required.")

    _validate_entra_urls(issuer, jwks_url)
    cache = _authenticate_jwt.__dict__.setdefault("cache", {})
    now = time.time()
    keys, expires = cache.get(jwks_url, (None, 0))
    if keys is None or expires <= now:
        try:
            response = requests.get(jwks_url, timeout=5, allow_redirects=False)
            response.raise_for_status()
            if 300 <= response.status_code < 400:
                raise ValueError("Signing key redirects are not permitted.")
            keys = response.json()["keys"]
            if not isinstance(keys, list) or not all(isinstance(item, dict) for item in keys):
                raise ValueError("Malformed signing key set.")
            cache[jwks_url] = (keys, now + 300)
        except (requests.RequestException, KeyError, TypeError, ValueError) as error:
            raise AuthenticationError("Unable to retrieve Entra signing keys.") from error

    try:
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid or header.get("alg") != "RS256":
            raise ValueError("Missing key identifier or unsupported algorithm.")
        matches = [item for item in keys if item.get("kid") == kid]
        if len(matches) != 1:
            raise ValueError("Missing or ambiguous signing key.")
        key_data = matches[0]
        if key_data.get("kty") != "RSA" or key_data.get("use", "sig") != "sig" or key_data.get("alg", "RS256") != "RS256":
            raise ValueError("Unexpected signing key type or purpose.")
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
    tenant_keys = [_identity_tenant(claims[key]) for key in ("flowops_tenant_key", "tenant_key") if key in claims]
    if len(set(tenant_keys)) > 1:
        raise AuthenticationError("Conflicting bearer tenant claims.")
    return Actor(subject=_identity_subject(claims["sub"]), roles=roles,
                 tenant_key=tenant_keys[0] if tenant_keys else None)


def validate_auth_configuration(
    environ: dict[str, str] | None = None,
) -> None:
    """Fail fast when protected mode lacks a usable credential source."""
    values = environ if environ is not None else os.environ
    environment = values.get("FLOWOPS_ENVIRONMENT", "development").lower()
    auth_required = values.get("FLOWOPS_AUTH_REQUIRED", "false").lower() == "true"
    if environment != "development" and not auth_required:
        raise AuthenticationError(
            "FLOWOPS_AUTH_REQUIRED=true is mandatory outside development."
        )
    if not auth_required:
        return
    if values.get("FLOWOPS_ENTRA_ISSUER"):
        if not values.get("FLOWOPS_ENTRA_AUDIENCE") or not values.get(
            "FLOWOPS_ENTRA_JWKS_URL"
        ):
            raise AuthenticationError(
                "Entra issuer, audience, and JWKS URL must all be configured."
            )
        _validate_entra_urls(values["FLOWOPS_ENTRA_ISSUER"], values["FLOWOPS_ENTRA_JWKS_URL"])
        return
    if environment == "development" and values.get("FLOWOPS_DEV_BEARER_TOKEN"):
        return
    raise AuthenticationError(
        "Protected mode requires Entra configuration or a development bearer token."
    )


def authenticate_bearer(
    authorization: str | None, *, environ: dict[str, str] | None = None
) -> Actor:
    """Validate Entra JWTs in protected mode, with an explicit local fallback."""
    values = environ if environ is not None else os.environ
    environment = values.get("FLOWOPS_ENVIRONMENT", "development").lower()
    if values.get("FLOWOPS_AUTH_REQUIRED", "false").lower() != "true":
        if environment != "development":
            raise AuthenticationError("Authentication is mandatory outside development.")
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
        return Actor(subject="configured-bearer", roles=("automation.operator",),
                     tenant_key=values.get("FLOWOPS_DEV_TENANT_KEY") or None)
    raise AuthenticationError("Entra authentication is required in protected mode.")
