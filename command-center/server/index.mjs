import express from 'express'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const DIST = path.resolve(__dirname, '..', 'dist')
const app = express()
app.use(express.json({ limit: '1mb' }))

const API_URL = (process.env.FLOWOPS_API_URL || '').replace(/\/+$/, '')
const API_TOKEN = process.env.FLOWOPS_API_TOKEN || ''
const DEV = process.env.NODE_ENV !== 'production' && (process.env.FLOWOPS_ENVIRONMENT || 'development') === 'development' && process.env.FLOWOPS_COMMAND_CENTER_DEV === 'true'
const SEG = '[A-Za-z0-9_.:-]+'
const rr = (s) => new RegExp(`^${s}$`)

const GET_PATHS = [
  rr('/health'), rr('/ready'), rr('/tickets'), rr(`/tickets/${SEG}`),
  rr('/onboarding-requests'), rr(`/onboarding-requests/${SEG}`),
  rr(`/onboarding-requests/${SEG}/run`), rr('/workflow-runs'),
  rr(`/workflow-runs/${SEG}`), rr('/workflow-definitions'),
  rr(`/workflow-definitions/${SEG}`), rr(`/workflow-definitions/${SEG}/diff`),
  rr('/execution-logs'), rr('/audit'),
]
const OPERATOR_POST = [
  rr('/tickets'), rr(`/tickets/${SEG}/ai-triage`),
  rr(`/tickets/${SEG}/ai-recommendations/apply`),
  rr('/onboarding-requests'), rr(`/onboarding-requests/${SEG}/run`),
  rr(`/workflow-runs/${SEG}/(pause|resume|cancel)`),
]
const ADMIN_POST = [
  rr(`/workflow-runs/${SEG}/steps/${SEG}/(approve|reject)`),
  rr('/workflow-definitions'), rr(`/workflow-definitions/${SEG}/publish`),
]
const ADMIN_PUT = [rr(`/workflow-definitions/${SEG}`)]

function configured() { return Boolean(API_URL && (!DEV || API_TOKEN)) }

async function identity(req) {
  if (DEV) {
    const role = process.env.DEV_USER_ROLE || 'operator'
    if (!['admin', 'operator', 'viewer'].includes(role)) return null
    return { subject: 'local-development', role, headers: { Authorization: `Bearer ${API_TOKEN}` } }
  }
  const headers = { Accept: 'application/json' }
  const authorization = req.get('authorization')
  const cookie = (req.get('cookie') || '').split(';').map(v => v.trim()).find(v => v.startsWith('flowops_ui_session='))
  if (authorization) headers.Authorization = authorization
  else if (cookie) headers.Cookie = cookie
  else return null
  const response = await fetch(API_URL + '/command-center/session', { headers, redirect: 'error', signal: AbortSignal.timeout(10000) })
  if (!response.ok) return null
  const actor = await response.json()
  if (typeof actor.subject !== 'string' || !actor.subject || !['admin', 'operator'].includes(actor.role)) return null
  if (actor.csrf_token) headers['X-CSRF-Token'] = actor.csrf_token
  return { ...actor, headers }
}

function allowed(method, p) {
  if (p.split('/').some(segment => segment === '.' || segment === '..')) return false
  if (method === 'GET') return GET_PATHS.some((r) => r.test(p))
  if (method === 'POST') return OPERATOR_POST.some((r) => r.test(p)) || ADMIN_POST.some((r) => r.test(p))
  if (method === 'PUT') return ADMIN_PUT.some((r) => r.test(p))
  return false
}
function adminOnly(method, p) {
  return (method === 'POST' && ADMIN_POST.some((r) => r.test(p))) ||
    (method === 'PUT' && ADMIN_PUT.some((r) => r.test(p)))
}

function detailMessage(detail) {
  if (!detail) return ''
  if (Array.isArray(detail)) return detail.map((e) => `${(e.loc || []).join('.')}: ${e.msg || 'invalid'}`).join('; ')
  if (typeof detail === 'string') return detail
  try { return JSON.stringify(detail) } catch { return 'request failed' }
}

function friendly(status, detail, retryAfter) {
  const base = status === 401 ? 'Not authorized with the FlowOps API (check the token).'
    : status === 403 ? 'No permission for this action.'
    : status === 404 ? 'Not found.'
    : status === 409 ? 'Conflict or invalid state.'
    : status === 422 ? 'Input rejected.'
    : status === 429 ? `Too many requests.${retryAfter ? ` Retry after ${retryAfter}s.` : ''}`
    : status >= 500 ? 'FlowOps server error.' : 'Request failed.'
  const d = detailMessage(detail)
  return d ? `${base} ${d}` : base
}

app.get('/api/session', async (req, res) => {
  res.set('Cache-Control', 'no-store')
  if (!configured()) return res.status(503).json({ role: 'viewer', configured: false, email: '' })
  try {
    const actor = await identity(req)
    if (!actor) return res.status(401).json({ role: 'viewer', configured: false, email: '' })
    return res.json({ role: actor.role, configured: true, email: actor.subject })
  } catch {
    return res.status(503).json({ role: 'viewer', configured: false, email: '' })
  }
})

app.post('/api/flowops', async (req, res) => {
  const { method = 'GET', path: p, query, body } = req.body || {}
  if (!configured()) return res.json({ ok: false, status: 0, error: 'FlowOps API is not configured. Check the console server configuration.' })
  if (typeof p !== 'string' || !allowed(method, p)) return res.status(400).json({ ok: false, status: 400, error: 'Method/path is not allowed by the Command Center proxy.' })
  res.set('Cache-Control', 'no-store')
  if (!req.is('application/json') || req.get('X-FlowOps-Request') !== '1') return res.status(403).json({ ok: false, status: 403, error: 'Invalid console request.' })
  let actor
  try { actor = await identity(req) } catch { return res.status(503).json({ ok: false, status: 503, error: 'Identity verification unavailable.' }) }
  if (!actor) return res.status(401).json({ ok: false, status: 401, error: 'Authentication required.' })
  const { role } = actor
  if (method !== 'GET' && role === 'viewer') return res.status(403).json({ ok: false, status: 403, error: 'Viewers cannot perform this action.' })
  if (adminOnly(method, p) && role !== 'admin') return res.status(403).json({ ok: false, status: 403, error: 'Admin role required.' })
  const url = new URL(API_URL + p)
  if (query && typeof query === 'object') for (const [k, v] of Object.entries(query)) if (v !== undefined && v !== null && v !== '') url.searchParams.set(k, String(v))
  const opts = { method, headers: { ...actor.headers, Accept: 'application/json' }, redirect: 'error', signal: AbortSignal.timeout(20000) }
  const idempotencyKey = req.get('Idempotency-Key')
  if (method !== 'GET' && idempotencyKey) {
    if (!/^[A-Za-z0-9_.:-]{1,128}$/.test(idempotencyKey)) return res.status(400).json({ ok: false, status: 400, error: 'Invalid idempotency key.' })
    opts.headers['Idempotency-Key'] = idempotencyKey
  }
  if (body !== undefined && method !== 'GET') { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body) }
  let status = 0
  try {
    const r = await fetch(url, opts); status = r.status
    const text = await r.text(); let data = null
    try { data = text ? JSON.parse(text) : null } catch { data = text }
    if (!r.ok) {
      const detail = data && typeof data === 'object' ? data.detail : data
      return res.json({ ok: false, status: r.status, error: friendly(r.status, detail, r.headers.get('retry-after')) })
    }
    return res.json({ ok: true, status: r.status, data })
  } catch (err) {
    const timeout = err && (err.name === 'TimeoutError' || err.name === 'AbortError')
    return res.json({ ok: false, status: timeout ? 504 : 503, error: timeout ? 'FlowOps API timed out after 20s.' : 'FlowOps API is unreachable.' })
  } finally {
    console.log(`${method} ${p} ${status}`)
  }
})

if (process.env.NODE_ENV === 'production' || process.env.SERVE_STATIC === 'true') {
  app.use(express.static(DIST))
  app.use((_req, res) => res.sendFile(path.join(DIST, 'index.html')))
}
const port = Number(process.env.PORT || 8787)
const host = DEV ? '127.0.0.1' : (process.env.HOST || '127.0.0.1')
export { app }
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  app.listen(port, host, () => console.log(`Command Center proxy on ${host}:${port}`))
}
