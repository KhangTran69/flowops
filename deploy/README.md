# Deploying FlowOps

Two deployment candidates (not a verified public release):

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

Before the first protected release, configure the real Entra issuer, audience
and HTTPS JWKS URL, a strong session secret and a distinct webhook API key.
Use the Fly secret manager and do not print values in release logs. Development
bearer tokens and demo password accounts must not be configured on this app.
Run the identity, tenant-isolation, backup and staging acceptance checks in
the roadmap before approving any public rollout.

The release command runs the local configuration checks and Alembic upgrade
before replacing application machines. It does not establish that the real
provider is working or that a release is approved. After the separately
authorized rollout, check `/ready`, `/health`, machine counts, worker
heartbeats and the protected operator journey. Do not seed production data;
use an isolated synthetic staging database for portfolio demonstrations.

Then scale-check: `fly machine list` should show one machine per process
group, and `fly scale count app=1 worker=1 scheduler=1 timers=1 outbox=1
workforce-outbox=1` pins that if anything drifts.

Keep outbound webhook delivery disabled on the simulated public candidate.

## Migrations are a release step (Fly)

`FLOWOPS_RUN_MIGRATIONS=false` prevents each process from independently
running migrations. Fly's `[deploy].release_command` invokes
`python -m scripts.release_preflight` once before rollout. Invalid security
configuration, missing PostgreSQL configuration or a failed migration
aborts the release. See https://fly.io/docs/reference/configuration/#the-deploy-section.

Before migration, record the previous image, schema revision and verified
backup. Keep concurrent releases serialized. Confirm compatibility between
the old and new images while the release command runs; a nonzero release
command does not reverse database changes already applied. If rollback is
needed, stop writes, evaluate schema compatibility, restore the verified
backup where necessary, and redeploy the recorded image only after approval.
Do not automatically downgrade a live database.

## Authentication and remaining release gates

The Fly configuration defaults to production mode with simulated adapters.
The release candidate still must not be made publicly accessible until the
real identity and all remaining release gates are independently validated.
It does not permit the previous development bearer-token or password-account
fallback. A real Entra registration and live authentication/authorization
validation remain mandatory. Workforce browser access and a protected React
gateway deployment remain separate acceptance criteria.

## Self-hosting with the Compose `prod` profile

`docker-compose.yml` defines a `prod` service profile with the same
hardening. Unlike Fly, this runs a single `prod-app` container, so
container-start migrations stay enabled.

Configure the required Entra, session, webhook and PostgreSQL secrets through
the operator-approved secret store. Do not set a development bearer token in
production. Validate a synthetic staging environment before authorizing the
Compose profile or any public rollout.

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

The simulated release candidate refuses outbound action-failure and notification
webhooks, n8n credentials and callback secrets, non-HTTPS identity endpoints,
and reused session/webhook credentials. The release command takes a
PostgreSQL session advisory lock before invoking Alembic (ten-minute command
timeout); overlapping preflight releases fail before migrating. This is not
a substitute for an approved maintenance window: manual Alembic invocations
do not participate in this lock. Public rollout remains blocked until the
real identity, staging and accessibility gates have been satisfied.

Protected preflight requires a positive integer admission limit (1–100000)
before any schema migration. Pair the protected release with the shared
PostgreSQL limiter from #92; ensure all replicas use the same value and
migrate the counter table before serving traffic. Retain a separate ingress
abuse limit and readiness monitor; API admission failures can also make the
authenticated metrics scrape unavailable. No public release is approved.
