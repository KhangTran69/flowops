import { useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { ChevronDown, ChevronRight, Pause, Play, Square } from 'lucide-react'
import { usePoll } from '../hooks/usePoll'
import { fmtDate, isDone, isOverdue, mutate, progressOf, requestOf, txt, workflowOf } from '../lib/api'
import { useSession } from '../lib/session'
import { DataState } from '../components/DataState'
import { BackLink, JsonView, PageHeader, ProgressBar } from '../components/Basics'
import { StatusBadge } from '../components/StatusBadge'
import { Confirm } from '../components/Confirm'
import type { WorkflowRun } from '../lib/contracts'

export default function RunDetail() {
  const { id } = useParams()
  const { data: run, error, status, loading, reload } = usePoll<WorkflowRun>(`/workflow-runs/${id}`)
  const { role } = useSession()
  const [expanded, setExpanded] = useState<string | number | null>(null)
  const [confirm, setConfirm] = useState<null | 'pause' | 'resume' | 'cancel'>(null)
  const [busy, setBusy] = useState(false)
  const attempts = run?.steps || []
  const steps = run?.checklist ? run.checklist.map((item) => { const history = attempts.filter((a) => a.checklist_item_id === item.id); const latest = history[history.length - 1]; return { ...item, attempts: history.length, attempts_json: history, error: latest?.error, output: item.output ?? latest?.output } }) : attempts
  const events = run?.events || []
  const finished = useMemo(() => ['completed', 'failed', 'cancelled'].includes(String(run?.status || '').toLowerCase()), [run])
  async function action() {
    if (!confirm) return
    setBusy(true); const r = await mutate('POST', `/workflow-runs/${id}/${confirm}`, {}, `Run ${confirm}d.`); setBusy(false)
    if (r.ok) { setConfirm(null); reload() }
  }
  return <>
    <BackLink to="/runs">Workflow runs</BackLink>
    <PageHeader title={`Run #${id}`} description={`${workflowOf(run)} · request ${requestOf(run)} · ${run?.employee_name || '—'}`} actions={<StatusBadge status={run?.status} />} />
    <DataState loading={loading} error={error} status={status} onRetry={reload}>
      <div className="card mb-5 p-4">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2"><div><div className="label">Successfully completed steps</div><div className="text-2xl font-semibold">{Math.round(progressOf(run))}%</div></div><div className="text-right text-sm"><div className="muted">Execution attempts running</div><div className="text-xl font-medium">{(attempts.filter((s: any) => String(s.status) === 'running')).length}</div></div></div>
        <ProgressBar value={progressOf(run)} />
        <div className="mt-3 flex flex-wrap gap-2">
          <Link className="btn" to={`/requests/${requestOf(run)}`}>Parent request</Link>
          {!finished && role !== 'viewer' && <>
            {run?.status === 'paused' ? <button className="btn" onClick={() => setConfirm('resume')}><Play size={14} /> Resume</button> : <button className="btn" onClick={() => setConfirm('pause')}><Pause size={14} /> Pause</button>}
            <button className="btn btn-danger" onClick={() => setConfirm('cancel')}><Square size={14} /> Cancel</button>
          </>}
        </div>
      </div>
      <section className="card mb-5">
        <div className="border-b border-[hsl(var(--border))] p-4"><h2 className="font-semibold">Steps</h2></div>
        <div className="table-wrap"><table><thead><tr><th></th><th>Step</th><th>Queue</th><th>Status</th><th>Overdue</th><th>Attempts</th><th>SLA due</th></tr></thead><tbody>
          {steps.map((s: any, i: number) => { const key = s.id ?? s.step_key ?? i; const open = expanded === key; return <><tr key={`${key}-row`} className="cursor-pointer hover:bg-black/5 dark:hover:bg-white/5" onClick={() => setExpanded(open ? null : key)}>
            <td>{open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}</td><td className="font-medium">{s.step || s.title || s.step_key || '—'}</td><td>{s.queue || '—'}</td><td><StatusBadge status={s.status} /></td><td>{isOverdue(s) ? <StatusBadge status="overdue" /> : '—'}</td><td>{s.attempts ?? s.attempt ?? '—'}</td><td>{fmtDate(s.sla_due_at || s.due_at)}</td>
          </tr>{open && <tr key={`${key}-detail`}><td colSpan={7} className="bg-black/5 dark:bg-white/5"><div className="grid gap-3 md:grid-cols-3"><div><div className="label">Error</div><p>{s.error || '—'}</p></div><JsonView label="Attempts" value={s.attempts_json || s.attempts || []} /><JsonView label="Output" value={s.output || s.output_json || {}} /></div></td></tr>}</> })}
          {!steps.length && <tr><td colSpan={7} className="p-6 text-center muted">No steps.</td></tr>}
        </tbody></table></div>
      </section>
      <section className="card p-4"><h2 className="mb-3 font-semibold">Event timeline</h2>{events.length ? <ol className="space-y-3">{events.map((e: any, i: number) => <li key={e.id ?? i} className="border-l-2 border-[var(--primary)] pl-3"><div className="font-medium">{e.event_type || e.action || 'event'}</div><div className="muted text-xs">{fmtDate(e.created_at || e.timestamp)}</div>{e.payload && <div className="muted text-xs">{txt(e.payload)}</div>}</li>)}</ol> : <p className="muted">No events.</p>}</section>
    </DataState>
    <Confirm open={confirm !== null} onOpenChange={(v) => !v && setConfirm(null)} title={`${confirm ? confirm[0].toUpperCase() + confirm.slice(1) : ''} run?`} description={confirm === 'cancel' ? 'This cancels queued work and prevents new steps from being scheduled.' : 'This changes the workflow run state.'} confirmLabel={confirm ? confirm[0].toUpperCase() + confirm.slice(1) : 'Confirm'} destructive={confirm === 'cancel'} busy={busy} onConfirm={action} />
  </>
}
