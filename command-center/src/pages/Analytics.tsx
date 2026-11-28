import { useState, type FormEvent, type ReactNode } from 'react'
import { DataState } from '../components/DataState'
import { PageHeader } from '../components/Basics'
import { RecordTable } from '../components/RecordTable'
import { StatusBadge } from '../components/StatusBadge'
import { usePoll } from '../hooks/usePoll'
import { fmtDate } from '../lib/api'
import type { AnalyticsCounts, AnalyticsHealth, AnalyticsStep, AnalyticsSummary } from '../lib/contracts'

type Filters = { days: number; stuck: number; tenant: string }
const duration = (seconds: number | null) => seconds === null ? '—' : seconds < 60 ? `${Math.round(seconds)}s` : `${Math.floor(seconds / 60)}m ${Math.floor(seconds % 60)}s`
const rate = (value: number | null) => value === null ? '—' : `${Math.round(value * 100)}%`

function Section({ title, children }: { title: string; children: ReactNode }) {
  return <section className="space-y-3"><h2 className="text-lg font-semibold">{title}</h2>{children}</section>
}
function Metric({ label, value }: { label: string; value: string | number }) {
  return <div className="card p-4"><div className="muted text-sm">{label}</div><div className="mt-1 text-xl font-semibold">{value}</div></div>
}
function Counts({ value, label }: { value: AnalyticsCounts; label: string }) {
  const rows = Object.entries(value.by_status).sort((a, b) => b[1] - a[1]).map(([status, count]) => ({ status, count }))
  return <RecordTable rows={rows} rowKey={r => r.status} empty={`No ${label.toLowerCase()} in this window.`} columns={[
    { key: 'status', header: 'Status', render: r => <StatusBadge status={r.status} /> },
    { key: 'count', header: label },
  ]} />
}

function AnalyticsResults({ filters }: { filters: Filters }) {
  const tenant = filters.tenant || undefined
  const summary = usePoll<AnalyticsSummary>('/analytics/summary', { days: filters.days, tenant })
  const health = usePoll<AnalyticsHealth>('/analytics/health', { stuck_minutes: filters.stuck, tenant })
  const steps = usePoll<AnalyticsStep[]>('/analytics/steps', { days: filters.days, tenant })
  const s = summary.data
  const h = health.data
  return <div className="space-y-6">
    <DataState loading={summary.loading} error={summary.error} status={summary.status} onRetry={summary.reload}>
      {s && <div className="space-y-6">
        <p className="muted text-sm" aria-live="polite">History: {s.window.days} days</p>
        <p className="muted text-sm">Tenant: {s.window.tenant_key ?? 'All authorized tenants'}</p>
        <Section title="Runs">
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            <Metric label="Runs in window" value={s.runs.total} />
            <Metric label="Active runs" value={s.runs.active} />
            <Metric label="Completion rate" value={rate(s.runs.completion_rate)} />
            <Metric label="Failure rate" value={rate(s.runs.failure_rate)} />
            <Metric label="Oldest active run" value={duration(s.runs.oldest_active_seconds)} />
            <Metric label="Median duration" value={duration(s.runs.median_duration_seconds)} />
            <Metric label="p95 duration" value={duration(s.runs.p95_duration_seconds)} />
            <Metric label="SLA items at risk" value={s.sla.breached + s.sla.overdue_pending} />
          </div>
          <Counts value={s.runs} label="Runs" />
        </Section>
        <Section title="By definition">
          <RecordTable rows={s.by_definition} rowKey={r => r.definition_key} columns={[
            { key: 'definition_key', header: 'Definition' }, { key: 'request_type', header: 'Type' },
            { key: 'runs', header: 'Runs' }, { key: 'completed', header: 'Completed' }, { key: 'failed', header: 'Failed' },
            { key: 'median_seconds', header: 'Median', render: r => duration(r.median_seconds) },
            { key: 'p95_seconds', header: 'p95', render: r => duration(r.p95_seconds) },
          ]} />
        </Section>
        <div className="grid gap-6 lg:grid-cols-2">
          <Section title="SLA timers">
            <p className="muted text-sm">Breach rate {rate(s.sla.breach_rate)} · {s.sla.overdue_pending} overdue pending · median overrun {duration(s.sla.median_overrun_seconds)}</p>
            <Counts value={s.sla} label="Timers" />
          </Section>
          <Section title="Approvals"><Counts value={s.approvals} label="Items" /></Section>
        </div>
        <Section title="Top tenants">
          <RecordTable rows={s.by_tenant} rowKey={r => r.tenant_key} columns={[{ key: 'tenant_key', header: 'Tenant' }, { key: 'requests', header: 'Requests' }]} />
        </Section>
      </div>}
    </DataState>
    <Section title="Workflow health">
      <DataState loading={health.loading} error={health.error} status={health.status} onRetry={health.reload}>
        {h && <>
          <p className="muted text-sm">Stale after {h.threshold_minutes} minutes</p>
          <p role="status">{h.stuck_count ? `${h.stuck_count} stale workflow heartbeats require review.` : 'No stale workflow heartbeats detected.'}</p>
          <RecordTable rows={h.stuck_runs} rowKey={r => r.run_id} rowHref={r => `/runs/${r.run_id}`} empty="No stale runs." columns={[
            { key: 'run_id', header: 'Run', render: r => `#${r.run_id}` }, { key: 'request_id', header: 'Request' },
            { key: 'tenant_key', header: 'Tenant' }, { key: 'heartbeat_at', header: 'Last heartbeat', render: r => fmtDate(r.heartbeat_at) },
            { key: 'age_seconds', header: 'Stale for', render: r => duration(r.age_seconds) }, { key: 'reason', header: 'Reason' },
          ]} />
        </>}
      </DataState>
    </Section>
    <Section title="Step bottlenecks">
      <p className="muted text-sm">Slowest median first. Fewer than two timed successes are not ranked.</p>
      <DataState loading={steps.loading} error={steps.error} status={steps.status} onRetry={steps.reload}>
        <RecordTable rows={steps.data ?? []} rowKey={r => r.step} empty="No completed step runs in this window." columns={[
          { key: 'step', header: 'Step' }, { key: 'median_seconds', header: 'Median', render: r => duration(r.median_seconds) },
          { key: 'p95_seconds', header: 'p95', render: r => duration(r.p95_seconds) }, { key: 'succeeded', header: 'Succeeded' },
          { key: 'failed', header: 'Failed' }, { key: 'sample', header: 'Timed samples' }, { key: 'ranked', header: 'Ranked', render: r => r.ranked ? 'Yes' : 'No' },
        ]} />
      </DataState>
    </Section>
  </div>
}

export default function Analytics() {
  const [draft, setDraft] = useState<Filters>({ days: 30, stuck: 30, tenant: '' })
  const [filters, setFilters] = useState(draft)
  function submit(event: FormEvent) {
    event.preventDefault()
    setFilters({ ...draft, tenant: draft.tenant.trim() })
  }
  return <>
    <PageHeader title="Run analytics and SLA reporting" description="Outcomes, durations, approvals, bottlenecks and stale workflow heartbeats." />
    <form className="card mb-5 flex flex-wrap items-end gap-3 p-4" onSubmit={submit}>
      <label className="text-sm"><span className="label">Tenant (optional)</span><input className="input" maxLength={64} value={draft.tenant} onChange={e => setDraft({ ...draft, tenant: e.target.value })} /></label>
      <label className="text-sm"><span className="label">History window</span><select className="input" value={draft.days} onChange={e => setDraft({ ...draft, days: Number(e.target.value) })}>{[7, 30, 90].map(v => <option key={v} value={v}>{v} days</option>)}</select></label>
      <label className="text-sm"><span className="label">Stale threshold</span><select className="input" value={draft.stuck} onChange={e => setDraft({ ...draft, stuck: Number(e.target.value) })}>{[15, 30, 60, 120].map(v => <option key={v} value={v}>{v} minutes</option>)}</select></label>
      <button className="btn" type="submit">Update view</button>
    </form>
    <AnalyticsResults key={JSON.stringify(filters)} filters={filters} />
  </>
}
