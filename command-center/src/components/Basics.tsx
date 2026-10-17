import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { ArrowLeft, ChevronLeft } from 'lucide-react'
import { cn } from '../lib/utils'

export function PageHeader({ title, description, actions }: { title: string; description?: string; actions?: ReactNode }) {
  return <div className="mb-5 flex flex-wrap items-start justify-between gap-3">
    <div><h1 className="text-2xl font-semibold tracking-tight">{title}</h1>{description && <p className="muted mt-1">{description}</p>}</div>
    {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
  </div>
}
export function BackLink({ to, children }: { to: string; children: ReactNode }) {
  return <Link to={to} className="muted mb-3 inline-flex items-center gap-1 text-sm hover:underline"><ChevronLeft size={14} /> {children}</Link>
}
export function JsonView({ value, label }: { value: unknown; label?: string }) {
  return <div><div className="label">{label || 'JSON'}</div><pre className="max-h-80 overflow-auto rounded bg-black/5 p-3 text-xs dark:bg-white/5">{JSON.stringify(value, null, 2)}</pre></div>
}
export function ProgressBar({ value }: { value: number }) {
  return <div className="h-2 w-full overflow-hidden rounded bg-black/10 dark:bg-white/10" role="progressbar" aria-valuenow={Math.round(value)} aria-valuemin={0} aria-valuemax={100}><div className="h-full bg-[var(--primary)] transition-all" style={{ width: `${Math.max(0, Math.min(100, value))}%` }} /></div>
}
export function FilterSelect({ label, value, onChange, options }: { label: string; value: string; onChange: (v: string) => void; options: Array<string | { value: string; label: string }> }) {
  const opts = options.map((o) => typeof o === 'string' ? { value: o, label: o } : o)
  return <label className="text-xs"><span className="label">{label}</span><select className="input" value={value} onChange={(e) => onChange(e.target.value)}>
    <option value="">All</option>{opts.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
  </select></label>
}
export function ArrowLink({ to, children }: { to: string; children: ReactNode }) {
  return <Link className="inline-flex items-center gap-1 text-sm underline-offset-2 hover:underline" to={to}>{children} <ArrowLeft className="rotate-180" size={13} /></Link>
}
export function InlineError({ children }: { children: ReactNode }) {
  return <p className={cn('text-sm text-[var(--danger)]')}>{children}</p>
}
