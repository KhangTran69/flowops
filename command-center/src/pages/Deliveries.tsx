import { useMemo, useState } from 'react'
import { usePoll } from '../hooks/usePoll'
import { fmtDate, mutate } from '../lib/api'
import { useSession } from '../lib/session'
import { PageHeader } from '../components/Basics'
import { DataState } from '../components/DataState'
import { RecordTable } from '../components/RecordTable'
import { StatusBadge } from '../components/StatusBadge'
import { Confirm } from '../components/Confirm'
import type { NotificationDelivery } from '../lib/contracts'

export default function Deliveries() {
  const [state, setState] = useState<'failed' | 'pending'>('failed')
  const [page, setPage] = useState(0)
  const [confirm, setConfirm] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)
  const { role } = useSession()
  const path = state === 'pending' ? '/notifications/pending' : '/notifications/failed'
  const query = useMemo(() => ({ limit: 51, offset: page * 50 }), [page])
  const { data, error, loading, status, reload } = usePoll<NotificationDelivery[]>(path, query)
  const rows = (data || []).slice(0, 50)
  async function requeue() {
    if (confirm === null) return
    setBusy(true)
    const r = await mutate('POST', `/notifications/${confirm}/requeue`, {}, 'Delivery returned to the pending queue.', crypto.randomUUID())
    setBusy(false)
    if (r.ok) { setConfirm(null); reload() }
  }
  async function retryPending() {
    await mutate('POST', '/notifications/retry', {}, 'Pending deliveries retried once each.', crypto.randomUUID())
    reload()
  }
  return <>
    <PageHeader title="Notification deliveries" description="Inspect pending and failed notifications. Reconcile the destination before retrying any external action." actions={state === 'pending' && role !== 'viewer' && <button className="btn" onClick={retryPending}>Retry pending</button>} />
    <div className="card mb-4 p-3"><label><span className="label">Delivery state</span><select className="input" value={state} onChange={e => { setState(e.target.value === 'pending' ? 'pending' : 'failed'); setPage(0) }}><option value="failed">Failed</option><option value="pending">Pending</option></select></label></div>
    <DataState loading={loading} error={error} status={status} onRetry={reload} empty={!rows.length}>
      <RecordTable rows={rows} columns={[
        { key: 'id', header: 'Delivery', render: r => `#${r.id}` },
        { key: 'ticket_id', header: 'Resource', render: r => r.ticket_id || '—' },
        { key: 'url', header: 'Destination', render: r => r.url },
        { key: 'status', header: 'State', render: r => <StatusBadge status={r.status} /> },
        { key: 'attempts', header: 'Attempts' },
        { key: 'last_error', header: 'Last error', render: r => r.last_error || '—' },
        { key: 'created_at', header: 'Created', render: r => fmtDate(r.created_at) },
        ...(state === 'failed' ? [{ key: 'action', header: 'Action', render: (r: NotificationDelivery) => role !== 'viewer' ? <button className="btn" onClick={() => setConfirm(r.id)}>Requeue</button> : <span className="muted text-xs">admin only</span> }] : []),
      ]}/>
    </DataState>
    <nav aria-label="Delivery pages" className="mt-3 flex items-center justify-between"><button className="btn" disabled={loading || !page} onClick={() => setPage(p => p - 1)}>Previous</button><span>Page {page + 1}</span><button className="btn" disabled={loading || (data || []).length <= 50} onClick={() => setPage(p => p + 1)}>Next</button></nav>
    <Confirm open={confirm !== null} onOpenChange={(v) => !v && setConfirm(null)} title="Requeue this delivery?" description="The dead-letter delivery returns to the pending queue; attempts and last error are preserved and one more delivery attempt is granted." confirmLabel="Requeue" destructive={false} busy={busy} onConfirm={requeue} />
  </>
}
