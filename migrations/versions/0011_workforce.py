"""Employee onboarding, IT tasks, assets, audit and automation outbox.

Revision ID: 0011_workforce
Revises: 0010_triggers
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011_workforce"
down_revision: Union[str, None] = "0010_triggers"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "employees",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("employee_number", sa.String(64), nullable=False, unique=True),
        sa.Column("full_name", sa.String(200), nullable=False),
        sa.Column("work_email", sa.String(320), nullable=False, unique=True),
        sa.Column("department", sa.String(120), nullable=False),
        sa.Column("job_title", sa.String(160), nullable=False),
        sa.Column("start_date", sa.String(10), nullable=False),
        sa.Column("manager_subject", sa.String(256), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="ACTIVE"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_employees_manager_subject", "employees", ["manager_subject"])
    op.create_table(
        "staff_onboarding_requests",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id"), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="DRAFT"),
        sa.Column("created_by", sa.String(256), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("rejection_reason", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_staff_onboarding_employee_id", "staff_onboarding_requests", ["employee_id"])
    op.create_index("ix_staff_onboarding_status", "staff_onboarding_requests", ["status"])
    op.create_table(
        "workforce_approval_decisions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("request_id", sa.Integer(), sa.ForeignKey("staff_onboarding_requests.id"), nullable=False, unique=True),
        sa.Column("actor_subject", sa.String(256), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("comment", sa.Text()),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_workforce_approval_decisions_request_id", "workforce_approval_decisions", ["request_id"])
    op.create_table(
        "it_setup_tasks",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("request_id", sa.Integer(), sa.ForeignKey("staff_onboarding_requests.id"), nullable=False),
        sa.Column("task_key", sa.String(64), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("assignee_subject", sa.String(256)),
        sa.Column("due_at", sa.DateTime(timezone=True)),
        sa.Column("required", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("status", sa.String(24), nullable=False, server_default="TODO"),
        sa.Column("completion_notes", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("request_id", "task_key", name="uq_it_task_request_key"),
    )
    op.create_index("ix_it_setup_tasks_request_id", "it_setup_tasks", ["request_id"])
    op.create_index("ix_it_setup_tasks_assignee_subject", "it_setup_tasks", ["assignee_subject"])
    op.create_index("ix_it_setup_tasks_status_due", "it_setup_tasks", ["status", "due_at"])
    op.create_table(
        "it_assets",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("asset_tag", sa.String(64), nullable=False, unique=True),
        sa.Column("serial_number", sa.String(120), unique=True),
        sa.Column("category", sa.String(80), nullable=False),
        sa.Column("brand_model", sa.String(200), nullable=False),
        sa.Column("condition", sa.String(24), nullable=False, server_default="GOOD"),
        sa.Column("status", sa.String(24), nullable=False, server_default="AVAILABLE"),
        sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_it_assets_status", "it_assets", ["status"])
    op.create_index("ix_it_assets_employee_id", "it_assets", ["employee_id"])
    op.create_table(
        "it_asset_assignments",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("asset_id", sa.Integer(), sa.ForeignKey("it_assets.id"), nullable=False),
        sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id"), nullable=False),
        sa.Column("assigned_by", sa.String(256), nullable=False),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("returned_at", sa.DateTime(timezone=True)),
        sa.Column("return_notes", sa.Text()),
    )
    op.create_index("ix_it_asset_assignments_asset_id", "it_asset_assignments", ["asset_id"])
    op.create_index("ix_it_asset_assignments_employee_id", "it_asset_assignments", ["employee_id"])
    op.create_index("ix_asset_assignment_current", "it_asset_assignments", ["asset_id", "returned_at"])
    op.create_table(
        "workforce_audit_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("actor_subject", sa.String(256), nullable=False),
        sa.Column("action", sa.String(80), nullable=False),
        sa.Column("entity_type", sa.String(48), nullable=False),
        sa.Column("entity_id", sa.String(64), nullable=False),
        sa.Column("detail", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_workforce_audit_created", "workforce_audit_events", ["created_at"])
    op.create_table(
        "workforce_automation_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.String(64), nullable=False, unique=True),
        sa.Column("event_type", sa.String(80), nullable=False),
        sa.Column("request_id", sa.Integer(), sa.ForeignKey("staff_onboarding_requests.id"), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("delivery_status", sa.String(24), nullable=False, server_default="PENDING"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column("workflow_status", sa.String(24), nullable=False, server_default="WAITING"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_workforce_event_delivery", "workforce_automation_events", ["delivery_status", "next_attempt_at"])
    op.create_index("ix_workforce_automation_events_request_id", "workforce_automation_events", ["request_id"])
    op.create_table(
        "workforce_reminder_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.String(128), nullable=False, unique=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("it_setup_tasks.id"), nullable=False),
        sa.Column("outcome", sa.String(24), nullable=False),
        sa.Column("detail", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_workforce_reminder_events_task_id", "workforce_reminder_events", ["task_id"])


def downgrade() -> None:
    op.drop_table("workforce_reminder_events")
    op.drop_table("workforce_automation_events")
    op.drop_table("workforce_audit_events")
    op.drop_table("it_asset_assignments")
    op.drop_table("it_assets")
    op.drop_table("it_setup_tasks")
    op.drop_table("workforce_approval_decisions")
    op.drop_table("staff_onboarding_requests")
    op.drop_table("employees")
