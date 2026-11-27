import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { once } from 'node:events'
import { test } from 'node:test'

test('production proxy verifies identity and forwards only authenticated credentials', async () => {
  const calls = []
  const backend = createServer((req, res) => {
    calls.push({ path: req.url, headers: req.headers, method: req.method })
    res.setHeader('Content-Type', 'application/json')
    const token = req.headers.authorization
    if (req.url === '/command-center/session') {
      if (token === 'Bearer operator' || token === 'Bearer admin') {
        res.end(JSON.stringify({ subject: 'verified-user', role: token.endsWith('admin') ? 'admin' : 'operator', tenant_key: 'tenant-a' }))
      } else if (req.headers.cookie === 'flowops_ui_session=verified') {
        res.end(JSON.stringify({ subject: 'session-user', role: 'operator', csrf_token: 'verified-csrf' }))
      } else { res.statusCode = 401; res.end('{}') }
    } else { res.end(JSON.stringify({ accepted: true })) }
  }).listen(0, '127.0.0.1')
  await once(backend, 'listening')
  let proxy
  try {
    process.env.NODE_ENV = 'production'
    process.env.FLOWOPS_API_URL = 'http://127.0.0.1:' + backend.address().port
    process.env.FLOWOPS_API_TOKEN = 'must-not-forward'
    process.env.FLOWOPS_COMMAND_CENTER_DEV = 'true'
    const { app } = await import('./index.mjs')
    proxy = app.listen(0, '127.0.0.1')
    await once(proxy, 'listening')
    const url = 'http://127.0.0.1:' + proxy.address().port
    const invoke = (method, path, headers = {}, marker = true) => fetch(url + '/api/flowops', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...(marker ? { 'X-FlowOps-Request': '1' } : {}), ...headers },
      body: JSON.stringify({ method, path, body: {} }),
    })
    assert.equal((await invoke('GET', '/tickets', { 'X-User-Role': 'admin', 'X-User-Email': 'forged' })).status, 401)
    assert.equal(calls.length, 0)
    assert.equal((await invoke('GET', '/tickets', { Authorization: 'Bearer invalid' })).status, 401)
    assert.equal((await invoke('POST', '/workflow-definitions', { Authorization: 'Bearer operator', 'X-User-Role': 'admin' })).status, 403)
    assert.equal((await invoke('POST', '/tickets', { Cookie: 'flowops_ui_session=verified' }, false)).status, 403)
    assert.equal((await invoke('GET', '/tickets/..', { Authorization: 'Bearer admin' })).status, 400)
    const session = await fetch(url + '/api/session', { headers: { Authorization: 'Bearer operator', 'X-User-Email': 'forged' } })
    assert.deepEqual(await session.json(), { role: 'operator', configured: true, email: 'verified-user' })
    const created = await invoke('POST', '/tickets', { Authorization: 'Bearer operator', 'Idempotency-Key': 'stable-123', 'X-Actor-Email': 'forged' })
    assert.equal((await created.json()).ok, true)
    let upstream = calls.at(-1)
    assert.equal(upstream.headers.authorization, 'Bearer operator')
    assert.equal(upstream.headers['idempotency-key'], 'stable-123')
    assert.equal(upstream.headers['x-actor-email'], undefined)
    const cookie = await invoke('POST', '/tickets', { Cookie: 'other=secret; flowops_ui_session=verified' })
    assert.equal((await cookie.json()).ok, true)
    upstream = calls.at(-1)
    assert.equal(upstream.headers.cookie, 'flowops_ui_session=verified')
    assert.equal(upstream.headers['x-csrf-token'], 'verified-csrf')
    assert.equal(upstream.headers.authorization, undefined)
  } finally {
    if (proxy) { proxy.closeAllConnections(); await new Promise(resolve => proxy.close(resolve)) }
    backend.closeAllConnections()
    await new Promise(resolve => backend.close(resolve))
  }
})
