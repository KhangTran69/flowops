import { toast } from 'sonner'

export type ApiResult<T = unknown> = { ok: boolean; status: number; data?: T; error?: string }

export async function api<T = unknown>(
  method: 'GET' | 'POST' | 'PUT',
  path: string,
  opts: { query?: Record<string, unknown>; body?: unknown } = {},
): Promise<ApiResult<T>> {
  try {
    const r = await fetch('/api/flowops', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ method, path, query: opts.query, body: opts.body }),
    })
    const json = (await r.json()) as ApiResult<T>
    if (typeof json?.ok !== 'boolean') return { ok: false, status: r.status, error: 'Unexpected proxy response.' }
    return json
  } catch {
    return { ok: false, status: 503, error: 'Command Center proxy is unreachable.' }
  }
}

export async function mutate(
  method: 'POST' | 'PUT',
  path: string,
  body: unknown,
  successMsg: string,
): Promise<ApiResult> {
  const result = await api(method, path, { body })
  if (result.ok) toast.success(successMsg)
  else toast.error(result.error || 'Request failed.')
  return result
}

export function asList<T = any>(d: unknown): T[] {
  if (Array.isArray(d)) return d as T[]
  if (d && typeof d === 'object') {
    const o = d as Record<string, unknown>
    for (const key of ['items', 'results', 'data', 'records']) if (Array.isArray(o[key])) return o[key] as T[]
  }
  return []
}

export function fmtDate(v: unknown): string {
  if (!v) return '—'
  const d = new Date(String(v))
  if (Number.isNaN(d.getTime())) return String(v)
  return d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

export function txt(v: unknown): string {
  if (v === null || v === undefined) return '—'
  if (typeof v === 'string') return v
  if (typeof v === 'number' || typeof v === 'boolean') return String(v)
  try { return JSON.stringify(v, null, 2) } catch { return String(v) }
}

const DONE = new Set(['completed', 'succeeded', 'skipped', 'done', 'cancelled', 'failed', 'approved', 'rejected'])
export const isDone = (status: unknown) => DONE.has(String(status || '').toLowerCase())

export function progressOf(run: any): number {
  const p = Number(run?.progress)
  if (Number.isFinite(p)) return Math.max(0, Math.min(100, p <= 1 ? p * 100 : p))
  const steps = run?.steps || run?.step_runs || []
  const total = Number(run?.total_steps ?? run?.step_count ?? steps.length)
  const completed = Number(run?.completed_steps ?? steps.filter((s: any) => isDone(s?.status)).length)
  return total > 0 ? Math.round((completed / total) * 100) : 0
}

export function isOverdue(step: any): boolean {
  if (!step || isDone(step.status)) return false
  if (step.overdue === true || step.is_overdue === true) return true
  const due = step.sla_due_at || step.due_at
  return Boolean(due && new Date(due).getTime() < Date.now())
}

export function isGated(step: any): boolean {
  if (!step) return false
  const s = String(step.status || '').toLowerCase()
  return s === 'awaiting_approval' || (s === 'waiting' && Boolean(step.approval_required ?? step.requires_approval))
}

export function collect(runs: any[], pred: (step: any) => boolean): Array<{ run: any; step: any }> {
  const out: Array<{ run: any; step: any }> = []
  for (const run of runs || []) for (const step of (run?.steps || run?.step_runs || [])) if (pred(step)) out.push({ run, step })
  return out
}

export const titleOf = (x: any) => x?.title ?? x?.subject ?? x?.step ?? x?.name ?? x?.key ?? 'Untitled'
export const workflowOf = (x: any) => x?.workflow_key ?? x?.definition_key ?? x?.workflow ?? '—'
export const requestOf = (x: any) => x?.request_id ?? x?.onboarding_request_id ?? x?.checklist_id ?? '—'
export const employeeOf = (x: any) => x?.employee_name ?? x?.requester_name ?? x?.requester_email ?? '—'
