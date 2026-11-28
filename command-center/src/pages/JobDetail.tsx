import { useState } from 'react'
import { useParams } from 'react-router-dom'
import { RotateCcw, Square } from 'lucide-react'
import { usePoll } from '../hooks/usePoll'
import { fmtDate, mutate } from '../lib/api'
import { useSession } from '../lib/session'
import { DataState } from '../components/DataState'
import { BackLink, JsonView, PageHeader } from '../components/Basics'
import { StatusBadge } from '../components/StatusBadge'
import { Confirm } from '../components/Confirm'
import type { ActionExecution, WorkflowJob } from '../lib/contracts'

export default function JobDetail() {
  const { id } = useParams()
  const { data: job, error, status, loading, reload } = usePoll<WorkflowJob>(`/workflow-jobs/${id}`)
  const { data: historyResult, error: historyError, status: historyStatus, loading: historyLoading } = usePoll<ActionExecution[]>(`/workflow-jobs/${id}/history`)
  const { role } = useSession()
  const [confirm, setConfirm] = useState<null | 'retry' | 'cancel'>(null)
  const [busy, setBusy] = useState(false)
  const history = historyResult || []
  async function action() {
    if (!confirm || !id) return
    setBusy(true)
    const r = await mutate('POST', `/workflow-jobs/${id}/${confirm}`, {}, confirm === 'retry' ? 'Job returned to the queue.' : 'Job cancelled.', crypto.randomUUID())
    setBusy(false)
    if (r.ok) { setConfirm(null); reload() }
  }
  const canRetry = job?.status === 'failed' && role !== 'viewer'
  const canCancel = !!job && ['queued', 'retry_scheduled'].includes(job.status) && role !== 'viewer'
  return <>
    <BackLink to="/jobs">Jobs</BackLink>
    <PageHeader title={`Job #${id}`} description={`${job?.label || job?.job_type || ''} · ${job?.job_type || ''}`} actions={<StatusBadge status={job?.status} />} />
    <DataState loading={loading} error={error} status={status} onRetry={reload}>
      <div className="card mb-5 p-4">
        <div className="grid gap-x-6 gap-y-2 sm:grid-cols-2">
          <div><div className="label">Attempts</div><p>{job ? `${job.attempts}/${job.max_attempts}` : '—'}</p></div>
          <div><div className="label">Available</div><p>{fmtDate(job?.available_at)}</p></div>
          <div><div className="label">Lease owner</div><p>{job?.lease_owner_token || '—'}</p></div>
          <div><div className="label">Lease expires</div><p>{fmtDate(job?.lease_expires_at)}</p></div>
          <div><div className="label">Created</div><p>{fmtDate(job?.created_at)}</p></div>
          <div><div className="label">Completed</div><p>{fmtDate(job?.completed_at)}</p></div>
        </div>
        {job?.last_error && <div className="mt-3"><div className="label">Last error</div><p className="text-sm text-red-600 dark:text-red-400">{job.last_error}</p></div>}
        <div className="mt-3 flex flex-wrap gap-2">
          {canRetry && <button className="btn" onClick={() => setConfirm('retry')}><RotateCcw size={14} /> Retry</button>}
          {canCancel && <button className="btn btn-danger" onClick={() => setConfirm('cancel')}><Square size={14} /> Cancel</button>}
        </div>
      </div>
      <section className="card mb-5 p-4">
        <h2 className="mb-1 font-semibold">What was requested</h2>
        <p className="muted mb-2 text-sm">Inputs used to run this job.</p>
        {job?.payload ? <JsonView label="Payload" value={job.payload} /> : <p className="muted">No request details available.</p>}
      </section>
      <section className="card mb-5 p-4">
        <h2 className="mb-1 font-semibold">Result</h2>
        {job?.result ? <JsonView label="Result" value={job.result} /> : <p className="muted">No result recorded.</p>}
      </section>
      <section className="card p-4">
        <div className="mb-3 border-b border-[hsl(var(--border))] pb-2"><h2 className="font-semibold">Attempt history</h2></div>
        <DataState loading={historyLoading} error={historyError} status={historyStatus} empty={!history.length}>
          <div className="table-wrap"><table><thead><tr><th>Attempt</th><th>Action</th><th>Status</th><th>Adapter</th><th>Started</th><th>Duration</th><th>Error</th></tr></thead><tbody>
            {history.map((h) => <tr key={h.id}>
              <td>{h.attempt}</td>
              <td className="font-medium">{h.action}</td>
              <td><StatusBadge status={h.status} /></td>
              <td>{h.adapter}</td>
              <td>{fmtDate(h.started_at)}</td>
              <td>{h.duration_ms == null ? '—' : `${h.duration_ms} ms`}</td>
              <td>{h.error_detail || '—'}</td>
            </tr>)}
          </tbody></table></div>
        </DataState>
      </section>
    </DataState>
    <Confirm open={confirm !== null} onOpenChange={(v) => !v && setConfirm(null)} title={confirm === 'retry' ? 'Retry this job?' : 'Cancel this job?'} description={confirm === 'retry' ? 'The job returns to the queue with a fresh attempt budget.' : 'This cancels queued work; a running lease cannot be cancelled.'} confirmLabel={confirm === 'cancel' ? 'Cancel job' : 'Retry'} destructive={confirm === 'cancel'} busy={busy} onConfirm={action} />
  </>
}
