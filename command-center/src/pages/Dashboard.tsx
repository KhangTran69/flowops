import { Link } from 'react-router-dom'
import { AlertTriangle, ArrowRight, CheckCircle2, Clock, Inbox, ShieldCheck, Ticket, Workflow } from 'lucide-react'
import { usePoll } from '../hooks/usePoll'
import { asList, collect, fmtDate, isDone, isOverdue, progressOf, titleOf, workflowOf } from '../lib/api'
import { DataState } from '../components/DataState'
import { PageHeader, ProgressBar } from '../components/Basics'
import { RecordTable } from '../components/RecordTable'
import { StatusBadge } from '../components/StatusBadge'

export default function Dashboard() {
  const tickets = usePoll<any>('/tickets')
  const runs = usePoll<any>('/workflow-runs')
  const requests = usePoll<any>('/onboarding-requests')
  const ts = asList<any>(tickets.data), rs = asList<any>(runs.data), rqs = asList<any>(requests.data)
  const open = ts.filter((t) => !isDone(t.status) && String(t.status || 'open') !== 'closed')
  const urgent = ts.filter((t) => String(t.priority || '').toLowerCase() === 'urgent' && !isDone(t.status))
  const active = rs.filter((r) => ['running', 'pending', 'queued', 'active'].includes(String(r.status || '').toLowerCase()))
  const gated = collect(rs, (s) => String(s.status || '').toLowerCase() === 'awaiting_approval' || s.approval_required === true)
  const overdue = collect(rs, isOverdue)
  const today = new Date().toDateString()
  const completedToday = ts.filter((t) => isDone(t.status) && t.completed_at && new Date(t.completed_at).toDateString() === today).length
  const kpis = [
    { label: 'Open tickets', value: open.length, to: '/tickets', icon: Ticket },
    { label: 'Urgent', value: urgent.length, to: '/tickets', icon: AlertTriangle, danger: true },
    { label: 'Pending approvals', value: gated.length, to: '/approvals', icon: ShieldCheck },
    { label: 'Active runs', value: active.length, to: '/runs', icon: Workflow },
    { label: 'Overdue steps', value: overdue.length, to: '/approvals', icon: Clock, danger: true },
    { label: 'Completed today', value: completedToday, to: '/tickets', icon: CheckCircle2 },
  ]
  const loading = tickets.loading || runs.loading || requests.loading
  const error = tickets.error || runs.error || requests.error
  return <>
    <PageHeader title="Operations dashboard" description="Live state across tickets, workflow runs, approvals and SLA timers." />
    <DataState loading={loading} error={error} status={tickets.status || runs.status} onRetry={() => { tickets.reload(); runs.reload(); requests.reload() }}>
      <div className="mb-5 grid gap-3 sm:grid-cols-2 xl:grid-cols-6">
        {kpis.map(({ label, value, to, icon: Icon, danger }) => <Link key={label} to={to} className="card p-4 transition hover:-translate-y-0.5 hover:shadow-md">
          <div className="flex items-center justify-between"><span className="muted text-xs uppercase tracking-wide">{label}</span><Icon size={16} className={danger && value ? 'text-[var(--danger)]' : 'muted'} /></div>
          <div className="mt-2 text-3xl font-semibold">{value}</div>
        </Link>)}
      </div>
      <div className="grid gap-5 xl:grid-cols-3">
        <section className="xl:col-span-2">
          <div className="mb-2 flex items-center justify-between"><h2 className="font-semibold">Recent tickets</h2><Link className="text-sm underline" to="/tickets">All tickets <ArrowRight className="inline" size={13} /></Link></div>
          <RecordTable rows={ts.slice(0, 8)} rowHref={(r) => `/tickets/${r.ticket_id ?? r.id}`} columns={[
            { key: 'title', header: 'Ticket', render: (r) => <div><div className="font-medium">{titleOf(r)}</div><div className="muted text-xs">{r.category || 'unclassified'}</div></div> },
            { key: 'priority', header: 'Priority', render: (r) => <StatusBadge status={r.priority} /> },
            { key: 'status', header: 'Status', render: (r) => <StatusBadge status={r.status || 'open'} /> },
            { key: 'queue', header: 'Queue', render: (r) => r.queue || '—' },
          ]} empty="No tickets yet." />
        </section>
        <section>
          <div className="mb-2 flex items-center justify-between"><h2 className="font-semibold">Active runs</h2><Link className="text-sm underline" to="/runs">All runs</Link></div>
          <div className="space-y-2">{active.slice(0, 6).map((r) => <Link key={r.id} to={`/runs/${r.id}`} className="card block p-3">
            <div className="mb-1 flex items-center justify-between gap-2"><span className="truncate font-medium">#{r.id} {workflowOf(r)}</span><StatusBadge status={r.status} /></div>
            <ProgressBar value={progressOf(r)} /><div className="muted mt-1 text-xs">{Math.round(progressOf(r))}% · {r.employee_name || r.request_id || '—'}</div>
          </Link>)}</div>
          {!active.length && <div className="card p-4 muted">No active runs.</div>}
        </section>
      </div>
      <section className="mt-6">
        <div className="mb-2 flex items-center gap-2"><h2 className="font-semibold">Overdue queue</h2>{overdue.length > 0 && <span className="badge bg-red-100 px-2 text-red-800 dark:bg-red-950 dark:text-red-300">{overdue.length}</span>}</div>
        <RecordTable rows={overdue} rowHref={({ run, step }) => `/runs/${run.id}`} columns={[
          { key: 'step', header: 'Step', render: ({ step }) => step.step || step.title || step.step_key || '—' },
          { key: 'run', header: 'Run', render: ({ run }) => `#${run.id}` },
          { key: 'queue', header: 'Queue', render: ({ step }) => step.queue || '—' },
          { key: 'due', header: 'SLA due', render: ({ step }) => fmtDate(step.sla_due_at || step.due_at) },
          { key: 'status', header: 'Status', render: ({ step }) => <StatusBadge status="overdue" /> },
        ]} empty="No overdue steps." />
      </section>
    </DataState>
  </>
}
