import type { ReactNode } from 'react'
import { LockKeyhole, RefreshCw } from 'lucide-react'
import { cn } from '../lib/utils'

export function DataState({ loading, error, status, empty, onRetry, children, className }: {
  loading?: boolean; error?: string | null; status?: number; empty?: boolean;
  onRetry?: () => void; children?: ReactNode; className?: string
}) {
  if (loading) return (
    <div className={cn('space-y-3', className)} aria-busy="true">
      {[0, 1, 2].map((i) => <div key={i} className="h-16 animate-pulse rounded bg-black/5 dark:bg-white/10" />)}
    </div>
  )
  if (error) {
    const auth = status === 401 || status === 403
    return (
      <div className={cn('card p-5', className)} role="alert">
        <div className="mb-2 flex items-center gap-2 font-medium">
          {auth && <LockKeyhole size={16} />} {auth ? 'Unauthorized' : 'Could not load data'}
        </div>
        <p className="muted mb-3">{error}</p>
        {onRetry && <button className="btn" onClick={onRetry}><RefreshCw size={14} /> Retry</button>}
      </div>
    )
  }
  if (empty) return <div className={cn('card p-6 text-center muted', className)}>Nothing here yet.</div>
  return <>{children}</>
}
