import { useState, type FormEvent } from 'react'
import { DataState } from '../components/DataState'
import { BackLink, PageHeader } from '../components/Basics'
import { usePoll } from '../hooks/usePoll'
import type { KnowledgeArticle } from '../lib/contracts'

export default function KnowledgeBase() {
  const [query, setQuery] = useState('')
  const [category, setCategory] = useState('')
  const [filters, setFilters] = useState({ query: '', category: '' })
  const articles = usePoll<KnowledgeArticle[]>('/knowledge-base', { ...filters, limit: 50 }, 60000)
  function search(e: FormEvent) { e.preventDefault(); setFilters({ query: query.trim(), category }) }
  return <>
    <BackLink to="/tickets">Tickets</BackLink>
    <PageHeader title="Knowledge base" description="Search troubleshooting guidance and recommended response steps." />
    <form onSubmit={search} className="card mb-4 flex flex-wrap items-end gap-3 p-4">
      <label className="flex-1">Search articles<input className="input" value={query} onChange={e => setQuery(e.target.value)} maxLength={200} placeholder="VPN, laptop, password reset…" /></label>
      <label>Category<select className="input" value={category} onChange={e => setCategory(e.target.value)}>
        <option value="">All categories</option>{['access','email','hardware','network','password'].map(c => <option key={c} value={c}>{c}</option>)}
      </select></label>
      <button className="btn" type="submit">Search</button>
    </form>
    <DataState loading={articles.loading} error={articles.error} status={articles.status} onRetry={articles.reload}>
      <p role="status" className="muted mb-3">{articles.data?.length ?? 0} matching articles</p>
      <div className="grid gap-3 lg:grid-cols-2">{(articles.data ?? []).map(a => <article className="card p-4 space-y-3" key={a.id}>
        <p className="muted text-sm">{a.category} · {a.id}</p><h2 className="text-lg font-semibold">{a.title}</h2><p>{a.summary}</p>
        <h3 className="font-semibold">Recommended steps</h3><ol className="list-decimal pl-5 space-y-1">{a.steps.map((s,i) => <li key={i}>{s}</li>)}</ol>
        <p className="muted text-sm">Keywords: {a.keywords.join(', ')}</p>
      </article>)}</div>
    </DataState>
  </>
}
