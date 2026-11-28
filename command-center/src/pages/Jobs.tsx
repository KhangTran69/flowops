import { useMemo, useState } from 'react'
import { usePoll } from '../hooks/usePoll'
import { asList, fmtDate } from '../lib/api'
import { DataState } from '../components/DataState'
import { FilterSelect, PageHeader } from '../components/Basics'
import { RecordTable } from '../components/RecordTable'
import { StatusBadge } from '../components/StatusBadge'
import type { WorkflowJob } from '../lib/contracts'

export default function Jobs() {
  const [status, setStatus] = useState('')
  const [page, setPage] = useState(0)
  const query = useMemo(() => ({ status: status || undefined, limit: 51, offset: page * 50 }), [status, page])
  const { data, error, status: http, loading, reload } = usePoll<WorkflowJob[]>('/workflow-jobs', query)
  const rows = asList<WorkflowJob>(data).slice(0, 50)
  return <>
    <PageHeader title="Workflow jobs" description="Durable, leased queue entries processed by the worker." />
    <div className="card mb-4 p-3"><FilterSelect label="Status" value={status} onChange={value => { setStatus(value); setPage(0) }} options={['queued', 'running', 'succeeded', 'retry_scheduled', 'failed', 'cancelled']} /></div>
    <DataState loading={loading} error={error} status={http} onRetry={reload} empty={!rows.length}>
      <RecordTable rows={rows} rowHref={(r) => `/jobs/${r.id}`} columns={[
        { key: 'id', header: 'Job', render: (r) => `#${r.id}` },
        { key: 'label', header: 'Label', render: (r) => r.label || r.job_type },
        { key: 'type', header: 'Type', render: (r) => r.job_type },
        { key: 'status', header: 'Status', render: (r) => <StatusBadge status={r.status} /> },
        { key: 'attempts', header: 'Attempts', render: (r) => `${r.attempts}/${r.max_attempts}` },
        { key: 'available', header: 'Available', render: (r) => fmtDate(r.available_at) },
        { key: 'updated', header: 'Updated', render: (r) => fmtDate(r.updated_at) },
      ]} />
    </DataState>
    <nav aria-label="Workflow job pages" className="mt-3 flex items-center justify-between"><button className="btn" disabled={page === 0 || loading} onClick={() => setPage(p => p - 1)}>Previous</button><span>Page {page + 1}</span><button className="btn" disabled={loading || asList(data).length <= 50} onClick={() => setPage(p => p + 1)}>Next</button></nav>
  </>
}
