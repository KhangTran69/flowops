import assert from 'node:assert/strict'
import { test } from 'node:test'
import { responseSchema } from '../src/lib/validation.ts'

test('core response schemas reject drift and permit additive fields', () => {
  const valid = { ticket_id: '42', requester_id: 7, title: 'VPN issue', status: 'open', priority: 'normal', category: null, queue: 'it-network' }
  const tickets = responseSchema('GET', '/tickets')
  assert(tickets.safeParse([{ ...valid, new_field: 'safe addition' }]).success)
  assert(!tickets.safeParse([{ ...valid, requester_id: '7' }]).success)
  assert(!tickets.safeParse([{ ...valid, status: 'unknown' }]).success)
  assert(!tickets.safeParse([{ ...valid, title: undefined }]).success)
  assert(!responseSchema('GET', '/workflow-runs/1').safeParse({ id: 1, status: 'running', steps: [] }).success)
  assert(!responseSchema('GET', '/command-center/summary').safeParse({ open_tickets: 1 }).success)
  assert(responseSchema('GET', '/workflow-definitions/test/diff').safeParse({ from_version: 1, to_version: 2, added: [], removed: [], changed: ['step'] }).success)
})


test('analytics schemas preserve unknown durations and reject incompatible data', () => {
  const counts = { total: 0, by_status: {} }
  const summary = {
    window: { days: 30, tenant_key: null },
    runs: { ...counts, completion_rate: null, failure_rate: null, active: 0, oldest_active_seconds: null, median_duration_seconds: null, p95_duration_seconds: null },
    approvals: counts, sla: { ...counts, breached: 0, overdue_pending: 0, breach_rate: null, median_overrun_seconds: null },
    by_definition: [], by_tenant: [],
  }
  const schema = responseSchema('GET', '/analytics/summary')
  assert(schema.safeParse(summary).success)
  assert(!schema.safeParse({ ...summary, runs: { ...summary.runs, total: -1 } }).success)
  assert(!schema.safeParse({ ...summary, runs: { ...summary.runs, completion_rate: 'unknown' } }).success)
  const health = responseSchema('GET', '/analytics/health')
  assert(health.safeParse({ threshold_minutes: 30, tenant_key: null, stuck_count: 0, stuck_runs: [] }).success)
  assert(!health.safeParse({ threshold_minutes: 30, tenant_key: null, stuck_count: 1, stuck_runs: [{}] }).success)
  const steps = responseSchema('GET', '/analytics/steps')
  assert(steps.safeParse([{ step: 'Manual', succeeded: 0, failed: 1, attempts: 1, failure_rate: 1, median_seconds: null, p95_seconds: null, sample: 0, ranked: false }]).success)
  assert(!steps.safeParse([{ step: 'Manual', median_seconds: -1 }]).success)
})
