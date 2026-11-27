import { useMemo, useState } from 'react'
import { usePoll } from '../hooks/usePoll'
import { fmtDate } from '../lib/api'
import { PageHeader } from '../components/Basics'
import { DataState } from '../components/DataState'
import { RecordTable } from '../components/RecordTable'
import { StatusBadge } from '../components/StatusBadge'

type Delivery = { id: number; ticket_id: string | null; status: string; attempts: number; created_at: string }

export default function Deliveries() {
  const [state, setState] = useState<'failed' | 'pending'>('failed')
  const [page, setPage] = useState(0)
  const query = useMemo(() => ({ state, limit: 51, offset: page * 50 }), [state, page])
  const { data, error, loading, status, reload } = usePoll<Delivery[]>('/command-center/deliveries', query)
  const rows = (data || []).slice(0, 50)
  return <>
    <PageHeader title="Notification deliveries" description="Inspect pending and failed notifications. Reconcile the destination before retrying any external action." />
    <div className="card mb-4 p-3"><label><span className="label">Delivery state</span><select className="input" value={state} onChange={e => { setState(e.target.value === 'pending' ? 'pending' : 'failed'); setPage(0) }}><option value="failed">Failed</option><option value="pending">Pending</option></select></label></div>
    <DataState loading={loading} error={error} status={status} onRetry={reload} empty={!rows.length}>
      <RecordTable rows={rows} columns={[
        { key: 'id', header: 'Delivery' },
        { key: 'ticket_id', header: 'Resource' },
        { key: 'status', header: 'State', render: r => <StatusBadge status={r.status} /> },
        { key: 'attempts', header: 'Attempts' },
        { key: 'created_at', header: 'Created', render: r => fmtDate(r.created_at) },
      ]}/>
    </DataState>
    <nav aria-label="Delivery pages" className="mt-3 flex items-center justify-between"><button className="btn" disabled={loading || !page} onClick={() => setPage(p => p - 1)}>Previous</button><span>Page {page + 1}</span><button className="btn" disabled={loading || (data || []).length <= 50} onClick={() => setPage(p => p + 1)}>Next</button></nav>
  </>
}
