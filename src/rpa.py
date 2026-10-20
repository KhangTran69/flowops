"""RPA webhook adapter for FlowOps.

Posts checklist actions to an RPA webhook (e.g. UiPath, Automation Anywhere,
Blue Prism) with a ``bot`` field identifying the automation target. The
``X-RPA-Secret`` header is set when a secret is supplied.
"""

import os
from typing import Any

import requests
from src.actions import ActionResult, ensure_action_response


class RPAWebhookAdapter:
    """POSTs checklist context to an RPA webhook endpoint."""

    adapter_name = "rpa"

    def __init__(
        self,
        url: str,
        bot: str = "default",
        headers: dict[str, str] | None = None,
        timeout: float = 10.0,
        secret: str | None = None,
    ) -> None:
        self.url = url
        self.bot = bot
        self.timeout = timeout
        self.secret = secret
        self.headers = dict(headers or {})
        if secret and "X-RPA-Secret" not in self.headers:
            self.headers["X-RPA-Secret"] = secret

    def execute(self, action: str, context: dict[str, Any]) -> ActionResult:
        payload: dict[str, Any] = {"bot": self.bot, "action": action, "context": context}
        if context.get("idempotency_key"):
            payload["idempotency_key"] = context["idempotency_key"]
        response = requests.post(
            self.url,
            json=payload,
            headers=self.headers or None,
            timeout=self.timeout,
        )
        ensure_action_response(response, "RPA")
        return ActionResult(
            action=action,
            detail="RPA job submitted",
            adapter=self.adapter_name,
            idempotency_key=context.get("idempotency_key"),
            correlation_id=context.get("correlation_id"),
        )


def rpa_from_env(env: dict[str, str] | None = None) -> RPAWebhookAdapter | None:
    """Return an ``RPAWebhookAdapter`` if ``FLOWOPS_RPA_WEBHOOK_URL`` is set."""
    values = env if env is not None else os.environ
    url = values.get("FLOWOPS_RPA_WEBHOOK_URL")
    if not url:
        return None
    return RPAWebhookAdapter(
        url=url,
        bot=values.get("FLOWOPS_RPA_BOT", "default"),
        timeout=float(values.get("FLOWOPS_RPA_TIMEOUT", "10.0")),
        secret=values.get("FLOWOPS_RPA_SECRET") or None,
    )
