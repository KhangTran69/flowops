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
