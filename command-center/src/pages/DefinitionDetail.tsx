import { useEffect, useMemo, useState } from 'react'
import { useParams } from 'react-router-dom'
import { usePoll } from '../hooks/usePoll'
import { api, fmtDate, mutate } from '../lib/api'
import { useSession } from '../lib/session'
import { DataState } from '../components/DataState'
import { BackLink, JsonView, PageHeader } from '../components/Basics'
import { StatusBadge } from '../components/StatusBadge'
import { Confirm } from '../components/Confirm'

export default function DefinitionDetail() {
  const { key } = useParams()
  const { data, error, status, loading, reload } = usePoll<any>(`/workflow-definitions/${key}`)
  const { role } = useSession()
  const versions = data?.versions || []
  const [selected, setSelected] = useState<number | null>(null)
  const [editor, setEditor] = useState(''); const [open, setOpen] = useState(false); const [busy, setBusy] = useState(false)
  const [publishOpen, setPublishOpen] = useState(false)
  const [from, setFrom] = useState(''); const [to, setTo] = useState(''); const [diff, setDiff] = useState<any>(null); const [diffError, setDiffError] = useState('')
  useEffect(() => { if (versions.length && selected === null) { const v = versions.find((x: any) => x.version === data.current_version) || versions[versions.length - 1]; setSelected(v.version); setEditor(JSON.stringify(v.steps, null, 2)); setFrom(String(versions[0].version)); setTo(String(v.version)) } }, [data])
  const current = useMemo(() => versions.find((v: any) => v.version === selected), [versions, selected])
  async function newVersion() {
    let steps: any
    try { steps = JSON.parse(editor) } catch { alert('Invalid steps JSON.'); return }
    setBusy(true); const r = await mutate('PUT', `/workflow-definitions/${key}`, { steps }, 'New version created.'); setBusy(false)
    if (r.ok) { setOpen(false); reload() }
  }
  async function publish() { setBusy(true); const r = await mutate('POST', `/workflow-definitions/${key}/publish`, { version: selected }, 'Definition published.'); setBusy(false); if (r.ok) { setPublishOpen(false); reload() } }
  async function compare() {
    setDiffError(''); const r = await api('GET', `/workflow-definitions/${key}/diff`, { query: { from: Number(from), to: Number(to) } })
    if (r.ok) setDiff(r.data); else setDiffError(r.error || 'Diff failed.')
  }
  return <>
    <BackLink to="/definitions">Definitions</BackLink>
    <PageHeader title={data?.name || String(key)} description={`${data?.request_type || 'workflow'} · ${data?.status || ''}`} actions={<StatusBadge status={data?.status} />} />
    <DataState loading={loading} error={error} status={status} onRetry={reload}>
      <div className="grid gap-5 xl:grid-cols-4">
        <aside className="card p-3"><h2 className="mb-2 font-semibold">Versions</h2><div className="space-y-1">{versions.map((v: any) => <button key={v.version} className={`w-full rounded p-2 text-left text-sm ${selected === v.version ? 'bg-[var(--primary)]/10 font-medium' : 'hover:bg-black/5 dark:hover:bg-white/5'}`} onClick={() => { setSelected(v.version); setEditor(JSON.stringify(v.steps, null, 2)) }}><div className="flex justify-between"><span>v{v.version}</span><span className="muted text-xs">{v.published_at ? 'published' : 'draft'}</span></div><div className="muted mono text-[10px]">{String(v.checksum || '').slice(0, 16)}…</div><div className="muted text-xs">{fmtDate(v.created_at)}</div></button>)}</div>
          {role === 'admin' && current && <div className="mt-3 space-y-2"><button className="btn w-full" onClick={() => { setEditor(JSON.stringify(current.steps, null, 2)); setOpen(true) }}>New version</button>{data?.current_version !== selected && <button className="btn btn-primary w-full" onClick={() => setPublishOpen(true)}>Publish v{selected}</button>}</div>}
        </aside>
        <section className="card overflow-hidden xl:col-span-3"><div className="border-b border-[hsl(var(--border))] p-4"><h2 className="font-semibold">Version {current?.version ?? '—'}</h2></div>
          <div className="table-wrap"><table><thead><tr><th>Step</th><th>Queue</th><th>Depends on</th><th>Join</th><th>Predicates</th><th>SLA</th><th>Escalation</th></tr></thead><tbody>
            {(current?.steps || []).map((s: any) => <tr key={s.key}><td><div className="font-medium">{s.step}</div><div className="muted mono text-xs">{s.key}</div></td><td>{s.queue}</td><td>{(s.depends_on || []).join(', ') || '—'}</td><td>{s.join || 'all'}</td><td className="mono text-xs">{(s.when || []).join(' and ') || '—'}</td><td>{s.sla_minutes || '—'}</td><td>{s.escalation_queue || '—'}</td></tr>)}
          </tbody></table></div>
        </section>
      </div>
      <section className="card mt-5 p-4"><h2 className="mb-3 font-semibold">Compare versions</h2><div className="flex flex-wrap items-end gap-2"><label><span className="label">From</span><select className="input" value={from} onChange={(e) => setFrom(e.target.value)}>{versions.map((v: any) => <option key={v.version} value={v.version}>v{v.version}</option>)}</select></label><label><span className="label">To</span><select className="input" value={to} onChange={(e) => setTo(e.target.value)}>{versions.map((v: any) => <option key={v.version} value={v.version}>v{v.version}</option>)}</select></label><button className="btn" onClick={compare} disabled={!from || !to}>Compare</button></div>
        {diffError && <p className="mt-3 text-sm text-[var(--danger)]">{diffError}</p>}{diff && <div className="mt-3 grid gap-3 md:grid-cols-3"><JsonView label="Added" value={diff.added || []} /><JsonView label="Removed" value={diff.removed || []} /><JsonView label="Changed" value={diff.changed || []} /></div>}
      </section>
    </DataState>
    <Confirm open={open} onOpenChange={setOpen} title="Create new version" description="Published versions are immutable; this creates a new draft version." confirmLabel="Create version" busy={busy} onConfirm={newVersion}><label><span className="label">Steps JSON</span><textarea className="input min-h-[360px] font-mono text-xs" value={editor} onChange={(e) => setEditor(e.target.value)} spellCheck={false} /></label></Confirm>
    <Confirm open={publishOpen} onOpenChange={setPublishOpen} title={`Publish version ${selected}?`} description="New requests will pin this version. In-flight requests keep their pinned version." confirmLabel="Publish" busy={busy} onConfirm={publish} />
  </>
}
