# Deploying FlowOps

Two supported paths for a public demo:

- **Fly.io (recommended)** — one Fly app runs the full topology as process
  groups, with Fly Postgres for data. Chosen over Render because FlowOps
  needs several long-running processes (worker, scheduler, timers, outbox),
  which Fly runs as process groups of one app; Render would need one service
  per process with more moving parts. The choice is recorded in
  [ROADMAP Phase 4](../docs/ROADMAP.md#phase-4--product-surface-portfolio-value-overlaps-phase-2).
- **Docker Compose `prod` profile** — single-host self-hosting with the same
  hardening (auth on, secure cookies, rate limit), for a VPS or an internal
  server.

Everything below uses **simulated adapters only** — no Microsoft Graph, n8n,
or ConnectWise credentials are needed or contacted. Secrets are referenced by
environment-variable name and set with `fly secrets set` (Fly) or a shell
environment / `.env` (Compose); they are never committed.

## What runs on Fly

One Fly app (`deploy/fly.toml`), one machine per process group:

| Process group | Command | Purpose |
| --- | --- | --- |
| `app` | `uvicorn src.api:app ...` | API, dashboards, operator console |
| `worker` | `python -m src.worker` | durable workflow jobs |
| `scheduler` | `python -m src.scheduler --poll` | DAG orchestration |
| `timers` | `python -m src.timers --poll` | SLA timers |
| `outbox` | `python -m src.outbox_worker` | webhook deliveries |
| `workforce-outbox` | `python -m src.workforce_worker` | workforce automation events |

**Single-replica limit:** the rate limiter is in-process
(`FLOWOPS_RATE_LIMIT_PER_MINUTE=120` applies per machine), and leases assume
one scheduler/worker per role. Keep every group at exactly one machine until
the multi-process rate limiting and lease-staleness work recorded in
[ROADMAP Phase 3](../docs/ROADMAP.md#phase-3--reliability-depth-ongoing-behind-the-gates)
lands.

## One-time bring-up (Fly)

Requires [flyctl](https://fly.io/docs/hands-on/install-flyctl/) and a Fly
account. Commands below assume `flyctl` v0.3.x; if a flag differs on your
version, check `fly <command> --help`.

```bash
brew install flyctl          # or: curl -L https://fly.io/install.sh | sh
fly auth login

# 1. Create the app (name must match app = ... in deploy/fly.toml)
fly apps create flowops-demo

# 2. Create a single-node Postgres and attach it (prints DATABASE_URL)
fly postgres create --name flowops-demo-db --region ams --initial-cluster-size 1
fly postgres attach flowops-demo-db --app flowops-demo
```

`attach` prints and stores a `postgres://` URL, but FlowOps needs SQLAlchemy's
`postgresql+psycopg://` driver scheme (the repo pins `psycopg`, not
`psycopg2`). Convert it and set it as the app's database URL:

```bash
fly secrets set FLOWOPS_DATABASE_URL="$(
  fly postgres connection-string flowops-demo-db \
  | sed 's|^postgres://|postgresql+psycopg://|')"
```

If `fly postgres connection-string` is unavailable in your flyctl version,
rerun `fly postgres attach` and copy the printed `DATABASE_URL` into the same
`sed` expression.

Next, the secrets the demo profile needs (values are examples — generate your
own):

```bash
# Session signing key (>= 32 chars)
fly secrets set FLOWOPS_SESSION_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"

# API bearer token (the demo's operator credential for API clients)
fly secrets set FLOWOPS_DEV_BEARER_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"

# UI login account for the operator console (password hash, not plaintext).
# Generate the JSON snippet with: python -m src.demo_users hash
fly secrets set FLOWOPS_DEMO_USERS='{"demo-operator":{"password_hash":"<paste>","roles":["automation.operator"],"tenant_key":"demo"}}'
```

Deploy, migrate, seed, verify:

```bash
# 3. Build and release
fly deploy --config deploy/fly.toml

# 4. Run migrations once per release (see "Migrations are a release step")
fly ssh console --select -C "alembic upgrade head"

# 5. Seed the demo dataset (needs the seed script from the demo-seed PR;
#    otherwise create data through the API/UI)
fly ssh console --select -C "python -m scripts.seed_demo"

# 6. Verify
fly status
curl --fail https://flowops-demo.fly.dev/ready
curl --fail https://flowops-demo.fly.dev/health
fly logs
```

Then scale-check: `fly machine list` should show one machine per process
group, and `fly scale count app=1 worker=1 scheduler=1 timers=1 outbox=1
workforce-outbox=1` pins that if anything drifts.

Optional: point `FLOWOPS_NOTIFICATION_WEBHOOK_URL` at an external request
inspector (e.g. a webhook.site URL via `fly secrets set`) so webhook delivery
and dead-letter retries are visible in the demo. Unset, notification delivery
is skipped cleanly.

## Migrations are a release step (Fly)

`deploy/fly.toml` sets `FLOWOPS_RUN_MIGRATIONS=false`. With six machines
booting concurrently, a container-start migration would race alembic against
itself, so each release runs `fly ssh console --select -C "alembic upgrade
head"` after `fly deploy` (step 4 above). Recording the follow-up decision
here: moving this into the release pipeline (a Fly release command or CI
deploy job) is pending — tracked in ROADMAP Phase 4. The Compose `prod`
profile keeps container-start migrations because it runs a single app
container on one host.

## Auth on the demo (and the Entra follow-up)

The demo profile enforces auth — `FLOWOPS_AUTH_REQUIRED=true` for API calls
(bearer token) and `FLOWOPS_UI_AUTH_REQUIRED=true` with secure cookies for
the console — but it runs with the app's `development` environment label.
That is deliberate: **outside `development`, the app requires an Entra
identity provider at startup** (issuer, audience, and JWKS URL — validated
fail-fast in `src/api.py`), and Entra registration is release gate 2a, which
only the maintainer can complete. Once that gate closes:

```bash
fly secrets set FLOWOPS_ENTRA_ISSUER=... FLOWOPS_ENTRA_AUDIENCE=... \
  FLOWOPS_ENTRA_JWKS_URL=... FLOWOPS_ENVIRONMENT=production
```

and the demo token path is replaced by real per-user sign-in. The gateway
must also strip client-supplied identity headers when that lands (ROADMAP
2a; tracked as its own task with an offline test).

## Self-hosting with the Compose `prod` profile

`docker-compose.yml` defines a `prod` service profile with the same
hardening. Unlike Fly, this runs a single `prod-app` container, so
container-start migrations stay enabled.

```bash
export FLOWOPS_SESSION_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
export FLOWOPS_DEV_BEARER_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
docker compose --profile prod up -d prod-app
curl --fail http://127.0.0.1:8000/ready
```

Because the profile defaults to `FLOWOPS_ENVIRONMENT=production`, it fails
fast at startup until an Entra configuration exists (same gate as above).
For the demo path on a single host, run it with
`FLOWOPS_ENVIRONMENT=development` and the demo secrets from the Fly section.

## What this deployment does not include

- The nginx gateway from `docker-compose.yml` (it is a local-monitoring
  convenience; on Fly, edge TLS is provided by the platform).
- Prometheus/Grafana (attach a Grafana Cloud agent to `/metrics` later).
- Live connectors and real Microsoft 365 actions — deliberately out of scope
  for the demo (simulated adapters; see ROADMAP gates).
