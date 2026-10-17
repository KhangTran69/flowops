import { useState } from 'react'
import { usePoll } from '../hooks/usePoll'
import { asList, mutate } from '../lib/api'
import { useSession } from '../lib/session'
import { DataState } from '../components/DataState'
import { PageHeader } from '../components/Basics'
import { RecordTable } from '../components/RecordTable'
import { StatusBadge } from '../components/StatusBadge'
import { Confirm } from '../components/Confirm'

const template = `{
  "key": "my-workflow",
  "name": "My workflow",
  "request_type": "onboarding",
  "steps": [
    {
      "key": "create_user",
      "step": "Create user account",
      "queue": "it-access-management",
      "requires_approval": true,
      "action": "none",
      "depends_on": [],
      "join": "all",
      "when": [],
      "sla_minutes": 60
    }
  ]
}`

export default function Definitions() {
  const { data, error, status, loading, reload } = usePoll<any>('/workflow-definitions')
  const { role } = useSession()
  const [open, setOpen] = useState(false); const [json, setJson] = useState(template); const [busy, setBusy] = useState(false)
  const rows = asList<any>(data)
  async function create() {
    let payload: any
    try { payload = JSON.parse(json) } catch { alert('Invalid JSON.'); return }
    if (!payload.key || !payload.name || !payload.request_type || !Array.isArray(payload.steps) || !payload.steps.every((s: any) => s.key)) { alert('key, name, request_type and steps[].key are required.'); return }
    setBusy(true); const r = await mutate('POST', '/workflow-definitions', payload, 'Definition draft created.'); setBusy(false)
    if (r.ok) { setOpen(false); reload() }
  }
  return <>
    <PageHeader title="Workflow definitions" description="Immutable versioned DAG definitions." actions={role === 'admin' ? <button className="btn btn-primary" onClick={() => setOpen(true)}>Create draft</button> : undefined} />
    <DataState loading={loading} error={error} status={status} onRetry={reload} empty={!rows.length}>
      <RecordTable rows={rows} rowHref={(r) => `/definitions/${r.key}`} columns={[
        { key: 'key', header: 'Key', render: (r) => <span className="mono">{r.key}</span> },
        { key: 'name', header: 'Name', render: (r) => r.name },
        { key: 'request_type', header: 'Request type', render: (r) => <StatusBadge status={r.request_type} /> },
        { key: 'status', header: 'Status', render: (r) => <StatusBadge status={r.status} /> },
        { key: 'current_version', header: 'Current version', render: (r) => r.current_version ?? '—' },
        { key: 'versions', header: 'Versions', render: (r) => (r.versions || []).length },
      ]} />
    </DataState>
    <Confirm open={open} onOpenChange={setOpen} title="Create workflow definition" description="Draft version 1 is created. Publish it separately." confirmLabel="Create draft" busy={busy} onConfirm={create}>
      <label><span className="label">Definition JSON</span><textarea className="input min-h-[420px] font-mono text-xs" value={json} onChange={(e) => setJson(e.target.value)} spellCheck={false} /></label>
    </Confirm>
  </>
}
