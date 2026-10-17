import { useState } from 'react'
import { Plus } from 'lucide-react'
import { z } from 'zod'
import { usePoll } from '../hooks/usePoll'
import { asList, fmtDate, mutate } from '../lib/api'
import { DataState } from '../components/DataState'
import { FilterSelect, PageHeader } from '../components/Basics'
import { RecordTable } from '../components/RecordTable'
import { StatusBadge } from '../components/StatusBadge'
import { Confirm } from '../components/Confirm'

const schema = z.object({ request_type: z.enum(['onboarding', 'offboarding']), employee_name: z.string().min(2), employee_email: z.string().email(), department: z.string().min(2), start_date: z.string().min(1) })
export default function Requests() {
  const [status, setStatus] = useState('')
  const { data, error, status: http, loading, reload } = usePoll<any>('/onboarding-requests', status ? { status } : undefined)
  const rows = asList<any>(data).filter((r) => !status || String(r.status) === status)
  const [open, setOpen] = useState(false); const [busy, setBusy] = useState(false)
  const [form, setForm] = useState<any>({ request_type: 'onboarding', employee_name: '', employee_email: '', department: '', start_date: '' })
  async function create() {
    const parsed = schema.safeParse(form)
    if (!parsed.success) { alert(parsed.error.issues.map((i) => `${i.path.join('.')}: ${i.message}`).join('\n')); return }
    setBusy(true); const r = await mutate('POST', '/onboarding-requests', { ...parsed.data, requester_id: 1 }, 'Request created.'); setBusy(false)
    if (r.ok) { setOpen(false); setForm({ request_type: 'onboarding', employee_name: '', employee_email: '', department: '', start_date: '' }); reload() }
  }
  return <>
    <PageHeader title="Onboarding & offboarding requests" description="Workflow requests and their operator run state." actions={<button className="btn btn-primary" onClick={() => setOpen(true)}><Plus size={15} /> New request</button>} />
    <div className="card mb-4 p-3"><FilterSelect label="Status" value={status} onChange={setStatus} options={['in_progress', 'completed', 'cancelled', 'failed']} /></div>
    <DataState loading={loading} error={error} status={http} onRetry={reload} empty={!rows.length}>
      <RecordTable rows={rows} rowHref={(r) => `/requests/${r.id}`} columns={[
        { key: 'employee', header: 'Employee', render: (r) => <div><div className="font-medium">{r.employee_name}</div><div className="muted text-xs">{r.employee_email}</div></div> },
        { key: 'type', header: 'Type', render: (r) => <StatusBadge status={r.request_type} /> },
        { key: 'department', header: 'Department', render: (r) => r.department || '—' },
        { key: 'status', header: 'Status', render: (r) => <StatusBadge status={r.status} /> },
        { key: 'created', header: 'Created', render: (r) => fmtDate(r.created_at) },
      ]} />
    </DataState>
    <Confirm open={open} onOpenChange={setOpen} title="Create workflow request" confirmLabel="Create" busy={busy} onConfirm={create}>
      <div className="space-y-3">
        <label><span className="label">Type</span><select className="input" value={form.request_type} onChange={(e) => setForm({ ...form, request_type: e.target.value })}><option value="onboarding">Onboarding</option><option value="offboarding">Offboarding</option></select></label>
        {[['employee_name', 'Employee name'], ['employee_email', 'Employee email'], ['department', 'Department'], ['start_date', 'Effective date (YYYY-MM-DD)']].map(([k, label]) => <label key={k}><span className="label">{label}</span><input className="input" value={form[k]} onChange={(e) => setForm({ ...form, [k]: e.target.value })} /></label>)}
      </div>
    </Confirm>
  </>
}
