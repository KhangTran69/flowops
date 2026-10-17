import { cn } from '../lib/utils'

const green = new Set(['completed', 'succeeded', 'published', 'approved', 'ok', 'healthy', 'ready', 'ready'])
const blue = new Set(['open', 'running', 'in_progress', 'active', 'queued'])
const amber = new Set(['paused', 'pending', 'draft', 'high', 'waiting', 'scheduled', 'retrying', 'received'])
const red = new Set(['failed', 'rejected', 'overdue', 'urgent', 'cancelled', 'dead_letter', 'error'])
const gray = new Set(['closed', 'low', 'skipped', 'archived', 'normal'])

export function StatusBadge({ status, className }: { status: unknown; className?: string }) {
  const raw = String(status ?? 'unknown')
  const s = raw.toLowerCase()
  const tone = green.has(s) ? 'bg-green-100 text-green-800 dark:bg-green-950 dark:text-green-300'
    : blue.has(s) ? 'bg-blue-100 text-blue-800 dark:bg-blue-950 dark:text-blue-300'
    : amber.has(s) ? 'bg-amber-100 text-amber-900 dark:bg-amber-950 dark:text-amber-300'
    : red.has(s) ? 'bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-300'
    : gray.has(s) ? 'bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300'
    : 'bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300'
  return <span className={cn('inline-flex items-center rounded px-2 py-0.5 text-xs font-medium', tone, className)}>{raw}</span>
}
