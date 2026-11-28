import assert from 'node:assert/strict'
import { spawn, spawnSync } from 'node:child_process'
import { createServer } from 'node:http'
import { once } from 'node:events'
import net from 'node:net'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { test } from 'node:test'

// Proves the compose gateway (docker/nginx/default.conf) strips
// client-supplied identity headers before they can reach the FlowOps app
// (ROADMAP 2a). The gateway config file is loaded verbatim with only
// mechanical substitutions (docker DNS names -> 127.0.0.1, privileged port,
// writable temp paths), so the assertions run against the real proxy
// configuration, not a re-implementation of it.

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const REPO_ROOT = path.resolve(__dirname, '..', '..')
const NGINX_CONF = path.join(REPO_ROOT, 'docker', 'nginx', 'default.conf')
const IDENTITY_HEADERS = {
  'X-User-Role': 'admin',
  'X-User-Email': 'attacker@example.test',
  'X-Actor-Email': 'attacker@example.test',
}

function findNginx() {
  for (const candidate of ['nginx', '/usr/sbin/nginx', '/usr/local/sbin/nginx', '/opt/homebrew/sbin/nginx']) {
    if (spawnSync(candidate, ['-v'], { stdio: 'ignore' }).status === 0) return candidate
  }
  return null
}

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer()
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address()
      server.close(() => resolve(port))
    })
    server.on('error', reject)
  })
}

async function waitForPort(port, { attempts = 50 } = {}) {
  for (let i = 0; i < attempts; i++) {
    const open = await new Promise((resolve) => {
      const socket = net.connect(port, '127.0.0.1')
      socket.on('connect', () => { socket.destroy(); resolve(true) })
      socket.on('error', () => resolve(false))
    })
    if (open) return
    await new Promise((resolve) => setTimeout(resolve, 100))
  }
  throw new Error(`nothing listening on 127.0.0.1:${port}`)
}

test('gateway strips client-supplied identity headers before forwarding', async (t) => {
  const nginx = findNginx()
  if (!nginx) { t.skip('nginx binary not available on this host'); return }

  const upstream = createServer((req, res) => {
    res.setHeader('Content-Type', 'application/json')
    res.end(JSON.stringify(req.headers))
  }).listen(0, '127.0.0.1')
  await once(upstream, 'listening')
  const upstreamPort = upstream.address().port

  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'flowops-gateway-'))
  let nginxProcess = null
  let listenPort = null
  try {
    const base = fs.readFileSync(NGINX_CONF, 'utf8')
    listenPort = await freePort()
    const conf = base
      .replace('worker_processes 1;', [
        'daemon off;',
        `pid ${dir}/nginx.pid;`,
        `error_log ${dir}/error.log warn;`,
        'worker_processes 1;',
      ].join('\n'))
      .replace('access_log /var/log/nginx/access.log flowops;', `access_log ${dir}/access.log flowops;`)
      .replace('http {', [
        'http {',
        `  client_body_temp_path ${dir}/client_body;`,
        `  proxy_temp_path ${dir}/proxy_temp;`,
        `  fastcgi_temp_path ${dir}/fastcgi_temp;`,
        `  uwsgi_temp_path ${dir}/uwsgi_temp;`,
        `  scgi_temp_path ${dir}/scgi_temp;`,
      ].join('\n'))
      .replace('server app:8000;', `server 127.0.0.1:${upstreamPort};`)
      .replace('server grafana:3000;', `server 127.0.0.1:${upstreamPort};`)
      .replace('server webhook-sink:8080;', `server 127.0.0.1:${upstreamPort};`)
      .replace('listen 80;', `listen ${listenPort};`)
    if (conf.includes('server app:8000;') || conf.includes('listen 80;') || conf.includes('/var/log/nginx')) {
      assert.fail('could not rewrite the gateway config for local execution')
    }
    const confPath = path.join(dir, 'gateway.conf')
    fs.writeFileSync(confPath, conf)
    const validated = spawnSync(nginx, ['-t', '-c', confPath, '-p', dir], { encoding: 'utf8' })
    assert.equal(validated.status, 0, `gateway config does not validate: ${validated.stderr}`)

    nginxProcess = spawn(nginx, ['-c', confPath, '-p', dir], { stdio: 'ignore' })
    await waitForPort(listenPort)

    const response = await fetch(`http://127.0.0.1:${listenPort}/tickets`, {
      headers: { ...IDENTITY_HEADERS, 'X-Keep-Me': 'control', Accept: 'application/json' },
    })
    assert.equal(response.status, 200)
    const seen = await response.json()
    for (const header of Object.keys(IDENTITY_HEADERS)) {
      assert.equal(seen[header.toLowerCase()], undefined,
        `client-supplied ${header} must never reach the app through the gateway`)
    }
    assert.equal(seen['x-keep-me'], 'control',
      'the gateway must still forward the request with untouched headers')
  } finally {
    if (nginxProcess) {
      nginxProcess.kill('SIGQUIT')
      await new Promise((resolve) => nginxProcess.on('exit', resolve))
    }
    upstream.closeAllConnections()
    await new Promise((resolve) => upstream.close(resolve))
    fs.rmSync(dir, { recursive: true, force: true })
  }
})
