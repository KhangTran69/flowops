import { z } from 'zod'

// Validate the fields the console actually consumes. Extra backend fields remain
// available, so additive FastAPI response changes do not break older clients.
const text = z.string()
const integer = z.number().int()
const nullableText = text.nullable()
const ticket = z.object({ ticket_id: text, requester_id: integer, title: text, status: z.enum(['open', 'closed']), priority: text, category: nullableText, queue: nullableText }).passthrough()
const history = z.object({ id: integer, action: text, status: text, actor_subject: nullableText, detail: nullableText, created_at: text }).passthrough()
const ticketDetail = ticket.extend({ description: nullableText, requester_email: nullableText, created_at: nullableText, updated_at: nullableText, closed_at: nullableText, resolution_note: nullableText, history: z.array(history) })
const checklist = z.object({ id: integer, step: text, step_key: nullableText, queue: text, requires_approval: z.boolean(), status: text, depends_on: z.array(text), overdue: z.boolean(), sla_due_at: nullableText, output: z.record(text, z.unknown()).nullable() }).passthrough()
const attempt = z.object({ id: integer, checklist_item_id: integer, attempt: integer, status: text, error: nullableText, output: z.record(text, z.unknown()).nullable(), scheduled_at: text, started_at: nullableText, completed_at: nullableText }).passthrough()
const event = z.object({ id: integer, event_type: text, created_at: text, payload: z.record(text, z.unknown()).nullable() }).passthrough()
const run = z.object({ id: integer, request_id: integer, status: text, definition_key: nullableText, employee_name: nullableText, created_at: text, started_at: nullableText, completed_at: nullableText, checklist: z.array(checklist), steps: z.array(attempt), events: z.array(event) }).passthrough()
const request = z.object({ id: integer, request_type: text, employee_name: text, employee_email: text, department: text, requester_id: integer, start_date: nullableText, status: text, definition_key: nullableText, definition_version: integer.nullable(), tenant_key: nullableText, checklist: z.array(checklist) }).passthrough()
const definitionStep = z.object({ key: text, step: text, queue: text, requires_approval: z.boolean(), action: text, depends_on: z.array(text).optional(), join: text.optional(), when: z.array(text).optional(), sla_minutes: integer.nullable().optional(), escalation_queue: text.nullable().optional() }).passthrough()
const definition = z.object({ key: text, name: text, request_type: text, status: text, current_version: integer.nullable(), versions: z.array(z.object({ version: integer, checksum: text, steps: z.array(definitionStep), created_by: nullableText, created_at: text, published_at: nullableText }).passthrough()) }).passthrough()
const diff = z.object({ from_version: integer, to_version: integer, added: z.array(text), removed: z.array(text), changed: z.array(text) })
const summary = z.object({ open_tickets: integer.nonnegative(), urgent_tickets: integer.nonnegative(), pending_approvals: integer.nonnegative(), active_runs: integer.nonnegative(), overdue_steps: integer.nonnegative(), recorded_closed_today: integer.nonnegative() })
const delivery = z.object({ id: integer, url: text, ticket_id: nullableText, status: text, attempts: integer.nonnegative(), last_error: nullableText, created_at: text }).passthrough()
const job = z.object({ id: integer, job_type: text, status: text, attempts: integer.nonnegative(), max_attempts: integer.nonnegative(), available_at: text, last_error: nullableText, resource_type: nullableText, resource_id: nullableText, label: nullableText, created_at: text, updated_at: text, completed_at: nullableText, payload: z.record(text, z.unknown()).nullable(), result: z.record(text, z.unknown()).nullable(), lease_owner_token: nullableText, lease_expires_at: nullableText }).passthrough()
const actionExecution = z.object({ id: integer, request_id: integer, checklist_item_id: integer, action: text, actor_subject: nullableText, adapter: text, correlation_id: text, attempt: integer, status: text, error_category: nullableText, error_detail: nullableText, external_reference: nullableText, started_at: text, completed_at: nullableText, duration_ms: integer.nullable() }).passthrough()
const triage = z.object({ ticket_id: text, summary: z.object({ text, urgency_signals: z.array(text) }).passthrough(), recommendations: z.array(z.object({ id: text, kind: text, title: text, current: nullableText, suggested: nullableText, rationale: text, confidence: integer, applicable: z.boolean() }).passthrough()), knowledge_base: z.array(z.object({ score: integer, article: z.object({ id: text, title: text, summary: text, category: text }).passthrough() }).passthrough()), adapter: text, generated_at: text }).passthrough()

export type Triage = z.infer<typeof triage>

export function responseSchema(method: string, path: string): z.ZodType | undefined {
  if (path === '/ready') return z.object({ status: text, database: text })
  if (path === '/command-center/summary') return summary
  if (path === '/command-center/deliveries') return z.array(z.object({ id: integer, ticket_id: nullableText, status: z.enum(['pending', 'failed']), attempts: integer.nonnegative(), created_at: text }))
  if (path === '/tickets') return method === 'GET' ? z.array(ticket) : ticket
  if (path === '/tickets/manual') return ticket
  if (/^\/tickets\/[^/]+\/ai-triage$/.test(path)) return triage
  if (/^\/tickets\/[^/]+\/ai-recommendations\/apply$/.test(path)) return z.object({ ticket, applied: z.array(text) })
  if (/^\/tickets\/[^/]+$/.test(path)) return ticketDetail
  if (path === '/onboarding-requests') return method === 'GET' ? z.array(request) : request
  if (/^\/onboarding-requests\/[^/]+\/run$/.test(path)) return run
  if (/^\/onboarding-requests\/[^/]+$/.test(path)) return request
  if (path === '/workflow-runs') return z.array(run)
  if (path.startsWith('/workflow-runs/')) return run
  if (path === '/workflow-definitions') return method === 'GET' ? z.array(definition) : definition
  if (path.endsWith('/diff') && path.startsWith('/workflow-definitions/')) return diff
  if (path === '/workflow-definitions/validate') return z.object({ steps: z.array(definitionStep), checksum: text })
  if (path.startsWith('/workflow-definitions/')) return definition
  if (path === '/notifications/pending' || path === '/notifications/failed') return z.array(delivery)
  if (path === '/notifications/retry') return z.array(delivery)
  if (/^\/notifications\/[^/]+\/requeue$/.test(path)) return delivery
  if (path === '/workflow-jobs') return z.array(job)
  if (/^\/workflow-jobs\/[^/]+\/history$/.test(path)) return z.array(actionExecution)
  if (path.startsWith('/workflow-jobs/')) return job
  if (path === '/audit' || path === '/execution-logs') return z.array(history)
  return undefined
}
