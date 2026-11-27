import { useMemo, useState } from 'react'
import { usePoll } from '../hooks/usePoll'
import { asList, fmtDate, progressOf, workflowOf } from '../lib/api'
import { DataState } from '../components/DataState'
import { FilterSelect, PageHeader, ProgressBar } from '../components/Basics'
import { RecordTable } from '../components/RecordTable'
import { StatusBadge } from '../components/StatusBadge'
import type { WorkflowRun } from '../lib/contracts'

export default function Runs() {
  const [status, setStatus] = useState('')
  const [definition, setDefinition] = useState('')
  const [page, setPage] = useState(0)
  const query = useMemo(() => ({ status: status || undefined, definition: definition.trim() || undefined, limit: 51, offset: page * 50 }), [status, definition, page])
  const { data, error, status: http, loading, reload } = usePoll<WorkflowRun[]>('/workflow-runs', query)
  const rows = (data || []).slice(0, 50)
  return <>
    <PageHeader title="Workflow runs" description="DAG execution state, progress, and operator controls." />
    <div className="card mb-4 p-3"><FilterSelect label="Status" value={status} onChange={value => { setStatus(value); setPage(0) }} options={['pending', 'running', 'paused', 'completed', 'failed', 'cancelled']} /><label><span className="label">Workflow definition key</span><input className="input" value={definition} onChange={e => { setDefinition(e.target.value); setPage(0) }} /></label></div>
    <DataState loading={loading} error={error} status={http} onRetry={reload} empty={!rows.length}>
      <RecordTable rows={rows} rowHref={(r) => `/runs/${r.id}`} columns={[
        { key: 'id', header: 'Run', render: (r) => `#${r.id}` },
        { key: 'workflow', header: 'Workflow', render: (r) => workflowOf(r) },
        { key: 'request', header: 'Request', render: (r) => r.request_id },
        { key: 'employee', header: 'Employee', render: (r) => r.employee_name || '—' },
        { key: 'status', header: 'Status', render: (r) => <StatusBadge status={r.status} /> },
        { key: 'started', header: 'Started', render: (r) => fmtDate(r.started_at || r.created_at) },
        { key: 'progress', header: 'Successful steps', render: (r) => <div className="w-32"><ProgressBar value={progressOf(r)} /><span className="muted text-xs">{Math.round(progressOf(r))}%</span></div> },
      ]} />
    </DataState>
    <nav aria-label="Workflow history pages" className="mt-3 flex items-center justify-between"><button className="btn" disabled={page === 0 || loading} onClick={() => setPage(p => p - 1)}>Previous</button><span>Page {page + 1}</span><button className="btn" disabled={loading || (data || []).length <= 50} onClick={() => setPage(p => p + 1)}>Next</button></nav>
  </>
}
