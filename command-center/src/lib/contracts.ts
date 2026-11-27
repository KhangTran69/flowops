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
export type ExecutionLog = { id: number; run_id: string; action: string; status: string; ticket_id: string | null; duration_ms: number; detail: string | null; actor_subject: string | null; correlation_id: string | null; created_at: string }
export type Readiness = { status: string; database: string }
