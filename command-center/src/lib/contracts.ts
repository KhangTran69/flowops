/** Core FastAPI console contracts. Keep in sync with src/schemas.py and
 * the command-center summary response in src/api.py. */
export type Ticket = {
  ticket_id: string
  requester_id: number
  title: string
  status: 'open' | 'closed'
  priority: string
  category: string | null
  queue: string | null
}
export type TicketHistory = {
  id: number
  action: string
  status: string
  actor_subject: string | null
  detail: string | null
  created_at: string
}
export type TicketDetail = Ticket & {
  description: string | null
  requester_email: string | null
  created_at: string | null
  updated_at: string | null
  closed_at: string | null
  resolution_note: string | null
  history: TicketHistory[]
}
export type ManualTicket = {
  requester_id: number
  title: string
  description: string
  requester_email?: string
}
export type ChecklistItem = {
  id: number
  step: string
  step_key: string | null
  queue: string
  requires_approval: boolean
  status: string
  depends_on: string[]
  overdue: boolean
  sla_due_at: string | null
  output: Record<string, unknown> | null
}
export type WorkflowAttempt = {
  id: number
  checklist_item_id: number
  attempt: number
  status: string
  error: string | null
  output: Record<string, unknown> | null
  scheduled_at: string
  started_at: string | null
  completed_at: string | null
}
export type WorkflowRun = {
  id: number
  request_id: number
  status: string
  definition_key: string | null
  employee_name: string | null
  created_at: string
  started_at: string | null
  completed_at: string | null
  checklist: ChecklistItem[]
  steps: WorkflowAttempt[]
  events: { id: number; event_type: string; created_at: string; payload: Record<string, unknown> | null }[]
}
export type DashboardSummary = {
  open_tickets: number
  urgent_tickets: number
  pending_approvals: number
  active_runs: number
  overdue_steps: number
  recorded_closed_today: number
}

export type OnboardingRequest = {
  id: number; request_type: string; employee_name: string; employee_email: string
  department: string; requester_id: number; start_date: string | null; status: string
  definition_key: string | null; definition_version: number | null; tenant_key: string | null
  checklist: ChecklistItem[]
}
export type DefinitionStep = {
  key: string; step: string; queue: string; requires_approval: boolean
  action: string; depends_on?: string[]; join?: string; when?: string[]
  sla_minutes?: number; escalation_queue?: string
}
export type DefinitionVersion = { version: number; checksum: string; steps: DefinitionStep[]; created_by: string | null; created_at: string; published_at: string | null }
export type WorkflowDefinition = { key: string; name: string; request_type: string; status: string; current_version: number | null; versions: DefinitionVersion[] }
export type DefinitionDiff = { from_version: number; to_version: number; added: string[]; removed: string[]; changed: string[] }
export type NotificationDelivery = {
  id: number
  url: string
  ticket_id: string | null
  status: string
  attempts: number
  last_error: string | null
  created_at: string
}
export type WorkflowJob = {
  id: number
  job_type: string
  status: string
  attempts: number
  max_attempts: number
  available_at: string
  last_error: string | null
  resource_type: string | null
  resource_id: string | null
  label: string | null
  created_at: string
  updated_at: string
  completed_at: string | null
  payload: Record<string, unknown> | null
  result: Record<string, unknown> | null
  lease_owner_token: string | null
  lease_expires_at: string | null
}
export type ActionExecution = {
  id: number
  request_id: number
  checklist_item_id: number
  action: string
  actor_subject: string | null
  adapter: string
  correlation_id: string
  attempt: number
  status: string
  error_category: string | null
  error_detail: string | null
  external_reference: string | null
  started_at: string
  completed_at: string | null
  duration_ms: number | null
}
export type ExecutionLog = { id: number; run_id: string; action: string; status: string; ticket_id: string | null; duration_ms: number; detail: string | null; actor_subject: string | null; correlation_id: string | null; created_at: string }
export type Readiness = { status: string; database: string }

export type AnalyticsCounts = { total: number; by_status: Record<string, number> }
export type AnalyticsSummary = {
  window: { days: number; tenant_key: string | null }
  runs: AnalyticsCounts & {
    completion_rate: number | null; failure_rate: number | null; active: number
    oldest_active_seconds: number | null; median_duration_seconds: number | null; p95_duration_seconds: number | null
  }
  approvals: AnalyticsCounts
  sla: AnalyticsCounts & { breached: number; overdue_pending: number; breach_rate: number | null; median_overrun_seconds: number | null }
  by_definition: { definition_key: string; request_type: string; runs: number; completed: number; failed: number; median_seconds: number | null; p95_seconds: number | null }[]
  by_tenant: { tenant_key: string; requests: number }[]
}
export type AnalyticsStep = {
  step: string; succeeded: number; failed: number; attempts: number; failure_rate: number | null
  median_seconds: number | null; p95_seconds: number | null; sample: number; ranked: boolean
}
export type AnalyticsHealth = {
  threshold_minutes: number; tenant_key: string | null; stuck_count: number
  stuck_runs: { run_id: number; request_id: number; tenant_key: string | null; definition_key: string; heartbeat_at: string; age_seconds: number; reason: string }[]
}

export type EventSource = { key: string; name: string; active: boolean; created_at: string; tenant_key: string | null; global_scope: boolean; secret_generated: boolean }
export type Trigger = { key: string; name: string; source_key: string; event_type: string; target_type: string; definition_key: string | null; mapping_json: Record<string, unknown>; schedule_kind: string; interval_seconds: number | null; daily_at_utc: string | null; active: boolean; last_fired_at: string | null; created_at: string; updated_at: string }
export type TriggerDelivery = { id: number; trigger_key: string; event_id: string; status: string; request_id: string | null; received_at: string; processed_at: string | null }
export type TriggerPreview = { trigger_key: string; target_type: string; mapped_payload: Record<string, unknown> }
export type KnowledgeArticle = { id: string; title: string; category: string; keywords: string[]; steps: string[]; summary: string }
