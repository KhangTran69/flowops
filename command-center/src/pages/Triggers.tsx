import { useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { DataState } from '../components/DataState'
import { PageHeader } from '../components/Basics'
import { RecordTable } from '../components/RecordTable'
import { StatusBadge } from '../components/StatusBadge'
import { usePoll } from '../hooks/usePoll'
import { useSession } from '../lib/session'
import { fmtDate, mutate } from '../lib/api'
import type { EventSource, Trigger } from '../lib/contracts'

export function scheduleLabel(t: Trigger) {
  return t.schedule_kind === 'interval' ? `Every ${t.interval_seconds}s` : t.schedule_kind === 'daily' ? `Daily at ${t.daily_at_utc} UTC` : 'Webhook'
}
export function jsonObject(raw: string): Record<string, unknown> {
  const data: unknown = JSON.parse(raw)
  if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Error('Enter a JSON object.')
  return data as Record<string, unknown>
}
function TriggerList() {
  const triggers = usePoll<Trigger[]>('/triggers')
  const sources = usePoll<EventSource[]>('/event-sources')
  const navigate = useNavigate()
  const [create, setCreate] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [kind, setKind] = useState('webhook')
  const [target, setTarget] = useState('ticket')
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (busy) return
    setError('')
    const fields = new FormData(event.currentTarget)
    const value = (key: string) => String(fields.get(key) ?? '').trim()
    let mapping: Record<string, unknown>
    try { mapping = jsonObject(value('mapping')) } catch { setError('Mapping must be a valid JSON object.'); return }
    setBusy(true)
    try {
      const result = await mutate('POST', '/triggers', {
        key: value('key'), name: value('name'), source_key: value('source'), event_type: value('event'),
        target_type: target, definition_key: target === 'ticket' ? null : value('definition'), mapping_json: mapping,
        schedule_kind: kind, interval_seconds: kind === 'interval' ? Number(value('interval')) : null,
        daily_at_utc: kind === 'daily' ? value('daily') : null,
      }, 'Trigger created.')
      if (result.ok) navigate('/triggers/' + encodeURIComponent(value('key')))
      else setError(result.error || 'Unable to create trigger.')
    } finally { setBusy(false) }
  }
  return <>
    <PageHeader title="Event triggers" description="Manage event mappings and scheduled workflow starts." actions={<button className="btn" onClick={() => setCreate(v => !v)} aria-expanded={create}>{create ? 'Close create form' : 'New trigger'}</button>} />
    {create && <form className="card mb-5 space-y-3 p-4" onSubmit={submit}>
      <h2 className="text-lg font-semibold">Create trigger</h2>
      <p className="muted text-sm">New triggers are active immediately. Use an approved source and verify the mapping before enabling external event delivery.</p>
      <div className="grid gap-3 sm:grid-cols-2">
        <label>Key<input name="key" className="input" required maxLength={80} pattern="[A-Za-z0-9_.:-]+" /></label>
        <label>Name<input name="name" className="input" required maxLength={200} /></label>
        <label>Event source<select name="source" className="input" required defaultValue=""><option value="">Select source</option>{(sources.data ?? []).filter(s => s.active).map(s => <option key={s.key} value={s.key}>{s.name} ({s.key})</option>)}</select></label>
        <label>Event type<input name="event" className="input" required maxLength={64} /></label>
        <label>Target<select className="input" value={target} onChange={e => setTarget(e.target.value)}><option value="ticket">Ticket</option><option value="onboarding_request">Onboarding</option><option value="offboarding_request">Offboarding</option></select></label>
        {target !== 'ticket' && <label>Published definition key<input name="definition" className="input" required maxLength={80} /></label>}
        <label>Schedule<select className="input" value={kind} onChange={e => setKind(e.target.value)}><option value="webhook">Webhook</option><option value="interval">Interval</option><option value="daily">Daily (UTC)</option></select></label>
        {kind === 'interval' && <label>Interval seconds<input name="interval" type="number" min={1} max={2147483647} className="input" required /></label>}
        {kind === 'daily' && <label>Daily time (UTC)<input name="daily" type="time" className="input" required /></label>}
      </div>
      <label className="block">Mapping JSON<textarea name="mapping" className="input font-mono" rows={5} defaultValue="{}" required /></label>
      {error && <p role="alert">{error}</p>}
      <button className="btn" disabled={busy || !!sources.error || sources.loading} type="submit">{busy ? 'Creating…' : 'Create active trigger'}</button>
    </form>}
    <DataState loading={triggers.loading} error={triggers.error} status={triggers.status} onRetry={triggers.reload}>
      <RecordTable rows={triggers.data ?? []} rowKey={r => r.key} rowHref={r => '/triggers/' + encodeURIComponent(r.key)} columns={[
        { key: 'key', header: 'Key' }, { key: 'name', header: 'Name' }, { key: 'source_key', header: 'Source' },
        { key: 'event_type', header: 'Event type' }, { key: 'target_type', header: 'Target' },
        { key: 'schedule', header: 'Schedule', render: scheduleLabel },
        { key: 'active', header: 'Status', render: r => <StatusBadge status={r.active ? 'active' : 'inactive'} /> },
        { key: 'last_fired_at', header: 'Last fired', render: r => fmtDate(r.last_fired_at) },
      ]} />
    </DataState>
    <h2 className="mt-6 mb-3 text-lg font-semibold">Event sources</h2>
    <DataState loading={sources.loading} error={sources.error} status={sources.status} onRetry={sources.reload}>
      <RecordTable rows={sources.data ?? []} rowKey={r => r.key} columns={[
        { key: 'key', header: 'Key' }, { key: 'name', header: 'Name' },
        { key: 'scope', header: 'Scope', render: r => r.global_scope ? 'Global' : r.tenant_key ?? 'Unconfigured' },
        { key: 'active', header: 'Status', render: r => r.active ? 'Active' : 'Inactive' },
        { key: 'secret', header: 'Webhook key configured', render: r => r.secret_generated ? 'Yes' : 'No' },
      ]} />
    </DataState>
    <p className="muted mt-3 text-sm">Source credential creation and rotation stay in the protected backend administration workflow.</p>
  </>
}
export default function Triggers() {
  const { role } = useSession()
  return role === 'admin' ? <TriggerList /> : <div role="alert" className="card p-4">Global administrator access required.</div>
}
