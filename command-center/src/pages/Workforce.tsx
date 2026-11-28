import { useState, type FormEvent, type ReactNode } from 'react'
import { DataState } from '../components/DataState'
import { PageHeader, JsonView } from '../components/Basics'
import { RecordTable, type Column } from '../components/RecordTable'
import { usePoll } from '../hooks/usePoll'
import { useSession } from '../lib/session'
import { mutate, fmtDate } from '../lib/api'
import type { WorkforceDashboard, WorkforceEmployee, WorkforceRequest, WorkforceRequestDetail, WorkforceTask, WorkforceAsset, WorkforceAssignment, WorkforceEvent, WorkforceReminder, WorkforceAudit } from '../lib/contracts'

const sections = ['Overview', 'Employees', 'Requests', 'Approvals', 'IT tasks', 'Assets', 'Automation', 'Audit'] as const
type Section = typeof sections[number]
const fields = (e: FormEvent<HTMLFormElement>) => Object.fromEntries(new FormData(e.currentTarget))
function Field({ name, label, type = 'text', required = true, maxLength = 200 }: { name: string; label: string; type?: string; required?: boolean; maxLength?: number }) {
  return <label className="block">{label}<input name={name} className="input" type={type} required={required} maxLength={maxLength} min={type === 'number' ? 1 : undefined} /></label>
}
function Form({ title, children, onSubmit, button }: { title: string; children: ReactNode; onSubmit: (values: Record<string, FormDataEntryValue>) => Promise<boolean>; button: string }) {
  const [busy, setBusy] = useState(false)
  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault()
    if (busy) return
    const form = e.currentTarget
    const values = fields(e)
    setBusy(true)
    try { if (await onSubmit(values)) form.reset() } finally { setBusy(false) }
  }
  return <form className="card mb-4 space-y-3 p-4" onSubmit={submit}><h2 className="text-lg font-semibold">{title}</h2><div className="grid gap-3 sm:grid-cols-2">{children}</div><button className="btn" type="submit" disabled={busy}>{button}</button></form>
}
function List<T extends object>({ path, columns, query = {}, actions, before, rowKey }: { path: string; columns: Column<T>[]; query?: Record<string, unknown>; actions?: (row: T, reload: () => void) => ReactNode; before?: (reload: () => void) => ReactNode; rowKey?: (r: T) => string | number }) {
  const [page, setPage] = useState(0)
  const records = usePoll<T[]>(path, { ...query, offset: page * 49, limit: 50 })
  const rows = (records.data ?? []).slice(0, 49)
  const reload = () => { void records.reload() }
  return <>
    {before?.(reload)}
    <DataState loading={records.loading} error={records.error} status={records.status} onRetry={reload}>
      <RecordTable rows={rows} rowKey={rowKey} columns={actions ? [...columns, { key: 'actions', header: 'Actions', render: r => actions(r, reload) }] : columns} />
    </DataState>
    <nav className="mt-3 flex justify-between items-center" aria-label="Workforce record pages"><button className="btn" disabled={page === 0 || records.loading} onClick={() => setPage(p => p - 1)}>Previous</button><span>Page {page + 1}</span><button className="btn" disabled={records.loading || (records.data?.length ?? 0) < 50} onClick={() => setPage(p => p + 1)}>Next</button></nav>
  </>
}
async function change(path: string, body: unknown, reload: () => void) {
  const result = await mutate('POST', path, body, 'Workforce record updated.')
  if (result.ok) reload()
  return result.ok
}
function Action({ label, path, body = {}, reload }: { label: string; path: string; body?: unknown; reload: () => void }) {
  const [busy, setBusy] = useState(false)
  return <button className="btn" disabled={busy} onClick={async () => {
    if (busy || !window.confirm(label + '?')) return
    setBusy(true); try { await change(path, body, reload) } finally { setBusy(false) }
  }}>{label}</button>
}
function Overview() {
  const counts = usePoll<WorkforceDashboard>('/workforce/dashboard')
  return <DataState loading={counts.loading} error={counts.error} status={counts.status} onRetry={counts.reload}>
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">{Object.entries(counts.data ?? {}).map(([name, value]) => <div className="card p-4" key={name}><p className="muted capitalize">{name.replaceAll('_',' ')}</p><p className="text-2xl font-semibold">{value}</p></div>)}</div>
  </DataState>
}
function Employees() {
  return <List<WorkforceEmployee> path="/workforce/employees" columns={[
    {key:'id',header:'ID'}, {key:'employee_number',header:'Employee number'}, {key:'full_name',header:'Employee'}, {key:'work_email',header:'Work email'}, {key:'department',header:'Department'}, {key:'job_title',header:'Title'}, {key:'start_date',header:'Start date'}, {key:'manager_subject',header:'Manager'}, {key:'status',header:'Status'},
  ]} before={reload => <Form title="Add synthetic employee" button="Create employee" onSubmit={v => change('/workforce/employees', v, reload)}>
    <Field name="employee_number" label="Employee number" maxLength={64}/><Field name="full_name" label="Full name"/><Field name="work_email" label="Work email" type="email" maxLength={320}/><Field name="department" label="Department" maxLength={120}/><Field name="job_title" label="Job title" maxLength={160}/><Field name="start_date" label="Start date" type="date"/><Field name="manager_subject" label="Manager account subject" maxLength={256}/>
  </Form>}/>
}
function RequestDetail({ id, close }: { id: number; close: () => void }) {
  const detail = usePoll<WorkforceRequestDetail>('/workforce/onboarding-requests/' + id)
  return <section className="card p-4 space-y-3 mb-4"><h2 className="text-lg font-semibold">Request #{id}</h2><button className="btn" onClick={close}>Close details</button>
    <DataState loading={detail.loading} error={detail.error} status={detail.status} onRetry={detail.reload}>{detail.data && <>
      <p>{detail.data.employee_name} · {detail.data.status}</p>
      <p>Manager: {detail.data.manager_subject}</p><p>Rejection reason: {detail.data.rejection_reason ?? '—'}</p>
      <JsonView label="Approval" value={detail.data.approval}/>
      <RecordTable rows={detail.data.tasks} columns={[{key:'title',header:'Task'},{key:'status',header:'Status'},{key:'due_at',header:'Due',render:r=>fmtDate(r.due_at)},{key:'completion_notes',header:'Notes'}]}/>
    </>}</DataState>
  </section>
}
function Requests({ approvals }: { approvals: boolean }) {
  const [selected, setSelected] = useState<number | null>(null)
  return <>{selected !== null && <RequestDetail id={selected} close={() => setSelected(null)}/>}
    <List<WorkforceRequest> path="/workforce/onboarding-requests" query={approvals ? {status:'PENDING_APPROVAL'} : {}} columns={[
      {key:'id',header:'Request'}, {key:'employee_name',header:'Employee'}, {key:'department',header:'Department'}, {key:'manager_subject',header:'Manager'}, {key:'start_date',header:'Start date'}, {key:'status',header:'Status'},
    ]} before={!approvals ? reload => <Form title="Start onboarding" button="Create draft" onSubmit={v => change('/workforce/onboarding-requests',{employee_id:Number(v.employee_id)},reload)}><Field name="employee_id" label="Employee ID" type="number"/></Form> : undefined}
      actions={(r,reload) => <div className="flex flex-wrap gap-1"><button className="btn" onClick={() => setSelected(r.id)}>Details #{r.id}</button>
        {r.status === 'DRAFT' && <Action label="Submit for approval" path={`/workforce/onboarding-requests/${r.id}/submit`} reload={reload}/>}
        {r.status === 'PENDING_APPROVAL' && approvals && <><Action label="Approve" path={`/workforce/onboarding-requests/${r.id}/approve`} reload={reload}/><Form title="Reject request" button="Reject" onSubmit={v => change(`/workforce/onboarding-requests/${r.id}/reject`,{reason:String(v.reason)},reload)}><Field name="reason" label="Rejection reason" maxLength={2000}/></Form></>}
        {['DRAFT','PENDING_APPROVAL','APPROVED','IN_PROGRESS'].includes(r.status) && !approvals && <Action label="Cancel request" path={`/workforce/onboarding-requests/${r.id}/cancel`} reload={reload}/>}
        {r.status === 'REJECTED' && <Action label="Copy rejected request" path={`/workforce/onboarding-requests/${r.id}/copy-rejected`} reload={reload}/>}
      </div>}/>
  </>
}
function Tasks() {
  return <List<WorkforceTask> path="/workforce/tasks" columns={[
    {key:'id',header:'ID'},{key:'title',header:'Task'},{key:'request_id',header:'Request'},{key:'due_at',header:'Due',render:r=>fmtDate(r.due_at)},{key:'assignee_subject',header:'Assignee'},{key:'required',header:'Required',render:r=>r.required?'Yes':'No'},{key:'status',header:'Status'},{key:'completion_notes',header:'Notes'},
  ]}/>
}
function AssetHistory({ id, close }: {id:number;close:()=>void}) {
  const history = usePoll<WorkforceAssignment[]>(`/workforce/assets/${id}/history`)
  return <section className="card p-4 mb-4"><h2 className="text-lg font-semibold">Asset #{id} assignment history</h2><button className="btn" onClick={close}>Close history</button><DataState loading={history.loading} error={history.error} status={history.status} onRetry={history.reload}><RecordTable rows={history.data ?? []} columns={[{key:'employee_id',header:'Employee'},{key:'assigned_by',header:'Assigned by'},{key:'assigned_at',header:'Assigned',render:r=>fmtDate(r.assigned_at)},{key:'returned_at',header:'Returned',render:r=>fmtDate(r.returned_at)},{key:'return_notes',header:'Notes'}]}/></DataState></section>
}
function Assets() {
  const [selected,setSelected] = useState<number|null>(null)
  return <>{selected!==null && <AssetHistory id={selected} close={()=>setSelected(null)}/>}
  <List<WorkforceAsset> path="/workforce/assets" columns={[{key:'id',header:'ID'},{key:'asset_tag',header:'Asset tag'},{key:'serial_number',header:'Serial'},{key:'category',header:'Category'},{key:'brand_model',header:'Model'},{key:'condition',header:'Condition'},{key:'status',header:'Status'},{key:'employee_id',header:'Employee'}]}
    before={reload=><Form title="Add synthetic asset" button="Add asset" onSubmit={v=>change('/workforce/assets',{...v,serial_number:v.serial_number||null},reload)}><Field name="asset_tag" label="Asset tag" maxLength={64}/><Field name="serial_number" label="Serial number" required={false} maxLength={120}/><Field name="category" label="Category" maxLength={80}/><Field name="brand_model" label="Brand and model"/><label>Condition<select className="input" name="condition">{['GOOD','FAIR','POOR','DAMAGED'].map(v=><option key={v}>{v}</option>)}</select></label></Form>}
    actions={(r,reload)=><div className="space-y-2"><button className="btn" onClick={()=>setSelected(r.id)}>History #{r.id}</button>{r.status==='AVAILABLE' && <Form title="Assign asset" button="Assign" onSubmit={v=>change(`/workforce/assets/${r.id}/assign`,{employee_id:Number(v.employee_id)},reload)}><Field name="employee_id" label="Employee ID" type="number"/></Form>}{r.status==='ASSIGNED' && <Action label="Return asset" path={`/workforce/assets/${r.id}/return`} reload={reload}/>}</div>}/>
  </>
}
function Automation() {
  return <><h2 className="text-lg font-semibold mb-3">Automation events</h2><p className="muted mb-3">Delivery and callback completion are separate states. This local console only permits simulated provisioning.</p>
    <List<WorkforceEvent> path="/workforce/automation/events" rowKey={r=>r.event_id} columns={[{key:'event_id',header:'Event'},{key:'request_id',header:'Request'},{key:'delivery_status',header:'Delivery'},{key:'workflow_status',header:'Workflow'},{key:'attempts',header:'Attempts'},{key:'callback_overdue',header:'Callback overdue',render:r=>r.callback_overdue?'Yes':'No'},{key:'last_error',header:'Last error'}]} actions={(r,reload)=>r.delivery_status==='PENDING'&&<Action label="Simulate provisioning" path={`/workforce/automation/events/${r.event_id}/simulate`} reload={reload}/>}/>
    <h2 className="text-lg font-semibold mt-6 mb-3">Reminder history</h2><List<WorkforceReminder> path="/workforce/automation/reminders" rowKey={r=>r.event_id} columns={[{key:'event_id',header:'Event'},{key:'task_id',header:'Task'},{key:'outcome',header:'Outcome'},{key:'created_at',header:'Created',render:r=>fmtDate(r.created_at)},{key:'detail',header:'Detail'}]}/>
  </>
}
function Audit() {
  return <List<WorkforceAudit> path="/workforce/audit" columns={[{key:'created_at',header:'Time',render:r=>fmtDate(r.created_at)},{key:'actor_subject',header:'Actor'},{key:'action',header:'Action'},{key:'entity_type',header:'Entity'},{key:'entity_id',header:'ID'},{key:'detail',header:'Detail'}]}/>
}
function LocalWorkforce() {
  const [section,setSection]=useState<Section>('Overview')
  return <><PageHeader title="Workforce operations" description="Local synthetic development only. No production workforce browser access."/><nav aria-label="Workforce pages" className="flex flex-wrap gap-2 mb-5">{sections.map(s=><button key={s} className="btn" aria-current={s===section?'page':undefined} onClick={()=>setSection(s)}>{s}</button>)}</nav>
    <h2 className="text-lg font-semibold mb-3">{section}</h2>
    {section==='Overview'?<Overview/>:section==='Employees'?<Employees/>:section==='Requests'?<Requests key="requests" approvals={false}/>:section==='Approvals'?<Requests key="approvals" approvals/>:section==='IT tasks'?<Tasks/>:section==='Assets'?<Assets/>:section==='Automation'?<Automation/>:<Audit/>}
  </>
}
export default function Workforce() {
  const { workforceDemo } = useSession()
  return workforceDemo?<LocalWorkforce/>:<div className="card p-5" role="alert"><h1 className="text-xl font-semibold">Workforce browser access unavailable</h1><p>Production workforce identity and role validation must be completed before this console can be released.</p></div>
}
