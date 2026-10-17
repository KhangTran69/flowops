import { useMemo, useState } from 'react'
import { useDebounce } from 'use-debounce'
import { usePoll } from '../hooks/usePoll'
import { asList, fmtDate, txt } from '../lib/api'
import { DataState } from '../components/DataState'
import { PageHeader } from '../components/Basics'
import { StatusBadge } from '../components/StatusBadge'

export default function Audit() {
  const [actor, setActor] = useState(''); const [action, setAction] = useState(''); const [ticket, setTicket] = useState(''); const [from, setFrom] = useState(''); const [to, setTo] = useState('')
  const [dActor] = useDebounce(actor, 350); const [dAction] = useDebounce(action, 350); const [dTicket] = useDebounce(ticket, 350)
  const query = useMemo(() => ({ actor: dActor || undefined, action: dAction || undefined, ticket_id: dTicket || undefined, from: from || undefined, to: to || undefined }), [dActor, dAction, dTicket, from, to])
  const { data, error, status, loading, reload } = usePoll<any>('/audit', query)
  const rows = asList<any>(data)
  const [expanded, setExpanded] = useState<number | string | null>(null)
  return <>
    <PageHeader title="Audit" description="Actor-attributed execution history. Read-only." />
    <div className="card mb-4 grid gap-3 p-3 sm:grid-cols-2 lg:grid-cols-5">
      <label><span className="label">Actor</span><input className="input" value={actor} onChange={(e) => setActor(e.target.value)} placeholder="email or subject" /></label>
      <label><span className="label">Action</span><input className="input" value={action} onChange={(e) => setAction(e.target.value)} placeholder="onboarding.item…" /></label>
      <label><span className="label">Ticket / request</span><input className="input" value={ticket} onChange={(e) => setTicket(e.target.value)} /></label>
      <label><span className="label">From</span><input type="date" className="input" value={from} onChange={(e) => setFrom(e.target.value)} /></label>
      <label><span className="label">To</span><input type="date" className="input" value={to} onChange={(e) => setTo(e.target.value)} /></label>
    </div>
    <DataState loading={loading} error={error} status={status} onRetry={reload} empty={!rows.length}>
      <div className="card overflow-hidden"><div className="table-wrap"><table><thead><tr><th>Time</th><th>Actor</th><th>Action</th><th>Status</th><th>Ticket/request</th><th>Detail</th></tr></thead><tbody>
        {rows.map((r, i) => { const key = r.id ?? i; const open = expanded === key; return <><tr key={`${key}-r`} className="cursor-pointer hover:bg-black/5 dark:hover:bg-white/5" onClick={() => setExpanded(open ? null : key)}><td>{fmtDate(r.created_at || r.timestamp)}</td><td>{r.actor_subject || r.actor || 'system'}</td><td className="mono text-xs">{r.action}</td><td><StatusBadge status={r.status} /></td><td>{r.ticket_id ?? r.request_id ?? '—'}</td><td className="max-w-md truncate">{txt(r.detail)}</td></tr>{open && <tr key={`${key}-d`}><td colSpan={6} className="bg-black/5 dark:bg-white/5"><pre className="max-h-72 overflow-auto text-xs">{JSON.stringify(r, null, 2)}</pre></td></tr>}</> })}
      </tbody></table></div></div>
    </DataState>
  </>
}
