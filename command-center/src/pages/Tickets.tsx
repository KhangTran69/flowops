import { useEffect, useMemo, useRef, useState } from 'react'
import { useDebounce } from 'use-debounce'
import { z } from 'zod'
import { Plus } from 'lucide-react'
import { usePoll } from '../hooks/usePoll'
import { mutate } from '../lib/api'
import { DataState } from '../components/DataState'
import { FilterSelect, PageHeader } from '../components/Basics'
import { RecordTable } from '../components/RecordTable'
import { StatusBadge } from '../components/StatusBadge'
import { Confirm } from '../components/Confirm'
import { useSession } from '../lib/session'
import type { Ticket } from '../lib/contracts'

const schema = z.object({ title: z.string().trim().min(3).max(500), description: z.string().trim().min(3).max(10000), requester_id: z.coerce.number().int().min(1).max(2147483647), requester_email: z.string().trim().email().max(254).optional() })
const emptyForm = { title: '', description: '', requester_id: '', requester_email: '' }

export default function Tickets() {
  const { role } = useSession()
  const [search, setSearch] = useState('')
  const [debounced] = useDebounce(search, 350)
  const [status, setStatus] = useState(''); const [priority, setPriority] = useState(''); const [queue, setQueue] = useState(''); const [category, setCategory] = useState('')
  const [page, setPage] = useState(0)
  const pageSize = 50
  const query = useMemo(() => ({ limit: pageSize + 1, offset: page * pageSize, search: debounced || undefined, status: status || undefined, priority: priority || undefined, queue: queue || undefined, category: category || undefined }), [debounced, status, priority, queue, category, page])
  useEffect(() => setPage(0), [debounced, status, priority, queue, category])
  const { data, error, status: http, loading, reload } = usePoll<Ticket[]>('/tickets', query)
  const [open, setOpen] = useState(false)
  const [form, setForm] = useState(emptyForm)
  const [busy, setBusy] = useState(false)
  const retry = useRef<{ payload: string; key: string } | null>(null)
  const records = data || []
  const rows = records.slice(0, pageSize)
  async function create() {
    const parsed = schema.safeParse({ ...form, requester_email: form.requester_email.trim() || undefined })
    if (!parsed.success) { alert(parsed.error.issues.map((i) => `${i.path.join('.')}: ${i.message}`).join('\n')); return }
    const payload = JSON.stringify(parsed.data)
    if (!retry.current || retry.current.payload !== payload) retry.current = { payload, key: crypto.randomUUID() }
    setBusy(true); const r = await mutate('POST', '/tickets/manual', parsed.data, 'Ticket created.', retry.current.key); setBusy(false)
    if (r.ok) { retry.current = null; setOpen(false); setForm(emptyForm); setPage(0); reload() }
  }
  return <>
    <PageHeader title="Tickets" description="Search, filter and triage incoming support work." actions={role !== 'viewer' && <button className="btn btn-primary" onClick={() => setOpen(true)}><Plus size={15} /> New ticket</button>} />
    <div className="card mb-4 grid gap-3 p-3 sm:grid-cols-2 lg:grid-cols-6">
      <label className="lg:col-span-2"><span className="label">Search</span><input className="input" value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Title or queue…" /></label>
      <FilterSelect label="Status" value={status} onChange={setStatus} options={['open', 'closed']} />
      <FilterSelect label="Priority" value={priority} onChange={setPriority} options={['low', 'normal', 'high', 'urgent']} />
      <label><span className="label">Queue</span><input className="input" value={queue} onChange={e => setQueue(e.target.value)} /></label>
      <label><span className="label">Category</span><input className="input" value={category} onChange={e => setCategory(e.target.value)} /></label>
    </div>
    <DataState loading={loading} error={error} status={http} onRetry={reload} empty={!rows.length}>
      <RecordTable rows={rows} rowHref={(r) => `/tickets/${r.ticket_id}`} columns={[
        { key: 'title', header: 'Ticket', render: (r) => <div className="font-medium">{r.title}</div> },
        { key: 'requester', header: 'Requester', render: (r) => r.requester_id },
        { key: 'priority', header: 'Priority', render: (r) => <StatusBadge status={r.priority} /> },
        { key: 'status', header: 'Status', render: (r) => <StatusBadge status={r.status} /> },
        { key: 'queue', header: 'Queue', render: (r) => r.queue || '—' },
      ]} />
    </DataState>
    <div className="mt-3 flex items-center justify-between"><button className="btn" disabled={!page || loading} onClick={() => setPage(p => p - 1)}>Previous</button><span className="muted">Page {page + 1}</span><button className="btn" disabled={records.length <= pageSize || loading} onClick={() => setPage(p => p + 1)}>Next</button></div>
    <Confirm open={open} onOpenChange={setOpen} title="Create ticket" description="This sends the ticket to the FlowOps API." confirmLabel="Create" busy={busy} onConfirm={create}>
      <div className="space-y-3"><label><span className="label">Title</span><input className="input" value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} /></label>
        <label><span className="label">Description</span><textarea className="input min-h-24" value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} /></label>
        <label><span className="label">Requester ID</span><input className="input" type="number" min={1} value={form.requester_id} onChange={(e) => setForm({ ...form, requester_id: e.target.value })} /></label><label><span className="label">Contact email (optional)</span><input className="input" type="email" value={form.requester_email} onChange={(e) => setForm({ ...form, requester_email: e.target.value })} /></label></div>
    </Confirm>
  </>
}
