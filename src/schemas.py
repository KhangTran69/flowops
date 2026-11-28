"""Pydantic request/response models for the FlowOps API."""

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator


class TicketIntakeRequest(BaseModel):
    """Incoming ticket in the source format (same schema as the sample API)."""

    userId: StrictInt = Field(ge=1, description="ID of the requester.")
    id: StrictInt = Field(ge=1, description="Upstream ticket ID.")
    title: StrictStr = Field(description="Short ticket description.")
    completed: StrictBool = Field(description="Whether the upstream item is done.")
    tenant: StrictStr | None = Field(
        default=None, max_length=64, description="Optional client tenant key (multi-tenant MSP)."
    )

    @field_validator("title")
    @classmethod
    def title_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("title must be a non-empty string")
        return value


class Ticket(BaseModel):
    """Normalized internal ticket format (also the API response shape)."""

    ticket_id: str
    requester_id: int
    title: str
    status: Literal["open", "closed"]
    priority: str
    category: str | None = None
    queue: str | None = None


class ManualTicketIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    requester_id: StrictInt = Field(ge=1, le=2147483647)
    title: StrictStr = Field(min_length=3, max_length=500)
    description: StrictStr = Field(min_length=3, max_length=10000)
    requester_email: StrictStr | None = Field(default=None, max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class TicketHistoryOut(BaseModel):
    id: int
    action: str
    status: str
    actor_subject: str | None = None
    detail: str | None = None
    created_at: datetime


class TicketDetailOut(Ticket):
    description: str | None = None
    requester_email: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    closed_at: datetime | None = None
    resolution_note: str | None = None
    history: list[TicketHistoryOut] = Field(default_factory=list)


class KnowledgeBaseArticleOut(BaseModel):
    id: str
    title: str
    category: str
    keywords: list[str]
    steps: list[str]
    summary: str


class KnowledgeBaseMatchOut(BaseModel):
    article: KnowledgeBaseArticleOut
    score: int


class AISummaryOut(BaseModel):
    text: str
    requester_id: int
    title: str
    status: str
    priority: str
    category: str | None = None
    queue: str | None = None
    urgency_signals: list[str]
    history_count: int


class AIRecommendationOut(BaseModel):
    id: str
    kind: str
    title: str
    current: str | None = None
    suggested: str | None = None
    rationale: str
    confidence: int
    applicable: bool


class AITriageOut(BaseModel):
    ticket_id: str
    summary: AISummaryOut
    knowledge_base: list[KnowledgeBaseMatchOut]
    recommendations: list[AIRecommendationOut]
    adapter: str
    generated_at: datetime


class AIApplyRequest(BaseModel):
    recommendation_ids: list[StrictStr] = Field(min_length=1)


class AIApplyOut(BaseModel):
    ticket: Ticket
    applied: list[str]


class WorkflowJobOut(BaseModel):
    """Durable workflow queue entry exposed to operators."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    job_type: str
    status: str
    attempts: int
    max_attempts: int
    available_at: datetime
    last_error: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    label: str | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None


class ExecutionLogOut(BaseModel):
    id: int
    run_id: str
    action: str
    status: str
    ticket_id: str | None = None
    duration_ms: int
    detail: str | None = None
    actor_subject: str | None = None
    correlation_id: str | None = None
    created_at: datetime


class ActionExecutionOut(BaseModel):
    id: int
    request_id: int
    checklist_item_id: int
    action: str
    actor_subject: str | None = None
    adapter: str
    correlation_id: str
    attempt: int
    status: str
    error_category: str | None = None
    error_detail: str | None = None
    external_reference: str | None = None
    started_at: datetime
    completed_at: datetime | None = None
    duration_ms: int | None = None


class ChecklistItemOut(BaseModel):
    id: int
    step: str
    # v0.6.1: stable identifier for the step; None on rows created before
    # this change existed (they predate the offboarding completion ledger).
    step_key: str | None = None
    queue: str
    requires_approval: bool
    status: str
    action: str | None = None
    result: dict | list | str | int | float | bool | None = None
    depends_on: list[str] = []
    output: dict | None = None
    join_mode: str = "all"
    when: list[str] = []
    sla_minutes: int | None = None
    overdue: bool = False
    sla_remaining_minutes: int | None = None
    sla_due_at: datetime | None = None


class DefinitionCreateIn(BaseModel):
    key: StrictStr
    name: StrictStr
    request_type: Literal["onboarding", "offboarding"]
    steps: list[dict[str, Any]]


class DefinitionVersionIn(BaseModel):
    steps: list[dict[str, Any]]


class DefinitionPublishIn(BaseModel):
    version: StrictInt = Field(ge=1)


class DefinitionMigrationIn(BaseModel):
    version: StrictInt = Field(ge=1)
    dry_run: StrictBool = True


class DefinitionVersionOut(BaseModel):
    version: int
    checksum: str
    steps: list[dict[str, Any]]
    created_by: str | None = None
    created_at: datetime
    published_at: datetime | None = None


class WorkflowDefinitionOut(BaseModel):
    key: str
    name: str
    request_type: str
    status: str
    current_version: int | None = None
    versions: list[DefinitionVersionOut] = []


class OnboardingRequestIn(BaseModel):
    """A manager's onboarding/offboarding request (v0.5)."""

    request_type: Literal["onboarding", "offboarding"]
    employee_name: StrictStr
    employee_email: StrictStr
    department: StrictStr
    requester_id: StrictInt = Field(ge=1)
    start_date: StrictStr | None = Field(
        default=None, description="ISO date (YYYY-MM-DD) the employee starts or leaves."
    )
    definition_key: StrictStr | None = Field(
        default=None, description="Optional published workflow definition key."
    )
    definition_version: StrictInt | None = Field(
        default=None, ge=1, description="Optional pinned definition version."
    )
    template: StrictStr | None = Field(
        default=None,
        description="Optional named workflow template (e.g. onboarding-dag).",
    )
    tenant_key: StrictStr | None = Field(
        default=None, max_length=64, description="Client tenant this request runs in."
    )

    @field_validator("employee_name", "department")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must be a non-empty string")
        return value

    @field_validator("employee_email")
    @classmethod
    def must_look_like_email(cls, value: str) -> str:
        if "@" not in value or " " in value:
            raise ValueError("employee_email must be a valid email address")
        return value.lower()

    @field_validator("start_date")
    @classmethod
    def iso_date(cls, value: str | None) -> str | None:
        import re

        if value is not None:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError("start_date must be an ISO date (YYYY-MM-DD)")
            try:
                date.fromisoformat(value)
            except ValueError as error:
                raise ValueError("start_date must be a valid calendar date") from error
        return value


class OnboardingRequestOut(BaseModel):
    id: int
    request_type: str
    employee_name: str
    employee_email: str
    department: str
    requester_id: int
    start_date: str | None = None
    status: str
    definition_key: str | None = None
    definition_version: int | None = None
    tenant_key: str | None = None
    checklist: list[ChecklistItemOut]


class WorkflowStepRunOut(BaseModel):
    id: int
    checklist_item_id: int
    attempt: int
    status: str
    job_id: int | None = None
    scheduled_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    output: dict | None = None
    error: str | None = None


class WorkflowEventOut(BaseModel):
    id: int
    event_type: str
    step_run_id: int | None = None
    payload: dict | None = None
    created_at: datetime


class WorkflowRunOut(BaseModel):
    id: int
    request_id: int
    status: str
    max_parallel: int
    error: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    steps: list[WorkflowStepRunOut] = []
    checklist: list[ChecklistItemOut] = Field(default_factory=list)
    definition_key: str | None = None
    employee_name: str | None = None
    events: list[WorkflowEventOut] = []


class OnboardingStatusSummaryOut(BaseModel):
    id: int
    request_type: str
    employee_name: str
    status: str
    total_steps: int
    completed_steps: int
    pending_approval_steps: int
    percent_complete: int


class OffboardingStepResultOut(BaseModel):
    """Stored outcome of one offboarding step (v0.6.1)."""

    step_key: StrictStr
    status: str
    executor_called: bool
    executed_at: datetime


class OffboardingCompletionOut(BaseModel):
    """Response body of the idempotent offboarding completion endpoint."""

    checklist_id: int
    employee_name: str
    step: OffboardingStepResultOut


class M365StatusOut(BaseModel):
    """Shape of the *simulated* M365 status response (v0.6 placeholder)."""

    simulated: StrictBool
    id: StrictStr
    provisioning_state: StrictStr
    last_sync: StrictStr


class NotificationOutboxOut(BaseModel):
    """One stored notification delivery, exposed for operators (v1.0 preview)."""

    id: int
    url: str
    ticket_id: str | None = None
    status: str
    attempts: int
    last_error: str | None = None
    created_at: datetime
