import { useState, type FormEvent } from 'react'
import { useParams } from 'react-router-dom'
import { DataState } from '../components/DataState'
import { BackLink, JsonView, PageHeader } from '../components/Basics'
import { RecordTable } from '../components/RecordTable'
import { usePoll } from '../hooks/usePoll'
import { useSession } from '../lib/session'
import { api, fmtDate, mutate } from '../lib/api'
import { jsonObject, scheduleLabel } from './Triggers'
import type { Trigger, TriggerDelivery, TriggerPreview } from '../lib/contracts'

function TriggerEditor({ trigger, reload }: { trigger: Trigger; reload: () => void }) {
  const [name, setName] = useState(trigger.name)
  const [mapping, setMapping] = useState(JSON.stringify(trigger.mapping_json, null, 2))
  const [event, setEvent] = useState('{}')
  const [result, setResult] = useState<TriggerPreview | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const path = '/triggers/' + encodeURIComponent(trigger.key)
  async function save(e: FormEvent) {
    e.preventDefault()
    if (busy) return
    setError('')
    let parsed: Record<string, unknown>
    try { parsed = jsonObject(mapping) } catch { setError('Mapping must be a valid JSON object.'); return }
    setBusy(true)
    try { const r = await mutate('PUT', path, { name, mapping_json: parsed }, 'Trigger saved.'); if (r.ok) reload(); else setError(r.error || 'Save failed.') } finally { setBusy(false) }
  }
  async function toggle() {
    if (busy || !window.confirm(trigger.active ? 'Deactivate this trigger?' : 'Activate this trigger? New matching events may start work.')) return
    setBusy(true); setError('')
    try { const r = await mutate('PUT', path, { active: !trigger.active }, 'Trigger status updated.'); if (r.ok) reload(); else setError(r.error || 'Update failed.') } finally { setBusy(false) }
  }
  async function preview(e: FormEvent) {
    e.preventDefault()
    if (busy) return
    setResult(null); setError('')
    let parsed: Record<string, unknown>
    try { parsed = jsonObject(event) } catch { setError('Test event must be a valid JSON object.'); return }
    setBusy(true)
    try { const r = await api<TriggerPreview>('POST', path + '/test', { body: parsed }); if (r.ok && r.data) setResult(r.data); else setError(r.error || 'Preview failed.') } finally { setBusy(false) }
  }
  return <div className="space-y-4">
    <form className="card p-4 space-y-3" onSubmit={save}>
      <h2 className="text-lg font-semibold">Configuration</h2>
      <p>Source: {trigger.source_key} · {trigger.event_type} · {trigger.target_type}</p>
      <p>Schedule: {scheduleLabel(trigger)} · {trigger.active ? 'Active' : 'Inactive'} · Last fired: {fmtDate(trigger.last_fired_at)}</p>
      <label className="block">Name<input className="input" value={name} onChange={e => setName(e.target.value)} required maxLength={200} /></label>
      <label className="block">Mapping JSON<textarea className="input font-mono" rows={6} value={mapping} onChange={e => setMapping(e.target.value)} /></label>
      <button className="btn" type="submit" disabled={busy}>Save mapping</button>{' '}
      <button className="btn" type="button" disabled={busy} onClick={toggle}>{trigger.active ? 'Deactivate trigger' : 'Activate trigger'}</button>
    </form>
    <form className="card p-4 space-y-3" onSubmit={preview}>
      <h2 className="text-lg font-semibold">Test saved mapping</h2>
      <p className="muted text-sm">Preview only: this endpoint does not execute the trigger or send external requests. Use synthetic event data.</p>
      <label className="block">Test event JSON<textarea className="input font-mono" rows={5} value={event} onChange={e => setEvent(e.target.value)} /></label>
      <button className="btn" type="submit" disabled={busy || !trigger.active}>Preview mapping</button>
      {result && <div role="status"><p>Mapping preview complete.</p><JsonView label="Mapped payload" value={result.mapped_payload} /></div>}
    </form>
    {error && <p role="alert" className="card p-3">{error}</p>}
  </div>
}
function Detail({ id }: { id: string }) {
  const path = '/triggers/' + encodeURIComponent(id)
  const trigger = usePoll<Trigger>(path)
  const deliveries = usePoll<TriggerDelivery[]>(path + '/deliveries')
  return <><BackLink to="/triggers">Event triggers</BackLink><PageHeader title={trigger.data?.name ?? 'Trigger details'} />
    <DataState loading={trigger.loading} error={trigger.error} status={trigger.status} onRetry={trigger.reload}>
      {trigger.data && <TriggerEditor key={trigger.data.key} trigger={trigger.data} reload={() => { void trigger.reload(); void deliveries.reload() }} />}
    </DataState>
    <h2 className="mt-6 mb-3 text-lg font-semibold">Recent deliveries</h2>
    <DataState loading={deliveries.loading} error={deliveries.error} status={deliveries.status} onRetry={deliveries.reload}>
      <RecordTable rows={deliveries.data ?? []} columns={[
        { key: 'id', header: 'ID' }, { key: 'event_id', header: 'Event ID' }, { key: 'status', header: 'Status' },
        { key: 'request_id', header: 'Request' }, { key: 'received_at', header: 'Received', render: r => fmtDate(r.received_at) },
      ]} />
    </DataState>
  </>
}
export default function TriggerDetail() {
  const { key = '' } = useParams()
  const { role } = useSession()
  return role === 'admin' ? <Detail key={key} id={key} /> : <div role="alert" className="card p-4">Global administrator access required.</div>
}
