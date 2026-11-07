"""Outbound webhook notifications with retries and exponential backoff.

Used to notify automation platforms (n8n, Power Automate) and chat tools
about ticket events. Failures are reported, never raised, so a flaky
webhook cannot fail ticket intake.
"""

import time
import ssl
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3 import HTTPSConnectionPool


@dataclass(frozen=True)
class DeliveryResult:
    success: bool
    attempts: int
    last_error: str | None


class _PinnedHTTPSAdapter(HTTPAdapter):
    """Connect to a validated IP while keeping the URL hostname for TLS/HTTP."""

    def __init__(self, pinned_ip: str, hostname: str) -> None:
        super().__init__(max_retries=0)
        self._pinned_ip = pinned_ip
        self._hostname = hostname
        self._pool: HTTPSConnectionPool | None = None

    def get_connection_with_tls_context(
        self, request, verify, proxies=None, cert=None
    ):
        if proxies:
            raise requests.RequestException(
                "Proxies are not supported for pinned webhook delivery."
            )
        parsed = urlsplit(request.url)
        request_hostname = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme != "https" or request_hostname != self._hostname:
            raise requests.RequestException("Pinned webhook URL changed before delivery.")
        if verify is False:
            raise requests.SSLError(
                "Pinned webhook delivery requires TLS certificate verification."
            )
        if self._pool is None:
            _, pool_kwargs = self.build_connection_pool_key_attributes(
                request, verify, cert
            )
            ssl_context = pool_kwargs.get("ssl_context")
            if ssl_context is not None:
                if ssl_context.verify_mode != ssl.CERT_REQUIRED:
                    raise requests.SSLError(
                        "Pinned webhook TLS verification is not configured."
                    )
            elif pool_kwargs.get("cert_reqs") not in (
                "CERT_REQUIRED",
                ssl.CERT_REQUIRED,
            ) or not (pool_kwargs.get("ca_certs") or pool_kwargs.get("ca_cert_dir")):
                raise requests.SSLError(
                    "Pinned webhook CA verification could not be established."
                )
            self._pool = HTTPSConnectionPool(
                host=self._pinned_ip,
                port=443,
                maxsize=1,
                block=True,
                assert_hostname=self._hostname,
                server_hostname=self._hostname,
                **pool_kwargs,
            )
        return self._pool

    def add_headers(self, request, **kwargs) -> None:
        # urllib3 would otherwise synthesize Host from the pinned IP pool.
        request.headers["Host"] = urlsplit(request.url).netloc

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool = None
        super().close()


def deliver_webhook(
    url: str,
    payload: dict,
    *,
    timeout: float = 5.0,
    max_attempts: int = 3,
    backoff_seconds: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
    pinned_ip: str | None = None,
) -> DeliveryResult:
    """POST the payload to the webhook, retrying with exponential backoff.

    Returns a DeliveryResult instead of raising so callers can log the
    outcome and continue.
    """
    last_error: str | None = None
    session = None
    if pinned_ip is not None:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        if not hostname:
            return DeliveryResult(
                success=False, attempts=1, last_error="Webhook URL has no hostname."
            )
        session = requests.Session()
        # A proxy would own the connection and defeat the validated-IP pin.
        session.trust_env = False
        session.mount(
            "https://", _PinnedHTTPSAdapter(pinned_ip, hostname.lower().rstrip("."))
        )
    for attempt in range(1, max_attempts + 1):
        try:
            post = session.post if session is not None else requests.post
            response = post(url, json=payload, timeout=timeout, allow_redirects=False)
            if 300 <= response.status_code < 400:
                raise requests.HTTPError(
                    "Webhook redirects are not allowed.", response=response
                )
            response.raise_for_status()
            if session is not None:
                session.close()
            return DeliveryResult(success=True, attempts=attempt, last_error=None)
        except requests.RequestException as error:
            last_error = str(error)
            if attempt < max_attempts:
                sleep(backoff_seconds * (2 ** (attempt - 1)))
    if session is not None:
        session.close()
    return DeliveryResult(success=False, attempts=max_attempts, last_error=last_error)
