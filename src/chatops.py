"""HMAC-signed, single-use ChatOps action tokens (v1.1).

Tokens bind ``request_id``, ``item_id``, and ``action`` (approve/reject) and
carry an expiry plus a random ``jti``. The API records each ``jti`` in
``chatops_token_uses`` so a token cannot be replayed. Secrets are read from
``FLOWOPS_CHATOPS_SECRET``; tests use a fixed secret and never call Slack or
Teams.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any, Mapping, Optional

DEFAULT_TTL_SECONDS = 24 * 60 * 60
VALID_ACTIONS = ("approve", "reject")


class ChatOpsError(ValueError):
    """Raised when a ChatOps token is missing, invalid, or expired."""


class ChatOpsUnavailable(ChatOpsError):
    """Raised when no ChatOps signing secret is configured."""


@dataclass(frozen=True)
class ChatOpsClaims:
    request_id: int
    item_id: int
    action: str
    jti: str
    expires_at: int


def _secret(environ: Optional[Mapping[str, str]] = None) -> str:
    values = environ if environ is not None else os.environ
    secret = values.get("FLOWOPS_CHATOPS_SECRET", "")
    if not secret:
        raise ChatOpsUnavailable(
            "FLOWOPS_CHATOPS_SECRET is required for ChatOps approval links."
        )
    return secret


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _sign(payload: str, secret: str) -> str:
    return _b64(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest())


def generate_token(
    request_id: int,
    item_id: int,
    action: str,
    *,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    now: Optional[int] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> str:
    if action not in VALID_ACTIONS:
        raise ChatOpsError(f"Unsupported ChatOps action: {action!r}.")
    issued = int(now if now is not None else time.time())
    payload = {
        "request_id": int(request_id),
        "item_id": int(item_id),
        "action": action,
        "exp": issued + int(ttl_seconds),
        "jti": secrets.token_urlsafe(18),
    }
    encoded = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    return f"{encoded}.{_sign(encoded, _secret(environ))}"


def verify_token(
    token: str,
    *,
    now: Optional[int] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> ChatOpsClaims:
    if not token or "." not in token:
        raise ChatOpsError("Malformed ChatOps token.")
    encoded, signature = token.split(".", 1)
    expected = _sign(encoded, _secret(environ))
    if not secrets.compare_digest(signature, expected):
        raise ChatOpsError("Invalid ChatOps token signature.")
    try:
        data: dict[str, Any] = json.loads(_unb64(encoded))
    except (ValueError, TypeError, KeyError) as error:
        raise ChatOpsError("Malformed ChatOps token payload.") from error
    action = str(data.get("action", ""))
    if action not in VALID_ACTIONS:
        raise ChatOpsError("Unsupported ChatOps token action.")
    expires_at = int(data.get("exp", 0))
    if expires_at < int(now if now is not None else time.time()):
        raise ChatOpsError("ChatOps token has expired.")
    return ChatOpsClaims(
        request_id=int(data["request_id"]),
        item_id=int(data["item_id"]),
        action=action,
        jti=str(data["jti"]),
        expires_at=expires_at,
    )


def approval_urls(
    request_id: int,
    item_id: int,
    *,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    environ: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    """Return approve/reject token URLs for UI display (empty if unconfigured)."""
    try:
        approve = generate_token(
            request_id, item_id, "approve", ttl_seconds=ttl_seconds, environ=environ
        )
        reject = generate_token(
            request_id, item_id, "reject", ttl_seconds=ttl_seconds, environ=environ
        )
    except ChatOpsUnavailable:
        return {}
    return {
        "approve_url": f"/webhooks/actions/approve?token={approve}",
        "reject_url": f"/webhooks/actions/reject?token={reject}",
    }
