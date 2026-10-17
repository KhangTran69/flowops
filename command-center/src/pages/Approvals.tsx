import { useState } from 'react'
import { Link } from 'react-router-dom'
import { Check, X } from 'lucide-react'
import { usePoll } from '../hooks/usePoll'
import { asList, collect, fmtDate, isDone, isOverdue, mutate, requestOf } from '../lib/api'
import { useSession } from '../lib/session'
import { DataState } from '../components/DataState'
import { PageHeader } from '../components/Basics'
import { RecordTable } from '../components/RecordTable'
import { StatusBadge } from '../components/StatusBadge'
import { Confirm } from '../components/Confirm'

const gated = (s: any) => String(s.status || '').toLowerCase() === 'awaiting_approval' || (String(s.status || '').toLowerCase() === 'waiting' && Boolean(s.requires_approval)) || (String(s.status || '').toLowerCase() === 'pending' && Boolean(s.requires_approval))

export default function Approvals() {
  const { data, error, status, loading, reload } = usePoll<any>('/workflow-runs', undefined, 10000)
  const { role } = useSession()
  const items = collect(asList<any>(data), gated)
  const [target, setTarget] = useState<null | { run: any; step: any; action: 'approve' | 'reject' }>(null)
  const [reason, setReason] = useState(''); const [busy, setBusy] = useState(false)
  async function submit() {
    if (!target || reason.trim().length < 3) return
    setBusy(true); const r = await mutate('POST', `/workflow-runs/${target.run.id}/steps/${target.step.step_key || target.step.key || target.step.id}/${target.action}`, { reason: reason.trim() }, `Step ${target.action}d.`); setBusy(false)
    if (r.ok) { setTarget(null); setReason(''); reload() }
  }
  return <>
    <PageHeader title="Approvals" description="Gated workflow steps. Nothing is auto-approved." />
    <DataState loading={loading} error={error} status={status} onRetry={reload} empty={!items.length}>
      <RecordTable rows={items} rowKey={({ run, step }) => `${run.id}-${step.id ?? step.step_key}`} columns={[
        { key: 'step', header: 'Step', render: ({ step }) => <div><div className="font-medium">{step.step || step.title || step.step_key}</div><div className="muted text-xs">run link: <Link className="underline" to={`/runs/${step.run_id ?? ''}`}>open run</Link></div></div> },
        { key: 'run', header: 'Run', render: ({ run }) => <Link className="underline" to={`/runs/${run.id}`}>#{run.id}</Link> },
        { key: 'request', header: 'Request', render: ({ run }) => requestOf(run) },
        { key: 'queue', header: 'Queue', render: ({ step }) => step.queue || '—' },
        { key: 'sla', header: 'SLA', render: ({ step }) => isOverdue(step) ? <StatusBadge status="overdue" /> : fmtDate(step.sla_due_at || step.due_at) },
        { key: 'blockers', header: 'Blockers', render: ({ step }) => (step.dependencies || step.depends_on || []).filter((d: any) => !isDone(d?.status)).length || '—' },
        { key: 'actions', header: 'Actions', render: ({ run, step }) => role === 'admin' ? <div className="flex gap-1"><button className="btn" onClick={(e) => { e.stopPropagation(); setTarget({ run, step, action: 'approve' }) }}><Check size={13} /> Approve</button><button className="btn btn-danger" onClick={(e) => { e.stopPropagation(); setTarget({ run, step, action: 'reject' }) }}><X size={13} /> Reject</button></div> : <span className="muted text-xs">admin only</span> },
      ]} />
    </DataState>
    <Confirm open={!!target} onOpenChange={(v) => !v && setTarget(null)} title={`${target?.action === 'reject' ? 'Reject' : 'Approve'} step`} description="A reason of at least 3 characters is required and will be audited." destructive={target?.action === 'reject'} busy={busy} confirmDisabled={reason.trim().length < 3} onConfirm={submit}>
      <label><span className="label">Reason</span><textarea className="input min-h-20" value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Why are you approving or rejecting this step?" /></label>
    </Confirm>
  </>
}
