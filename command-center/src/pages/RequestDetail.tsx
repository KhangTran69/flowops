import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { Play } from 'lucide-react'
import { usePoll } from '../hooks/usePoll'
import { fmtDate, mutate, progressOf } from '../lib/api'
import { DataState } from '../components/DataState'
import { BackLink, PageHeader, ProgressBar } from '../components/Basics'
import { StatusBadge } from '../components/StatusBadge'
import { Confirm } from '../components/Confirm'

export default function RequestDetail() {
  const { id } = useParams()
  const request = usePoll<any>(`/onboarding-requests/${id}`)
  const run = usePoll<any>(`/onboarding-requests/${id}/run`)
  const [open, setOpen] = useState(false); const [busy, setBusy] = useState(false)
  const noRun = run.status === 404 || (!run.loading && !run.data && run.error)
  async function start() {
    setBusy(true); const r = await mutate('POST', `/onboarding-requests/${id}/run`, {}, 'Workflow run started.'); setBusy(false)
    if (r.ok) { setOpen(false); run.reload() }
  }
  return <>
    <BackLink to="/requests">Requests</BackLink>
    <PageHeader title={request.data?.employee_name || `Request #${id}`} description={`${request.data?.request_type || 'workflow'} · ${request.data?.department || ''}`} actions={<StatusBadge status={request.data?.status} />} />
    <DataState loading={request.loading} error={request.error} status={request.status} onRetry={request.reload}>
      <div className="grid gap-5 lg:grid-cols-3">
        <section className="card p-4 lg:col-span-2"><h2 className="mb-3 font-semibold">Request</h2><dl className="grid gap-3 sm:grid-cols-2">
          {[['Employee', request.data?.employee_name], ['Email', request.data?.employee_email], ['Department', request.data?.department], ['Type', request.data?.request_type], ['Effective date', request.data?.start_date], ['Created', fmtDate(request.data?.created_at)]].map(([k, v]) => <div key={String(k)}><dt className="label">{k}</dt><dd>{v || '—'}</dd></div>)}
        </dl></section>
        <section className="card p-4"><h2 className="mb-3 font-semibold">Workflow run</h2>
          {noRun ? <><p className="muted mb-3">No run has been started for this request.</p><button className="btn btn-primary" onClick={() => setOpen(true)}><Play size={14} /> Start run</button></> :
            <DataState loading={run.loading} error={run.error} status={run.status} onRetry={run.reload}>
              {run.data && <><div className="mb-2 flex items-center justify-between"><Link className="font-medium underline" to={`/runs/${run.data.id}`}>Run #{run.data.id}</Link><StatusBadge status={run.data.status} /></div><ProgressBar value={progressOf(run.data)} /><div className="muted mt-2 text-sm">{Math.round(progressOf(run.data))}% complete</div><Link className="btn mt-3" to={`/runs/${run.data.id}`}>Open run</Link></>}
            </DataState>}
        </section>
      </div>
    </DataState>
    <Confirm open={open} onOpenChange={setOpen} title="Start workflow run?" description="The DAG scheduler will run ready steps and stop at approval gates." confirmLabel="Start run" busy={busy} onConfirm={start} />
  </>
}
