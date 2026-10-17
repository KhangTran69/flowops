"""Credential-free AI support agent and simulated IT knowledge base (v0.7).

The agent is suggest-only: it summarizes a ticket, searches a local KB, and
proposes actions. Applying a suggestion is a separate, human-approved API call.
The default adapter is deterministic and needs no external credentials; the
LLM adapter is an optional pluggable boundary.
"""

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence

import httpx

from src.routing import route_ticket

PRIORITY_RANK = {"normal": 1, "high": 2, "urgent": 3}
STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has",
    "have", "i", "in", "is", "it", "my", "of", "on", "or", "our", "the",
    "this", "to", "was", "we", "will", "with", "cannot", "can", "not",
}
URGENCY_TERMS = (
    "outage", "all users", "everyone", "cannot work", "is down",
    "system down", "urgent", "asap", "blocked", "security", "phishing",
    "breach", "locked out",
)


class AIAvailabilityError(RuntimeError):
    """Raised when an optional LLM adapter has no usable client."""


@dataclass(frozen=True)
class KnowledgeBaseArticle:
    id: str
    title: str
    category: str
    keywords: tuple[str, ...]
    steps: tuple[str, ...]
    summary: str


@dataclass(frozen=True)
class KnowledgeBaseMatch:
    article: KnowledgeBaseArticle
    score: int


@dataclass(frozen=True)
class TicketSnapshot:
    ticket_id: str
    requester_id: int
    title: str
    status: str
    priority: str
    category: Optional[str] = None
    queue: Optional[str] = None

    @classmethod
    def from_record(cls, record: Any) -> "TicketSnapshot":
        return cls(
            ticket_id=str(record.ticket_id),
            requester_id=record.requester_id,
            title=record.title,
            status=record.status,
            priority=record.priority,
            category=record.category,
            queue=record.queue,
        )


@dataclass(frozen=True)
class TicketSummary:
    text: str
    requester_id: int
    title: str
    status: str
    priority: str
    category: Optional[str]
    queue: Optional[str]
    urgency_signals: tuple[str, ...]
    history_count: int


@dataclass(frozen=True)
class Recommendation:
    id: str
    kind: str
    title: str
    current: Optional[str]
    suggested: Optional[str]
    rationale: str
    confidence: int
    applicable: bool


@dataclass(frozen=True)
class TriageResult:
    ticket: TicketSnapshot
    summary: TicketSummary
    knowledge_base: tuple[KnowledgeBaseMatch, ...]
    recommendations: tuple[Recommendation, ...]
    adapter: str


KNOWLEDGE_BASE: tuple[KnowledgeBaseArticle, ...] = (
    KnowledgeBaseArticle(
        id="kb-password-reset",
        title="Reset a forgotten or expired password",
        category="password",
        keywords=("password", "reset", "locked out", "sign in", "login", "mfa"),
        steps=(
            "Verify the requester's identity using the approved verification steps.",
            "Send a self-service password reset link to the registered address.",
            "Ask the user to re-enroll MFA if the authenticator was replaced.",
            "Confirm they can sign in and close the ticket.",
        ),
        summary="Guide the user through self-service reset, then verify MFA and sign-in.",
    ),
    KnowledgeBaseArticle(
        id="kb-vpn",
        title="Troubleshoot VPN and remote connectivity",
        category="network",
        keywords=("vpn", "remote", "connectivity", "network", "internet", "wifi", "wi-fi"),
        steps=(
            "Confirm whether the outage affects one user or all users.",
            "Check the VPN client version and reconnect after a full restart.",
            "Validate the user's network path and DNS resolution.",
            "Escalate to the network queue if multiple users are affected.",
        ),
        summary="Isolate single-user VPN faults from wider network outages before escalating.",
    ),
    KnowledgeBaseArticle(
        id="kb-mfa",
        title="Recover access when MFA fails",
        category="password",
        keywords=("mfa", "authenticator", "2fa", "verification", "code", "device"),
        steps=(
            "Verify the user's identity before changing MFA devices.",
            "Remove the old authenticator registration and re-enroll the new device.",
            "Ask the user to test sign-in with a fresh code.",
            "Record the change and close the ticket.",
        ),
        summary="Identity-check first, then re-enroll the authenticator and test sign-in.",
    ),
    KnowledgeBaseArticle(
        id="kb-saas-access",
        title="Provision SaaS application access",
        category="access",
        keywords=("access", "license", "saas", "application", "permission", "group", "sharepoint"),
        steps=(
            "Confirm the requested application and the user's role.",
            "Verify manager approval for the entitlement.",
            "Add the user to the approved group or license assignment.",
            "Ask the user to sign out and back in, then test access.",
        ),
        summary="Check approval and role, grant the least-privilege entitlement, then verify.",
    ),
    KnowledgeBaseArticle(
        id="kb-hardware",
        title="Triage laptop, monitor, and peripheral faults",
        category="hardware",
        keywords=("laptop", "monitor", "keyboard", "mouse", "printer", "docking", "screen", "hardware"),
        steps=(
            "Ask for the exact symptom and any recent changes.",
            "Try a known-good cable, port, or peripheral.",
            "Capture the device asset tag and warranty status.",
            "Arrange repair or replacement if hardware failure is confirmed.",
        ),
        summary="Confirm the failing component and warranty status before arranging repair.",
    ),
    KnowledgeBaseArticle(
        id="kb-email",
        title="Fix mailbox and Outlook delivery issues",
        category="email",
        keywords=("email", "outlook", "mailbox", "mail", "distribution list", "calendar"),
        steps=(
            "Confirm whether the issue is sending, receiving, or both.",
            "Check mailbox quota and service health.",
            "Recreate the Outlook profile if the client is corrupted.",
            "Escalate to messaging if delivery is failing server-side.",
        ),
        summary="Scope the failure to client, quota, or server before rebuilding the profile.",
    ),
    KnowledgeBaseArticle(
        id="kb-network-outage",
        title="Handle a site-wide network outage",
        category="network",
        keywords=("outage", "all users", "down", "internet", "network", "everyone"),
        steps=(
            "Confirm the scope and start time of the outage.",
            "Check the ISP and internal network status.",
            "Notify affected users with an ETA if known.",
            "Open an incident and escalate to the network queue.",
        ),
        summary="Treat multi-user connectivity loss as an incident and escalate immediately.",
    ),
    KnowledgeBaseArticle(
        id="kb-onboarding",
        title="Start an onboarding or offboarding checklist",
        category="access",
        keywords=("onboarding", "offboarding", "new hire", "leaver", "account", "provisioning"),
        steps=(
            "Confirm the employee's start or end date and department.",
            "Create the onboarding or offboarding request in FlowOps.",
            "Route approval-gated steps to the owning queue.",
            "Track completion through the checklist.",
        ),
        summary="Create the workflow early so approvals and equipment are not on the critical path.",
    ),
)


def _tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", text.lower())
        if token not in STOPWORDS and len(token) > 1
    }


def search_knowledge_base(
    query: str, *, limit: int = 3
) -> list[KnowledgeBaseMatch]:
    """Rank KB articles with a deterministic keyword relevance score."""
    query_tokens = _tokens(query)
    if not query_tokens:
        return []
    lowered = query.lower()
    matches: list[KnowledgeBaseMatch] = []
    for article in KNOWLEDGE_BASE:
        title_tokens = _tokens(article.title)
        keyword_tokens = set(article.keywords)
        text_tokens = _tokens(" ".join((article.summary, *article.steps)))
        score = 0
        score += 5 * len(query_tokens & title_tokens)
        score += 3 * len(query_tokens & keyword_tokens)
        score += 1 * len(query_tokens & text_tokens)
        if lowered and (lowered in article.title.lower() or lowered in article.summary.lower()):
            score += 5
        if score > 0:
            matches.append(KnowledgeBaseMatch(article=article, score=score))
    matches.sort(key=lambda match: (-match.score, match.article.id))
    return matches[: max(1, min(limit, len(KNOWLEDGE_BASE)))]


def list_knowledge_base() -> tuple[KnowledgeBaseArticle, ...]:
    return KNOWLEDGE_BASE


def summarize_ticket(
    ticket: TicketSnapshot,
    history: Sequence[Mapping[str, Any]] = (),
) -> TicketSummary:
    """Build a deterministic, structured ticket summary."""
    text = ticket.title.lower()
    signals = []
    for term in URGENCY_TERMS:
        if term in text:
            signals.append(term)
    if ticket.priority in ("high", "urgent") and ticket.priority not in signals:
        signals.append(f"priority={ticket.priority}")
    if ticket.status == "closed":
        signals.append("ticket already closed")
    history_count = len(history)
    recent = ", ".join(
        f"{entry.get('action', 'action')}:{entry.get('status', '?')}"
        for entry in list(history)[-3:]
    )
    signal_text = ", ".join(signals) if signals else "none detected"
    summary = (
        f"Requester #{ticket.requester_id} reported '{ticket.title}'. "
        f"Current state: status={ticket.status}, priority={ticket.priority}, "
        f"category={ticket.category or 'unclassified'}, queue={ticket.queue or 'unrouted'}. "
        f"Urgency signals: {signal_text}. "
        f"History: {history_count} action(s)"
        + (f" (recent: {recent})." if recent else ".")
    )
    return TicketSummary(
        text=summary,
        requester_id=ticket.requester_id,
        title=ticket.title,
        status=ticket.status,
        priority=ticket.priority,
        category=ticket.category,
        queue=ticket.queue,
        urgency_signals=tuple(signals),
        history_count=history_count,
    )


def recommend_actions(
    ticket: TicketSnapshot, kb_matches: Sequence[KnowledgeBaseMatch]
) -> tuple[Recommendation, ...]:
    """Propose queue/priority changes and informational next steps."""
    decision = route_ticket(ticket.title)
    recommendations: list[Recommendation] = []
    if decision.queue != ticket.queue:
        recommendations.append(
            Recommendation(
                id="queue-reassign",
                kind="queue",
                title="Reassign to the matching support queue",
                current=ticket.queue,
                suggested=decision.queue,
                rationale=(
                    f"Routing rules classify '{ticket.title}' as "
                    f"{decision.category}, which belongs to {decision.queue}."
                ),
                confidence=85,
                applicable=True,
            )
        )
    if PRIORITY_RANK.get(decision.priority, 0) > PRIORITY_RANK.get(ticket.priority, 0):
        recommendations.append(
            Recommendation(
                id="priority-upgrade",
                kind="priority",
                title="Raise the ticket priority",
                current=ticket.priority,
                suggested=decision.priority,
                rationale=(
                    f"Detected urgency signals map to priority '{decision.priority}'."
                ),
                confidence=80,
                applicable=True,
            )
        )
    if kb_matches:
        article = kb_matches[0].article
        recommendations.append(
            Recommendation(
                id="reply-draft",
                kind="response",
                title="Send the matching knowledge-base guidance",
                current=None,
                suggested=article.summary,
                rationale=f"Closest KB article: {article.title}.",
                confidence=65,
                applicable=False,
            )
        )
    lowered = ticket.title.lower()
    if any(term in lowered for term in ("onboarding", "offboarding", "new hire", "leaver")):
        recommendations.append(
            Recommendation(
                id="onboarding-checklist",
                kind="workflow",
                title="Start an onboarding/offboarding checklist",
                current=None,
                suggested="POST /onboarding-requests",
                rationale="The ticket describes an employee lifecycle event.",
                confidence=70,
                applicable=False,
            )
        )
    return tuple(recommendations)


class AIAdapter(Protocol):
    name: str

    def analyze(
        self,
        ticket: TicketSnapshot,
        history: Sequence[Mapping[str, Any]] = (),
    ) -> TriageResult:
        """Return a summary, KB matches, and suggestions without mutating state."""


class SimulatedAIAdapter:
    """Deterministic, credential-free adapter used by default and in tests."""

    name = "simulated"

    def analyze(
        self,
        ticket: TicketSnapshot,
        history: Sequence[Mapping[str, Any]] = (),
    ) -> TriageResult:
        summary = summarize_ticket(ticket, history)
        matches = tuple(search_knowledge_base(ticket.title, limit=3))
        recommendations = recommend_actions(ticket, matches)
        return TriageResult(
            ticket=ticket,
            summary=summary,
            knowledge_base=matches,
            recommendations=recommendations,
            adapter=self.name,
        )


PROVIDER_DEFAULTS: dict[str, tuple[str, Optional[str]]] = {
    "gemini": (
        "https://generativelanguage.googleapis.com/v1beta/openai",
        "gemini-2.5-flash",
    ),
    "groq": ("https://api.groq.com/openai/v1", "openai/gpt-oss-20b"),
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini"),
    "openrouter": ("https://openrouter.ai/api/v1", None),
    "ollama": ("http://localhost:11434/v1", "llama3.2"),
    "llm": (None, None),
}


def provider_settings(
    provider: str, environ: Optional[Mapping[str, str]] = None
) -> tuple[Optional[str], Optional[str], float]:
    """Resolve base URL, model, and timeout from provider defaults and env."""
    values = environ if environ is not None else os.environ
    default_base, default_model = PROVIDER_DEFAULTS.get(provider, (None, None))
    base_url = values.get("FLOWOPS_AI_BASE_URL") or default_base
    model = values.get("FLOWOPS_AI_MODEL") or default_model
    try:
        timeout = float(values.get("FLOWOPS_AI_TIMEOUT", "30"))
    except (TypeError, ValueError):
        timeout = 30.0
    return base_url, model, max(1.0, timeout)


class OpenAICompatibleClient:
    """Minimal OpenAI-compatible chat client used by the LLM adapter.

    Works with Gemini's OpenAI-compatible endpoint, Groq, OpenRouter, OpenAI,
    and local Ollama. ``http_client`` is injectable so tests can use
    ``httpx.MockTransport`` and never touch the network.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: str = "",
        timeout: float = 30.0,
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.http_client = http_client

    def __call__(self, prompt: str) -> str:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        owned_client = self.http_client is None
        client = self.http_client or httpx.Client(timeout=self.timeout)
        try:
            response = client.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
            body = response.json()
            content = body["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as error:
            raise AIAvailabilityError(
                f"LLM request to {self.base_url} failed: {error}"
            ) from error
        finally:
            if owned_client:
                client.close()
        if not isinstance(content, str) or not content.strip():
            raise AIAvailabilityError("LLM response did not contain text content.")
        return content


class LLMAIAdapter:
    """Optional pluggable LLM boundary.

    ``client`` is a callable accepting a prompt and returning JSON text. The
    caller supplies a Claude/Gemini/OpenAI-backed client in production; tests
    inject a fake callable. No network call happens here by default.
    """

    name = "llm"

    def __init__(
        self,
        api_key: str,
        *,
        provider: str = "openai",
        client: Optional[Callable[[str], str]] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: float = 30.0,
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        self.api_key = api_key
        self.provider = provider
        self.client = client
        self.base_url = base_url
        self.model = model
        self.timeout = timeout
        self.http_client = http_client

    def _client(self) -> Callable[[str], str]:
        if self.client is not None:
            return self.client
        environ = dict(os.environ)
        if self.base_url:
            environ["FLOWOPS_AI_BASE_URL"] = self.base_url
        if self.model:
            environ["FLOWOPS_AI_MODEL"] = self.model
        base_url, model, timeout = provider_settings(self.provider, environ)
        if not base_url or not model:
            raise AIAvailabilityError(
                "LLM adapter requires FLOWOPS_AI_BASE_URL and FLOWOPS_AI_MODEL "
                f"for provider {self.provider!r}."
            )
        return OpenAICompatibleClient(
            base_url,
            model,
            api_key=self.api_key,
            timeout=timeout,
            http_client=self.http_client,
        )

    def _prompt(self, ticket: TicketSnapshot) -> str:
        articles = ", ".join(article.id for article in KNOWLEDGE_BASE)
        return (
            "You are an IT support triage assistant. Return JSON with keys "
            "summary (string) and recommendations (list of objects with id, "
            "kind, title, current, suggested, rationale, confidence, applicable). "
            f"Ticket: {json.dumps(ticket.__dict__, sort_keys=True)}. "
            f"Knowledge base ids: {articles}."
        )

    def analyze(
        self,
        ticket: TicketSnapshot,
        history: Sequence[Mapping[str, Any]] = (),
    ) -> TriageResult:
        if not self.api_key and self.provider != "ollama":
            raise AIAvailabilityError("FLOWOPS_AI_API_KEY is required for the LLM adapter.")
        client = self._client()
        try:
            payload = json.loads(client(self._prompt(ticket)))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise AIAvailabilityError("LLM adapter returned invalid JSON.") from error
        summary_text = str(payload.get("summary", "")).strip()
        if not summary_text:
            raise AIAvailabilityError("LLM adapter returned an empty summary.")
        matches = tuple(search_knowledge_base(ticket.title, limit=3))
        recommendations = []
        for item in payload.get("recommendations", []):
            try:
                recommendations.append(
                    Recommendation(
                        id=str(item["id"]),
                        kind=str(item["kind"]),
                        title=str(item["title"]),
                        current=item.get("current"),
                        suggested=item.get("suggested"),
                        rationale=str(item.get("rationale", "")),
                        confidence=int(item.get("confidence", 50)),
                        applicable=bool(item.get("applicable", False)),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        summary = summarize_ticket(ticket, history)
        return TriageResult(
            ticket=ticket,
            summary=TicketSummary(
                **{
                    **summary.__dict__,
                    "text": summary_text,
                }
            ),
            knowledge_base=matches,
            recommendations=tuple(recommendations),
            adapter=self.name,
        )


def adapter_from_environment(environ: Optional[Mapping[str, str]] = None) -> AIAdapter:
    """Select the LLM adapter only when explicitly configured with a key."""
    values = environ if environ is not None else {}
    from os import environ as process_environ

    values = values or process_environ
    name = values.get("FLOWOPS_AI_ADAPTER", "simulated").lower()
    api_key = values.get("FLOWOPS_AI_API_KEY", "")
    if name == "ollama":
        return LLMAIAdapter("", provider="ollama")
    if name in PROVIDER_DEFAULTS and api_key:
        return LLMAIAdapter(api_key, provider=name)
    return SimulatedAIAdapter()
