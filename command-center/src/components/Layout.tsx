import { useEffect, useState, type ReactNode } from 'react'
import { Link, NavLink, useLocation } from 'react-router-dom'
import { Activity, BookOpen, ClipboardList, FileText, GitBranch, LayoutDashboard, Menu, Moon, ShieldCheck, Sun, Ticket, Workflow, X } from 'lucide-react'
import { useSession } from '../lib/session'
import { usePoll } from '../hooks/usePoll'
import { cn } from '../lib/utils'

const nav = [
  { to: '/', label: 'Dashboard', icon: LayoutDashboard },
  { to: '/tickets', label: 'Tickets', icon: Ticket },
  { to: '/requests', label: 'Requests', icon: ClipboardList },
  { to: '/runs', label: 'Workflow runs', icon: Workflow },
  { to: '/approvals', label: 'Approvals', icon: ShieldCheck },
  { to: '/definitions', label: 'Definitions', icon: GitBranch },
  { to: '/audit', label: 'Audit', icon: FileText },
]

export function Layout({ children }: { children: ReactNode }) {
  const { role, email, configured } = useSession()
  const [mobile, setMobile] = useState(false)
  const [dark, setDark] = useState(() => localStorage.getItem('flowops-theme') === 'dark')
  const location = useLocation()
  const ready = usePoll<any>('/ready', undefined, 15000)
  useEffect(() => { document.documentElement.classList.toggle('dark', dark); localStorage.setItem('flowops-theme', dark ? 'dark' : 'light') }, [dark])
  useEffect(() => { setMobile(false) }, [location.pathname])
  const healthy = ready.loading || ready.error ? ready.error ? false : true : ['ok', 'ready', 'healthy'].includes(String((ready.data as any)?.status ?? 'ok').toLowerCase()) || (ready.status >= 200 && ready.status < 400)
  if (configured === false) return <div className="grid min-h-screen place-items-center p-6"><div className="card max-w-lg p-6"><h1 className="text-xl font-semibold">API not configured</h1><p className="muted mt-2">Set <code>FLOWOPS_API_URL</code> and <code>FLOWOPS_API_TOKEN</code> on the server, then restart the Command Center proxy.</p></div></div>
  return <div className="min-h-screen md:flex">
    <aside className={cn('fixed inset-y-0 left-0 z-40 w-64 bg-slate-950 p-3 text-slate-200 transition-transform md:static md:translate-x-0', mobile ? 'translate-x-0' : '-translate-x-full')}>
      <div className="mb-6 flex items-center justify-between px-2 py-2">
        <Link to="/" className="flex items-center gap-2 text-lg font-semibold"><span className="grid h-7 w-7 place-items-center rounded bg-[var(--primary)] text-white">F</span> FlowOps</Link>
        <button className="md:hidden" onClick={() => setMobile(false)} aria-label="Close menu"><X size={18} /></button>
      </div>
      <nav className="space-y-1" aria-label="Main">
        {nav.map(({ to, label, icon: Icon }) => <NavLink key={to} to={to} end={to === '/'} className={({ isActive }) => cn('flex items-center gap-2 rounded px-3 py-2 text-sm text-slate-300 hover:bg-white/10', isActive && 'bg-white/10 text-white')}><Icon size={16} /> {label}</NavLink>)}
      </nav>
      <div className="absolute bottom-3 left-3 right-3 rounded bg-white/5 p-3 text-xs text-slate-400"><div className="mb-1 flex items-center gap-1"><Activity size={13} /> {healthy ? 'backend ready' : 'backend unhealthy'}</div><div className="truncate">{email}</div><div className="capitalize">{role}</div></div>
    </aside>
    {mobile && <div className="fixed inset-0 z-30 bg-black/40 md:hidden" onClick={() => setMobile(false)} />}
    <div className="min-w-0 flex-1">
      <header className="sticky top-0 z-20 flex items-center justify-between gap-3 border-b border-[hsl(var(--border))] bg-[hsl(var(--surface))]/95 px-4 py-3 backdrop-blur md:px-6">
        <button className="btn md:hidden" onClick={() => setMobile(true)} aria-label="Open menu"><Menu size={16} /></button>
        <div className="flex items-center gap-2 text-sm"><span className="badge rounded bg-black/5 px-2 py-0.5 capitalize dark:bg-white/10">{role}</span><span className="muted hidden truncate sm:inline">{email}</span></div>
        <button className="btn" onClick={() => setDark(!dark)} aria-label="Toggle theme">{dark ? <Sun size={15} /> : <Moon size={15} />}</button>
      </header>
      {!healthy && <div className="border-b border-red-300 bg-red-50 px-4 py-2 text-sm text-red-800 dark:border-red-900 dark:bg-red-950 dark:text-red-200">Backend readiness check is failing. Data may be stale. <Link className="underline" to="/audit">Open audit</Link></div>}
      <main className="mx-auto max-w-[1500px] p-4 md:p-6">{children}</main>
    </div>
  </div>
}
