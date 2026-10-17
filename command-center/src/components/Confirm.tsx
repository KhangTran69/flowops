import type { ReactNode } from 'react'
import { Loader2 } from 'lucide-react'
import { cn } from '../lib/utils'

export function Confirm({ open, onOpenChange, title, description, onConfirm, busy, destructive, confirmDisabled, confirmLabel = 'Confirm', children }: {
  open: boolean; onOpenChange: (v: boolean) => void; title: string; description?: string;
  onConfirm: () => void; busy?: boolean; destructive?: boolean; confirmDisabled?: boolean;
  confirmLabel?: string; children?: ReactNode
}) {
  if (!open) return null
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4" role="dialog" aria-modal="true" aria-label={title} onMouseDown={(e) => { if (e.target === e.currentTarget && !busy) onOpenChange(false) }}>
      <div className="card w-full max-w-lg p-5">
        <h2 className="text-lg font-semibold">{title}</h2>
        {description && <p className="muted mt-1">{description}</p>}
        {children && <div className="mt-4">{children}</div>}
        <div className="mt-5 flex justify-end gap-2">
          <button className="btn" onClick={() => onOpenChange(false)} disabled={busy}>Cancel</button>
          <button className={cn('btn', destructive ? 'btn-danger' : 'btn-primary')} onClick={onConfirm} disabled={busy || confirmDisabled}>
            {busy && <Loader2 className="animate-spin" size={14} />} {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  )
}
