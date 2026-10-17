import { useState } from 'react'
import { usePoll } from '../hooks/usePoll'
import { asList, fmtDate, progressOf, workflowOf } from '../lib/api'
import { DataState } from '../components/DataState'
import { FilterSelect, PageHeader, ProgressBar } from '../components/Basics'
import { RecordTable } from '../components/RecordTable'
import { StatusBadge } from '../components/StatusBadge'

export default function Runs() {
  const [status, setStatus] = useState('')
  const { data, error, status: http, loading, reload } = usePoll<any>('/workflow-runs', status ? { status } : undefined)
  const rows = asList<any>(data).filter((r) => !status || String(r.status) === status)
  return <>
    <PageHeader title="Workflow runs" description="DAG execution state, progress, and operator controls." />
    <div className="card mb-4 p-3"><FilterSelect label="Status" value={status} onChange={setStatus} options={['running', 'paused', 'completed', 'failed', 'cancelled']} /></div>
    <DataState loading={loading} error={error} status={http} onRetry={reload} empty={!rows.length}>
      <RecordTable rows={rows} rowHref={(r) => `/runs/${r.id}`} columns={[
        { key: 'id', header: 'Run', render: (r) => `#${r.id}` },
        { key: 'workflow', header: 'Workflow', render: (r) => workflowOf(r) },
        { key: 'request', header: 'Request', render: (r) => r.request_id ?? r.onboarding_request_id ?? '—' },
        { key: 'employee', header: 'Employee', render: (r) => r.employee_name || '—' },
        { key: 'status', header: 'Status', render: (r) => <StatusBadge status={r.status} /> },
        { key: 'started', header: 'Started', render: (r) => fmtDate(r.started_at || r.created_at) },
        { key: 'progress', header: 'Progress', render: (r) => <div className="w-32"><ProgressBar value={progressOf(r)} /><span className="muted text-xs">{Math.round(progressOf(r))}%</span></div> },
      ]} />
    </DataState>
  </>
}
