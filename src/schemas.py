"""Pydantic request/response models for the FlowOps API."""

from datetime import date, datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator




class TicketIntakeRequest(BaseModel):
    """Incoming ticket in the source format (same schema as the sample API)."""

    userId: StrictInt = Field(ge=1, description="ID of the requester.")
    id: StrictInt = Field(ge=1, description="Upstream ticket ID.")
    title: StrictStr = Field(description="Short ticket description.")
    completed: StrictBool = Field(description="Whether the upstream item is done.")
    tenant: Optional[StrictStr] = Field(
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
    category: Optional[str] = None
    queue: Optional[str] = None


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
    category: Optional[str] = None
    queue: Optional[str] = None
    urgency_signals: list[str]
    history_count: int


class AIRecommendationOut(BaseModel):
    id: str
    kind: str
    title: str
    current: Optional[str] = None
    suggested: Optional[str] = None
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
    last_error: Optional[str] = None
    resource_type: Optional[str] = None
    resource_id: Optional[str] = None
    label: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime] = None


class ExecutionLogOut(BaseModel):
    id: int
    run_id: str
    action: str
    status: str
    ticket_id: Optional[str] = None
    duration_ms: int
    detail: Optional[str] = None
    actor_subject: Optional[str] = None
    correlation_id: Optional[str] = None
    created_at: datetime


class ActionExecutionOut(BaseModel):
    id: int
    request_id: int
    checklist_item_id: int
    action: str
    actor_subject: Optional[str] = None
    adapter: str
    correlation_id: str
    attempt: int
    status: str
    error_category: Optional[str] = None
    error_detail: Optional[str] = None
    external_reference: Optional[str] = None
    started_at: datetime
    completed_at: Optional[datetime] = None
    duration_ms: Optional[int] = None


class ChecklistItemOut(BaseModel):
    id: int
    step: str
    # v0.6.1: stable identifier for the step; None on rows created before
    # this change existed (they predate the offboarding completion ledger).
    step_key: Optional[str] = None
    queue: str
    requires_approval: bool
    status: str
    action: Optional[str] = None
    result: Optional[dict | list | str | int | float | bool | None] = None
    depends_on: list[str] = []
    output: Optional[dict] = None
    join_mode: str = "all"
    when: list[str] = []
    sla_minutes: Optional[int] = None
    overdue: bool = False
    sla_remaining_minutes: Optional[int] = None


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
    created_by: Optional[str] = None
    created_at: datetime
    published_at: Optional[datetime] = None


class WorkflowDefinitionOut(BaseModel):
    key: str
    name: str
    request_type: str
    status: str
    current_version: Optional[int] = None
    versions: list[DefinitionVersionOut] = []


class OnboardingRequestIn(BaseModel):
    """A manager's onboarding/offboarding request (v0.5)."""

    request_type: Literal["onboarding", "offboarding"]
    employee_name: StrictStr
    employee_email: StrictStr
    department: StrictStr
    requester_id: StrictInt = Field(ge=1)
    start_date: Optional[StrictStr] = Field(
        default=None, description="ISO date (YYYY-MM-DD) the employee starts or leaves."
    )
    definition_key: Optional[StrictStr] = Field(
        default=None, description="Optional published workflow definition key."
    )
    definition_version: Optional[StrictInt] = Field(
        default=None, ge=1, description="Optional pinned definition version."
    )
    template: Optional[StrictStr] = Field(
        default=None,
        description="Optional named workflow template (e.g. onboarding-dag).",
    )
    tenant_key: Optional[StrictStr] = Field(
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
    def iso_date(cls, value: Optional[str]) -> Optional[str]:
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
    start_date: Optional[str] = None
    status: str
    definition_key: Optional[str] = None
    definition_version: Optional[int] = None
    tenant_key: Optional[str] = None
    checklist: list[ChecklistItemOut]


class WorkflowStepRunOut(BaseModel):
    id: int
    checklist_item_id: int
    attempt: int
    status: str
    job_id: Optional[int] = None
    scheduled_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    output: Optional[dict] = None
    error: Optional[str] = None


class WorkflowEventOut(BaseModel):
    id: int
    event_type: str
    step_run_id: Optional[int] = None
    payload: Optional[dict] = None
    created_at: datetime


class WorkflowRunOut(BaseModel):
    id: int
    request_id: int
    status: str
    max_parallel: int
    error: Optional[str] = None
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    steps: list[WorkflowStepRunOut] = []
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
    ticket_id: Optional[str] = None
    status: str
    attempts: int
    last_error: Optional[str] = None
    created_at: datetime
