import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Link, NavLink, useLocation } from 'react-router-dom'
import { Activity, BookOpen, ClipboardList, FileText, GitBranch, LayoutDashboard, Menu, Moon, ShieldCheck, Sun, Ticket, Workflow, X } from 'lucide-react'
import { useSession } from '../lib/session'
import { usePoll } from '../hooks/usePoll'
import { cn } from '../lib/utils'

import type { Readiness } from '../lib/contracts'

const nav = [
  { to: '/', label: 'Dashboard', icon: LayoutDashboard },
  { to: '/tickets', label: 'Tickets', icon: Ticket },
  { to: '/requests', label: 'Requests', icon: ClipboardList },
  { to: '/runs', label: 'Workflow runs', icon: Workflow },
  { to: '/approvals', label: 'Approvals', icon: ShieldCheck },
  { to: '/definitions', label: 'Definitions', icon: GitBranch },
  { to: '/audit', label: 'Audit', icon: FileText },
  { to: '/deliveries', label: 'Deliveries', icon: Activity },
]

export function Layout({ children }: { children: ReactNode }) {
  const { role, email, configured, loading: sessionLoading } = useSession()
  const [mobile, setMobile] = useState(false)
  const menuTrigger = useRef<HTMLButtonElement>(null)
  const closeMenu = useRef<HTMLButtonElement>(null)
  const [dark, setDark] = useState(() => localStorage.getItem('flowops-theme') === 'dark')
  const location = useLocation()
  const ready = usePoll<Readiness>('/ready', undefined, 15000)
  useEffect(() => { document.documentElement.classList.toggle('dark', dark); localStorage.setItem('flowops-theme', dark ? 'dark' : 'light') }, [dark])
  useEffect(() => { setMobile(false) }, [location.pathname])
  useEffect(() => {
    if (!mobile) return
    closeMenu.current?.focus()
    const close = (e: KeyboardEvent) => { if (e.key === 'Escape') { setMobile(false); menuTrigger.current?.focus() } }
    window.addEventListener('keydown', close)
    return () => window.removeEventListener('keydown', close)
  }, [mobile])
  const healthLabel = ready.loading && !ready.updatedAt ? 'checking backend' : ready.error ? (ready.updatedAt ? 'backend stale' : 'backend unavailable') : ready.data?.status === 'ready' && ready.status === 200 ? 'backend ready' : 'backend unavailable'
  const healthy = healthLabel === 'backend ready'
  if (sessionLoading) return <div role="status" className="grid min-h-screen place-items-center muted">Verifying console access…</div>
  if (configured === false) return <div className="grid min-h-screen place-items-center p-6"><div className="card max-w-lg p-6"><h1 className="text-xl font-semibold">Console access unavailable</h1><p className="muted mt-2">Sign in through the configured authentication gateway, or ask your administrator to verify the console configuration.</p></div></div>
  return <div className="min-h-screen md:flex">
    <a href="#main-content" className="sr-only focus:not-sr-only focus:absolute focus:z-50 focus:bg-white focus:p-3 focus:text-black">Skip to main content</a>
    <aside className={cn('fixed inset-y-0 left-0 z-40 w-64 bg-slate-950 p-3 text-slate-200 transition-transform md:static md:translate-x-0', mobile ? 'visible translate-x-0' : 'invisible -translate-x-full md:visible')}>
      <div className="mb-6 flex items-center justify-between px-2 py-2">
        <Link to="/" className="flex items-center gap-2 text-lg font-semibold"><span className="grid h-7 w-7 place-items-center rounded bg-[var(--primary)] text-white">F</span> FlowOps</Link>
        <button ref={closeMenu} className="md:hidden" onClick={() => { setMobile(false); menuTrigger.current?.focus() }} aria-label="Close menu"><X size={18} /></button>
      </div>
      <nav id="main-navigation" className="space-y-1" aria-label="Main">
        {nav.map(({ to, label, icon: Icon }) => <NavLink key={to} to={to} end={to === '/'} className={({ isActive }) => cn('flex items-center gap-2 rounded px-3 py-2 text-sm text-slate-300 hover:bg-white/10', isActive && 'bg-white/10 text-white')}><Icon size={16} /> {label}</NavLink>)}
      </nav>
      <div className="absolute bottom-3 left-3 right-3 rounded bg-white/5 p-3 text-xs text-slate-400"><div className="mb-1 flex items-center gap-1"><Activity size={13} /> {healthLabel}</div><div className="truncate">{email}</div><div className="capitalize">{role}</div></div>
    </aside>
    {mobile && <div className="fixed inset-0 z-30 bg-black/40 md:hidden" onClick={() => setMobile(false)} />}
    <div className="min-w-0 flex-1">
      <header className="sticky top-0 z-20 flex items-center justify-between gap-3 border-b border-[hsl(var(--border))] bg-[hsl(var(--surface))]/95 px-4 py-3 backdrop-blur md:px-6">
        <button ref={menuTrigger} className="btn md:hidden" onClick={() => setMobile(true)} aria-label="Open menu" aria-expanded={mobile} aria-controls="main-navigation"><Menu size={16} /></button>
        <div className="flex items-center gap-2 text-sm"><span className="badge rounded bg-black/5 px-2 py-0.5 capitalize dark:bg-white/10">{role}</span><span className="muted hidden truncate sm:inline">{email}</span></div>
        <button className="btn" onClick={() => setDark(!dark)} aria-label="Toggle theme">{dark ? <Sun size={15} /> : <Moon size={15} />}</button>
      </header>
      {!healthy && !ready.loading && <div className="border-b border-red-300 bg-red-50 px-4 py-2 text-sm text-red-800 dark:border-red-900 dark:bg-red-950 dark:text-red-200">Backend readiness check is failing. Data may be stale. <Link className="underline" to="/audit">Open audit</Link></div>}
      <main id="main-content" className="mx-auto max-w-[1500px] p-4 md:p-6">{children}</main>
    </div>
  </div>
}
