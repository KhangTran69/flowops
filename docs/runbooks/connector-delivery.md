# Connector delivery and incident runbook

Scope: existing Jira, ServiceNow, Slack, Teams and generic connector
boundaries. These are implementation contracts, not live provider certification.

## Preconditions and setup

Use an isolated test tenant and separate provider credentials. Store secret
values in the server's secret store and use the managed environment names
required by the connector configuration. Approve only the tenant's intended
HTTPS provider hosts in FLOWOPS_CONNECTOR_ALLOWED_HOSTS_JSON. Keep an
outbound network policy that prevents access to private/internal destinations.
Do not enter credentials in connector JSON or log provider response bodies.

Jira and ServiceNow polling accepts at most 50 records and 2 MiB per page,
uses a 15-second request timeout and rejects redirects. Jira Cloud uses enhanced search continuation tokens and ServiceNow uses a
validated sys_id keyset scan. One page is processed per poll. The checkpoint
commits with successful ingestion; partial failures replay that page, with
stable fingerprints suppressing duplicates. Configuration changes reset the
scan. A completed scan starts over on the next poll. This preserves existing
first-seen object identity; it is not a change-data capture watermark and does
not guarantee every intermediate provider update. Before enabling real sources,
validate token expiration, JQL scope, ACL-limited pages and pagination in a
provider sandbox. First full scans can discover older records: review trigger
rules and use an isolated tenant before enabling. Never edit opaque tokens by
hand.

Slack requires a configured signing secret, validates the raw body and
five-minute replay window, and uses a stable delivery identity. Teams and
generic webhook sources must supply a stable string ID. Existing historical
fingerprints remain unchanged. Use the existing per-tenant webhook credential
and do not reuse IDs for unrelated deliveries.

## Incident response

1. Disable the affected connector using the existing admin API; preserve
   event and execution-log IDs and tenant scope.
2. Inspect connector.poll_failed and connector.event_failed entries, poll
   failure counts, last-polled time, and pending workflow/outbox jobs.
   Do not paste provider credentials or raw payloads into incident reports.
3. Verify provider reachability, configured hostname, credential validity,
   tenant status and response format in the isolated environment. Do not
   bypass redirect, size or tenant checks.
4. Re-enable only after the cause is resolved; confirm successful polling,
   then compare provider event IDs to persisted fingerprints. Reconcile
   uncertain external effects before any manual retry.
5. Escalate persistent backlog or lost provider events; current polling is
   bounded and has resumable scan checkpoints, not a change-data watermark. Database deduplication
   does not establish exactly-once external delivery.

PostgreSQL coordinates competing pollers with row locks; SQLite remains
the single-process local development path. CI uses fake providers and a
disposable database. Real-provider authentication, actual delivery latency,
pagination completeness and network egress controls require staging evidence.
