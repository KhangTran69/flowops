import { useMemo, useState } from 'react'
import { useDebounce } from 'use-debounce'
import { z } from 'zod'
import { Plus } from 'lucide-react'
import { usePoll } from '../hooks/usePoll'
import { asList, fmtDate, isDone, mutate, titleOf } from '../lib/api'
import { DataState } from '../components/DataState'
import { FilterSelect, PageHeader } from '../components/Basics'
import { RecordTable } from '../components/RecordTable'
import { StatusBadge } from '../components/StatusBadge'
import { Confirm } from '../components/Confirm'

const schema = z.object({ title: z.string().min(3), description: z.string().min(3), requester_email: z.string().email() })

export default function Tickets() {
  const [search, setSearch] = useState('')
  const [debounced] = useDebounce(search, 350)
  const [status, setStatus] = useState(''); const [priority, setPriority] = useState(''); const [queue, setQueue] = useState(''); const [category, setCategory] = useState('')
  const query = useMemo(() => ({ search: debounced || undefined, status: status || undefined, priority: priority || undefined, queue: queue || undefined, category: category || undefined }), [debounced, status, priority, queue, category])
  const { data, error, status: http, loading, reload } = usePoll<any>('/tickets', query)
  const [open, setOpen] = useState(false)
  const [form, setForm] = useState({ title: '', description: '', requester_email: '' })
  const [busy, setBusy] = useState(false)
  const rows = asList<any>(data).filter((t) => {
    const q = debounced.toLowerCase()
    return (!q || [t.title, t.subject, t.description, t.queue].some((v) => String(v || '').toLowerCase().includes(q)))
      && (!status || String(t.status) === status) && (!priority || String(t.priority) === priority)
      && (!queue || String(t.queue) === queue) && (!category || String(t.category) === category)
  })
  async function create() {
    const parsed = schema.safeParse(form)
    if (!parsed.success) { alert(parsed.error.issues.map((i) => `${i.path.join('.')}: ${i.message}`).join('\n')); return }
    setBusy(true); const r = await mutate('POST', '/tickets', parsed.data, 'Ticket created.'); setBusy(false)
    if (r.ok) { setOpen(false); setForm({ title: '', description: '', requester_email: '' }); reload() }
  }
  return <>
    <PageHeader title="Tickets" description="Search, filter and triage incoming support work." actions={<button className="btn btn-primary" onClick={() => setOpen(true)}><Plus size={15} /> New ticket</button>} />
    <div className="card mb-4 grid gap-3 p-3 sm:grid-cols-2 lg:grid-cols-6">
      <label className="lg:col-span-2"><span className="label">Search</span><input className="input" value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Title, description or queue…" /></label>
      <FilterSelect label="Status" value={status} onChange={setStatus} options={['open', 'pending', 'running', 'completed', 'closed', 'failed']} />
      <FilterSelect label="Priority" value={priority} onChange={setPriority} options={['low', 'normal', 'high', 'urgent']} />
      <FilterSelect label="Queue" value={queue} onChange={setQueue} options={[...new Set(asList<any>(data).map((t) => t.queue).filter(Boolean))]} />
      <FilterSelect label="Category" value={category} onChange={setCategory} options={[...new Set(asList<any>(data).map((t) => t.category).filter(Boolean))]} />
    </div>
    <DataState loading={loading} error={error} status={http} onRetry={reload} empty={!rows.length}>
      <RecordTable rows={rows} rowHref={(r) => `/tickets/${r.ticket_id ?? r.id}`} columns={[
        { key: 'title', header: 'Ticket', render: (r) => <div><div className="font-medium">{titleOf(r)}</div><div className="muted text-xs">{r.description?.slice(0, 90) || '—'}</div></div> },
        { key: 'requester', header: 'Requester', render: (r) => r.requester_email || r.requester_id || '—' },
        { key: 'priority', header: 'Priority', render: (r) => <StatusBadge status={r.priority} /> },
        { key: 'status', header: 'Status', render: (r) => <StatusBadge status={r.status || (r.completed ? 'closed' : 'open')} /> },
        { key: 'queue', header: 'Queue', render: (r) => r.queue || '—' },
        { key: 'created', header: 'Created', render: (r) => fmtDate(r.created_at) },
      ]} />
    </DataState>
    <Confirm open={open} onOpenChange={setOpen} title="Create ticket" description="This sends the ticket to the FlowOps API." confirmLabel="Create" busy={busy} onConfirm={create}>
      <div className="space-y-3"><label><span className="label">Title</span><input className="input" value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} /></label>
        <label><span className="label">Description</span><textarea className="input min-h-24" value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} /></label>
        <label><span className="label">Requester email</span><input className="input" value={form.requester_email} onChange={(e) => setForm({ ...form, requester_email: e.target.value })} /></label></div>
    </Confirm>
  </>
}
