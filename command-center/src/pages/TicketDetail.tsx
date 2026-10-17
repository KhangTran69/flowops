import { useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { Loader2, Sparkles } from 'lucide-react'
import { usePoll } from '../hooks/usePoll'
import { api, fmtDate, mutate, titleOf, txt } from '../lib/api'
import { DataState } from '../components/DataState'
import { BackLink, JsonView, PageHeader } from '../components/Basics'
import { StatusBadge } from '../components/StatusBadge'
import { Confirm } from '../components/Confirm'

export default function TicketDetail() {
  const { id } = useParams()
  const { data: ticket, error, status, loading, reload } = usePoll<any>(`/tickets/${id}`)
  const [triage, setTriage] = useState<any>(null)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const recommendations = triage?.recommendations || ticket?.ai_triage?.recommendations || []
  const eligible = useMemo(() => recommendations.filter((r: any) =>
    ['queue', 'priority'].includes(String(r.kind || '').toLowerCase()) && r.applicable !== false &&
    String(r.suggested ?? '') !== String(r.current ?? ''),), [recommendations])
  useEffect(() => { setSelected(new Set(eligible.map((r: any) => r.id))) }, [triage])
  async function runTriage() {
    setBusy(true); const r = await api('POST', `/tickets/${id}/ai-triage`)
    setBusy(false)
    if (r.ok) { setTriage(r.data); reload() } else alert(r.error || 'Triage failed.')
  }
  async function apply() {
    const ids = [...selected]
    if (!ids.length) return
    const fields = eligible.filter((r: any) => ids.includes(r.id)).map((r: any) => ({ id: r.id, field: r.kind, current: r.current, suggested: r.suggested }))
    setBusy(true); const r = await mutate('POST', `/tickets/${id}/ai-recommendations/apply`, { recommendation_ids: ids, fields }, 'AI recommendations applied.')
    setBusy(false); if (r.ok) { setConfirmOpen(false); setTriage(null); setSelected(new Set()); reload() }
  }
  const history = ticket?.history || ticket?.events || ticket?.execution_logs || []
  return <>
    <BackLink to="/tickets">Tickets</BackLink>
    <PageHeader title={titleOf(ticket)} description={`Ticket #${id} · ${ticket?.requester_email || ticket?.requester_id || 'unknown requester'}`} actions={<StatusBadge status={ticket?.status || 'open'} />} />
    <DataState loading={loading} error={error} status={status} onRetry={reload}>
      <div className="grid gap-5 xl:grid-cols-3">
        <section className="card p-4 xl:col-span-2">
          <h2 className="mb-3 font-semibold">Summary</h2>
          <dl className="grid gap-3 sm:grid-cols-2">
            {[['Priority', ticket?.priority], ['Status', ticket?.status], ['Queue', ticket?.queue], ['Category', ticket?.category], ['Created', fmtDate(ticket?.created_at)], ['Updated', fmtDate(ticket?.updated_at)]].map(([k, v]) => <div key={String(k)}><dt className="label">{k}</dt><dd>{k === 'Priority' || k === 'Status' ? <StatusBadge status={v} /> : (v || '—')}</dd></div>)}
          </dl>
          <div className="mt-5"><div className="label">Description</div><p className="whitespace-pre-wrap">{ticket?.description || 'No description.'}</p></div>
          {ticket?.resolution_note && <div className="mt-4"><div className="label">Resolution</div><p>{ticket.resolution_note}</p></div>}
        </section>
        <aside className="card p-4"><h2 className="mb-3 font-semibold">History</h2>{history.length ? <ol className="space-y-3">{history.map((h: any, i: number) => <li key={h.id ?? i} className="border-l-2 border-[var(--primary)] pl-3"><div className="text-sm font-medium">{h.action || h.event || h.status || 'event'}</div><div className="muted text-xs">{fmtDate(h.created_at || h.timestamp)} · {h.actor_subject || h.actor || 'system'}</div>{h.detail && <div className="muted mt-1 text-xs">{txt(h.detail)}</div>}</li>)}</ol> : <p className="muted">No history available.</p>}</aside>
      </div>

      <section className="card mt-5 p-4">
        <div className="mb-2 flex flex-wrap items-center justify-between gap-2"><h2 className="flex items-center gap-2 font-semibold"><Sparkles size={16} /> AI Triage</h2><button className="btn btn-primary" onClick={runTriage} disabled={busy}>{busy ? <Loader2 className="animate-spin" size={14} /> : <Sparkles size={14} />} Run / Re-run triage</button></div>
        <p className="muted mb-3 text-sm">AI can only change queue/priority after you confirm; AI never changes status.</p>
        {!triage && <p className="muted">Run triage to get a summary, knowledge-base guidance, and recommendations.</p>}
        {triage && <>
          <p className="mb-3 whitespace-pre-wrap">{triage.summary?.text || txt(triage.summary)}</p>
          {(triage.summary?.urgency_signals || []).length > 0 && <div className="mb-4 flex flex-wrap gap-1"><span className="muted text-xs uppercase">Urgency:</span>{(triage.summary.urgency_signals || []).map((s: string) => <StatusBadge key={s} status={s} />)}</div>}
          <h3 className="mb-2 font-medium">Knowledge base matches</h3>
          <div className="mb-4 grid gap-3 md:grid-cols-2">{[...(triage.knowledge_base || [])].sort((a: any, b: any) => (b.score || 0) - (a.score || 0)).map((m: any) => <article key={m.article?.id || m.id} className="rounded border border-[hsl(var(--border))] p-3"><div className="font-medium">{m.article?.title || m.title}</div><p className="muted mt-1 text-sm">{m.article?.summary || m.summary}</p><div className="muted mt-1 text-xs">score {m.score ?? '—'} · {m.article?.category || m.category || '—'}</div></article>)}</div>
          <h3 className="mb-2 font-medium">Recommendations</h3>
          {recommendations.length ? <div className="table-wrap"><table><thead><tr><th>Approve</th><th>Recommendation</th><th>Current</th><th>Suggested</th><th>Why</th><th>Confidence</th></tr></thead><tbody>
            {recommendations.map((r: any) => { const can = eligible.some((e: any) => e.id === r.id); return <tr key={r.id}><td>{can ? <input type="checkbox" checked={selected.has(r.id)} onChange={(e) => { const n = new Set(selected); e.target.checked ? n.add(r.id) : n.delete(r.id); setSelected(n) }} aria-label={`Approve ${r.title || r.id}`} /> : <span className="muted text-xs">info only</span>}</td><td>{r.title || r.id}</td><td>{r.current ?? '—'}</td><td>{r.suggested ?? '—'}</td><td className="muted text-sm">{r.rationale || '—'}</td><td>{r.confidence != null ? `${r.confidence}%` : '—'}</td></tr> })}
          </tbody></table></div> : <p className="muted">No recommendations.</p>}
          <div className="mt-3 flex flex-wrap items-center gap-2">
            {eligible.length ? <><button className="btn btn-primary" onClick={() => setConfirmOpen(true)} disabled={!selected.size}>Apply selected ({selected.size})</button><span className="muted text-xs">Applying changes only queue/priority. Status is never changed by AI.</span></> : <><button className="btn" disabled>No queue/priority changes</button><span className="muted text-xs">The ticket already matches routing; knowledge-base guidance is informational only.</span></>}
          </div>
        </>}
      </section>
    </DataState>
    <Confirm open={confirmOpen} onOpenChange={setConfirmOpen} title="Apply AI recommendations" description="Review each change before applying." busy={busy} confirmLabel="Apply changes" onConfirm={apply}>
      <ul className="space-y-2">{eligible.filter((r: any) => selected.has(r.id)).map((r: any) => <li key={r.id} className="rounded border border-[hsl(var(--border))] p-2 text-sm"><span className="font-medium">{r.kind}</span>: {r.current ?? '—'} → <span className="font-medium">{r.suggested ?? '—'}</span></li>)}</ul>
    </Confirm>
  </>
}
